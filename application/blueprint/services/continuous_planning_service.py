"""AI 持续规划服务

整合宏观规划、幕级规划、AI 续规划为统一的服务
"""

import json
import uuid
import logging
import re
import sys
import copy
from contextlib import contextmanager
from typing import Dict, List, Optional
from datetime import datetime
from json_repair import repair_json

from domain.structure.story_node import StoryNode, NodeType, PlanningStatus, PlanningSource
from domain.structure.chapter_element import ChapterElement, ElementType, RelationType, Importance
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.value_objects.generation_preferences import GenerationPreferences
from domain.novel.repositories.chapter_repository import ChapterRepository
from domain.novel.repositories.novel_repository import NovelRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
from domain.ai.services.llm_service import LLMService, GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.prompt_contract import PromptContract
from infrastructure.ai.prompt_contracts.continuous_planning import (
    CONTINUOUS_PLANNING_NEXT_ACT_CONTRACT,
    PLANNING_ACT_CONTRACT,
    PLANNING_PRECISE_MACRO_CONTRACT,
    PLANNING_PRECISE_REPAIR_CONTRACT,
    PLANNING_PRECISE_VOLUME_CONTRACT,
    PLANNING_QUICK_MACRO_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway
from infrastructure.ai.generation_profiles import generation_config_from_profile
from application.blueprint.services.chapter_plan_renderer import (
    format_decision,
    format_dialogue,
    format_event,
    format_scene_transition,
    render_chapter_execution_plan,
    render_lightweight_act_chapter_outline,
    render_list_section,
    stringify_plan_value,
)
from application.blueprint.services.chapter_continuity_ledger import ChapterContinuityLedgerService
from application.blueprint.services.chapter_planning_policy import validate_lightweight_act_plan
from application.audit.services.macro_merge_engine import MacroMergeEngine, MergePlan, MergeConflictException
from application.blueprint.services.chapter_book_structure_sync import (
    collect_structure_chapter_numbers,
)

logger = logging.getLogger(__name__)
# 有界存储（容量 LRU + TTL）：替换裸 dict，根治长跑内存泄漏。
# 进度/结果按 novel_id、LLM 流文本按 act_id 累积，无淘汰时无限增长。
from application.blueprint.services.bounded_stores import BoundedStore

_macro_plan_progress_store = BoundedStore(max_size=64, ttl_seconds=6 * 3600)
_macro_plan_result_store = BoundedStore(max_size=64, ttl_seconds=6 * 3600)
_act_chapters_llm_stream_store = BoundedStore(max_size=128, ttl_seconds=6 * 3600)


def _macro_plan_progress_shared_key(novel_id: str) -> str:
    """与 novel:xxx 隔离，避免与共享内存里小说载荷键冲突。"""
    return f"macro_plan_progress:{novel_id}"


def _get_cross_process_shared_dict():
    """守护进程与 API 进程各有一份模块内进度 dict；跨进程观测必须走 Manager.dict。"""
    shared = sys.modules.get("__shared_state")
    if shared is not None:
        return shared
    try:
        from interfaces.runtime_state import _get_shared_state

        return _get_shared_state()
    except Exception:
        return None


def _mirror_macro_plan_progress_to_shared(novel_id: str) -> None:
    """将本进程内的宏观进度快照写入跨进程共享 dict（供 API SSE / GET progress 读取）。"""
    prog = _macro_plan_progress_store.get(novel_id)
    if not prog:
        return
    shared = _get_cross_process_shared_dict()
    if shared is None:
        return
    try:
        shared[_macro_plan_progress_shared_key(novel_id)] = copy.deepcopy(prog)
    except Exception:
        logger.debug(
            "mirror macro_plan_progress to shared failed novel=%s",
            novel_id,
            exc_info=True,
        )


def get_act_chapters_llm_stream(act_id: str) -> str:
    """幕级章节规划 LLM 流式累积文本（供 SSE 增量推送）。"""
    return _act_chapters_llm_stream_store.get(act_id, "")


def _reset_act_chapters_llm_stream(act_id: str) -> None:
    _act_chapters_llm_stream_store[act_id] = ""


def _append_act_chapters_llm_stream(act_id: str, delta: str) -> None:
    if not delta:
        return
    _act_chapters_llm_stream_store[act_id] = (
        _act_chapters_llm_stream_store.get(act_id, "") + delta
    )


# ======================================================================
#  结构计算引擎 (Structure Calculator)
#  -------------------------------
#  唯一真相源：根据 target_chapters 计算出合理的部/卷/幕/章分布。
#  所有路径（极速模式 / 精密模式 / fallback / 动态幕生成）都必须通过本引擎
#  获取结构参数，禁止任何地方再硬编码 "3幕" 或 "5章"。
#
#  实现已下沉到 structure_budget 模块（零重依赖，engine/ 层也要用同一口径），
#  此处保留再导出以保持历史 import 路径可用。
# ======================================================================
from application.blueprint.services.structure_budget import (  # noqa: E402
    calculate_structure_params,
    resolve_chapter_ceiling,
)


def _as_str_list(value) -> List[str]:
    """把 LLM 可能给出的 str / list / None 统一成字符串列表。

    LLM 经常把 key_events 返回成单个字符串（"事件1、事件2"）或把 core_conflict
    返回成一句话，这里统一成列表，避免下游拿到脏类型。
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("；", ";").replace("，", ",").split(";")]
        parts = [p for p in parts if p]
        return parts or ([value] if value.strip() else [])
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif item is not None and not isinstance(item, (dict, list, tuple)):
                out.append(str(item).strip())
        return out
    return [str(value)]


def _as_positive_int(value) -> Optional[int]:
    """LLM 常把章数返回成字符串（"6" / "6章"），统一成正整数；非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            parsed = int(digits)
            return parsed if parsed > 0 else None
    return None


def _first_text(*candidates) -> Optional[str]:
    """返回第一个非空字符串候选（用于 quick / precise 两种 schema 的字段归一）。"""
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


def _count_total_acts(structure: List[Dict]) -> int:
    """统计嵌套结构里规划出的幕总数（用于标记收束幕）。"""
    total = 0
    for part_data in structure or []:
        for volume_data in part_data.get("volumes") or []:
            total += len(volume_data.get("acts") or [])
    return total


def _sanitize_llm_json_output(raw: str) -> str:
    content = (raw or "").strip()
    content = re.sub(r"\x1b\[[0-9;]*m", "", content)
    content = re.sub(r"<think\|?>.*?</think\|?>", "", content, flags=re.DOTALL)
    content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
    if "```json" in content:
        content = content.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0]
    return content.strip()


def _extract_outer_json_value(text: str) -> str:
    obj_start = text.find("{")
    arr_start = text.find("[")
    if obj_start != -1:
        start = obj_start
    elif arr_start != -1:
        start = arr_start
    else:
        return text

    root_char = text[start]
    root_close = "}" if root_char == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for idx in range(start, len(text)):
        ch = text[idx]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == root_char:
            depth += 1
            continue
        if ch == root_close:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return text[start:]


_last_macro_incremental_preview_ts: Dict[str, float] = {}


def _incremental_macro_parts_trustworthy(parts: List[Dict]) -> bool:
    """流式预览专用：挡住 repair_json 在半截输出上生成的单字/碎片化标题。

    正式落库仍走 ``_parse_llm_response``（可继续 repair）；此处只约束「提前挂载到树上的预览」。
    """
    if not parts:
        return False
    for p in parts:
        if not isinstance(p, dict):
            return False
        pt = str(p.get("title") or "").strip()
        if len(pt) == 1:
            return False
        for v in p.get("volumes") or []:
            if not isinstance(v, dict):
                return False
            vt = str(v.get("title") or "").strip()
            if len(vt) == 1:
                return False
            for a in v.get("acts") or []:
                if not isinstance(a, dict):
                    return False
                at = str(a.get("title") or "").strip()
                if len(at) == 1:
                    return False
    return True


def _macro_incremental_tree_score(parts: Optional[List[Dict]]) -> int:
    """启发式评分：标题齐全且非单字占位，说明增量 JSON 越可呈现。"""
    if not parts:
        return 0
    score = 0
    for p in parts:
        if not isinstance(p, dict):
            continue
        pt = str(p.get("title") or "").strip()
        if len(pt) > 1:
            score += 2
        for v in p.get("volumes") or []:
            if not isinstance(v, dict):
                continue
            vt = str(v.get("title") or "").strip()
            if len(vt) > 1:
                score += 3
            for a in v.get("acts") or []:
                if isinstance(a, dict):
                    at = str(a.get("title") or "").strip()
                    if len(at) > 1:
                        score += 5
    return score


def _try_parse_parts_from_llm_buffer(raw: str) -> Optional[List[Dict]]:
    """尝试从未完成的 LLM 缓冲中解析出 parts。

    优先严格 ``json.loads``；仅在首尾括号看似闭合时再尝试 ``repair_json``，
    且结果必须通过 :func:`_incremental_macro_parts_trustworthy`，避免半段输出被修成乱码树。
    """
    if not raw or len(raw.strip()) < 40:
        return None
    cleaned = _sanitize_llm_json_output(raw)
    cleaned = _extract_outer_json_value(cleaned)
    candidates: List[str] = [cleaned]
    tail = cleaned.rstrip()
    if tail.endswith("}") or tail.endswith("]"):
        try:
            candidates.append(repair_json(cleaned))
        except Exception:
            pass
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            data = json.loads(cand)
            parts = data.get("parts")
            if isinstance(parts, list) and len(parts) > 0 and isinstance(parts[0], dict):
                if not _incremental_macro_parts_trustworthy(parts):
                    continue
                return parts
        except Exception:
            continue
    return None


def _repair_json_string(text: str) -> str:
    text = text.strip()
    if not text:
        return text

    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass

    def _close_json(s: str) -> str:
        s = s.strip()
        if not s:
            return "{}"

        in_string = False
        escape = False
        stack = []
        result = []

        for ch in s:
            if escape:
                result.append(ch)
                escape = False
                continue
            if ch == "\\" and in_string:
                result.append(ch)
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                continue
            if in_string:
                result.append(ch)
                continue
            if ch == "{":
                stack.append("}")
                result.append(ch)
                continue
            if ch == "[":
                stack.append("]")
                result.append(ch)
                continue
            if ch in "}]":
                if stack and stack[-1] == ch:
                    stack.pop()
                result.append(ch)
                continue
            result.append(ch)

        if in_string:
            result.append('"')

        repaired = "".join(result).rstrip()
        while repaired.endswith(","):
            repaired = repaired[:-1].rstrip()
        while stack:
            while repaired.endswith(","):
                repaired = repaired[:-1].rstrip()
            repaired += stack.pop()
        return repaired

    candidate = text
    retries = 15
    while retries > 0 and candidate:
        repaired = _close_json(candidate)
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            last_comma = candidate.rfind(",")
            if last_comma == -1:
                break
            candidate = candidate[:last_comma]
        retries -= 1
    return _close_json(text)


# 导出 MergeConflictException 供路由层使用
__all__ = ['ContinuousPlanningService', 'MergeConflictException']


def get_macro_plan_progress(novel_id: str) -> Dict:
    defaults = {
        "status": "idle",
        "current": 0,
        "total": 0,
        "percent": 0,
        "message": "",
        "llm_stream_text": "",
        "preview_parts": None,
    }
    shared = _get_cross_process_shared_dict()
    if shared is not None:
        try:
            got = shared.get(_macro_plan_progress_shared_key(novel_id))
            if isinstance(got, dict):
                return copy.deepcopy(got)
        except Exception:
            logger.debug(
                "read macro_plan_progress from shared failed novel=%s",
                novel_id,
                exc_info=True,
            )
    return copy.deepcopy(_macro_plan_progress_store.get(novel_id, defaults))


def get_macro_plan_result(novel_id: str) -> Dict:
    return _macro_plan_result_store.get(novel_id, {
        "ready": False,
        "result": None,
        "error": None,
    }).copy()


class ContinuousPlanningService:
    """AI 持续规划服务

    统一的规划服务，包含：
    1. 宏观规划：生成部-卷-幕结构框架
    2. 幕级规划：为指定幕生成章节规划
    3. AI 续规划：自动判断何时创建新幕
    """

    def __init__(
        self,
        story_node_repo: StoryNodeRepository,
        chapter_element_repo: ChapterElementRepository,
        llm_service: LLMService,
        bible_service=None,
        chapter_repository: Optional[ChapterRepository] = None,
        novel_repository: Optional[NovelRepository] = None,
    ):
        self.story_node_repo = story_node_repo
        self.chapter_element_repo = chapter_element_repo
        self.llm_service = llm_service
        self.bible_service = bible_service
        self.chapter_repository = chapter_repository
        self.novel_repository = novel_repository

    # CPMS 提示词渲染

    @staticmethod
    def _render_contract_prompt(contract: PromptContract, variables: Dict) -> Prompt:
        """通过 PromptGateway 渲染 CPMS 契约，避免业务代码保留大段提示词。"""
        return get_prompt_gateway().render(contract, variables).prompt

    async def generate_macro_plan(
        self,
        novel_id: str,
        target_chapters: int,
        structure_preference: Optional[Dict[str, int]] = None,
    ) -> Dict:
        """生成宏观规划"""
        import time
        start_time = time.time()

        logger.info(f"Generating macro plan for novel {novel_id}")
        # 每轮生成清空流式缓冲，避免全托管未走 initialize_macro_plan_task 时与前一次输出拼接
        self._clear_macro_llm_stream(novel_id)
        self._update_macro_progress(novel_id, status="running", current=0, total=0, message="正在准备结构规划")

        # 获取 Bible 信息
        bible_context = self._get_bible_context(novel_id)

        try:
            if structure_preference is None:
                # 构建提示词
                prompt = self._build_macro_planning_prompt(
                    bible_context=bible_context,
                    target_chapters=target_chapters,
                    structure_preference=structure_preference
                )

                # 调用 LLM 流式生成规划（SSE 通过 llm_stream_text 推送增量）
                config = generation_config_from_profile("planning_macro")
                self._update_macro_progress(
                    novel_id,
                    status="running",
                    message="模型正在输出叙事结构…",
                )
                raw = await self._stream_macro_llm_text(novel_id, prompt, config)
                structure = self._parse_llm_response(raw)
            else:
                structure = await self._generate_precise_macro_plan(
                    novel_id=novel_id,
                    bible_context=bible_context,
                    target_chapters=target_chapters,
                    structure_preference=structure_preference,
                )

            if isinstance(structure, list):
                structure = {"parts": structure}

            parts_n = len(structure.get("parts", [])) if isinstance(structure, dict) else 0
            logger.debug(
                "[MacroPlan] novel=%s generate_done parts=%d target_chapters=%s pref=%s",
                novel_id,
                parts_n,
                target_chapters,
                "free" if structure_preference is None else "precise",
            )

            # 评估规划质量
            elapsed_time = time.time() - start_time
            quality_metrics = self._evaluate_macro_plan_quality(
                structure=structure,
                bible_context=bible_context,
                target_chapters=target_chapters,
                structure_preference=structure_preference
            )

            logger.info(f"[MacroPlanQuality] novel={novel_id}, time={elapsed_time:.2f}s, metrics={quality_metrics}")
            self._set_macro_preview_parts(novel_id, structure.get("parts", []) or [])
            self._update_macro_progress(
                novel_id,
                status="completed",
                current=self._get_total_volumes(structure_preference),
                total=self._get_total_volumes(structure_preference),
                message="结构规划生成完成",
            )

            return {
                "success": True,
                "structure": structure.get("parts", []),
                "quality_metrics": quality_metrics,
                "generation_time": elapsed_time
            }
        except Exception:
            self._update_macro_progress(
                novel_id,
                status="failed",
                message="结构规划生成失败",
            )
            raise

    async def _generate_precise_macro_plan(
        self,
        novel_id: str,
        bible_context: Dict,
        target_chapters: int,
        structure_preference: Dict[str, int]
    ) -> Dict:
        """精密模式：系统先搭固定骨架，整版生成后再定向补全缺失字段。"""
        skeleton = self._build_precise_structure_skeleton(target_chapters, structure_preference)
        total_volumes = self._get_total_volumes(structure_preference)
        self._update_macro_progress(
            novel_id,
            status="running",
            current=0,
            total=total_volumes,
            message="正在生成整版叙事骨架",
        )

        prompt = self._build_precise_macro_prompt(
            bible_context=bible_context,
            target_chapters=target_chapters,
            structure_preference=structure_preference,
            skeleton=skeleton,
        )
        config = generation_config_from_profile(
            "planning_macro",
            max_tokens=self._calculate_precise_max_tokens(structure_preference),
        )
        raw = await self._stream_macro_llm_text(novel_id, prompt, config)
        updates = self._parse_llm_response(raw)
        self._merge_precise_structure_updates(
            skeleton=skeleton,
            updates=updates,
            target_chapters=target_chapters,
            rebalance=False,
        )
        self._update_macro_progress(
            novel_id,
            status="running",
            current=max(total_volumes - 1, 0),
            total=total_volumes,
            message="正在检查并补全缺失字段",
        )

        incomplete_acts = self._find_incomplete_precise_acts(skeleton)
        if incomplete_acts:
            repair_prompt = self._build_precise_repair_prompt(
                bible_context=bible_context,
                target_chapters=target_chapters,
                structure_preference=structure_preference,
                incomplete_acts=incomplete_acts,
            )
            repair_config = generation_config_from_profile(
                "planning_repair",
                max_tokens=self._calculate_precise_repair_max_tokens(incomplete_acts),
            )
            self._clear_macro_llm_stream(novel_id)
            self._update_macro_progress(
                novel_id,
                status="running",
                message="模型正在补全缺失字段…",
            )
            repair_raw = await self._stream_macro_llm_text(novel_id, repair_prompt, repair_config)
            repair_updates = self._parse_llm_response(repair_raw)
            self._merge_precise_structure_updates(
                skeleton=skeleton,
                updates=repair_updates,
                target_chapters=target_chapters,
                rebalance=False,
            )

        all_acts = [
            act
            for part in skeleton.get("parts", [])
            for volume in part.get("volumes", [])
            for act in volume.get("acts", [])
        ]
        self._rebalance_act_chapters(all_acts, target_chapters)
        return skeleton

    def _build_precise_structure_skeleton(
        self,
        target_chapters: int,
        structure_preference: Dict[str, int]
    ) -> Dict:
        """按用户指定网格构造固定骨架，节点数量不交给 AI 决定。"""
        parts = structure_preference.get("parts", 3)
        volumes_per_part = structure_preference.get("volumes_per_part", 3)
        acts_per_volume = structure_preference.get("acts_per_volume", 3)
        total_acts = max(parts * volumes_per_part * acts_per_volume, 1)
        avg_chapters_per_act = max(target_chapters // total_acts, 1)

        structure = {"parts": []}
        for part_index in range(1, parts + 1):
            part_node = {
                "node_id": f"P{part_index}",
                "title": f"第{part_index}部",
                "description": "",
                "volumes": [],
            }
            for volume_index in range(1, volumes_per_part + 1):
                volume_node = {
                    "node_id": f"V{part_index}_{volume_index}",
                    "title": f"第{volume_index}卷",
                    "description": "",
                    "acts": [],
                }
                for act_index in range(1, acts_per_volume + 1):
                    volume_node["acts"].append({
                        "node_id": f"A{part_index}_{volume_index}_{act_index}",
                        "title": f"第{act_index}幕",
                        "description": "",
                        "estimated_chapters": avg_chapters_per_act,
                        "narrative_goal": "",
                        "plot_points": [],
                        "key_characters": [],
                        "key_locations": [],
                        "emotional_arc": "",
                        "setup_for": [],
                        "payoff_from": [],
                    })
                part_node["volumes"].append(volume_node)
            structure["parts"].append(part_node)
        return structure

    def _merge_precise_structure_updates(
        self,
        skeleton: Dict,
        updates: Dict,
        target_chapters: int,
        rebalance: bool = True,
    ) -> Dict:
        """将 AI 返回的内容更新合并回固定骨架。"""
        node_index: Dict[str, Dict] = {}
        acts: List[Dict] = []

        for part in skeleton.get("parts", []):
            node_index[part["node_id"]] = part
            for volume in part.get("volumes", []):
                node_index[volume["node_id"]] = volume
                for act in volume.get("acts", []):
                    node_index[act["node_id"]] = act
                    acts.append(act)

        for update in updates.get("node_updates", []):
            if not isinstance(update, dict):
                continue
            node_id = str(update.get("node_id") or "").strip()
            if not node_id or node_id not in node_index:
                continue
            self._apply_precise_node_update(node_index[node_id], update)

        if rebalance:
            self._rebalance_act_chapters(acts, target_chapters)
        return skeleton

    def _apply_precise_node_update(self, node: Dict, update: Dict) -> None:
        title = str(update.get("title") or "").strip()
        description = str(update.get("description") or "").strip()
        if title:
            node["title"] = title
        if description:
            node["description"] = description

        if "estimated_chapters" in node:
            estimated = update.get("estimated_chapters")
            try:
                node["estimated_chapters"] = max(int(estimated), 0)
            except (TypeError, ValueError):
                pass
            for field in (
                "narrative_goal",
                "emotional_arc",
            ):
                value = str(update.get(field) or "").strip()
                if value:
                    node[field] = value
            for field in (
                "plot_points",
                "key_characters",
                "key_locations",
                "setup_for",
                "payoff_from",
            ):
                value = update.get(field)
                if isinstance(value, list):
                    node[field] = [str(item).strip() for item in value if str(item).strip()]

    def _rebalance_act_chapters(self, acts: List[Dict], target_chapters: int) -> None:
        """将各幕 estimated_chapters 归一到目标总章数。"""
        if not acts:
            return

        min_each = 1 if target_chapters >= len(acts) else 0
        remaining = max(target_chapters - min_each * len(acts), 0)

        weights = []
        for act in acts:
            try:
                value = int(act.get("estimated_chapters", 0) or 0)
            except (TypeError, ValueError):
                value = 0
            weights.append(max(value, 1))

        total_weight = sum(weights) or len(acts)
        scaled = [weight * remaining / total_weight for weight in weights]
        allocations = [int(value) for value in scaled]
        leftover = remaining - sum(allocations)
        remainders = sorted(
            enumerate(scaled),
            key=lambda item: item[1] - int(item[1]),
            reverse=True,
        )
        for index, _ in remainders[:leftover]:
            allocations[index] += 1

        for act, extra in zip(acts, allocations):
            act["estimated_chapters"] = min_each + extra

    def _calculate_precise_max_tokens(self, structure_preference: Dict[str, int]) -> int:
        total_volumes = self._get_total_volumes(structure_preference)
        total_acts = max(
            structure_preference.get("parts", 0)
            * structure_preference.get("volumes_per_part", 0)
            * structure_preference.get("acts_per_volume", 0),
            1,
        )
        return min(12_000, max(3_072, 2_048 + total_volumes * 400 + total_acts * 120))

    def _calculate_precise_repair_max_tokens(self, incomplete_acts: List[Dict]) -> int:
        return min(6_000, max(1_536, 768 + len(incomplete_acts) * 320))

    def _find_incomplete_precise_acts(self, skeleton: Dict) -> List[Dict]:
        required_text_fields = ("narrative_goal", "emotional_arc")
        required_list_fields = ("plot_points", "key_characters", "key_locations")
        incomplete = []
        for part in skeleton.get("parts", []):
            for volume in part.get("volumes", []):
                for act in volume.get("acts", []):
                    missing_fields = [
                        field for field in required_text_fields
                        if not str(act.get(field) or "").strip()
                    ]
                    missing_fields.extend(
                        field for field in required_list_fields
                        if not isinstance(act.get(field), list) or not act.get(field)
                    )
                    if missing_fields:
                        incomplete.append({
                            "node_id": act["node_id"],
                            "title": act.get("title", ""),
                            "description": act.get("description", ""),
                            "missing_fields": missing_fields,
                        })
        return incomplete

    def _get_total_volumes(self, structure_preference: Optional[Dict[str, int]]) -> int:
        if not structure_preference:
            return 0
        return max(
            structure_preference.get("parts", 0) * structure_preference.get("volumes_per_part", 0),
            0,
        )

    def _update_macro_progress(
        self,
        novel_id: str,
        *,
        status: str,
        current: Optional[int] = None,
        total: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        progress = _macro_plan_progress_store.get(novel_id, {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percent": 0,
            "message": "",
            "llm_stream_text": "",
            "preview_parts": None,
        }).copy()
        progress["status"] = status
        if current is not None:
            progress["current"] = current
        if total is not None:
            progress["total"] = total
        total_value = progress.get("total", 0) or 0
        current_value = progress.get("current", 0) or 0
        progress["percent"] = round(current_value / total_value * 100, 1) if total_value else 0
        if message is not None:
            progress["message"] = message
        _macro_plan_progress_store[novel_id] = progress
        _mirror_macro_plan_progress_to_shared(novel_id)

    def _clear_macro_llm_stream(self, novel_id: str) -> None:
        prog = _macro_plan_progress_store.get(novel_id)
        if not prog:
            return
        prog = prog.copy()
        prog["llm_stream_text"] = ""
        prog["preview_parts"] = None
        _macro_plan_progress_store[novel_id] = prog
        _mirror_macro_plan_progress_to_shared(novel_id)

    def _append_macro_llm_stream(self, novel_id: str, delta: str) -> None:
        if not delta:
            return
        prog = _macro_plan_progress_store.get(novel_id, {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percent": 0,
            "message": "",
            "llm_stream_text": "",
            "preview_parts": None,
        }).copy()
        prog["llm_stream_text"] = (prog.get("llm_stream_text") or "") + delta
        _macro_plan_progress_store[novel_id] = prog
        _mirror_macro_plan_progress_to_shared(novel_id)
        self._maybe_incremental_macro_preview_from_stream(novel_id)

    def _set_macro_preview_parts(self, novel_id: str, parts: List[Dict]) -> None:
        """解析完成后暂存部/卷/幕列表，供全托管 SSE 旁路监听端逐节点推送。"""
        prog = _macro_plan_progress_store.get(novel_id, {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percent": 0,
            "message": "",
            "llm_stream_text": "",
            "preview_parts": None,
        }).copy()
        prog["preview_parts"] = parts
        _macro_plan_progress_store[novel_id] = prog
        _mirror_macro_plan_progress_to_shared(novel_id)

    def _maybe_incremental_macro_preview_from_stream(self, novel_id: str) -> None:
        """流式生成过程中周期性解析缓冲 JSON，提前写入 preview_parts 供 SSE 推节点。"""
        import time

        prog = _macro_plan_progress_store.get(novel_id)
        if not prog or str(prog.get("status") or "") != "running":
            return
        buf = prog.get("llm_stream_text") or ""
        if len(buf) < 320:
            return

        now = time.monotonic()
        if now - _last_macro_incremental_preview_ts.get(novel_id, 0) < 0.42:
            return
        _last_macro_incremental_preview_ts[novel_id] = now

        parsed = _try_parse_parts_from_llm_buffer(buf)
        if not parsed:
            return

        old_parts = prog.get("preview_parts")
        old_list = old_parts if isinstance(old_parts, list) else None
        old_score = _macro_incremental_tree_score(old_list)
        new_score = _macro_incremental_tree_score(parsed)
        if new_score <= old_score:
            return

        self._set_macro_preview_parts(novel_id, parsed)
        logger.debug(
            "[MacroIncrementalPreview] novel=%s tree_score %s→%s top_parts=%d",
            novel_id,
            old_score,
            new_score,
            len(parsed),
        )

    async def _stream_macro_llm_text(
        self,
        novel_id: str,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> str:
        """流式调用 LLM，边收 token 边写入宏观进度（供 SSE / 轮询展示）。"""
        import inspect

        parts: List[str] = []
        chunk_count = 0
        try:
            stream = self.llm_service.stream_generate(prompt, config)
            if not hasattr(stream, "__aiter__"):
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
                generated = self.llm_service.generate(prompt, config)
                if inspect.isawaitable(generated):
                    generated = await generated
                content = generated.content if hasattr(generated, "content") else str(generated or "")
                if content:
                    parts.append(content)
                    self._append_macro_llm_stream(novel_id, content)
                return "".join(parts)

            async for chunk in stream:
                parts.append(chunk)
                chunk_count += 1
                self._append_macro_llm_stream(novel_id, chunk)
                if chunk_count == 1 or chunk_count % 50 == 0:
                    total_so_far = sum(len(p) for p in parts)
                    logger.debug(
                        "[MacroLLMStream] novel=%s upstream_chunks=%d accumulated_chars=%d",
                        novel_id,
                        chunk_count,
                        total_so_far,
                    )
        except Exception as exc:
            joined = "".join(parts)
            if joined:
                try:
                    parsed = self._parse_llm_response(joined)
                    preview_parts = parsed.get("parts") if isinstance(parsed, dict) else None
                    if isinstance(preview_parts, list) and _macro_incremental_tree_score(preview_parts) > 0:
                        logger.warning(
                            "[MacroLLMStream] novel=%s upstream failed after %d chunks/%d chars; "
                            "using repaired partial macro plan: %s",
                            novel_id,
                            chunk_count,
                            len(joined),
                            exc,
                        )
                        self._set_macro_preview_parts(novel_id, preview_parts)
                        return json.dumps({"parts": preview_parts}, ensure_ascii=False)
                except Exception:
                    logger.debug(
                        "[MacroLLMStream] novel=%s partial repair failed after upstream error",
                        novel_id,
                        exc_info=True,
                    )
            raise
        joined = "".join(parts)
        logger.debug(
            "[MacroLLMStream] novel=%s finished upstream_chunks=%d raw_chars=%d",
            novel_id,
            chunk_count,
            len(joined),
        )
        if not joined:
            raise RuntimeError(
                f"LLM stream produced zero output for novel={novel_id} "
                f"(chunk_count={chunk_count}). "
                "The upstream provider may have dropped the stream before any tokens were received."
            )
        return joined

    async def _stream_act_plan_llm_text(
        self,
        act_id: str,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> str:
        parts: List[str] = []
        async for chunk in self.llm_service.stream_generate(prompt, config):
            parts.append(chunk)
            _append_act_chapters_llm_stream(act_id, chunk)
        return "".join(parts)

    async def _collect_llm_stream_text(
        self,
        prompt: Prompt,
        config: GenerationConfig,
    ) -> str:
        """流式调用 LLM 并拼接全文（无进度存储，用于内部步骤）。"""
        parts: List[str] = []
        async for chunk in self.llm_service.stream_generate(prompt, config):
            parts.append(chunk)
        return "".join(parts)

    def initialize_macro_plan_task(self, novel_id: str) -> None:
        _macro_plan_result_store[novel_id] = {
            "ready": False,
            "result": None,
            "error": None,
        }
        self._update_macro_progress(
            novel_id,
            status="running",
            current=0,
            total=0,
            message="正在准备结构规划",
        )
        prog = _macro_plan_progress_store.setdefault(novel_id, {})
        prog["llm_stream_text"] = ""
        prog["preview_parts"] = None
        _mirror_macro_plan_progress_to_shared(novel_id)

    def store_macro_plan_result(self, novel_id: str, result: Dict) -> None:
        _macro_plan_result_store[novel_id] = {
            "ready": True,
            "result": result,
            "error": None,
        }

    def store_macro_plan_error(self, novel_id: str, error: str) -> None:
        _macro_plan_result_store[novel_id] = {
            "ready": False,
            "result": None,
            "error": error,
        }

    def _evaluate_macro_plan_quality(
        self,
        structure: Dict,
        bible_context: Dict,
        target_chapters: int,
        structure_preference: Dict[str, int]
    ) -> Dict:
        """评估宏观规划质量

        用于持续优化提示词效果，记录关键指标：
        - 结构完整性：部/卷/幕数量是否符合预期
        - 世界观融合度：Bible 元素在结构中的出现比例
        - 冲突密度：每幕是否有明确的冲突描述
        - 标题质量：标题长度、词汇多样性等
        """
        parts = structure.get("parts", [])

        # 基础统计
        part_count = len(parts)
        volume_count = sum(len(p.get("volumes", [])) for p in parts)
        act_count = sum(
            len(v.get("acts", []))
            for p in parts
            for v in p.get("volumes", [])
        )

        # 检查结构偏好匹配度
        expected_parts = structure_preference.get("parts") if structure_preference else None
        structure_match = {
            "parts_match": expected_parts is None or part_count == expected_parts,
            "expected_parts": expected_parts,
            "actual_parts": part_count
        }

        # 收集所有幕
        all_acts = []
        for p in parts:
            for v in p.get("volumes", []):
                all_acts.extend(v.get("acts", []))

        # 评估冲突密度
        acts_with_conflict = sum(
            1 for a in all_acts
            if a.get("core_conflict") and len(a.get("core_conflict", "")) > 10
        )
        conflict_density = acts_with_conflict / len(all_acts) if all_acts else 0

        # 评估世界观融合度
        bible_chars = {c.get("name", "").lower() for c in bible_context.get("characters", [])}
        bible_locations = {l.get("name", "").lower() for l in bible_context.get("locations", [])}

        char_mentions = 0
        location_mentions = 0
        for act in all_acts:
            desc = act.get("description", "").lower()
            title = act.get("title", "").lower()
            text = desc + " " + title

            for char in bible_chars:
                if char and char in text:
                    char_mentions += 1
                    break

            for loc in bible_locations:
                if loc and loc in text:
                    location_mentions += 1
                    break

        world_fusion = {
            "character_coverage": char_mentions / len(all_acts) if all_acts else 0,
            "location_coverage": location_mentions / len(all_acts) if all_acts else 0
        }

        # 评估标题质量
        titles = [a.get("title", "") for a in all_acts]
        avg_title_length = sum(len(t) for t in titles) / len(titles) if titles else 0

        # 检查标题是否包含动词（简单启发式）
        action_words = ["战", "杀", "破", "夺", "逃", "追", "救", "毁", "变", "觉醒", "背叛", "降临", "崛起", "坠落", "燃烧", "冻结", "撕裂", "缝合"]
        titles_with_action = sum(1 for t in titles if any(w in t for w in action_words))
        title_action_ratio = titles_with_action / len(titles) if titles else 0

        # 检查是否有情绪反转字段（新提示词特性）
        acts_with_emotion = sum(1 for a in all_acts if a.get("emotional_turn"))
        emotion_field_ratio = acts_with_emotion / len(all_acts) if all_acts else 0

        return {
            "structure_stats": {
                "parts": part_count,
                "volumes": volume_count,
                "acts": act_count
            },
            "structure_match": structure_match,
            "conflict_density": round(conflict_density, 2),
            "world_fusion": {
                "character_coverage": round(world_fusion["character_coverage"], 2),
                "location_coverage": round(world_fusion["location_coverage"], 2)
            },
            "title_quality": {
                "avg_length": round(avg_title_length, 1),
                "action_word_ratio": round(title_action_ratio, 2)
            },
            "prompt_version_features": {
                "emotional_turn_field": round(emotion_field_ratio, 2),
                "key_characters_field": round(sum(1 for a in all_acts if a.get("key_characters")) / len(all_acts), 2) if all_acts else 0
            }
        }

    async def confirm_macro_plan(self, novel_id: str, structure: List[Dict]) -> Dict:
        """确认宏观规划（旧版本，不安全，保留用于向后兼容）

        ⚠️ 警告：此方法不检查已有数据，可能导致僵尸节点或数据丢失
        推荐使用 confirm_macro_plan_safe() 方法
        """
        logger.warning(f"Using unsafe confirm_macro_plan for novel {novel_id}")
        logger.info(f"Confirming macro plan for novel {novel_id}")

        created_nodes = []
        order_index = 0
        part_number = 0
        volume_number = 0
        act_number = 0

        for part_data in structure:
            part_number += 1
            part_data["number"] = part_number
            part_node = self._create_node_from_data(
                novel_id, None, NodeType.PART, part_data, order_index
            )
            created_nodes.append(part_node)
            order_index += 1

            for volume_data in part_data.get("volumes", []):
                volume_number += 1
                volume_data["number"] = volume_number
                volume_node = self._create_node_from_data(
                    novel_id, part_node.id, NodeType.VOLUME, volume_data, order_index
                )
                created_nodes.append(volume_node)
                order_index += 1

                for act_data in volume_data.get("acts", []):
                    act_number += 1
                    act_data["number"] = act_number
                    act_node = self._create_node_from_data(
                        novel_id, volume_node.id, NodeType.ACT, act_data, order_index
                    )
                    created_nodes.append(act_node)
                    order_index += 1

        await self.story_node_repo.save_batch(created_nodes)

        return {
            "success": True,
            "created_nodes": len(created_nodes),
            "message": f"已创建 {len(created_nodes)} 个结构节点"
        }

    async def confirm_macro_plan_safe(self, novel_id: str, structure: List[Dict]) -> Dict:
        """安全的宏观规划确认（带血缘继承的智能合并）

        核心机制：
        1. 自底向上标记承载者（有正文的节点）
        2. 三路比对（create/update/delete）
        3. 冲突检测（红色阻断）
        4. 原子性事务执行

        Args:
            novel_id: 小说 ID
            structure: 新的宏观结构（部-卷-幕）

        Returns:
            合并结果，包含 summary（GREEN/YELLOW/RED 状态）

        Raises:
            MergeConflictException: 当新结构试图删除包含正文的节点时
        """
        logger.info(f"[SafeMerge] Starting safe macro plan confirmation for novel {novel_id}")

        # 阶段 1：深度扫描 - 获取旧结构
        old_nodes_entities = await self.story_node_repo.get_by_novel(novel_id)
        logger.info(f"[SafeMerge] Found {len(old_nodes_entities)} existing nodes")

        # 标准化旧节点：Entity → Dict（Enum 序列化）
        old_nodes = [
            {
                "id": node.id,
                "novel_id": node.novel_id,
                "parent_id": node.parent_id,
                "node_type": node.node_type.value,  # NodeType.CHAPTER → 'CHAPTER'
                "number": node.number,
                "title": node.title,
                "description": node.description,
                "order_index": node.order_index,
            }
            for node in old_nodes_entities
        ]

        # 标准化新节点：扁平化嵌套结构 → 平面列表
        new_nodes = self._flatten_structure_to_nodes(novel_id, structure)
        logger.info(f"[SafeMerge] Generated {len(new_nodes)} new nodes")

        # 阶段 2：匹配与继承 - 执行比对
        engine = MacroMergeEngine(old_nodes, new_nodes)
        plan = engine.execute_diff()
        logger.info(f"[SafeMerge] Merge plan: creates={len(plan.creates)}, updates={len(plan.updates)}, deletes={len(plan.deletes)}, conflicts={len(plan.conflicts)}")

        # 阶段 3：冲突检测 - 红色阻断
        if plan.has_fatal_conflict:
            logger.error(f"[SafeMerge] Fatal conflicts detected: {plan.conflicts}")
            raise MergeConflictException(
                message="重构导致部分已有正文的章节孤立",
                conflicts=plan.conflicts
            )

        # 阶段 4：执行合并 - 原子性事务
        logger.info(f"[SafeMerge] Applying merge plan...")
        await self.story_node_repo.apply_merge_plan(
            creates=plan.creates,
            updates=plan.updates,
            deletes=plan.deletes
        )

        logger.info(f"[SafeMerge] Merge completed successfully: {plan.summary}")
        return {
            "success": True,
            "summary": plan.summary
        }

    async def _count_macro_structure_nodes(self, novel_id: str) -> int:
        """部 / 卷 / 幕节点数量（用于落库后展示规模）。"""
        nodes = await self.story_node_repo.get_by_novel(novel_id)
        return sum(
            1
            for n in nodes
            if n.node_type in (NodeType.PART, NodeType.VOLUME, NodeType.ACT)
        )

    async def persist_macro_structure_with_fallback(
        self,
        novel_id: str,
        structure: List[Dict],
    ) -> Dict:
        """先安全合并，失败则回退为一次性写入（与全托管守护进程行为一致）。"""
        try:
            await self.confirm_macro_plan_safe(novel_id=novel_id, structure=structure)
            count = await self._count_macro_structure_nodes(novel_id)
            return {
                "success": True,
                "created_nodes": count,
                "message": f"已同步 {count} 个结构节点",
            }
        except Exception as e:
            logger.warning(
                f"[{novel_id}] confirm_macro_plan_safe 失败，回退 confirm_macro_plan：{e}"
            )
            return await self.confirm_macro_plan(novel_id=novel_id, structure=structure)

    def build_minimal_macro_structure(
        self,
        target_chapters: int,
        *,
        placeholder_description: str = (
            "系统生成的占位结构（可在审阅后于结构树中调整）"
        ),
    ) -> List[Dict]:
        """LLM 无有效输出时的最小部–卷–幕骨架（左侧规划与全托管共用）。

        不再硬编码 3 幕！改用 calculate_structure_params 动态计算。
        """
        target = max(int(target_chapters or 30), 1)
        params = calculate_structure_params(target)
        parts_count = params["parts"]
        volumes_per_part = params["volumes_per_part"]
        acts_per_volume = params["acts_per_volume"]
        chapters_per_act = params["chapters_per_act"]

        logger.info(
            f"[MinimalFallback] target={target} → "
            f"{parts_count}部×{volumes_per_part}卷×{acts_per_volume}幕, "
            f"每幕{chapters_per_act}章 ({params['reasoning']})"
        )

        structure = []
        for p in range(1, parts_count + 1):
            part_node = {
                "title": f"第{p}部",
                "description": placeholder_description,
                "volumes": [],
            }
            for v in range(1, volumes_per_part + 1):
                volume_node = {
                    "title": f"第{v}卷",
                    "description": "",
                    "acts": [],
                }
                for a in range(1, acts_per_volume + 1):
                    global_act = (p - 1) * volumes_per_part * acts_per_volume + (v - 1) * acts_per_volume + a
                    act_titles = ["开端 · 世界建立", "发展 · 冲突升级", "转折 · 陷入深渊",
                                   "高潮 · 终极对决", "收尾 · 新世界", "过渡 · 力量积蓄",
                                   "阴谋 · 暗流涌动", "觉醒 · 真相大白", "抉择 · 牺牲与重生",
                                   "终局 · 一切归零"]
                    title_prefix = act_titles[a - 1] if a <= len(act_titles) else f"第{a}幕"
                    volume_node["acts"].append({
                        "title": f"第{a}幕 · {title_prefix}",
                        "description": f"第{p}部-第{v}卷-第{a}幕叙事单元",
                        "suggested_chapter_count": chapters_per_act,
                    })
                part_node["volumes"].append(volume_node)
            structure.append(part_node)
        return structure

    def _validate_macro_structure_completeness(self, structure: List[Dict], target_chapters: int) -> bool:
        """Validate that the macro structure has minimum viable nodes (parts + volumes).

        Returns False if structure is missing volumes, which would cause act planning to fail.
        """
        if not structure or not isinstance(structure, list):
            return False

        has_volumes = False
        for part in structure:
            volumes = part.get("volumes", [])
            if volumes and len(volumes) > 0:
                has_volumes = True
                break

        if not has_volumes:
            logger.warning(
                f"Macro structure validation failed: structure has parts but no volumes. "
                f"This will cause act planning to fail."
            )
            return False

        return True

    async def apply_macro_plan_from_llm_result(
        self,
        llm_result: Dict,
        novel_id: str,
        target_chapters: int,
        *,
        allow_minimal_placeholder_on_empty: bool = False,
    ) -> Dict:
        """在 `generate_macro_plan` 之后统一落库：有效结构则写入，否则阻塞。

        供 POST /novels/{id}/plan 与全托管守护进程共用，避免两处逻辑分叉。
        """
        struct = llm_result.get("structure") if isinstance(llm_result, dict) else None

        # Validate structure completeness (must have parts AND volumes)
        is_valid_structure = (
            llm_result.get("success")
            and isinstance(struct, list)
            and len(struct) > 0
            and self._validate_macro_structure_completeness(struct, target_chapters)
        )

        if is_valid_structure:
            confirm = await self.persist_macro_structure_with_fallback(
                novel_id, struct
            )
            return {
                "success": True,
                "created_nodes": confirm["created_nodes"],
                "used_minimal_placeholder": False,
                "message": confirm.get("message", ""),
            }

        if not allow_minimal_placeholder_on_empty:
            raise ValueError(
                "宏观规划未返回有效结构（success 或 structure 无效或缺少卷节点）"
            )

        logger.warning(
            "宏观规划未返回有效结构（success=%r，有卷=%r），写入最小占位结构 novel_id=%s",
            llm_result.get("success") if isinstance(llm_result, dict) else None,
            self._validate_macro_structure_completeness(struct, target_chapters) if struct else False,
            novel_id,
        )
        minimal = self.build_minimal_macro_structure(target_chapters)
        confirm = await self.persist_macro_structure_with_fallback(
            novel_id, minimal
        )
        return {
            "success": True,
            "created_nodes": confirm["created_nodes"],
            "used_minimal_placeholder": True,
            "message": confirm.get("message", ""),
        }

    # ==================== 幕级规划 ====================

    async def resolve_act_planning_chapter_count(
        self, act_id: str, custom_chapter_count: Optional[int] = None
    ) -> int:
        """与 plan_act_chapters 相同的章数解析逻辑，供 SSE 骨架行数等使用。"""
        act_node = await self.story_node_repo.get_by_id(act_id)
        if not act_node:
            raise ValueError(f"幕节点不存在: {act_id}")
        # 与动态建幕同一口径：按本书真实目标算每幕章数，不再用 100 章硬编码基准
        _default_cpa = self._act_budget_context(
            act_node.novel_id, int(act_node.number or 0)
        )["chapters_per_act"]
        chapter_count = custom_chapter_count or act_node.suggested_chapter_count or _default_cpa
        if not custom_chapter_count and not act_node.suggested_chapter_count:
            logger.info(
                f"[ActPlanning] act={act_id} 无自定义章数且无 suggested_chapter_count，"
                f"使用引擎推荐值 {_default_cpa}"
            )
        return chapter_count

    async def plan_act_chapters(
        self, act_id: str, custom_chapter_count: Optional[int] = None
    ) -> Dict:
        """为指定幕生成章节规划"""
        logger.info(f"Planning chapters for act {act_id}")

        act_node = await self.story_node_repo.get_by_id(act_id)
        if not act_node:
            raise ValueError(f"幕节点不存在: {act_id}")

        bible_context = self._get_bible_context(act_node.novel_id)
        previous_summary = await self._get_previous_acts_summary(act_node)
        chapter_count = await self.resolve_act_planning_chapter_count(
            act_id, custom_chapter_count
        )

        prompt = self._build_act_planning_prompt(
            act_node, bible_context, previous_summary, chapter_count
        )

        _reset_act_chapters_llm_stream(act_id)
        config = generation_config_from_profile("planning_act")
        try:
            raw = await self._stream_act_plan_llm_text(act_id, prompt, config)
        except Exception as e:
            logger.warning(f"幕级规划 LLM 调用失败 act={act_id}: {e}")
            return {"success": False, "act_id": act_id, "chapters": [], "error": str(e)}

        try:
            plan = self._parse_llm_response(raw)
        except Exception as e:
            logger.warning(f"幕级规划 JSON 解析失败 act={act_id}: {e}")
            return {"success": False, "act_id": act_id, "chapters": [], "parse_error": str(e)}

        if not isinstance(plan, dict):
            logger.warning(f"幕级规划解析结果非对象 act={act_id}: {type(plan)}")
            return {"success": False, "act_id": act_id, "chapters": []}

        chapters = plan.get("chapters", [])
        if not isinstance(chapters, list):
            chapters = []
        errors = validate_lightweight_act_plan(chapters, expected_count=chapter_count)
        if errors:
            logger.warning(
                "幕级规划不完整或被截断 act=%s expected=%s errors=%s",
                act_id,
                chapter_count,
                errors,
            )
            return {
                "success": False,
                "act_id": act_id,
                "chapters": [],
                "error": "章节规划不完整/被截断",
                "validation_errors": errors,
            }

        return {
            "success": True,
            "act_id": act_id,
            "chapters": chapters,
        }

    async def _remove_chapter_children_of_act(self, act_id: str) -> None:
        """同一幕再次确认规划时，先删掉本幕下已有章节节点及对应正文行、元素关联，避免重复堆积。"""
        children = self.story_node_repo.get_children_sync(act_id)
        chapter_nodes = [n for n in children if n.node_type == NodeType.CHAPTER]
        for n in chapter_nodes:
            await self.chapter_element_repo.delete_by_chapter(n.id)
            if self.chapter_repository:
                self.chapter_repository.delete(ChapterId(n.id))
            await self.story_node_repo.delete(n.id)

    @contextmanager
    def _write_txn(self):
        """幕级确认的原子写事务：writer/直连线程上单连接 BEGIN..COMMIT，API 线程上收集为一条 TXN_BATCH 原子入队。"""
        from infrastructure.persistence.database.connection import get_database

        with get_database().transaction() as conn:
            yield conn

    async def confirm_act_planning(self, act_id: str, chapters: List[Dict]) -> Dict:
        """确认幕级规划：在同一事务内原子替换本幕章节（story_nodes + chapters + chapter_elements）。

        与旧实现的区别：
        - 删旧与插新同事务提交：中途失败（外键冲突、SQL 错误）整体回滚，
          不再出现「旧章已删、新章未写」的丢章窗口；
        - 删除走「同号替换」语义（不触发全书重排序号）：旧实现复用带 renumber
          级联的章节删除，重确认中间幕会把后续幕的正文行整体错号，随后被
          树↔正文对齐逻辑当作孤儿行误删；
        - 新章号沿用本幕原起始章号；若新规划章数扩张到撞上后续幕占用的章号，
          显式报错而非静默产生同号双节点。
        """
        logger.info(f"Confirming act planning for act {act_id}")

        act_node = await self.story_node_repo.get_by_id(act_id)
        if not act_node:
            raise ValueError(f"幕节点不存在: {act_id}")

        validation_errors = validate_lightweight_act_plan(chapters, expected_count=len(chapters))
        if validation_errors:
            raise ValueError("章节规划不完整/被截断：" + "；".join(validation_errors))

        novel_id_str = act_node.novel_id
        novel_id_vo = NovelId(novel_id_str)

        # B4-3 防御：章节 ID 是 f"chapter-{novel_id}-chapter-{number}" 拼串，
        # novel_id 自带 "chapter-" 前缀会让 id 解析产生歧义（历史数据如此
        # 时不阻断，只告警提醒治理）
        if "chapter-" in novel_id_str:
            logger.warning(
                "[ActPlanning] novel_id=%r 含 'chapter-' 子串，章节 ID 拼串存在解析歧义风险",
                novel_id_str,
            )

        # ---- 读阶段（纯读，无写副作用）----
        old_children = self.story_node_repo.get_children_sync(act_id)
        old_chapter_nodes = [n for n in old_children if n.node_type == NodeType.CHAPTER]
        old_nums = {int(n.number) for n in old_chapter_nodes}

        tree_numbers = collect_structure_chapter_numbers(self.story_node_repo, novel_id_str)
        if old_nums:
            base_number = min(old_nums)
        else:
            base_number = (max(tree_numbers) + 1) if tree_numbers else 1
        new_numbers = list(range(base_number, base_number + len(chapters)))

        clash = sorted(n for n in new_numbers if n in tree_numbers and n not in old_nums)
        if clash:
            raise ValueError(
                f"无法写入第 {clash[0]}—{clash[-1]} 章：目标章号已被其他幕的章节占用。"
                f"本幕新规划共 {len(chapters)} 章（原起始章号 {base_number}），"
                f"请减少章数或先调整后续幕结构。"
            )

        final_tree_numbers = (tree_numbers - old_nums) | set(new_numbers)
        new_row_ids = {f"chapter-{novel_id_str}-chapter-{n}" for n in new_numbers}

        def _cid(ch) -> str:
            return getattr(ch.id, "value", ch.id)

        # 孤儿行（树无此号）与目标区间内将被新章覆盖、但 id 不匹配的残留行
        rows_to_delete: List = []
        if self.chapter_repository:
            for ch in self.chapter_repository.list_by_novel(novel_id_vo):
                num = int(ch.number)
                if num not in final_tree_numbers or (
                    num in new_numbers and _cid(ch) not in new_row_ids
                ):
                    rows_to_delete.append((ch, num))

        # ---- 构建阶段（纯内存）----
        created_chapters: List[StoryNode] = []
        created_elements: List[ChapterElement] = []
        book_chapters: List[Chapter] = []

        for idx, raw in enumerate(chapters):
            row = self._normalize_act_chapter_row(raw, act_local_index=idx + 1)
            global_number = new_numbers[idx]
            # 与 novel_service.add_chapter / 前端树选择一致：id 以 chapter-{全局章号} 结尾
            story_chapter_id = f"chapter-{act_node.novel_id}-chapter-{global_number}"

            metadata = {"act_chapter_plan": row.get("act_chapter_plan") or row}
            if row.get("chapter_plan"):
                metadata["chapter_plan"] = row.get("chapter_plan")
            chapter_node = StoryNode(
                id=story_chapter_id,
                novel_id=act_node.novel_id,
                parent_id=act_id,
                node_type=NodeType.CHAPTER,
                number=global_number,
                title=row["title"],
                order_index=act_node.order_index + 1 + idx,
                planning_status=PlanningStatus.CONFIRMED,
                planning_source=PlanningSource.AI_ACT,
                outline=row.get("outline"),
                pov_character_id=row.get("pov_character_id"),
                metadata=metadata,
            )
            created_chapters.append(chapter_node)

            created_elements.extend(
                self._create_elements_from_data(
                    story_chapter_id, self._merged_elements_dict(row)
                )
            )

            if self.chapter_repository:
                book_chapters.append(
                    Chapter(
                        id=story_chapter_id,
                        novel_id=novel_id_vo,
                        number=global_number,
                        title=row["title"],
                        content="",
                        outline=row.get("outline") or "",
                        status=ChapterStatus.DRAFT,
                    )
                )

        # 幕聚合字段按新章集合直接计算（写后立刻回读在入队写模式下读不到刚写的行）
        act_node.chapter_start = new_numbers[0] if new_numbers else None
        act_node.chapter_end = new_numbers[-1] if new_numbers else None
        act_node.chapter_count = len(new_numbers)

        cascade_delete = getattr(
            self.chapter_repository, "delete_chapter_cascade_on_conn", None
        )

        # ---- 写阶段：单事务原子提交 ----
        with self._write_txn() as conn:
            for n in old_chapter_nodes:
                num = int(n.number)
                if cascade_delete is not None:
                    cascade_delete(conn, n.id, novel_id_str, num)
                else:
                    self.chapter_element_repo.delete_by_chapter_on_conn(conn, n.id)
                    self.story_node_repo.delete_node_on_conn(conn, n.id)
            for ch, num in rows_to_delete:
                cascade_delete(conn, _cid(ch), novel_id_str, num)

            self.story_node_repo.save_batch_on_conn(conn, created_chapters)
            self.chapter_element_repo.save_batch_on_conn(conn, created_elements)
            for book_ch in book_chapters:
                self.chapter_repository.save_on_conn(conn, book_ch)

            self.story_node_repo.update_node_on_conn(conn, act_node)

        # 变量表写入是增强数据（失败已有内部兜底），不阻塞正文落库
        for idx, raw in enumerate(chapters):
            row = self._normalize_act_chapter_row(raw, act_local_index=idx + 1)
            self._write_chapter_plan_variables(
                novel_id=novel_id_str,
                chapter_number=new_numbers[idx],
                title=row["title"],
                outline=row.get("outline") or "",
                characters=row.get("characters") or [],
            )

        # 回填卷/部聚合章节范围：守护进程动态补幕的卷只有在此刷新后，
        # 卷摘要与卷完结宏观诊断钩子（按 chapter_end 比较）才能正常触发
        try:
            await self.story_node_repo.update_chapter_ranges(novel_id_str)
        except Exception as e:
            logger.warning(
                "[ActPlanning] 回填卷/部章节范围失败 novel=%s: %s", novel_id_str, e
            )

        return {
            "success": True,
            "created_chapters": len(created_chapters),
            "created_elements": len(created_elements),
            "message": f"已写入 {len(created_chapters)} 个章节（本幕旧规划已替换）",
        }

    # ==================== AI 续规划 ====================

    async def continue_planning(self, novel_id: str, current_chapter_number: int) -> Dict:
        """AI 续规划"""
        logger.info(f"Continue planning for novel {novel_id}, chapter {current_chapter_number}")

        current_act = await self._find_act_for_chapter(novel_id, current_chapter_number)
        if not current_act:
            return {"success": False, "message": "未找到当前章节所属的幕"}

        chapters_written = await self._count_written_chapters_in_act(current_act.id)
        chapters_planned = await self._count_planned_chapters_in_act(current_act.id)

        should_end = chapters_written >= chapters_planned

        if should_end:
            next_act = await self._get_next_act(current_act)

            if next_act:
                return {
                    "success": True,
                    "act_completed": True,
                    "has_next_act": True,
                    "current_act": current_act.to_dict(),
                    "next_act": next_act.to_dict(),
                    "message": f"第 {current_act.number} 幕已完成，可以开始第 {next_act.number} 幕"
                }
            else:
                return {
                    "success": True,
                    "act_completed": True,
                    "has_next_act": False,
                    "current_act": current_act.to_dict(),
                    "suggest_create_next": True,
                    "message": f"第 {current_act.number} 幕已完成，是否需要 AI 生成下一幕？"
                }
        else:
            return {
                "success": True,
                "act_completed": False,
                "current_act": current_act.to_dict(),
                "progress": f"{chapters_written}/{chapters_planned}",
                "message": f"继续第 {current_act.number} 幕"
            }

    async def create_next_act_auto(
        self,
        novel_id: str,
        current_act_id: str,
        parent_volume_id: Optional[str] = None,
    ) -> Dict:
        """自动创建下一幕

        parent_volume_id：显式指定新幕的父卷。守护进程按「只向前换卷」策略
        （daemon_host._find_parent_volume_for_new_act）选出后传入；
        不传时保持历史语义——挂在 current_act 的父卷下（手动「创建下一幕」入口）。
        """
        logger.info(f"Creating next act after {current_act_id}")

        current_act = await self.story_node_repo.get_by_id(current_act_id)
        if not current_act:
            raise ValueError(f"当前幕不存在: {current_act_id}")

        target_volume = None
        if parent_volume_id and parent_volume_id != current_act.parent_id:
            candidate = await self.story_node_repo.get_by_id(parent_volume_id)
            if candidate is not None and getattr(candidate.node_type, "value", "") == "volume":
                target_volume = candidate
            else:
                logger.warning(
                    "[create_next_act_auto] 指定父卷 %s 不存在或不是卷节点，回退挂到当前父卷",
                    parent_volume_id,
                )

        parent_id = target_volume.id if target_volume is not None else current_act.parent_id

        bible_context = self._get_bible_context(novel_id)
        next_act_info = await self._generate_next_act_info(
            novel_id,
            current_act,
            bible_context,
            target_volume=target_volume,
        )

        next_act = self._create_node_from_data(
            novel_id,
            parent_id,
            NodeType.ACT,
            {
                "number": current_act.number + 1,
                "title": next_act_info["title"],
                "description": next_act_info["description"],
                "suggested_chapter_count": next_act_info.get("suggested_chapter_count", 5),
                "key_events": next_act_info.get("key_events", []),
                "narrative_arc": next_act_info.get("narrative_arc"),
                "conflicts": next_act_info.get("conflicts", []),
            },
            current_act.order_index + 1
        )

        await self.story_node_repo.save(next_act)

        return {
            "success": True,
            "next_act": next_act.to_dict(),
            "message": f"已创建第 {next_act.number} 幕，请为其规划章节"
        }

    # ==================== 辅助方法 ====================

    def _get_bible_context(self, novel_id: str) -> Dict:
        """获取 Bible 上下文"""
        if not self.bible_service:
            return {}

        bible = self.bible_service.get_bible_by_novel(novel_id)
        if not bible:
            return {}

        return {
            "characters": [{"id": c.id, "name": c.name, "description": c.description}
                           for c in bible.characters],
            "world_settings": [{"id": w.id, "name": w.name, "description": w.description}
                               for w in bible.world_settings],
            "locations": [{"id": l.id, "name": l.name, "description": l.description}
                          for l in bible.locations],
            "timeline_notes": [{"id": t.id, "event": t.event, "description": t.description}
                               for t in bible.timeline_notes],
        }

    def _create_node_from_data(
        self, novel_id: str, parent_id: Optional[str], node_type: NodeType,
        data: Dict, order_index: int
    ) -> StoryNode:
        """从数据创建节点"""
        return StoryNode(
            id=f"{node_type.value}-{uuid.uuid4().hex[:8]}",
            novel_id=novel_id,
            parent_id=parent_id,
            node_type=node_type,
            number=data["number"],
            title=data["title"],
            description=data.get("description"),
            order_index=order_index,
            planning_status=PlanningStatus.CONFIRMED,
            planning_source=PlanningSource.AI_MACRO,
            suggested_chapter_count=data.get("suggested_chapter_count"),
            themes=data.get("themes", []),
            key_events=data.get("key_events", []) if node_type == NodeType.ACT else [],
            narrative_arc=data.get("narrative_arc") if node_type == NodeType.ACT else None,
            conflicts=data.get("conflicts", []) if node_type == NodeType.ACT else [],
        )

    def _flatten_structure_to_nodes(self, novel_id: str, structure: List[Dict]) -> List[Dict]:
        """将嵌套的部-卷-幕结构扁平化为节点列表（用于 MacroMergeEngine）

        Args:
            novel_id: 小说 ID
            structure: 嵌套结构 [{"title": "第一部", "volumes": [...]}]

        Returns:
            平面节点列表 [{"id": "part-xxx", "node_type": "PART", ...}]

        预算与内容透传（2026-10 修复）：此前本方法只输出 title/description 等 8 个字段，
        把 LLM 已经规划好的 ``estimated_chapters``（每幕章数）以及叙事目标、情节点、
        情绪弧、核心冲突**全部丢弃**。后果有二：
          1. 幕级规划拿不到 suggested_chapter_count，只能回落到硬编码默认值；
          2. 幕节点只剩一个标题，没有任何"这一幕要干什么"的内容规划，
             这是"结构只有骨架、写不到头"的第二层成因。
        这些列 story_nodes 本来就有，apply_merge_plan 的 INSERT 也已经支持，这里只补映射。
        """
        nodes = []
        order_index = 0
        part_number = 0
        volume_number = 0
        act_number = 0
        total_acts = _count_total_acts(structure)

        for part_data in structure:
            part_number += 1
            part_data["number"] = part_number
            part_id = f"part-{novel_id}-{part_number}"

            nodes.append({
                "id": part_id,
                "novel_id": novel_id,
                "parent_id": None,
                "node_type": "part",
                "number": part_number,
                "title": part_data["title"],
                "description": part_data.get("description", ""),
                "themes": _as_str_list(part_data.get("themes")),
                "key_events": _as_str_list(part_data.get("key_events")),
                "order_index": order_index,
            })
            order_index += 1

            for volume_data in part_data.get("volumes", []):
                volume_number += 1
                volume_data["number"] = volume_number
                volume_id = f"volume-{novel_id}-{volume_number}"

                nodes.append({
                    "id": volume_id,
                    "novel_id": novel_id,
                    "parent_id": part_id,
                    "node_type": "volume",
                    "number": volume_number,
                    "title": volume_data["title"],
                    "description": volume_data.get("description", ""),
                    "themes": _as_str_list(volume_data.get("themes")),
                    "key_events": _as_str_list(volume_data.get("key_events")),
                    "order_index": order_index,
                })
                order_index += 1

                for act_data in volume_data.get("acts", []):
                    act_number += 1
                    act_data["number"] = act_number
                    act_id = f"act-{novel_id}-{act_number}"

                    node = {
                        "id": act_id,
                        "novel_id": novel_id,
                        "parent_id": volume_id,
                        "node_type": "act",
                        "number": act_number,
                        "title": act_data["title"],
                        "description": act_data.get("description", ""),
                        "order_index": order_index,
                        # 章节预算：LLM 的 estimated_chapters 与兜底骨架的
                        # suggested_chapter_count 是同一个语义的两个来源名。
                        "suggested_chapter_count": _as_positive_int(
                            act_data.get("estimated_chapters")
                            or act_data.get("suggested_chapter_count")
                        ),
                        # 内容规划：quick 模式与 precise 模式的字段名不同，这里做归一
                        "content": _first_text(
                            act_data.get("narrative_goal"),
                            act_data.get("description"),
                        ),
                        "key_events": _as_str_list(
                            act_data.get("key_events") or act_data.get("plot_points")
                        ),
                        "narrative_arc": _first_text(
                            act_data.get("emotional_arc"), act_data.get("narrative_arc")
                        ),
                        "conflicts": _as_str_list(
                            act_data.get("conflicts") or act_data.get("core_conflict")
                        ),
                        # 最后一幕标记为收束幕：幕级规划据此把预算倾倒向结局
                        "metadata": {
                            "is_final_act": act_number >= total_acts,
                            "planned_act_index": act_number,
                            "planned_total_acts": total_acts,
                        },
                    }
                    nodes.append(node)
                    order_index += 1

        return nodes

    def _normalize_act_chapter_row(self, raw: Dict, act_local_index: int) -> Dict:
        """LLM / 前端可能缺 number、title，或 number 为字符串；统一为可落库结构。"""
        title = (raw.get("title") or "").strip() or f"第{act_local_index}章"
        num = raw.get("number")
        try:
            num_int = int(num) if num is not None else act_local_index
        except (TypeError, ValueError):
            num_int = act_local_index
        chapter_plan = raw.get("chapter_plan")
        outline = self._render_chapter_execution_plan(chapter_plan)
        if not outline:
            outline_raw = raw.get("outline") or raw.get("description") or ""
            outline = outline_raw.strip() if isinstance(outline_raw, str) else ""
        if not outline:
            outline = render_lightweight_act_chapter_outline(raw)
        outline = outline or None
        return {
            **raw,
            "number": num_int,
            "title": title,
            "outline": outline,
            "chapter_plan": chapter_plan,
            "act_chapter_plan": {
                key: raw.get(key)
                for key in (
                    "number",
                    "title",
                    "main_event",
                    "handoff_from_previous",
                    "handoff_to_next",
                    "required_threads",
                    "location_hint",
                    "cast_hint",
                    "characters",
                    "locations",
                    "thrill_type",
                    "thrill_description",
                    "foreshadow_action",
                    "foreshadow_detail",
                )
                if key in raw
            },
        }

    @staticmethod
    def _stringify_plan_value(value) -> str:
        return stringify_plan_value(value)

    @classmethod
    def _format_scene_transition(cls, item, index: int) -> str:
        if not isinstance(item, dict):
            text = cls._stringify_plan_value(item)
            return text
        return format_scene_transition(item, index)

    @classmethod
    def _format_dialogue(cls, item, index: int) -> str:
        return format_dialogue(item, index)

    @classmethod
    def _format_event(cls, item, index: int) -> str:
        return format_event(item, index)

    @classmethod
    def _format_decision(cls, item) -> str:
        return format_decision(item)

    @classmethod
    def _render_list_section(cls, items, formatter) -> List[str]:
        return render_list_section(items, formatter)

    @classmethod
    def _render_chapter_execution_plan(cls, chapter_plan) -> str:
        return render_chapter_execution_plan(chapter_plan)

    def _merged_elements_dict(self, chapter_row: Dict) -> Dict:
        """提示词里人物/地点在 chapters[].characters；落库时期望 elements.characters 为带 id 的对象列表。"""
        merged: Dict = {}
        inner = chapter_row.get("elements")
        if isinstance(inner, dict):
            merged.update(inner)
        for key in ("characters", "locations"):
            top = chapter_row.get(key)
            if top and not merged.get(key):
                merged[key] = top
        return merged

    def _write_chapter_plan_variables(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        title: str,
        outline: str,
        characters: List,
    ) -> None:
        """把幕规划产出的执行剧本写入 Variable Hub，供单章/托管生成默认取值。"""
        if not outline:
            return
        try:
            from application.ai_invocation.variable_hub import VariableWrite
            from infrastructure.ai.prompt_keys import PLANNING_ACT
            from infrastructure.persistence.database.connection import get_database
            from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository

            repo = SqliteVariableHubRepository(get_database())
            context_key = f"novel_id:{novel_id}|chapter_number:{chapter_number}"
            writes = [
                VariableWrite(
                    key="chapter.number",
                    value=chapter_number,
                    context_key=context_key,
                    source_node_key=PLANNING_ACT,
                    source_trace_id=PLANNING_ACT,
                    value_type="integer",
                    display_name="章节序号",
                    scope="chapter",
                    stage="planning",
                ),
                VariableWrite(
                    key="chapter.title",
                    value=title,
                    context_key=context_key,
                    source_node_key=PLANNING_ACT,
                    source_trace_id=PLANNING_ACT,
                    display_name="章节标题",
                    scope="chapter",
                    stage="planning",
                ),
                VariableWrite(
                    key="chapter.outline",
                    value=outline,
                    context_key=context_key,
                    source_node_key=PLANNING_ACT,
                    source_trace_id=PLANNING_ACT,
                    display_name="章节执行剧本",
                    scope="chapter",
                    stage="planning",
                ),
                VariableWrite(
                    key="character.active_cast",
                    value=characters,
                    context_key=context_key,
                    source_node_key=PLANNING_ACT,
                    source_trace_id=PLANNING_ACT,
                    value_type="list",
                    display_name="本章登场角色",
                    scope="chapter",
                    stage="planning",
                ),
            ]
            for write in writes:
                repo.set_value(write)
        except Exception as e:
            logger.debug(
                "[ActPlanning] 写入章节执行剧本变量失败 novel=%s chapter=%s: %s",
                novel_id,
                chapter_number,
                e,
            )

    def _create_elements_from_data(self, chapter_id: str, elements_data: Dict) -> List[ChapterElement]:
        """从数据创建章节元素"""
        elements = []
        if not isinstance(elements_data, dict):
            return elements

        for char_data in elements_data.get("characters", []):
            if isinstance(char_data, str):
                char_data = {"id": char_data}
            if not isinstance(char_data, dict) or not char_data.get("id"):
                continue
            elements.append(ChapterElement(
                id=f"elem-{uuid.uuid4().hex[:8]}",
                chapter_id=chapter_id,
                element_type=ElementType.CHARACTER,
                element_id=str(char_data["id"]),
                relation_type=RelationType(char_data.get("relation", "appears")),
                importance=Importance(char_data.get("importance", "normal")),
            ))

        for loc_data in elements_data.get("locations", []):
            if isinstance(loc_data, str):
                loc_data = {"id": loc_data}
            if not isinstance(loc_data, dict) or not loc_data.get("id"):
                continue
            elements.append(ChapterElement(
                id=f"elem-{uuid.uuid4().hex[:8]}",
                chapter_id=chapter_id,
                element_type=ElementType.LOCATION,
                element_id=str(loc_data["id"]),
                relation_type=RelationType.SCENE,
                importance=Importance.NORMAL,
            ))

        return elements

    def _parse_llm_response(self, response) -> Dict:
        """解析 LLM 响应"""
        # 如果是 GenerationResult 对象，提取 content 属性
        if hasattr(response, 'content'):
            content = response.content
        else:
            content = response

        if not content or not str(content).strip():
            raise ValueError(
                "LLM returned empty response during planning. "
                "This may indicate a streaming failure or model-side issue. "
                "Check upstream provider logs for SSE/SDK stream errors."
            )

        cleaned = _sanitize_llm_json_output(content)
        cleaned = _extract_outer_json_value(cleaned)

        if not cleaned or not cleaned.strip():
            raise ValueError(
                "LLM response was stripped to empty after sanitization. "
                f"Raw content length: {len(str(content))}, "
                f"first 500 chars: {str(content)[:500]}"
            )

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        try:
            repaired = repair_json(cleaned)
            return json.loads(repaired)
        except Exception:
            pass

        cleaned = _repair_json_string(cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse planning JSON: %s", e)
            logger.error("Planning content length: %d", len(cleaned))
            logger.error("Planning raw content (first 1000 chars): %s", cleaned[:1000])
            logger.error("Planning raw content (last 500 chars): %s", cleaned[-500:])
            raise

    def _calculate_chapter_distribution(self, total_chapters: int, parts: int) -> Dict[str, List[int]]:
        """计算黄金比例的章数分配

        核心算法：
        - 3部：25% - 50% - 25% (起源-深渊-决战)
        - 4部：20% - 30% - 30% - 20% (双峰中段)
        - 5部+：首尾各20%，中间平分剩余60%

        Returns:
            {
                "part_chapters": [250, 500, 250],  # 每部的章数
                "part_ratios": [0.25, 0.5, 0.25]   # 每部的占比
            }
        """
        if parts == 1:
            return {"part_chapters": [total_chapters], "part_ratios": [1.0]}

        if parts == 2:
            # 双部结构：40% - 60% (铺垫-高潮)
            p1 = int(total_chapters * 0.4)
            p2 = total_chapters - p1
            return {"part_chapters": [p1, p2], "part_ratios": [0.4, 0.6]}

        if parts == 3:
            # 经典三幕剧：25% - 50% - 25%
            p1 = int(total_chapters * 0.25)
            p3 = int(total_chapters * 0.25)
            p2 = total_chapters - p1 - p3
            return {"part_chapters": [p1, p2, p3], "part_ratios": [0.25, 0.5, 0.25]}

        if parts == 4:
            # 双峰中段：20% - 30% - 30% - 20%
            p1 = int(total_chapters * 0.2)
            p4 = int(total_chapters * 0.2)
            remaining = total_chapters - p1 - p4
            p2 = remaining // 2
            p3 = remaining - p2
            return {"part_chapters": [p1, p2, p3, p4], "part_ratios": [0.2, 0.3, 0.3, 0.2]}

        # 5部及以上：首尾各20%，中间平分60%
        first = int(total_chapters * 0.2)
        last = int(total_chapters * 0.2)
        middle_total = total_chapters - first - last
        middle_parts = parts - 2
        middle_each = middle_total // middle_parts

        part_chapters = [first]
        for i in range(middle_parts):
            if i == middle_parts - 1:
                # 最后一个中间部分吃掉余数
                part_chapters.append(middle_total - middle_each * (middle_parts - 1))
            else:
                part_chapters.append(middle_each)
        part_chapters.append(last)

        part_ratios = [c / total_chapters for c in part_chapters]
        return {"part_chapters": part_chapters, "part_ratios": part_ratios}

    def _format_bible_context(
        self,
        bible_context: Dict,
        *,
        character_limit: int = 5,
        location_limit: int = 5,
        include_ids: bool = False,
        empty_hint: str = "暂无详细设定，请优先依据作者梗概与题材赛道生成结构；只补全必要因果，不套入无关热门题材。",
    ) -> str:
        """把 Bible 结构化数据转为可注入 CPMS 的上下文块。"""
        blocks: List[str] = []

        worldview = bible_context.get("worldview") or ""
        if worldview:
            blocks.append(f"【世界观】\n{worldview}")

        world_settings = bible_context.get("world_settings") or []
        if world_settings:
            lines = ["【世界设定】"]
            for item in world_settings[:location_limit]:
                name = item.get("name", "未命名设定")
                desc = item.get("description", "")
                item_id = f" (ID: {item.get('id', 'N/A')})" if include_ids else ""
                lines.append(f"- {name}{item_id}: {desc}")
            blocks.append("\n".join(lines))

        characters = bible_context.get("characters") or []
        if characters:
            lines = ["【角色设定】"]
            for char in characters[:character_limit]:
                name = char.get("name", "Unknown")
                desc = char.get("description", "")
                role = char.get("role", "")
                arc = char.get("character_arc", "")
                char_id = f" (ID: {char.get('id', 'N/A')})" if include_ids else ""
                role_text = f" [{role}]" if role else ""
                lines.append(f"- {name}{char_id}{role_text}: {desc}")
                if arc:
                    lines.append(f"  人物弧光：{arc}")
            blocks.append("\n".join(lines))

        relationships = bible_context.get("relationships") or []
        if relationships:
            lines = ["【角色关系】"]
            for rel in relationships[:character_limit]:
                char1 = rel.get("character1", "")
                char2 = rel.get("character2", "")
                rel_type = rel.get("relationship_type", "")
                rel_desc = rel.get("description", "")
                lines.append(f"- {char1} ↔ {char2} ({rel_type}): {rel_desc}")
            blocks.append("\n".join(lines))

        locations = bible_context.get("locations") or []
        if locations:
            lines = ["【关键地点】"]
            for loc in locations[:location_limit]:
                name = loc.get("name", "Unknown")
                desc = loc.get("description", "")
                significance = loc.get("significance", "")
                loc_id = f" (ID: {loc.get('id', 'N/A')})" if include_ids else ""
                lines.append(f"- {name}{loc_id}: {desc}")
                if significance:
                    lines.append(f"  叙事意义：{significance}")
            blocks.append("\n".join(lines))

        timeline_notes = bible_context.get("timeline_notes") or []
        if timeline_notes:
            lines = ["【时间线事件】"]
            for note in timeline_notes[:location_limit]:
                event = note.get("event", "")
                desc = note.get("description", "")
                impact = note.get("impact", "")
                lines.append(f"- {event}: {desc}")
                if impact:
                    lines.append(f"  情节影响：{impact}")
            blocks.append("\n".join(lines))

        return "\n\n".join(blocks) if blocks else f"【世界观与人物】\n{empty_hint}"

    @staticmethod
    def _format_locations_context(bible_context: Dict, *, limit: int = 5) -> str:
        locations = bible_context.get("locations") or []
        if not locations:
            return ""
        lines = []
        for loc in locations[:limit]:
            name = loc.get("name", "Unknown")
            desc = loc.get("description", "")
            significance = loc.get("significance", "")
            line = f"- {name}: {desc}".strip()
            if significance:
                line = f"{line}\n  叙事意义：{significance}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_worldbuilding_context(bible_context: Dict, *, limit: int = 5) -> str:
        blocks: List[str] = []

        worldview = bible_context.get("worldview") or ""
        if worldview:
            blocks.append(str(worldview))

        world_settings = bible_context.get("world_settings") or []
        if world_settings:
            lines = []
            for item in world_settings[:limit]:
                name = item.get("name", "未命名设定")
                desc = item.get("description", "")
                lines.append(f"- {name}: {desc}")
            blocks.append("\n".join(lines))

        timeline_notes = bible_context.get("timeline_notes") or []
        if timeline_notes:
            lines = []
            for note in timeline_notes[:limit]:
                event = note.get("event", "")
                desc = note.get("description", "")
                lines.append(f"- {event}: {desc}")
            blocks.append("\n".join(lines))

        return "\n\n".join(block for block in blocks if block.strip())

    @staticmethod
    def _pick_premise_from_context(bible_context: Dict) -> str:
        """从 Bible 上下文中提取宏观规划可用的核心梗概。"""
        for key in ("premise", "summary", "logline", "title"):
            value = bible_context.get(key)
            if value:
                return str(value)
        return "暂无独立梗概，请严格依据已提供的世界观、角色、地点与时间线推演。"

    def _build_quick_macro_prompt(self, bible_context: Dict, target_chapters: int) -> Prompt:
        """极速模式：通过 planning-quick-macro 契约渲染提示词。"""
        return self._render_contract_prompt(
            PLANNING_QUICK_MACRO_CONTRACT,
            self.build_quick_macro_variables(bible_context, target_chapters),
        )

    def build_quick_macro_variables(self, bible_context: Dict, target_chapters: int) -> Dict:
        """构建 planning-quick-macro 的运行变量。

        AI Invocation 需要变量快照，旧宏观规划提示词也复用同一份变量构造，避免两套规则漂移。

        注意这里**不再**计算任何"推荐网格"（部数 / 卷数 / 幕数 / 每幕章数）：
        结构形状改由 LLM 依据剧情容量自行决定，唯一硬约束是各层
        ``estimated_chapters`` 之和等于 ``target_chapters``（由 user.md 强制）。
        引擎侧的封顶改走 ``structure_budget.resolve_act_ceiling``，不依赖公式预测。
        """
        if target_chapters > 500:
            planning_depth = "framework"
        elif target_chapters > 100:
            planning_depth = "partial"
        else:
            planning_depth = "full"

        logger.info(
            "[QuickPrompt] target=%s → 结构层数由 LLM 按剧情容量自定（planning_depth=%s），"
            "引擎只封顶总章数",
            target_chapters,
            planning_depth,
        )

        story_context = self._format_bible_context(bible_context, character_limit=5, location_limit=5)
        worldbuilding_context = self._format_worldbuilding_context(bible_context, limit=5)
        locations_context = self._format_locations_context(bible_context, limit=5)
        character_context = self._format_bible_context(
            {"characters": bible_context.get("characters", []), "relationships": bible_context.get("relationships", [])},
            character_limit=5,
            empty_hint="暂无角色设定，请不要编造与题材无关的固定示例名词。",
        )
        return {
            "premise": self._pick_premise_from_context(bible_context),
            "target_chapters": target_chapters,
            "worldview": story_context,
            "worldbuilding": {"content": worldbuilding_context or story_context},
            "locations": {"list": locations_context},
            "characters": character_context,
            "planning_depth": planning_depth,
            # 兼容旧模板/旧变量绑定：模板已不再引用这些网格值，但变量中枢里
            # 已有的 required 绑定仍需能取到值，否则 invocation 组装会失败。
            "rec_parts": 1,
            "rec_volumes_per_part": 1,
            "rec_acts_per_volume": 1,
            "rec_chapters_per_act": 1,
            "total_recommended_acts": 1,
        }

    def _build_precise_macro_prompt(
        self,
        bible_context: Dict,
        target_chapters: int,
        structure_preference: Dict,
        skeleton: Dict,
    ) -> Prompt:
        """精密模式：固定网格由代码计算，文案由 CPMS 契约渲染。"""
        parts = structure_preference.get("parts", 3)
        volumes_per_part = structure_preference.get("volumes_per_part", 3)
        acts_per_volume = structure_preference.get("acts_per_volume", 3)
        total_acts = max(parts * volumes_per_part * acts_per_volume, 1)

        distribution = self._calculate_chapter_distribution(target_chapters, parts)
        part_chapters = distribution["part_chapters"]
        avg_chapters_per_act = max(target_chapters // total_acts, 1)

        pacing_lines = ["<PACING_GUIDE>"]
        for index, chapters in enumerate(part_chapters, 1):
            if index == 1:
                role = "起源"
                demand = "紧凑、抛出核心悬念、建立主角目标"
            elif index == parts:
                role = "决战"
                demand = "收束主线、终极对决、完成情绪兑现"
            else:
                role = "发展/深渊"
                demand = "扩大容量、多线叙事、推动主角重大转变"
            pacing_lines.append(f"- 第{index}部（{role}）：分配 {chapters} 章；情节要求：{demand}。")
        pacing_lines.extend([
            "</PACING_GUIDE>",
            "<ACT_PACING>",
            f"- 总幕数：{total_acts} 幕",
            f"- 平均每幕：约 {avg_chapters_per_act} 章",
            "- 节奏建议：前 1/3 铺垫，中 1/3 发展并设置小高潮，后 1/3 爆发并收束。",
            "</ACT_PACING>",
        ])

        skeleton_lines = ["【固定结构骨架】"]
        for part_index, part in enumerate(skeleton.get("parts", []), 1):
            skeleton_lines.append(f'- {part["node_id"]}: 第{part_index}部')
            for volume_index, volume in enumerate(part.get("volumes", []), 1):
                skeleton_lines.append(f'  - {volume["node_id"]}: 第{part_index}部第{volume_index}卷')
                for act_index, act in enumerate(volume.get("acts", []), 1):
                    skeleton_lines.append(
                        f'    - {act["node_id"]}: 第{part_index}部第{volume_index}卷第{act_index}幕，参考 {avg_chapters_per_act} 章'
                    )

        return self._render_contract_prompt(
            PLANNING_PRECISE_MACRO_CONTRACT,
            {
                "story_context": self._format_bible_context(
                    bible_context,
                    character_limit=5,
                    location_limit=5,
                    include_ids=True,
                    empty_hint="暂无详细设定，请依据作者梗概与题材赛道生成结构框架；只补必要因果，不引入无关题材外壳。",
                ),
                "target_chapters": target_chapters,
                "parts": parts,
                "volumes_per_part": volumes_per_part,
                "acts_per_volume": acts_per_volume,
                "total_acts": total_acts,
                "avg_chapters_per_act": avg_chapters_per_act,
                "pacing_guide": "\n".join(pacing_lines),
                "skeleton_block": "\n".join(skeleton_lines),
            },
        )

    def _build_precise_volume_prompt(
        self,
        bible_context: Dict,
        target_chapters: int,
        structure_preference: Dict,
        skeleton: Dict,
        part_index: int,
        volume_index: int,
    ) -> Prompt:
        """按卷生成内容，缩小上下文范围以提高字段完整度。"""
        parts = structure_preference.get("parts", 3)
        volumes_per_part = structure_preference.get("volumes_per_part", 3)
        acts_per_volume = structure_preference.get("acts_per_volume", 3)
        total_acts = max(parts * volumes_per_part * acts_per_volume, 1)
        avg_chapters_per_act = max(target_chapters // total_acts, 1)

        current_part = skeleton["parts"][part_index - 1]
        current_volume = current_part["volumes"][volume_index - 1]
        act_scope = current_volume.get("acts", [])

        scope_lines = [
            f"【当前生成范围】第{part_index}部 / 第{volume_index}卷",
            f'- {current_part["node_id"]}: {current_part["title"]}',
            f'- {current_volume["node_id"]}: {current_volume["title"]}',
        ]
        for act in act_scope:
            scope_lines.append(
                f'- {act["node_id"]}: {act["title"]}，需完整填写 narrative_goal / plot_points / key_characters / key_locations / emotional_arc / setup_for / payoff_from'
            )

        example_node_id = act_scope[0]["node_id"] if act_scope else "A1_1_1"
        return self._render_contract_prompt(
            PLANNING_PRECISE_VOLUME_CONTRACT,
            {
                "story_context": self._format_bible_context(
                    bible_context,
                    character_limit=8,
                    location_limit=8,
                    include_ids=True,
                    empty_hint="暂无详细设定，请给出通用但完整的单卷叙事设计。",
                ),
                "target_chapters": target_chapters,
                "parts": parts,
                "volumes_per_part": volumes_per_part,
                "acts_per_volume": acts_per_volume,
                "avg_chapters_per_act": avg_chapters_per_act,
                "scope_block": "\n".join(scope_lines),
                "example_node_id": example_node_id,
            },
        )

    def _build_precise_repair_prompt(
        self,
        bible_context: Dict,
        target_chapters: int,
        structure_preference: Dict,
        incomplete_acts: List[Dict],
    ) -> Prompt:
        """只为缺字段的幕生成补丁。"""
        parts = structure_preference.get("parts", 3)
        volumes_per_part = structure_preference.get("volumes_per_part", 3)
        acts_per_volume = structure_preference.get("acts_per_volume", 3)
        total_acts = max(parts * volumes_per_part * acts_per_volume, 1)
        avg_chapters_per_act = max(target_chapters // total_acts, 1)

        act_lines = []
        for act in incomplete_acts:
            missing = ", ".join(act.get("missing_fields", []))
            act_lines.append(
                f'- {act.get("node_id", "")}: 标题《{act.get("title", "")}》；简介《{act.get("description", "")}》；缺失字段：{missing}'
            )

        return self._render_contract_prompt(
            PLANNING_PRECISE_REPAIR_CONTRACT,
            {
                "story_context": self._format_bible_context(
                    bible_context,
                    character_limit=8,
                    location_limit=8,
                    include_ids=True,
                    empty_hint="暂无详细设定，请补齐通用但完整的叙事字段。",
                ),
                "target_chapters": target_chapters,
                "parts": parts,
                "volumes_per_part": volumes_per_part,
                "acts_per_volume": acts_per_volume,
                "avg_chapters_per_act": avg_chapters_per_act,
                "incomplete_acts_block": "\n".join(act_lines),
            },
        )

    def _build_macro_planning_prompt(self, bible_context: Dict, target_chapters: int, structure_preference: Dict) -> Prompt:
        """构建宏观规划提示词（向后兼容的包装器）

        根据 structure_preference 是否为 None 来判断模式：
        - None: 极速模式（AI自主决定）
        - Dict: 精密模式（用户指定结构）
        """
        if structure_preference is None:
            # 极速模式：使用默认的3×3×3结构
            return self._build_quick_macro_prompt(bible_context, target_chapters)
        else:
            # 精密模式：使用用户指定的结构
            skeleton = self._build_precise_structure_skeleton(target_chapters, structure_preference)
            return self._build_precise_macro_prompt(
                bible_context,
                target_chapters,
                structure_preference,
                skeleton,
            )

    def _novel_target_chapters(self, novel_id: str, default: int = 100) -> int:
        """读取小说目标章数（读不到时用 default，绝不返回 0/None 参与运算）。"""
        if self.novel_repository is None:
            return default
        try:
            novel = self.novel_repository.get_by_id(NovelId(novel_id))
        except Exception as exc:
            logger.debug("[ActPlanning] 读取目标章数失败 novel=%s: %s", novel_id, exc)
            return default
        value = getattr(novel, "target_chapters", None) if novel is not None else None
        try:
            value = int(value or 0)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _observed_total_acts(self, novel_id: str) -> int:
        """统计已落库的幕节点数（LLM 实际规划出的结构规模）。

        部/卷/幕的层数改由 LLM 依据剧情容量自行决定，公式网格预测的 total_acts
        不再代表真实结构；收束幕判定必须以实际落库结构为准，否则会出现
        「LLM 规划了 5 幕但公式说 3 幕」→ 第 3 幕被误标为终局、故事提前收口。

        读取失败时返回 0，由调用方退回公式估算（宁可用旧口径，也不要崩）。
        """
        if not novel_id or self.story_node_repo is None:
            return 0
        try:
            tree = self.story_node_repo.get_tree_sync(novel_id)
            nodes = getattr(tree, "nodes", None)
            if nodes is None:
                return 0
            return sum(1 for n in nodes if getattr(n.node_type, "value", "") == "act")
        except Exception as exc:
            logger.debug("[ActPlanning] 统计幕节点总数失败 novel=%s: %s", novel_id, exc)
            return 0

    def _act_budget_context(self, novel_id: str, act_number: int) -> Dict[str, object]:
        """判断某一幕在全书里的位置，并给出收束指令。

        幕节点可能来自宏观规划（带 metadata.is_final_act），也可能是写作途中
        动态补出来的（无该标记），因此统一按「幕号 vs 结构总幕数」重新判定，
        两个来源口径一致。

        「结构总幕数」优先取**已落库的幕节点数**（LLM 依据剧情容量自行决定层数，
        公式网格预测值不再作为依据）；读不到时才退回公式估算。

        Returns:
            {"target_chapters", "total_acts", "chapters_per_act", "is_final_act", "brief"}
        """
        target = self._novel_target_chapters(novel_id)
        params = calculate_structure_params(target)
        try:
            act_number = int(act_number or 0)
        except (TypeError, ValueError):
            # 脏数据/测试替身可能给出非数字幕号：按 0 处理（视为未定位置），
            # 不让提示词构建因为一个字段崩掉。
            act_number = 0
        total_acts = max(self._observed_total_acts(novel_id) or 0, 0) or max(
            int(params["total_acts"]), 1
        )
        is_final = act_number >= total_acts
        if is_final:
            brief = (
                f"【全书收束】第 {act_number} 幕是全书最后一幕（全书 {target} 章）。"
                "本幕必须完成结局：核心冲突正面了结、主要矛盾给出结果、"
                "本卷与全书主线留下的悬念必须回收，不要再埋新的大悬念或开启新战线。"
                "章节推进要向'尘埃落定'倾斜，最后一章必须落在结局而非新悬念上。"
            )
        else:
            brief = (
                f"【全书进度】本幕为第 {act_number}/{total_acts} 幕，"
                f"全书目标 {target} 章，每幕约 {params['chapters_per_act']} 章。"
                "本幕需为后续幕次留出推进空间，不要在本幕内提前解决全书主线。"
            )
        return {
            "target_chapters": target,
            "total_acts": total_acts,
            "chapters_per_act": params["chapters_per_act"],
            "is_final_act": is_final,
            "brief": brief,
        }

    def _build_act_planning_prompt(
        self,
        act_node: StoryNode,
        bible_context: Dict,
        previous_summary: Optional[str],
        chapter_count: int,
    ) -> Prompt:
        """构建幕级章节规划提示词。"""
        context_parts = [f"幕信息：《{act_node.title}》"]
        if act_node.description:
            context_parts.append(f"幕简介：{act_node.description}")
        if act_node.content:
            context_parts.append(f"幕叙事目标：{act_node.content}")
        if previous_summary:
            context_parts.append(f"前情提要：{previous_summary}")
        # 全书进度/收束指令：告诉模型这一幕在全书里的真实位置，
        # 否则每一幕都被当成"中段推进"，故事自然没有终点。
        budget_ctx = self._act_budget_context(
            getattr(act_node, "novel_id", None) or "", getattr(act_node, "number", 0)
        )
        context_parts.append(budget_ctx["brief"])

        try:
            next_chapter_number = 1
            if self.chapter_repository is not None:
                existing_chapters = self.chapter_repository.list_by_novel(NovelId(act_node.novel_id))
                numbers = [int(getattr(ch, "number", 0) or 0) for ch in existing_chapters]
                if numbers:
                    next_chapter_number = max(numbers) + 1
            ledger_text = ChapterContinuityLedgerService(
                chapter_repository=self.chapter_repository,
                story_node_repo=self.story_node_repo,
            ).build_for_chapter(act_node.novel_id, next_chapter_number).to_prompt_text()
            if ledger_text:
                context_parts.append("近章连续性台账：\n" + ledger_text)
        except Exception as exc:
            logger.debug("[ActPlanning] 构建近章连续性台账失败 act=%s: %s", act_node.id, exc)

        if bible_context.get("characters"):
            char_list = [
                f"- {c.get('name', 'Unknown')} (ID: {c.get('id', 'N/A')})"
                for c in bible_context["characters"][:5]
            ]
            context_parts.append("可用人物：\n" + "\n".join(char_list))

        if bible_context.get("locations"):
            loc_list = [
                f"- {l.get('name', 'Unknown')} (ID: {l.get('id', 'N/A')})"
                for l in bible_context["locations"][:5]
            ]
            context_parts.append("可用地点：\n" + "\n".join(loc_list))

        return self._render_contract_prompt(
            PLANNING_ACT_CONTRACT,
            {
                "context": "\n\n".join(context_parts),
                "chapter_count": chapter_count,
                **self._build_act_contract_variables(act_node.novel_id),
            },
        )

    def _build_act_contract_variables(self, novel_id: str) -> Dict[str, str]:
        """Collect locked narrative contract fields for CPMS rendering."""
        prefs = self._get_generation_preferences(novel_id)
        if not prefs:
            return {}
        return {
            "genre_label": prefs.locked_genre,
            "world_preset": prefs.locked_world_preset,
            "story_structure": prefs.locked_story_structure,
            "pacing_control": prefs.locked_pacing_control,
            "writing_style": prefs.locked_writing_style,
            "special_requirements": prefs.locked_special_requirements,
        }

    def _get_generation_preferences(self, novel_id: str) -> Optional[GenerationPreferences]:
        if self.novel_repository is None:
            return None
        try:
            novel = self.novel_repository.get_by_id(NovelId(novel_id))
        except Exception as exc:
            logger.debug("[ActPlanning] 读取小说题材偏好失败 novel=%s: %s", novel_id, exc)
            return None
        return getattr(novel, "generation_prefs", None) if novel is not None else None

    async def _get_previous_acts_summary(self, act_node: StoryNode) -> Optional[str]:
        """获取前面幕的摘要"""
        return None

    async def _find_act_for_chapter(self, novel_id: str, chapter_number: int) -> Optional[StoryNode]:
        """查找章节所属的幕"""
        tree = self.story_node_repo.get_tree(novel_id)
        acts = [n for n in tree.nodes if n.node_type == NodeType.ACT]
        return max(acts, key=lambda x: x.number) if acts else None

    async def _count_written_chapters_in_act(self, act_id: str) -> int:
        """统计已写章节数"""
        children = self.story_node_repo.get_children(act_id, None)
        return sum(1 for n in children if n.node_type == NodeType.CHAPTER and n.word_count and n.word_count > 0)

    async def _count_planned_chapters_in_act(self, act_id: str) -> int:
        """统计已规划章节数"""
        children = self.story_node_repo.get_children(act_id, None)
        return sum(1 for n in children if n.node_type == NodeType.CHAPTER)

    async def _get_next_act(self, current_act: StoryNode) -> Optional[StoryNode]:
        """获取下一幕"""
        tree = self.story_node_repo.get_tree(current_act.novel_id)
        acts = [n for n in tree.nodes if n.node_type == NodeType.ACT and n.number == current_act.number + 1]
        return acts[0] if acts else None

    async def _generate_next_act_info(
        self,
        novel_id: str,
        current_act: StoryNode,
        bible_context: Dict,
        target_volume: Optional[StoryNode] = None,
    ) -> Dict:
        """生成下一幕信息（双轨融合版）

        轨道一：宏观摘要线
        - 注入前一卷/前一部的高浓缩摘要
        - 提供时空基石，防止时间线错乱

        轨道二：微观高亮线
        - 强制注入待回收伏笔
        - 注入角色当前状态锚点

        target_volume：新幕将要挂载的卷（换卷时传入），用于向 LLM 标注卷切换。
        """
        # 每幕章数取自本书真实目标，而不是历史遗留的硬编码 100 章基准
        # （对一部 5 章目标的书，按 100 章算会得出每幕 6 章，直接把预算撑破）。
        _default_cpa = self._act_budget_context(
            novel_id, int(current_act.number or 0) + 1
        )["chapters_per_act"]

        # 收集双轨上下文
        dual_track_context = await self._collect_dual_track_context(novel_id, current_act, bible_context)

        # 构建增强型 Prompt
        prompt = self._build_next_act_prompt_with_dual_track(
            current_act,
            dual_track_context,
            target_volume=target_volume,
        )
        
        try:
            raw = await self._collect_llm_stream_text(
                prompt,
                generation_config_from_profile("planning_act"),
            )
            result = self._parse_llm_response(raw)
            
            # 确保返回必要的字段
            if not isinstance(result, dict):
                result = {}
            result.setdefault("title", f"第{current_act.number + 1}幕")
            result.setdefault("description", "继续推进剧情")
            result.setdefault("suggested_chapter_count", _default_cpa)
            
            return result
        except Exception as e:
            logger.warning(f"生成下一幕信息失败: {e}")
            return {
                "title": f"第{current_act.number + 1}幕",
                "description": "描述",
                "suggested_chapter_count": _default_cpa
            }
    
    async def _collect_dual_track_context(
        self,
        novel_id: str,
        current_act: StoryNode,
        bible_context: Dict,
    ) -> Dict[str, str]:
        """收集双轨上下文
        
        Returns:
            {
                "volume_summary": "前一卷的摘要",
                "current_volume_summary": "当前卷的摘要",
                "pending_foreshadowings": "待回收伏笔列表",
                "character_states": "角色状态锚点",
            }
        """
        context = {
            "volume_summary": "",
            "current_volume_summary": "",
            "pending_foreshadowings": "",
            "character_states": "",
        }
        
        try:
            # 获取所有节点
            all_nodes = await self.story_node_repo.get_by_novel(novel_id)
            
            # 找到当前幕所属的卷
            current_volume = None
            if current_act.parent_id:
                current_volume = next(
                    (n for n in all_nodes if n.id == current_act.parent_id),
                    None
                )
            
            # 轨道一：获取卷摘要
            if current_volume:
                vol_summary = current_volume.metadata.get("summary", "") if current_volume.metadata else ""
                if vol_summary:
                    context["current_volume_summary"] = f"【当前卷进度】{current_volume.title}\n{vol_summary}"
            
            # 获取前一卷的摘要
            volume_nodes = sorted(
                [n for n in all_nodes if n.node_type.value == "volume"],
                key=lambda x: x.number
            )
            if current_volume:
                prev_volumes = [v for v in volume_nodes if v.number < (current_volume.number or 0)]
                if prev_volumes:
                    prev_vol = prev_volumes[-1]
                    prev_summary = prev_vol.metadata.get("summary", "") if prev_vol.metadata else ""
                    if prev_summary:
                        context["volume_summary"] = f"【前一卷回顾】{prev_vol.title}\n{prev_summary}"
            
            # 轨道二：获取待回收伏笔
            if hasattr(self, 'chapter_repository') and self.chapter_repository:
                try:
                    from domain.novel.repositories.foreshadowing_repository import ForeshadowingRepository
                    from domain.novel.value_objects.novel_id import NovelId
                    
                    # 尝试获取伏笔仓库（通过依赖注入或直接创建）
                    if hasattr(self.story_node_repo, 'db_path'):
                        from infrastructure.persistence.database.foreshadowing_repository import ForeshadowingRepositoryImpl
                        foreshadowing_repo = ForeshadowingRepositoryImpl(self.story_node_repo.db_path)
                        registry = foreshadowing_repo.get_by_novel_id(NovelId(novel_id))
                        
                        if registry:
                            pending = registry.get_unresolved()
                            if pending:
                                lines = ["【待回收伏笔】"]
                                for f in pending[:5]:
                                    lines.append(f"- 第{f.planted_in_chapter}章: {f.description}")
                                context["pending_foreshadowings"] = "\n".join(lines)
                except Exception as e:
                    logger.debug(f"获取伏笔信息时出错: {e}")
            
            # 轨道二：获取角色状态
            if bible_context and bible_context.get("characters"):
                char_lines = ["【角色当前状态】"]
                for char in bible_context["characters"][:3]:
                    name = char.get("name", "")
                    desc = char.get("description", "")
                    mental = char.get("mental_state", "")
                    tic = char.get("verbal_tic", "")
                    
                    char_info = f"- {name}: {desc[:50]}"
                    if mental:
                        char_info += f" [心理: {mental}]"
                    if tic:
                        char_info += f" 口头禅: {tic}"
                    char_lines.append(char_info)
                
                context["character_states"] = "\n".join(char_lines)
        
        except Exception as e:
            logger.warning(f"收集双轨上下文失败: {e}")
        
        return context
    
    def _build_next_act_prompt_with_dual_track(
        self,
        current_act: StoryNode,
        dual_track_context: Dict[str, str],
        target_volume: Optional[StoryNode] = None,
    ) -> Prompt:
        """构建双轨融合的下一幕生成 Prompt。"""
        context_parts = [
            dual_track_context[key]
            for key in (
                "volume_summary",
                "current_volume_summary",
                "pending_foreshadowings",
                "character_states",
            )
            if dual_track_context.get(key)
        ]
        if target_volume is not None and target_volume.id != current_act.parent_id:
            # 换卷建幕：向 LLM 标注新卷的阶段定位，让幕内容衔接新卷的承诺与回报
            note = f"【卷切换】本幕起进入第{target_volume.number}卷《{target_volume.title}》"
            volume_desc = (target_volume.description or "").strip()
            if volume_desc:
                note += f"：{volume_desc}"
            note += "\n请让剧情自然过渡到新卷的阶段目标，并在卷末给出阶段性回报。"
            context_parts.append(note)
        context_block = "\n\n".join(context_parts) if context_parts else "暂无前文上下文"
        current_number = current_act.number or 0

        # 新幕自身在全书里的位置（尤其"这是最后一幕"必须写进 prompt，
        # 否则动态补出来的幕永远只是"继续推进"，故事不会收口）
        budget_ctx = self._act_budget_context(
            getattr(current_act, "novel_id", None) or "", getattr(current_act, "number", 0) + 1
        )
        context_block = f"{context_block}\n\n{budget_ctx['brief']}"

        return self._render_contract_prompt(
            CONTINUOUS_PLANNING_NEXT_ACT_CONTRACT,
            {
                "context_block": context_block,
                "current_act_title": current_act.title or "未命名幕",
                "current_act_description": current_act.description or "无",
                "current_act_number": current_number,
                "next_act_number": current_number + 1,
            },
        )
