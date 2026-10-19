import re
import uuid
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
from domain.novel.value_objects.chapter_state import ChapterState
from domain.novel.value_objects.novel_id import NovelId
from domain.bible.entities.character import Character
from domain.bible.value_objects.character_id import CharacterId
from domain.novel.value_objects.foreshadowing import (
    DEFAULT_BINDING_LEVEL,
    DEFAULT_EMOTIONAL_WEIGHT,
    FORESHADOW_BINDING_CHARACTER,
    FORESHADOW_BINDING_WORLD,
    Foreshadowing,
    ForeshadowingStatus,
    ImportanceLevel
)
from domain.novel.value_objects.timeline_event import TimelineEvent
from domain.novel.entities.storyline import Storyline
from domain.novel.value_objects.storyline_type import StorylineType
from domain.novel.value_objects.storyline_status import StorylineStatus
from domain.bible.repositories.bible_repository import BibleRepository
from domain.novel.repositories.foreshadowing_repository import ForeshadowingRepository
from domain.novel.repositories.timeline_repository import TimelineRepository
from domain.novel.repositories.storyline_repository import StorylineRepository
from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.entities.timeline_registry import TimelineRegistry

# G-01 / G-03 的取值裁决。foreshadow_pacing 无第三方 import，不构成循环。
from application.world.services.foreshadow_pacing import (
    SOURCE_AI,
    SOURCE_AI_ADJUSTED,
    SOURCE_FALLBACK,
    resolve_offset as resolve_offset_from_ai,
    resolve_storyline_end,
)

logger = logging.getLogger(__name__)


def _resolve_plant_offset(importance_value: int, raw_offset: Any) -> Tuple[int, str, str]:
    """G-01：一条新埋伏笔的「回收偏移 + 来源 + 说明」。返回 ``(章数, 来源, 说明)``。

    三种来源，与 ``foreshadow_pacing`` 的三态一致：
      - ``ai``          模型给了偏移且在 [2,30] 内 ⇒ 直接用
      - ``ai_adjusted`` 模型给了但被钳位 ⇒ 用钳后的值（最终取值非模型原值）
      - ``fallback``    模型没给/不可解析 ⇒ 按 importance 推 +15/+10/+5

    ⚠ fallback 分支的说明**完全自建**，不引用裁决器那句「按兜底 5 章」——
    实际采用的不是 5。这段文本会随 ``resolve_offset_note`` 落 registry JSON，
    前后半句打架就是假 provenance 落库。

    抽成纯函数是为了可测：整个 StateUpdater 的协作依赖太多，
    在单测里搭最小替身跑不到这里。
    """
    offset, source, adjudicator_note = resolve_offset_from_ai(raw_offset)
    if source != SOURCE_FALLBACK:
        return offset, source, adjudicator_note

    if raw_offset is None or (isinstance(raw_offset, str) and not raw_offset.strip()):
        why = "模型未给出回收偏移"
    else:
        why = f"模型给出的回收偏移不可解析（{raw_offset!r}）"

    if importance_value >= ImportanceLevel.HIGH.value:
        delta, by = 15, "+15"
    elif importance_value >= ImportanceLevel.MEDIUM.value:
        delta, by = 10, "+10"
    else:
        delta, by = 5, "+5"
    return delta, SOURCE_FALLBACK, f"{why}；改按 importance 推算 {by}"


@dataclass
class UnresolvedForeshadowingResolution:
    """一条没能配对到已注册伏笔的回收指令。

    AI 说「这条伏笔已回收」，代码却配不上具体伏笔时，**绝不能静默丢弃**：
    丢弃意味着伏笔永远停在 PLANTED，且没有任何痕迹说明它本该被回收。

    这里只如实记录发生了什么，不替 AI 做「选哪个候选」的创作判断 ——
    那是创作决定，应交还给 LLM / 作者重新决策。
    """

    reason: str                    # unpaired_resolution / mark_resolved_failed
    chapter_number: int            # 该回收指令声称的回收章节
    resolved_data: Dict[str, Any]  # LLM 原始载荷，供复盘与重新配对
    detail: str = ""               # 失败细节（异常消息等）


@dataclass
class ForeshadowingUpdateReport:
    """章后伏笔同步的显式结果。

    调用方拿到它，就能区分「回收指令全部落库」与「有指令没落地」。
    丢弃这个返回值 = 重新引入静默丢失。
    """

    novel_id: str
    chapter_number: int
    resolved_ids: List[str] = field(default_factory=list)
    unresolved: List[UnresolvedForeshadowingResolution] = field(default_factory=list)

    @property
    def has_unresolved(self) -> bool:
        return bool(self.unresolved)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _role_to_chapter_importance(role: str) -> str:
    """将角色的 Bible role 映射到 chapter_elements.importance。

    Bible role (PROTAGONIST/SUPPORTING/MINOR) → 章节级别 (major/normal/minor)
    """
    r = (role or '').upper()
    if r == 'PROTAGONIST':
        return 'major'
    if r in ('SUPPORTING', 'MAJOR_SUPPORTING', 'IMPORTANT_SUPPORTING', 'MAJOR'):
        return 'normal'
    return 'minor'


def _safe_chapter_int(value: Any, fallback: int) -> int:
    """LLM 常在 chapter 字段填「本章」「当前章节」等，避免 int() 直接崩。"""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"\d+", s)
    if m:
        return int(m.group(0))
    return fallback
def _parse_importance(value: Any) -> "ImportanceLevel":
    """将 LLM 返回的 importance 值解析为 ImportanceLevel 枚举。

    支持格式：
    - 字符串: "high", "medium", "low", "critical", "高", "中", "低", "关键"
    - 数字: 1-4 (对应 ImportanceLevel 枚举值)
    - 默认: MEDIUM
    """
    from domain.novel.value_objects.foreshadowing import ImportanceLevel

    if isinstance(value, ImportanceLevel):
        return value
    if isinstance(value, int):
        try:
            return ImportanceLevel(value)
        except ValueError:
            return ImportanceLevel.MEDIUM
    if isinstance(value, str):
        low_map = {"low", "低", "1"}
        medium_map = {"medium", "中", "中等", "2"}
        high_map = {"high", "高", "重要", "3"}
        critical_map = {"critical", "关键", "核心", "至关重要", "4"}
        v_lower = value.strip().lower()
        if v_lower in low_map:
            return ImportanceLevel.LOW
        if v_lower in medium_map:
            return ImportanceLevel.MEDIUM
        if v_lower in high_map:
            return ImportanceLevel.HIGH
        if v_lower in critical_map:
            return ImportanceLevel.CRITICAL
    return ImportanceLevel.MEDIUM


def _parse_emotional_weight(value: Any) -> float:
    """解析 LLM 返回的 emotional_weight，非法/缺失一律回落默认 0.5。

    与 chapter_narrative_sync 的同名解析保持一致：这里是第二条活的伏笔种植路径，
    不解析就意味着种下的伏笔永远停在惰性默认值，绑定层级校验对它永远不生效。
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_EMOTIONAL_WEIGHT
    if not 0.0 <= parsed <= 1.0:
        return DEFAULT_EMOTIONAL_WEIGHT
    return parsed


def _parse_binding_level(value: Any) -> int:
    """解析 LLM 返回的 binding_level（1=角色 2=势力 3=事件 4=世界）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BINDING_LEVEL
    if not FORESHADOW_BINDING_CHARACTER <= parsed <= FORESHADOW_BINDING_WORLD:
        return DEFAULT_BINDING_LEVEL
    return parsed


class StateUpdater:
    """状态更新应用服务

    根据 ChapterState 更新各个领域对象
    """

    def __init__(
        self,
        bible_repository: BibleRepository,
        foreshadowing_repository: ForeshadowingRepository,
        timeline_repository: Optional[TimelineRepository] = None,
        storyline_repository: Optional[StorylineRepository] = None,
        knowledge_service=None,
        chapter_repository=None,
        db_connection=None
    ):
        self.bible_repository = bible_repository
        self.foreshadowing_repository = foreshadowing_repository
        self.timeline_repository = timeline_repository
        self.storyline_repository = storyline_repository
        self.knowledge_service = knowledge_service
        self.chapter_repository = chapter_repository
        self.db_connection = db_connection

    def update_from_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        chapter_state: ChapterState
    ) -> ForeshadowingUpdateReport:
        """从章节状态更新所有相关对象

        Args:
            novel_id: 小说ID
            chapter_number: 章节号
            chapter_state: 章节状态

        Returns:
            ForeshadowingUpdateReport: 显式结果。``unresolved`` 非空表示本章存在
            「AI 说已回收、但配不上伏笔」的指令，必须由调用方决定如何呈现，
            不得当作无事发生。
        """
        chapter_number = _safe_chapter_int(chapter_number, 1)
        if chapter_number < 1:
            chapter_number = 1
        novel_id_obj = NovelId(novel_id)
        report = ForeshadowingUpdateReport(novel_id=novel_id, chapter_number=chapter_number)
        logger.info(
            f"StateUpdater.update_from_chapter: novel={novel_id}, chapter={chapter_number}, "
            f"new_characters={len(chapter_state.new_characters)}, "
            f"foreshadowing_planted={len(chapter_state.foreshadowing_planted)}, "
            f"foreshadowing_resolved={len(chapter_state.foreshadowing_resolved)}"
        )

        # 更新 Bible（新角色）
        if chapter_state.has_new_characters():
            logger.debug(f"Updating Bible with {len(chapter_state.new_characters)} new characters")
            bible = self.bible_repository.get_by_novel_id(novel_id_obj)
            if bible is None:
                raise ValueError(f"Bible not found for novel {novel_id}")
            else:
                for char_data in chapter_state.new_characters:
                    char_id = CharacterId(str(uuid.uuid4()))
                    character = Character(
                        id=char_id,
                        name=char_data.get("name", "未知角色"),
                        description=char_data.get("description", "")
                    )
                    bible.add_character(character)
                    logger.debug(f"Added character: {char_data.get('name')}")

                self.bible_repository.save(bible)
                logger.info(f"Bible updated: added {len(chapter_state.new_characters)} new characters for novel {novel_id}")
        else:
            logger.debug("No new characters to add")

        # 更新 ForeshadowingRegistry（新伏笔和解决伏笔）
        if chapter_state.has_foreshadowing_activity():
            logger.debug(
                f"Updating ForeshadowingRegistry: "
                f"plant={len(chapter_state.foreshadowing_planted)}, "
                f"resolve={len(chapter_state.foreshadowing_resolved)}"
            )
            foreshadowing_registry = self.foreshadowing_repository.get_by_novel_id(novel_id_obj)
            if foreshadowing_registry is None:
                logger.info(f"ForeshadowingRegistry not found for novel {novel_id}, creating new one")
                foreshadowing_registry = ForeshadowingRegistry(
                    id=str(uuid.uuid4()),
                    novel_id=novel_id_obj
                )

            # ★ Phase 1: 添加新伏笔 — 使用 LLM 提取的 importance + 自动推算 suggested_resolve_chapter
            for foreshadow_data in chapter_state.foreshadowing_planted:
                # 解析 importance（LLM 可能返回字符串或数字）
                importance_val = foreshadow_data.get("importance", "medium")
                importance = _parse_importance(importance_val)

                # G-01：推算 suggested_resolve_chapter。
                # 决策已抽成纯函数 _resolve_plant_offset —— 三种来源（ai /
                # ai_adjusted / fallback）与说明生成都在那里，两条红线
                # （「代码改写了取值就别标成 ai」「note 必须自述实际采用值」）
                # 也都在那里收口，本段只负责取键与落库。
                planted_ch = _safe_chapter_int(
                    foreshadow_data.get("chapter"), chapter_number
                )
                #
                # ⚠ 用共享裁决器而不是手写 `if x and isinstance(x, (int, float))`：
                #   · 手写版把模型明说的 **0** 和字符串 "7" 当成缺失，然后写下
                #     「模型未给出回收偏移」这句**假说明**
                #   · 裁决器还负责钳到 [2, 30]，手写版完全不钳
                #
                # ⚠ 键名与 narrative_sync 对齐为 `suggested_resolve_offset`；
                # `resolve_offset` 保留读取以兼容更早的输出。
                raw_offset = foreshadow_data.get("suggested_resolve_offset")
                if raw_offset is None:
                    raw_offset = foreshadow_data.get("resolve_offset")
                offset, offset_source, offset_note = _resolve_plant_offset(
                    importance.value, raw_offset
                )
                suggested_resolve = planted_ch + offset


                foreshadowing = Foreshadowing(
                    id=str(uuid.uuid4()),
                    planted_in_chapter=planted_ch,
                    description=foreshadow_data.get("description", ""),
                    importance=importance,
                    status=ForeshadowingStatus.PLANTED,
                    suggested_resolve_chapter=suggested_resolve,
                    emotional_weight=_parse_emotional_weight(
                        foreshadow_data.get("emotional_weight")
                    ),
                    binding_level=_parse_binding_level(
                        foreshadow_data.get("binding_level")
                    ),
                    resolve_offset_source=offset_source,
                    resolve_offset_note=offset_note,
                )
                foreshadowing_registry.register(foreshadowing)
                logger.debug(
                    f"Planted foreshadowing: {foreshadow_data.get('description', '')[:50]} "
                    f"(importance={importance.name}, resolve_by_ch={suggested_resolve})"
                )

            # 解决伏笔
            for resolved_data in chapter_state.foreshadowing_resolved:
                fid = self._resolve_foreshadowing_id(foreshadowing_registry, resolved_data)
                resolved_ch = _safe_chapter_int(
                    resolved_data.get("chapter"), chapter_number
                )
                # ★ 未配对的回收指令不得静默跳过（登记表 G-02）。
                #   歧义 / 无匹配 / fid 不存在时若直接 continue，这条伏笔会永远停在
                #   PLANTED 且无人知晓；此处显式记入 report 并打 ERROR 日志。
                if fid is None:
                    report.unresolved.append(
                        UnresolvedForeshadowingResolution(
                            reason="unpaired_resolution",
                            chapter_number=resolved_ch,
                            resolved_data=dict(resolved_data),
                            detail="回收指令未配对到任何已注册伏笔；具体原因见同批 ERROR 日志",
                        )
                    )
                    logger.error(
                        "伏笔回收指令未配对，已记入 report.unresolved（该伏笔不会被标记回收）: "
                        "novel=%s chapter=%s resolved_data=%s",
                        novel_id, resolved_ch, resolved_data,
                    )
                    continue
                try:
                    foreshadowing_registry.mark_resolved(
                        foreshadowing_id=fid,
                        resolved_in_chapter=resolved_ch
                    )
                    report.resolved_ids.append(fid)
                    logger.debug(f"Resolved foreshadowing: {fid}")
                except Exception as e:
                    # 落库失败同样不能只 warning 了事——记入 report 交由调用方呈现。
                    report.unresolved.append(
                        UnresolvedForeshadowingResolution(
                            reason="mark_resolved_failed",
                            chapter_number=resolved_ch,
                            resolved_data=dict(resolved_data),
                            detail=str(e),
                        )
                    )
                    logger.error(
                        "伏笔回收落库失败，已记入 report.unresolved: "
                        "novel=%s chapter=%s fid=%s error=%s",
                        novel_id, resolved_ch, fid, e,
                    )

            self.foreshadowing_repository.save(foreshadowing_registry)
            logger.info(f"ForeshadowingRegistry updated for novel {novel_id}")
        else:
            logger.debug("No foreshadowing activity to update")

        # 更新 TimelineRegistry（时间线事件 - 第一梯队）
        if self.timeline_repository and chapter_state.has_timeline_events():
            logger.debug(f"Updating TimelineRegistry: {len(chapter_state.timeline_events)} events")
            timeline_registry = self.timeline_repository.get_by_novel_id(novel_id_obj)
            if timeline_registry is None:
                logger.info(f"TimelineRegistry not found for novel {novel_id}, creating new one")
                timeline_registry = TimelineRegistry(
                    id=str(uuid.uuid4()),
                    novel_id=novel_id_obj
                )

            # 添加时间线事件
            for event_data in chapter_state.timeline_events:
                timeline_event = TimelineEvent(
                    id=str(uuid.uuid4()),
                    chapter_number=chapter_number,
                    event=event_data.get("event", ""),
                    timestamp=event_data.get("timestamp", ""),
                    timestamp_type=event_data.get("timestamp_type", "vague")
                )
                timeline_registry.add_event(timeline_event)
                logger.debug(f"Added timeline event: {event_data.get('event', '')[:50]}")

            self.timeline_repository.save(timeline_registry)
            logger.info(f"TimelineRegistry updated for novel {novel_id}")
        else:
            logger.debug("No timeline events to update")

        # 更新 Storyline（故事线进度 - 第二梯队）
        if self.storyline_repository and chapter_state.has_storyline_activity():
            logger.debug(
                f"Updating Storylines: "
                f"advanced={len(chapter_state.advanced_storylines)}, "
                f"new={len(chapter_state.new_storylines)}"
            )

            # 推进已有故事线
            for advanced_data in chapter_state.advanced_storylines:
                storyline_id = advanced_data.get("storyline_id")
                storyline_name = advanced_data.get("storyline_name")
                progress_summary = advanced_data.get("progress_summary", "")

                if storyline_id:
                    # 通过 ID 查找
                    storyline = self.storyline_repository.get_by_id(storyline_id)
                    if storyline:
                        storyline.update_progress(chapter_number, progress_summary)
                        self.storyline_repository.save(storyline)
                        logger.debug(f"Updated storyline {storyline_id}: {progress_summary[:50]}")
                    else:
                        logger.warning(f"Storyline {storyline_id} not found")
                elif storyline_name:
                    # 通过名称查找（需要遍历）
                    storylines = self.storyline_repository.get_by_novel_id(novel_id_obj)
                    found = False
                    for sl in storylines:
                        if sl.name == storyline_name:
                            sl.update_progress(chapter_number, progress_summary)
                            self.storyline_repository.save(sl)
                            logger.debug(f"Updated storyline '{storyline_name}': {progress_summary[:50]}")
                            found = True
                            break
                    if not found:
                        logger.warning(f"Storyline '{storyline_name}' not found")

            # 创建新故事线
            for new_data in chapter_state.new_storylines:
                storyline_type_str = new_data.get("type", "sub")
                # 根据类型字符串映射到枚举值
                if storyline_type_str == "main":
                    storyline_type = StorylineType.MAIN_PLOT
                else:
                    # 默认使用 GROWTH 作为支线类型
                    storyline_type = StorylineType.GROWTH

                # G-03：收尾章优先用模型给的；没给才退回代码默认的 +50。
                # 此前这里是无条件 `chapter_number + 50` —— 登记表 G-03 指向的
                # 就是这一行，一个创作判断被写成了常数。
                storyline_end, end_source, end_note = resolve_storyline_end(
                    chapter_number, new_data.get("estimated_chapter_end")
                )
                if end_source == SOURCE_AI:
                    logger.info(
                        "故事线收尾章采用模型判断 novel=%s ch=%s end=%s",
                        novel_id, chapter_number, storyline_end,
                    )
                elif end_source == SOURCE_AI_ADJUSTED:
                    # 与 chapter_narrative_sync 侧保持可观测通道对称
                    logger.info(
                        "故事线收尾章 novel=%s ch=%s end=%s（%s）"
                        " —— 模型给了值但被代码改写，最终取值非模型原值",
                        novel_id, chapter_number, storyline_end, end_note,
                    )
                elif end_source == SOURCE_FALLBACK:
                    logger.info(
                        "故事线收尾章兜底 novel=%s ch=%s end=%s（%s，来源已落库标注）",
                        novel_id, chapter_number, storyline_end, end_note,
                    )
                new_storyline = Storyline(
                    id=str(uuid.uuid4()),
                    novel_id=novel_id_obj,
                    storyline_type=storyline_type,
                    status=StorylineStatus.ACTIVE,
                    estimated_chapter_start=chapter_number,
                    estimated_chapter_end=storyline_end,
                    estimated_chapter_end_source=end_source,
                    estimated_chapter_end_note=end_note,
                    name=new_data.get("name", ""),
                    description=new_data.get("description", ""),
                    last_active_chapter=chapter_number,
                    progress_summary=f"在第{chapter_number}章引入"
                )
                self.storyline_repository.save(new_storyline)
                logger.debug(f"Created new storyline: {new_data.get('name', '')}")

            logger.info(f"Storylines updated for novel {novel_id}")
        else:
            logger.debug("No storyline activity to update")

        # 更新 Knowledge（章节摘要）
        if self.knowledge_service:
            self._update_knowledge(novel_id, chapter_number, chapter_state)

        # 写入 chapter_elements（角色出场信息）
        if chapter_state.has_new_characters() and self.db_connection:
            self._write_chapter_elements(novel_id, chapter_number, chapter_state.new_characters)

        return report

    def _resolve_foreshadowing_id(
        self,
        registry: ForeshadowingRegistry,
        resolved_data: Dict[str, Any],
    ) -> Optional[str]:
        """兼容 LLM 返回描述而非伏笔 ID 的情况。

        返回 None 表示「这条回收指令配不上任何已注册伏笔」。调用方**必须**让它
        可见：静默跳过等于让伏笔永远停在 PLANTED 而无人知晓（登记表 G-02）。

        绝不返回未经 ``registry.get_by_id`` 校验的 fid —— 那种 fid 会在
        ``mark_resolved`` 处抛错，异常再被降级成 warning，回收指令同样凭空消失。
        歧义时也绝不替 AI 选一个候选。
        """
        fid = str(resolved_data.get("foreshadowing_id", "")).strip()
        verified = self._verified_foreshadowing_id(registry, fid)
        if verified is not None:
            return verified

        description = str(
            resolved_data.get("description")
            or resolved_data.get("foreshadowing_description")
            or resolved_data.get("resolved_foreshadowing")
            or fid
            or ""
        ).strip()
        if not description:
            return None

        normalized = _normalize_text(description)
        exact_match = None
        fuzzy_matches = []
        for foreshadowing in registry.foreshadowings:
            candidate = _normalize_text(foreshadowing.description)
            if not candidate:
                continue
            if candidate == normalized:
                exact_match = foreshadowing.id
                break
            if normalized in candidate or candidate in normalized:
                fuzzy_matches.append(foreshadowing.id)

        if exact_match:
            return exact_match
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0]
        if len(fuzzy_matches) > 1:
            logger.error(
                "Ambiguous foreshadowing resolution for %r, candidates=%s "
                "— 不替 AI 选候选，该伏笔保持 PLANTED",
                description,
                fuzzy_matches,
            )
            return None
        # 描述匹配不上任何已注册伏笔：不再回退到未经校验的 fid。
        logger.error(
            "Foreshadowing resolution matched nothing for %r (raw fid=%r) — 该伏笔保持 PLANTED",
            description,
            fid,
        )
        return None

    @staticmethod
    def _verified_foreshadowing_id(
        registry: ForeshadowingRegistry,
        fid: str,
    ) -> Optional[str]:
        """fid 非空且 registry 里真实存在才返回它，否则 None。

        三个返回点共用这一个校验，避免各自复述一遍判断。
        """
        if fid and registry.get_by_id(fid):
            return fid
        return None

    def _update_knowledge(
        self,
        novel_id: str,
        chapter_number: int,
        chapter_state: ChapterState
    ) -> None:
        """更新 Knowledge 中的章节摘要和知识三元组

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            chapter_state: 章节状态
        """
        try:
            # 构建关键事件描述
            events_desc = ""
            if chapter_state.events:
                event_texts = [e.get("description", "") for e in chapter_state.events[:5]]
                events_desc = "；".join(t for t in event_texts if t)

            # 构建新角色描述
            new_chars_desc = ""
            if chapter_state.new_characters:
                new_chars_desc = "新登场：" + "、".join(
                    c.get("name", "") for c in chapter_state.new_characters
                )

            key_events = events_desc or new_chars_desc or "（本章无特殊标注事件）"

            # 构建未解伏笔描述
            open_threads = ""
            if chapter_state.foreshadowing_planted:
                threads = [f.get("description", "")[:40] for f in chapter_state.foreshadowing_planted]
                open_threads = "伏笔：" + "；".join(t for t in threads if t)

            self.knowledge_service.upsert_chapter_summary(
                novel_id=novel_id,
                chapter_id=chapter_number,
                summary="",  # 暂不生成摘要文字（节省 token）
                key_events=key_events,
                open_threads=open_threads,
                consistency_note="",
                beat_sections=[],
                sync_status="auto"
            )
            logger.info(f"Knowledge chapter summary updated for novel {novel_id}, chapter {chapter_number}")

            # 如有新角色，添加为知识三元组
            for char_data in chapter_state.new_characters:
                char_name = char_data.get("name", "")
                char_desc = char_data.get("description", "")
                if char_name and char_desc:
                    fact_id = f"char-{novel_id}-{char_name}-ch{chapter_number}"
                    self.knowledge_service.upsert_fact(
                        novel_id=novel_id,
                        fact_id=fact_id,
                        subject=char_name,
                        predicate="登场于",
                        object=f"第{chapter_number}章",
                        chapter_id=chapter_number,
                        note=char_desc[:100]
                    )
        except Exception as e:
            logger.warning(f"Failed to update Knowledge for novel {novel_id}: {e}")

    def _write_chapter_elements(
        self,
        novel_id: str,
        chapter_number: int,
        new_characters: List[Dict[str, Any]]
    ) -> None:
        """写入角色出场信息到 chapter_elements 表

        Args:
            novel_id: 小说ID
            chapter_number: 章节号
            new_characters: 新角色列表
        """
        try:
            # 获取 chapter_id（需要查询 story_nodes 表）
            from domain.novel.value_objects.chapter_id import ChapterId

            # 简化实现：直接使用数据库连接查询
            # 实际项目中应该通过 chapter_repository 获取
            cursor = self.db_connection.cursor()

            # 查询章节对应的 story_node_id
            cursor.execute(
                """
                SELECT id FROM story_nodes
                WHERE novel_id = ? AND node_type = 'chapter' AND number = ?
                LIMIT 1
                """,
                (novel_id, chapter_number)
            )
            row = cursor.fetchone()

            if not row:
                logger.warning(f"Story node not found for chapter {chapter_number}")
                return

            chapter_id = row[0]

            # 批量插入角色出场记录
            inserted_count = 0
            for char_data in new_characters:
                char_name = char_data.get("name", "")
                if not char_name:
                    continue

                # 查询角色ID及 role — 优先 unified_characters，回退 bible_characters
                char_id: str | None = None
                role: str = char_data.get("role", "")

                cursor.execute(
                    """
                    SELECT id, role FROM unified_characters
                    WHERE novel_id = ? AND name = ?
                    LIMIT 1
                    """,
                    (novel_id, char_name)
                )
                unified_row = cursor.fetchone()
                if unified_row:
                    char_id = unified_row[0]
                    if not role:
                        role = unified_row[1] or ""
                else:
                    cursor.execute(
                        """
                        SELECT id FROM bible_characters
                        WHERE novel_id = ? AND name = ?
                        LIMIT 1
                        """,
                        (novel_id, char_name)
                    )
                    char_row = cursor.fetchone()
                    char_id = char_row[0] if char_row else str(uuid.uuid4())

                importance = _role_to_chapter_importance(role)

                # 插入 chapter_elements — INSERT OR IGNORE 保留作者预设的 importance
                element_id = f"elem-{uuid.uuid4().hex[:8]}"

                try:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO chapter_elements (
                            id, chapter_id, element_type, element_id,
                            relation_type, importance, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            element_id,
                            chapter_id,
                            'character',
                            char_id,
                            'appears',
                            importance,
                            datetime.now().isoformat()
                        )
                    )
                    inserted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to insert chapter_element for {char_name}: {e}")

            self.db_connection.commit()
            logger.info(f"Written {inserted_count} character appearances to chapter_elements")

        except Exception as e:
            logger.error(f"Failed to write chapter_elements: {e}", exc_info=True)
            if self.db_connection:
                self.db_connection.rollback()
