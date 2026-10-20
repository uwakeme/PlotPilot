
"""向导 Step 4：主线候选的**规范化**与上下文构建。

本模块原先还提供 `suggest_options`（直接调 LLM 生成三条主线候选）——
那是 invocation 审阅流之前的旧架构：向导两条路由都迁到 `create_invocation`
之后，生产路径上**没有任何调用方**，整条「直接调 LLM」的链路（
`suggest_options` → `parse_suggested_options` → `_build_prompt_and_config`）
连同只服务于它的 `_build_context` 一并删除。

现在活着的只有两件事：
  - `build_context`：向导上下文（21 处生产调用方）
  - `normalize_main_plot_options` / `_data`：把模型输出规范化成可落库的候选
    （invocation 续写处理器 `setup_main_plot_continuation` 在用）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping

from domain.ai.services.llm_service import LLMService
from application.world.services.bible_service import BibleService
from application.core.services.novel_service import NovelService
from application.blueprint.services.setup_context_builder import SetupContextBuilder
from application.ai.knowledge_llm_contract import parse_json_from_response

from application.blueprint.services.subline_contract import (  # noqa: E402
    normalize_sublines,
)

DEFAULT_TARGET_CHAPTERS = 100


def _ctx_target_chapters(ctx: Any) -> int:
    """从 ctx 取目标章数。

    区分「没有 / 非法」与「真的是那个数」：``or 100`` 会把合法的 0 也顶成 100，
    而这里的目标章数参与「merge_chapter ≤ 目标章数」不变量的上界计算，
    取错就等于整条校验空转。
    """
    if not isinstance(ctx, Mapping):
        return DEFAULT_TARGET_CHAPTERS
    raw = ctx.get("target_chapters")
    if raw is None:
        return DEFAULT_TARGET_CHAPTERS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TARGET_CHAPTERS
    return value if value > 0 else DEFAULT_TARGET_CHAPTERS


logger = logging.getLogger(__name__)

SETUP_TASK_MARKER = "setup_main_plot_options_v1"


class MainPlotSuggestionContractError(RuntimeError):
    """主线候选输出不满足合同。"""


def normalize_main_plot_options(raw: str, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """将模型输出规范化为可落库的主线候选。"""
    try:
        return normalize_main_plot_options_data(SetupMainPlotSuggestionService._parse_plot_json(raw), ctx)
    except Exception as e:
        logger.warning("Main plot suggestion parse failed: %s", e)
        raise MainPlotSuggestionContractError("主线候选 JSON 解析失败或结构不符合合同") from e

    raise MainPlotSuggestionContractError("主线候选数量不足：需要至少 3 条有效方案")


def normalize_main_plot_options_data(raw_data: Any, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(raw_data, Mapping):
        raw_list = raw_data.get("plot_options")
    else:
        raw_list = raw_data
    if not isinstance(raw_list, list):
        raise MainPlotSuggestionContractError("主线候选输出必须是数组")
    # 用 ctx 里的真实目标章数，**不要**退回硬编码的 100 ——
    # 「merge_chapter ≤ 目标章数」这条不变量的上界取错，整条校验就是空转。
    normalized = SetupMainPlotSuggestionService._normalize_options(
        raw_list, target_chapters=_ctx_target_chapters(ctx)
    )
    normalized = SetupMainPlotSuggestionService._complete_option_architecture(ctx, normalized)
    if len(normalized) >= 3:
        return normalized[:3]
    raise MainPlotSuggestionContractError("主线候选数量不足：需要至少 3 条有效方案")


class SetupMainPlotSuggestionService:
    def __init__(
        self,
        llm_service: LLMService,
        bible_service: BibleService,
        novel_service: NovelService,
    ):
        self._llm = llm_service
        self._bible_service = bible_service
        self._novel_service = novel_service
        self._context_builder = SetupContextBuilder(
            bible_service=bible_service,
            novel_service=novel_service,
        )

    def build_context(self, novel_id: str) -> Dict[str, Any]:
        """公开的向导上下文构建入口，供 AI Invocation 路由复用。"""
        return self._context_builder.build_context(novel_id)

    @staticmethod
    def _parse_plot_json(raw: str) -> List[Dict[str, Any]]:
        data = parse_json_from_response(raw)
        opts = data.get("plot_options")
        if not isinstance(opts, list):
            raise ValueError("plot_options must be a list")
        return opts

    @staticmethod
    def _normalize_options(
        raw_list: List[Dict[str, Any]],
        *,
        target_chapters: int,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for i, item in enumerate(raw_list[:5]):
            if not isinstance(item, dict):
                continue
            raw_sublines = item.get("sublines") or item.get("supporting_storylines") or []
            # A-01：走共享规范化。merge_chapter 缺失时记 None 而非 0 ——
            # 0 会同时违反「≤ 目标章数」与「晚于引入章」，且完全静默。
            sublines, sublines_source, subline_errors = normalize_sublines(
                raw_sublines, target_chapters=target_chapters
            )
            oid = str(item.get("id") or f"option_{chr(ord('a') + i)}")
            out.append(
                {
                    "id": oid,
                    "type": str(item.get("type") or "")[:120],
                    "title": str(item.get("title") or f"主线方案 {i + 1}")[:200],
                    "logline": str(item.get("logline") or "")[:2000],
                    "core_conflict": str(item.get("core_conflict") or "")[:2000],
                    "starting_hook": str(item.get("starting_hook") or "")[:2000],
                    "main_axis": str(item.get("main_axis") or item.get("axis") or "")[:2000],
                    "opening_pressure": str(item.get("opening_pressure") or "")[:1200],
                    "forbidden_drift": str(item.get("forbidden_drift") or "")[:1200],
                    "sublines": sublines,
                    # A-01：区分「模型规划了支线」与「模型没给」——
                    # 改造前两者都会被补成一条编造的支线，数据上无从分辨。
                    "sublines_source": sublines_source,
                    "subline_issues": subline_errors,
                }
            )
        return out

    @staticmethod
    def _complete_option_architecture(
        ctx: Dict[str, Any],
        options: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        target_chapters = _ctx_target_chapters(ctx)
        axis = ctx.get("fusion_axis") or {}
        core_promise = str(axis.get("core_promise") or "").strip()
        central_conflict = str(axis.get("central_conflict") or "").strip()
        false_mystery = str(axis.get("false_mystery") or "").strip()
        true_mystery = str(axis.get("true_mystery") or "").strip()
        forbidden_competitors = [
            str(x).strip()
            for x in (axis.get("forbidden_mainline_competitors") or [])
            if str(x).strip()
        ]
        taboos = [
            str(x).strip()
            for x in (axis.get("taboos") or [])
            if str(x).strip()
        ]
        mode_titles = {
            "survival": "生存求证线",
            "conspiracy": "黑箱揭露线",
            "anomaly": "规则变数线",
        }
        completed: List[Dict[str, Any]] = []
        for idx, item in enumerate(options):
            mode = "survival" if idx == 0 else "conspiracy" if idx == 1 else "anomaly"
            if not item.get("main_axis"):
                if core_promise or central_conflict:
                    parts = [
                        f"{mode_titles[mode]}必须服务融合题材主轴锁。",
                    ]
                    if core_promise:
                        parts.append(f"核心承诺：{core_promise}")
                    if central_conflict:
                        parts.append(f"中心冲突：{central_conflict}")
                    item["main_axis"] = " ".join(parts)
                else:
                    item["main_axis"] = (
                        f"第一主线围绕「{item.get('core_conflict') or item.get('logline') or '核心冲突'}」持续推进，"
                        "所有支线都必须回到这个承诺。"
                    )
            if not item.get("opening_pressure"):
                hook = str(item.get("starting_hook") or "").strip()
                if hook:
                    item["opening_pressure"] = (
                        "前三章必须把开篇钩子外化为现实压力、限时风险、外部追捕、资源损失或关系代价："
                        + hook[:500]
                    )
                else:
                    item["opening_pressure"] = "前三章必须出现外部压力、限时损失或不可回避的现实行动，不能只解释设定。"
            if not item.get("forbidden_drift"):
                drift_rules = []
                if forbidden_competitors:
                    drift_rules.append(
                        "不得抬成第一主线：" + "；".join(forbidden_competitors)
                    )
                if taboos:
                    drift_rules.append("融合禁忌：" + "；".join(taboos[:2]))
                if false_mystery and true_mystery:
                    drift_rules.append(
                        f"表层谜团「{false_mystery}」只能误导或铺证，必须回收至真实谜团「{true_mystery}」。"
                    )
                if drift_rules:
                    item["forbidden_drift"] = " ".join(drift_rules)
                else:
                    item["forbidden_drift"] = "不得让新谜团、新支线或回忆说明连续抢走主角的现实行动目标。"
            completed.append(item)
        return completed
