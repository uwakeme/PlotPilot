"""V6 记忆引擎 - LLM 驱动的章间状态机

解决长文本生成的三大状态崩溃问题：
1. FACT_LOCK: 不可篡改事实块（角色白名单、死亡名单、关系图谱、身份锁、时间线）
2. COMPLETED_BEATS: 已完成节拍锁（防止剧情鬼打墙/重复）
3. REVEALED_CLUES: 已揭露线索清单（防止前后矛盾）

设计原则：
- 不使用关键词匹配/启发式规则，所有状态提取均由 LLM 结构化完成
- 与现有 StateExtractor（9维领域状态）并行但职责正交：
  · StateExtractor → 写入 Bible/Foreshadowing/Timeline 等仓储（"发生了什么"）
  · MemoryEngine → 维护跨章一致性约束（"已经知道了什么，不能再怎么写"）
- 持久化到 memory_engine_state 表，重启不丢失
- 输出直接注入 ContextBudgetAllocator 的 T0 槽位（权重=∞）

架构：
    ChapterStateMachine (LLM Contract)
        ├── FactLockBuilder      ← 从 Bible + KnowledgeGraph 动态构建
        ├── BeatExtractor         ← LLM 提取本章完成的剧情节拍
        └── ClueExtractor         ← LLM 提取本章向读者揭露的真相/信息

    MemoryEngine (Facade)
        ├── build_fact_lock()          → str (注入 T0-α)
        ├── get_completed_beats()       → str (注入 T0-β)
        ├── get_revealed_clues()       → str (注入 T0-γ)
        └── update_from_chapter()      → 异步 LLM 调用 + 持久化
"""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from domain.ai.services.llm_service import LLMService
from domain.bible.repositories.bible_repository import BibleRepository
from domain.novel.value_objects.novel_id import NovelId

from application.ai.llm_json_extract import parse_llm_json_to_dict
from application.engine.services.memory_engine_settings import (
    MemoryEngineRuntimeSettings,
    get_memory_engine_runtime_settings,
)
from application.world.services.bible_classification import location_type_label
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.memory_extraction import MEMORY_EXTRACTION_CONTRACT
from infrastructure.ai.prompt_gateway import PromptGatewayError, get_prompt_gateway

logger = logging.getLogger(__name__)

# ============================================================
# LLM Contract: MemoryDelta（增量状态提取）
# ============================================================

_MAX_BEATS = 50
_MAX_CLUES = 100


class CompletedBeatItem(BaseModel):
    """已完成的一条剧情节拍"""
    beat_id: str = Field(description="节拍唯一标识，如 'ch3-meeting-first-time'")
    summary: str = Field(description="一句话概括这个已发生的事件")
    chapter: int = Field(description="该事件发生在第几章")
    characters_involved: List[str] = Field(default_factory=list, description="涉及的角色名")


class RevealedClueItem(BaseModel):
    """已向读者揭露的一条线索/真相"""
    clue_id: str = Field(description="线索唯一标识")
    content: str = Field(description="线索内容（读者和主角现在已知的信息）")
    revealed_at_chapter: int = Field(description="在第几章揭露")
    category: str = Field(
        default="truth",
        description="类别: truth(真相)/relationship(关系变化)/identity(身份暴露)/ability(能力揭示)/other"
    )
    is_still_valid: bool = Field(
        default=True,
        description="该线索是否仍然有效（未被后续章节推翻/证伪）"
    )


class FactViolationItem(BaseModel):
    """事实违反检测（LLM 检测到生成文本与 FACT_LOCK 冲突）"""
    violation_type: str = Field(
        description="违反类型: dead_character_resurrected/character_drift/"
                       "timeline_contradiction/relationship_error/unauthorized_character"
    )
    description: str = Field(description="具体描述哪里违反了事实锁")
    severity: str = Field(default="warning", description="critical / warning / info")
    location_hint: str = Field(default="", description="大致位置提示（如'对话段落中'）")


class MemoryDeltaPayload(BaseModel):
    """LLM 返回的记忆增量（extra=forbid 防止模型塞垃圾字段）"""
    model_config = ConfigDict(extra="forbid")

    completed_beats: List[CompletedBeatItem] = Field(
        default_factory=list, max_length=_MAX_BEATS,
        description="本章新完成的剧情节拍（之前没发生过的事件）"
    )
    revealed_clues: List[RevealedClueItem] = Field(
        default_factory=list, max_length=_MAX_CLUES,
        description="本章新向读者/主角揭露的信息或真相"
    )
    fact_violations: List[FactViolationItem] = Field(
        default_factory=list, max_length=20,
        description="检测到的事实锁违反（如果有）"
    )


# ============================================================
# FactLockBuilder: 从 Bible 动态构建不可篡改事实块
# ============================================================

#: FACT_LOCK 的**分节标题**（**单一来源**）。
#:
#: 用途有两个：
#: 1. ``_build_from_bible`` 用它们拼标题（⇒ 标题只有这一份）；
#: 2. 槽位被截断时**点名被截掉的小节**（复审 §4：此前截断只说「仅是部分内容」，
#:    而 FACT_LOCK 整段被截时**模型看不到「身份锁死」这一节存在**
#:    ⇒ 对「一旦违背即为废稿」的硬约束块，截掉整节比截断半截更危险）。
#:
#: ⚠ 用**具名常量**而不是下标：此前是 ``FACT_LOCK_SECTION_HEADERS[0..4]``，
#: 而插入新节会让后面全部错位 ⇒ 新节一旦加错位置，
#: 「身份锁死」的内容会被贴到「死亡锁」标题下，而**守卫仍然是绿的**
#: （守卫只查「标题在不在 tuple 里」）。
FACT_LOCK_HEADER_WHITELIST = "角色白名单（只可使用以下有名字的角色）："
FACT_LOCK_HEADER_LOCATION_WHITELIST = "地点白名单（只可使用以下地点，不得自造）："
FACT_LOCK_HEADER_CHARACTER_SETTINGS = "角色设定（不可更改）："
FACT_LOCK_HEADER_DEAD = "已死亡角色（绝对不可复活、不可在当下时间线中出现）："
FACT_LOCK_HEADER_IDENTITY = "身份锁死："
FACT_LOCK_HEADER_RELATIONS = "核心关系（不可更改）："
FACT_LOCK_HEADER_TIMELINE = "核心事件时间线（不可矛盾）："

#: ⚠ **顺序 = 截断时的存活优先级**（正文侧那把刀是从**尾部**切的）。
#:
#: 实测（生产书真实数据，第 24 批）：
#:   事实锁原始 **3876 字** ⇒ 正文侧上限 **2000 字**
#:   ⇒ 尾部被切。此前「角色设定」排在「身份锁死」**前面**（1025 字）
#:   ⇒ 刀口正好落在**最硬**的「身份锁死」这一节**中间**
#:   ⇒ 8 个角色的 POV 防火墙只活下来 **4 个**
#:   ⇒ 后 4 个角色的硬约束**没送到模型** ⇒ 模型自由发挥
#:   ⇒ 而生产实测抓到的「能力越界 / 性别矛盾」根因就在这里。
#:
#: ⇒ 硬约束在前、软约束在后（**按硬度排，不按篇幅排**）：
#:   **白名单 → 地点白名单 → 死亡锁 → 身份锁死 → 角色设定 → 核心关系 → 时间线**。
#:   * 死亡锁：极硬且**极短**（每个死者一行）⇒ 提到最前面不吃预算；
#:   * 身份锁死：**最硬**（已揭露的秘密 + POV 防火墙）⇒ 必须在「角色设定」前面；
#:   * 地点白名单：与角色白名单同级的**「只可使用」硬约束**（防自造地点），
#:     实测 ~130 字 ⇒ 放在身份锁前不吃预算（第 31 批实测：8/8 防火墙仍全到）；
#:   * 角色设定：仍是很硬的约束，但**篇幅最大** ⇒ 放在硬约束之后；
#:   * 核心关系 / 时间线：**软**（还能从别处推）⇒ 被切可接受（且**如实点名**）。
#:
#: ⚠ 调序后（生产数据实测）：**8/8** 个 POV 防火墙全部送达正文提示词。
FACT_LOCK_SECTION_HEADERS: tuple = (
    FACT_LOCK_HEADER_WHITELIST,
    FACT_LOCK_HEADER_LOCATION_WHITELIST,
    FACT_LOCK_HEADER_DEAD,
    FACT_LOCK_HEADER_IDENTITY,
    FACT_LOCK_HEADER_CHARACTER_SETTINGS,
    FACT_LOCK_HEADER_RELATIONS,
    FACT_LOCK_HEADER_TIMELINE,
)

#: 「角色设定」里**每个角色**最多给多少字。
#:
#: ⚠ 事实锁整块在正文提示词里只有 2000 字
#: （``prose_context_settings.DEFAULT_MAX_CHARS_PER_SLOT``），
#: 而角色数会随篇幅增长 ⇒ 不限长就会把「白名单 / 死亡锁」这些**更硬的**约束挤掉。
#: ⇒ 只给「职能 + 一句话定位」，超长截断（且如实留痕）。
CHARACTER_SETTING_MAX_CHARS = 60
#: 「角色设定」最多给多少个角色（超出的**如实点名**，不静默丢）。
CHARACTER_SETTING_MAX_CHARACTERS = 12

#: 身份锁里「[已揭露] 秘密主体」的限长（字符）——第 24 批。
#:
#: ⚠ 为什么要限：事实锁整块在正文提示词里只有 2000 字
#:   ⇒ 实测（生产书真实数据）原始 4202 字 ⇒ 刀口落在「身份锁死」这一节**中间**
#:   ⇒ 后半角色的硬约束（秘密 + POV 防火墙）**根本没送到模型**
#:   ⇒ 模型自由发挥 ⇒ 生产实测抓到的「角色能力越界 / 性别矛盾」根因就在这里。
#:
#: ⚠ 限的只是**叙述性描述**（可以被别处推出来）；
#:   **POV 防火墙不受此限**（它最硬：违反即人物失格）。
IDENTITY_SECRET_MAX_CHARS = 40

#: 身份锁里「当前心理」的限长（字符）——第 24 批（最容易从别处推 ⇒ 先砍它）。
IDENTITY_MENTAL_MAX_CHARS = 20

#: 身份锁里「公开面」的限长（字符）——第 24 批。
#:
#: ⚠ 公开面**不能删**（它在「角色设定」节里每条只给 60 字、且只给前 12 个角色
#:   ⇒ 删掉会让「只有公开面」的角色整行消失）⇒ 只能限长。
IDENTITY_PUBLIC_MAX_CHARS = 60

#: 身份锁里「性别 / 年龄」的限长（字符）——第 40 批。
#:
#: ⚠ 为什么必须进（生产实测）：Bible 的 ``Character.gender`` / ``age`` **全部有值**
#:   （生产书 8 个角色都有：沈砚青=女/25、郑铁珊=男/56…），
#:   而事实锁此前**只给** ``description`` ⇒ 性别/年龄**没进模型**
#:   ⇒ ⇒ 第 21 批生产实测抓到的「郑铁珊（基准=男）被正文写成女性」
#:   **根因就在这里** ⇒ ⇒ 性别/年龄是**极硬**的事实（写错即人物失格）。
#:
#: ⚠ 为什么放在**身份锁**这一节（而不是「角色设定」节）：
#:   「角色设定」节排在硬约束**之后** ⇒ 预算紧时**先被切**
#:   ⇒ ⇒ 性别/年龄必须跟「已揭露的秘密 + POV 防火墙」放在一起才活得下来。
#: ⚠ 限长很紧（各 8 字）⇒ 生产书全部 8 个角色加起来才 26 字。
IDENTITY_GENDER_MAX_CHARS = 8
IDENTITY_AGE_MAX_CHARS = 8

#: 地点白名单里「类型 + 一句话描述」的限长（字符）——第 40 批。
#:
#: ⚠ 为什么必须加（生产实测）：``Location`` 有 ``description``（89~132 字/条，全是
#:   **很硬的设定事实**：九缸地窖底下埋着九口老铜缸、回声戏院后台是压票的老印刷机、
#:   济生当是贺九「只收不问」的当铺兼帮会账房…），
#:   而地点白名单此前**只给名字** ⇒ ⇒ 模型**不知道这些地方是什么地方**
#:   ⇒ ⇒ 场景写错（把地窖写成阁楼、把后台写成前厅）没有任何防线。
#:
#: ⚠ 限长 40 字/地点 ⇒ 生产书 11 个地点 ≈ 440 字 ⇒ 事实锁总量仍在预算内。
LOCATION_DESCRIPTION_MAX_CHARS = 40

#: 「核心事件时间线」的**字符预算**（第 42 批）。
#:
#: ⚠ 为什么按**字符**而不是按条数（与蓝图侧 ``format_bible_context`` 同一口径）：
#:   条数只是兜底，字符预算才是硬约束 —— 一条 5 字的「陆深抉择补全」和一条
#:   500 字的事件占的 prompt 完全不同。
#: ⚠ 实测（长书 529 章那本）：时间线 **2074 条 / 36618 字** ⇒ 占事实锁 **94%**
#:   ⇒ 而硬约束节合计仅 2273 字 ⇒ **预算全被流水账吃掉了**。
TIMELINE_MAX_CHARS = 4000
#: 时间线的**条数兜底**（防「几百条 5 字事件」把预算吃光）。
TIMELINE_MAX_ENTRIES = 200
#: 单条「事件」限长（生产实测中位 13 字、最长 39 字）。
TIMELINE_EVENT_MAX_CHARS = 40
#: 单条「时间点」限长（生产实测 100% 有值，如「同夜九十秒倒计时内」）。
TIMELINE_TIME_POINT_MAX_CHARS = 20
#: 单条「事件描述」限长（生产实测 100% 有值，中位约 40 字）。
TIMELINE_DESC_MAX_CHARS = 60


def _split_hidden_profile(hidden: str) -> tuple[str, str]:
    """把 ``hidden_profile`` 拆成「秘密主体」与「POV 防火墙」。

    ⇒ 返回 ``(秘密主体, POV 防火墙整块)``。

    ⚠ 为什么要拆：POV 防火墙藏在隐藏面文本的**尾巴**上（形如
    ``……（POV防火墙：她的视角在后台认人之前，不得确知送票人身份。）``），
    而秘密主体限长时若不做区分，**刀口会落在防火墙中间**
    ⇒ 留下半句防火墙（模型可能当成别的意思）⇒ 比没有更糟。
    ⇒ 拆开 ⇒ 主体可限长，防火墙**整块保留**。
    """
    text = str(hidden or "")
    marker = text.find("POV防火墙")
    if marker < 0:
        return text, ""
    start = max(text.rfind("（", 0, marker), text.rfind("(", 0, marker))
    end = max(text.rfind("）", marker), text.rfind(")", marker))
    if start < 0 or end <= marker:
        return text, ""
    return text[:start].rstrip(), text[start:end + 1]


def _truncate_with_mark(text: str, limit: int) -> str:
    """截断并**留痕**（砍掉就要说：模型得知道这句话没写完）。

    「…」是**模型可见**的 ⇒ 必须在提示词里说清楚「这里被砍了」，
    否则模型会把半句话当成完整设定。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"



def character_names(bible) -> List[str]:
    """Bible 里的**有名字的角色**名单（白名单）。

    ⚠ 生成侧（``_build_from_bible``）与审计侧（``_facts_from_bible``）
    **必须用同一个来源** —— 否则「正文被锁住的」与「审计拿来比对的」不是一回事。
    """
    characters = getattr(bible, "characters", None) or []
    return [c.name for c in characters if getattr(c, "name", "")]


def whitelist_lines(names: List[str]) -> List[str]:
    """角色白名单的**文本行**（生成侧与审计侧**同一份** ⇒ 不会漂）。

    ⚠ 此前这两处各抄了一份（生成侧用「允许: A, B」+「禁止: …」，审计侧内联一行）
    ⇒ 改一边另一边不会跟着改 ⇒ 两边口径可能悄悄分叉。
    """
    if not names:
        return []
    return [
        FACT_LOCK_HEADER_WHITELIST,
        f"   允许: {', '.join(names)}",
        "   禁止: 创造任何其他有名字的角色！路人可以无名但不许命名！",
    ]


def location_names(bible) -> List[str]:
    """Bible 里的**地点**名单（白名单）—— 与 :func:`character_names` 同构。

    ⚠ 为什么需要（第 31 批）：审计侧拿地点当基准，生成侧却**只给本场地点**的描述
      ⇒ 模型压根不知道「有名字的地点」全集 ⇒ 可能**自造**地点
      ⇒ 而「自造地点」在审计侧**审不到**（基准里没有它）
      ⇒ ⇒ 与「角色白名单」同构：先告诉模型「只可使用这些」，审计才判得了。
    """
    locations = getattr(bible, "locations", None) or []
    return [loc.name for loc in locations if getattr(loc, "name", "")]


def location_whitelist_lines(locations: List[Any]) -> List[str]:
    """地点白名单的**文本行**（生成侧与审计侧**同一份** ⇒ 不会漂）。

    ⚠ 第 40 批：除了**名字**，还给「类型 + 一句话描述」
      ⇒ 此前只给名字 ⇒ 模型**不知道这些地方是什么地方**
      ⇒ 场景写错（把地窖写成阁楼、把后台写成前厅）没有任何防线
      ⇒ 实测：生产书 11 个地点的 description 共 1203 字（全是硬设定），
      限长 40 字/条 ⇒ ≈ 440 字。
    """
    locs = [loc for loc in (locations or []) if getattr(loc, "name", "")]
    if not locs:
        return []
    lines = [
        FACT_LOCK_HEADER_LOCATION_WHITELIST,
        f"   允许: {', '.join(loc.name for loc in locs)}",
        "   禁止: 创造其他有名字的地点！无名场所可以用，但不许起名成新地点。",
    ]
    # 逐个地点：类型 + 一句话描述（限长，砍了要留痕）
    for loc in locs:
        bits = []
        loc_type = location_type_label(getattr(loc, "location_type", ""))
        if loc_type:
            bits.append(f"（{loc_type}）")
        desc = (getattr(loc, "description", "") or "").strip()
        if desc:
            bits.append(_truncate_with_mark(desc, LOCATION_DESCRIPTION_MAX_CHARS))
        if bits:
            lines.append(f"   {loc.name}{''.join(bits)}")
    return lines


class FactLockBuilder:
    """从 Bible + KnowledgeGraph 动态构建 FACT_LOCK 文本
    
    不是硬编码！数据来源：
    - Bible.characters → 角色白名单 + 死亡检测 + 身份锁
    - Bible.timeline_notes → 核心时间线
    - Character.relationships → 关系图谱
    """

    def __init__(self, bible_repository: BibleRepository):
        self.bible_repository = bible_repository

    def build(self, novel_id: str, current_chapter: int = 0) -> str:
        """构建完整的 FACT_LOCK 文本块
        
        Args:
            novel_id: 小说 ID
            current_chapter: 当前章节号（用于判断 hidden_profile 是否可见
            
        Returns:
            格式化的 FACT_LOCK 文本
        """
        try:
            novel_id_obj = NovelId(novel_id)
            bible = self.bible_repository.get_by_novel_id(novel_id_obj)
            if not bible:
                return ""
            return self._build_from_bible(bible, current_chapter)
        except Exception as e:
            logger.warning(f"FactLock 构建失败: {e}")
            return ""

    def build_facts(self, novel_id: str, current_chapter: int = 0) -> List[str]:
        """结构化事实清单（逐条）—— 与 :meth:`build` 的文本**同源**。

        生成侧把 ``build()`` 的文本锁进 T0（prompt 侧约束）；审计侧要用**同一份**
        事实去比对正文。两侧不同源的话，审计比的与生成时被锁的不是一回事：
        生成锁的是 characters / timeline / relationships，审计却拿
        world_settings / locations 比对 —— 口径对不上，结论也就对不上。

        推导复用 ``build()`` 的同一批 helper（角色白名单 / 死亡锁 / 关系 /
        身份锁 / 时间线），**不另写一套**，避免两边漂移。
        """
        try:
            bible = self.bible_repository.get_by_novel_id(NovelId(novel_id))
            if not bible:
                return []
            return self._facts_from_bible(bible, current_chapter)
        except Exception as e:
            logger.warning(f"FactLock 事实清单构建失败: {e}")
            return []

    def _facts_from_bible(self, bible, current_chapter: int) -> List[str]:
        """把 ``_build_from_bible`` 的那几组推导，逐条摊成清单。"""
        facts: List[str] = []
        characters = bible.characters

        # 角色白名单：与生成侧**同一份**（``whitelist_lines``）⇒ 两边不会分叉
        wl_lines = whitelist_lines(character_names(bible))
        if wl_lines:
            facts.append("\n".join(wl_lines))

        # 地点白名单（第 31 批）：与生成侧**同一份** ⇒ 审计才判得了「自造地点」
        # ⚠ 第 40 批：同一份**输入**（Bible 的 locations）⇒ 两侧连「类型+描述」都同源
        loc_wl_lines = location_whitelist_lines(
            getattr(bible, "locations", None) or []
        )
        if loc_wl_lines:
            facts.append("\n".join(loc_wl_lines))

        for sl in self._build_character_setting_lines(characters):
            facts.append(f"角色设定：{sl}")

        for dc in self._extract_dead_characters(characters):
            facts.append(
                f"死亡锁：{dc.get('name', '')}（{dc.get('role', '未知')}）已死"
                f"（{dc.get('cause', '原因不详')}）—— 不可复活、不可出现在当下时间线"
            )

        for il in self._build_identity_lines(characters, current_chapter):
            facts.append(f"身份锁：{il}")

        for rel in self._build_relation_lines(characters):
            facts.append(f"关系锁：{rel}")

        for tl in self._build_timeline_lines(bible):
            facts.append(f"时间线锁：{tl}")

        return facts

    def _build_from_bible(self, bible, current_chapter: int) -> str:
        """从 Bible 实体构建 FACT_LOCK"""
        lines = ["【绝对事实边界（一旦违背即为废稿）】\n"]

        # ── 1. 角色白名单（与审计侧同一份：``whitelist_lines``）──
        characters = bible.characters
        names = character_names(bible)
        if names:
            lines.extend(whitelist_lines(names))
            lines.append("")

        # ── 1b. 地点白名单（与审计侧同一份：``location_whitelist_lines``）──
        # ⚠ 第 31 批：与「角色白名单」同构的**「只可使用」硬约束**
        #   ⇒ 模型不会自造有名字的地点 ⇒ 审计侧才判得了「未登记地点」。
        # ⚠ 第 40 批：输入改成 Bible 的 **locations**（不是名字列表）
        #   ⇒ 白名单里除了名字，还有「类型 + 一句话描述」
        #   ⇒ 而两侧共用**同一个函数 + 同一份输入** ⇒ 不会漂。
        locations = getattr(bible, "locations", None) or []
        loc_wl_lines = location_whitelist_lines(locations)
        if loc_wl_lines:
            lines.extend(loc_wl_lines)
            lines.append("")

        # ── 2. 已死亡角色（从描述/关系中推断，或标记为 dead 的）──
        # ⚠ 第 24 批调序：死亡锁**极硬且极短**（每个死者一行）⇒ 提到最前面，
        #   「不可复活」这种约束一旦被截掉就是**灾难级**违规，而它几乎不占预算。
        dead_chars = self._extract_dead_characters(characters)
        if dead_chars:
            lines.append(FACT_LOCK_HEADER_DEAD)
            for dc in dead_chars:
                lines.append(f"   禁止: {dc['name']}({dc.get('role', '未知')}) - {dc.get('cause', '原因不详')}（死于{dc.get('when', '未知时间')}）")
            lines.append("")

        # ── 3. 身份锁死（已揭露的秘密 / POV 防火墙 —— **硬约束**）──
        # ⚠ 第 24 批调序（**实测驱动**）：此前这一节排在「角色设定」**后面**
        #   ⇒ 正文侧那把刀（2000 字上限）正好切在**这一节中间**
        #   ⇒ 8 个角色只有 4 个的 POV 防火墙活下来 ⇒ 后 4 个角色的硬约束
        #   **没送到模型** ⇒ 模型自由发挥（生产实测抓到的「能力越界 / 性别矛盾」
        #   根因就在这里）⇒ 调序后实测 **8/8** 全部送达。
        identity_lines = self._build_identity_lines(characters, current_chapter)
        if identity_lines:
            lines.append(FACT_LOCK_HEADER_IDENTITY)
            for il in identity_lines:
                lines.append(f"   {il}")
            lines.append("")

        # ── 4. 角色设定（谁是谁、干什么的）—— N-0 的关键一节 ──
        # ⚠ 此前事实锁**只有名字**（白名单），没有任何一节装角色的职能/身份
        # ⇒ 正文提示词里模型只知道「有这个人」，不知道「他是干什么的」
        # ⇒ 只能靠名字+梗概+大纲去猜 ⇒ 人设必然漂移
        # （真实跑一本真书实测：7 个角色里 4 个被重定义，
        #   连 Bible 里的「白塔旧案主谋」都被写成了报社好领导）。
        # ⚠ 第 24 批调序：这一节**篇幅最大**（每个角色两段各 60 字）
        #   ⇒ 放在**硬约束之后** ⇒ 被切时硬约束先活下来（且这一节被切会**如实点名**）。
        setting_lines = self._build_character_setting_lines(characters)
        if setting_lines:
            lines.append(FACT_LOCK_HEADER_CHARACTER_SETTINGS)
            for sl in setting_lines:
                lines.append(f"   {sl}")
            lines.append("")

        # ── 5. 核心关系图谱 ──
        relations = self._build_relation_lines(characters)
        if relations:
            lines.append(FACT_LOCK_HEADER_RELATIONS)
            for rel in relations:
                lines.append(f"   {rel}")
            lines.append("")

        # ── 6. 时间线锁定 ──
        timeline_lines = self._build_timeline_lines(bible)
        if timeline_lines:
            lines.append(FACT_LOCK_HEADER_TIMELINE)
            for tl in timeline_lines:
                lines.append(f"   {tl}")

        return "\n".join(lines)

    def _build_character_setting_lines(self, characters: list) -> List[str]:
        """角色的**公开设定**（谁是谁、干什么的）—— 事实锁「角色设定」节。

        数据来源：``description``（AI 路径下是「定位 - 描述」，**定位在最前**）
        + ``public_profile``（公开面）。

        ⚠ 为什么要这一节（真实跑一本真书才暴露出来的问题）：
        事实锁原先只有「白名单（名字）/ 死亡锁 / 关系锁 / 身份锁 / 时间线锁」，
        **没有任何一节装角色的职能** ⇒ 正文提示词里只有角色名，
        模型无从知道「贺鸣珂是白塔前放映师」⇒ 只能猜 ⇒ 人设漂移。

        ⚠ 预算：整块事实锁在正文提示词里只有 2000 字 ⇒ 逐角色不限长会挤掉
        「白名单 / 死亡锁」这些**更硬**的约束 ⇒ 每行限长、角色数上限，
        且**超出的如实点名**（不静默丢）。
        """
        lines: List[str] = []
        omitted: List[str] = []
        for char in characters or []:
            name = str(getattr(char, "name", "") or "").strip()
            if not name:
                continue
            parts: List[str] = []
            description = str(getattr(char, "description", "") or "").strip()
            if description:
                parts.append(_truncate_with_mark(description, CHARACTER_SETTING_MAX_CHARS))
            public_profile = str(getattr(char, "public_profile", "") or "").strip()
            if public_profile:
                parts.append(_truncate_with_mark(public_profile, CHARACTER_SETTING_MAX_CHARS))
            if not parts:
                continue
            if len(lines) >= CHARACTER_SETTING_MAX_CHARACTERS:
                omitted.append(name)
                continue
            lines.append(f"{name} = {'；'.join(parts)}")
        if not lines:
            # 一个角色的设定都没有 ⇒ 如实说「没有」，但**不许**暗示「没有约束」。
            return ["（本节暂无角色设定可锁：角色只有名字，没有身份/职能信息）"]
        if omitted:
            lines.append(
                f"（另有 {len(omitted)} 个角色的设定未注入，受事实锁篇幅限制："
                f"{'、'.join(omitted[:8])}）"
            )
        return lines

    def _extract_dead_characters(self, characters: list) -> list[Dict]:
        """从角色列表中推断已死亡角色
        
        策略：
        - 检查 description 中是否包含死亡相关关键词
        - 检查 relationships 中是否有 "dead/died/死亡/已故" 关系
        - 后续可扩展为 Bible 的 is_dead 显式字段
        """
        dead_keywords = [
            "死亡", " died ", "身亡", "去世", "已故", "牺牲",
            " killed ", "被杀", "遇害", "丧命", "殒命",
        ]
        dead_chars = []

        for char in characters:
            # 检查描述
            desc_lower = (char.description or "").lower()
            name = char.name

            # 检查关系标签
            is_dead = False
            death_info = {"name": name, "role": char.public_profile or ""}

            for kw in dead_keywords:
                if kw in desc_lower:
                    is_dead = True
                    # 尝试提取死亡原因片段
                    death_info["cause"] = char.description[:80]
                    break

            # 检查 relationships
            if not is_dead and char.relationships:
                for rel in char.relationships:
                    rel_str = str(rel).lower()
                    for kw in dead_keywords:
                        if kw in rel_str:
                            is_dead = True
                            death_info["cause"] = str(rel)[:80]
                            break
                    if is_dead:
                        break

            if is_dead:
                dead_chars.append(death_info)

        return dead_chars

    def _build_relation_lines(self, characters: list) -> list[str]:
        """从角色的 relationships 字段构建关系行"""
        lines = []
        for char in characters:
            if not char.relationships:
                continue
            name = char.name
            for rel in char.relationships:
                if isinstance(rel, dict):
                    target = rel.get("target", rel.get("with", "?"))
                    rel_type = rel.get("type", rel.get("relation", rel.get("predicate", "—")))
                    lines.append(f"{name} ——{rel_type}→ {target}")
                elif isinstance(rel, str):
                    # 尝试解析 "A —关系→ B" 格式
                    if "—" in rel and "→" in rel:
                        lines.append(rel)
                    else:
                        lines.append(f"{name} ——{rel}→ (?)")
        return lines

    def _build_identity_lines(self, characters: list, current_chapter: int) -> list[str]:
        """构建身份锁死行。

        ⚠ **预算**（第 24 批，生产实测出来的硬事实）：
          事实锁整块在正文提示词里只有 **2000 字**
          （``prose_context_settings.DEFAULT_MAX_CHARS_PER_SLOT``）
          ⇒ 实测（生产书真实数据）：原始 4202 字 ⇒ 裁到 2000 字时
          **刀口正好落在「身份锁死」这一节中间** ⇒ 后半角色的硬约束
          （已揭露的秘密 + POV 防火墙）**根本没送到模型**
          ⇒ 模型可以自由发挥 ⇒ 第 21 批生产实测抓到的「郑铁珊被无解释赋予
          写票能力」「郑铁珊的性别矛盾」**根因就在这里**。

        ⇒ 两处压缩（把整块事实锁压进 2000 字 ⇒ 硬约束**整段**送达）：
          ① 「[已揭露] 秘密主体」**限长**（``IDENTITY_SECRET_MAX_CHARS``）。
          ② 「当前心理」限长（``IDENTITY_MENTAL_MAX_CHARS``）。
        ⚠ **公开面（``public_profile``）保留**：它虽然也在「角色设定」节里，
          但那一节每条限 60 字、且只给前 12 个角色 ⇒ 删掉它会让
          「只有公开面、没有隐藏面」的角色**整行消失**（信息丢失）⇒ 只能限长，不能删。
        ⚠ **POV 防火墙完整保留**（不受任何限长约束）——
          它是**最硬**的约束（违反即人物失格）⇒ 砍它等于砍掉整节的价值。
        """
        lines = []
        for char in characters:
            name = char.name
            identity_parts = []

            # 性别 / 年龄（第 40 批）：**极硬**的身份事实 ⇒ 放在最前（先被切就晚了）
            facts = []
            gender = (getattr(char, "gender", "") or "").strip()
            if gender:
                facts.append(_truncate_with_mark(gender, IDENTITY_GENDER_MAX_CHARS))
            age = (getattr(char, "age", "") or "").strip()
            if age:
                facts.append(_truncate_with_mark(age, IDENTITY_AGE_MAX_CHARS))
            if facts:
                identity_parts.append(" / ".join(facts))

            # 公开面（**限长但保留**）：见上面的说明 ⇒ 不能删
            if char.public_profile:
                identity_parts.append(
                    _truncate_with_mark(char.public_profile, IDENTITY_PUBLIC_MAX_CHARS)
                )

            # reveal_chapter 为 None 表示「总是可见」（见 bible_dto.CharacterDTO
            # 「reveal_chapter: 揭示章节号，None 表示总是可见」）。
            # 此前这里额外要求 `reveal_chapter is not None`，于是 None 的角色
            # 隐藏面永远不出现 —— 与 context_builder、context_budget_allocator、
            # entities.character、character_narrative_kernel 及字段文档**相反**，
            # 同一章里 AI 会拿到两套互相矛盾的角色设定。
            reveal = char.reveal_chapter
            if char.hidden_profile and (reveal is None or current_chapter >= int(reveal)):
                secret_body, pov_block = _split_hidden_profile(char.hidden_profile)
                hidden_text = _truncate_with_mark(secret_body, IDENTITY_SECRET_MAX_CHARS)
                if pov_block:
                    # ⚠ POV 防火墙**完整保留**（限长只作用于秘密主体）
                    hidden_text = f"{hidden_text}{pov_block}"
                identity_parts.append(f"[已揭露] {hidden_text}")

            # 心理状态
            if char.mental_state and char.mental_state != "NORMAL":
                identity_parts.append(
                    "当前心理: "
                    + _truncate_with_mark(char.mental_state, IDENTITY_MENTAL_MAX_CHARS)
                )

            if identity_parts:
                lines.append(f"{name} = {' | '.join(identity_parts)}")

        return lines

    def _build_timeline_lines(self, bible) -> list[str]:
        """构建「核心事件时间线」行（**只送最近的** + **按预算收口** + **如实留痕**）。

        ⚠ 第 42 批（生产实测，长书 529 章那本）发现三个叠在一起的缺陷：

        1. **字段残缺**：此前每行只有 ``event``（中位 **13 字**，「陆深抉择补全」那种）
           ⇒ 而 ``description``（真正有信息量的一行，生产书 **100% 有值**）
           与 ``time_point``（故事内时间点，**100% 有值**）**从来没进过模型**；
        2. **砍错位置**：时间线是事实锁的**最后一节** ⇒ 预算紧时**最先被砍**
           ⇒ 而最近的事件在**列表尾部** ⇒ **最该保留的先被砍掉**（最坏的砍法）；
        3. **砍了不说**：2074 条里只给模型看一部分，却**没有任何提示**说还有更多。

        ⇒ 改法（与蓝图侧 ``format_bible_context`` **同一口径**）：
          ① 送 ``event + time_point + description``（三个字段都限长）；
          ② **只取最近的一段**（列表**尾部** = 故事顺序的末尾）⇒ 按**字符预算**收口；
          ③ 丢了 N 条 ⇒ **如实说**（不装作那就是全部）。

        ⚠ 「尾部 = 最近」这个前提的证据与边界（生产实测）：
          * ``bible_timeline_notes.sort_order`` 实测**全为 0**（两本书都是）
            ⇒ 读取的 ``ORDER BY sort_order, id`` 实际是**按 id 排**；
          * 而 id **字典序单调递增** ⇒ 列表顺序 = 写入顺序；
          * 内容内在次序也对得上（「封面进度条**第二**次逆向跳跃」在中段、
            「**第三**次」在末尾）；
          * ⚠ 但 ``created_at`` **不单调** ⇒ 这些条目是**批量回填**的，不是逐章追加
            ⇒ ⇒ 「尾部 ≈ 最近」是**推断**，不是有章号字段可证的事实
            ⇒ ⇒ 正因如此，留痕必须**如实**（模型得知道还有更早的没给它）。
        """
        notes = list(getattr(bible, "timeline_notes", None) or [])
        candidates = []
        for note in notes:
            event = str(getattr(note, "event", "") or "").strip()
            if not event:
                continue
            candidates.append((note, event))
        if not candidates:
            return []

        picked: list[str] = []
        used = 0
        # ⚠ 从**尾部**（最近）往前取 ⇒ 但渲染时**恢复故事顺序**
        for note, event in reversed(candidates):
            if len(picked) >= TIMELINE_MAX_ENTRIES:
                break
            head = _truncate_with_mark(event, TIMELINE_EVENT_MAX_CHARS)
            time_point = str(getattr(note, "time_point", "") or "").strip()
            if time_point:
                head = (
                    f"{head}（"
                    f"{_truncate_with_mark(time_point, TIMELINE_TIME_POINT_MAX_CHARS)}）"
                )
            desc = str(getattr(note, "description", "") or "").strip()
            entry = (
                f"{head}：{_truncate_with_mark(desc, TIMELINE_DESC_MAX_CHARS)}"
                if desc
                else head
            )
            cost = len(entry) + 1
            # ⚠ 第一条**总是**收（宁可一条，也不要空节）
            if picked and used + cost > TIMELINE_MAX_CHARS:
                break
            picked.append(entry)
            used += cost

        if not picked:
            return []
        picked.reverse()  # 恢复故事顺序（模型读的是历史，不该倒着读）
        dropped = len(candidates) - len(picked)
        if dropped > 0:
            # ⚠ 与蓝图侧 ``_dropped_note`` **同一措辞口径**：砍了就要说
            picked.append(
                f"（另有 {dropped} 条更早的时间线事件未注入："
                "受事实锁篇幅限制，非本作品没有）"
            )
        return picked


# ============================================================
# MemoryEngine: 主入口（Facade）
# ============================================================

@dataclass
class MemoryState:
    """持久化的记忆状态快照"""
    novel_id: str = ""
    last_updated_chapter: int = 0
    completed_beats: List[Dict[str, Any]] = field(default_factory=list)
    revealed_clues: List[Dict[str, Any]] = field(default_factory=list)
    fact_violations_history: List[Dict[str, Any]] = field(default_factory=list)


class MemoryEngine:
    """V6 记忆引擎主入口
    
    职责：
    1. 构建 FACT_LOCK（通过 FactLockBuilder 从 Bible 动态生成）
    2. 管理 COMPLETED_BEATS（LLM 提取 + 去重累积）
    3. 管理 REVEALED_CLUES（LLM 提取 + 有效性追踪）
    4. 提供 T0 注入文本接口（供 ContextBudgetAllocator 使用）
    5. 章后异步回写（调用 LLM 提取增量 + 持久化）
    
    使用方式：
        engine = MemoryEngine(llm_service, bible_repository, db_connection)
        
        # 生成前：获取 T0 注入文本
        fact_lock = engine.build_fact_lock_section(novel_id, chapter_number)
        beats = engine.get_completed_beats_section()
        clues = engine.get_revealed_clues_section()
        
        # 生成后：更新状态
        await engine.update_from_chapter(novel_id, ch_num, content, outline)
    """

    def __init__(
        self,
        llm_service: LLMService,
        bible_repository: BibleRepository,
        db_connection=None,
        runtime_settings: MemoryEngineRuntimeSettings | None = None,
    ):
        self.llm_service = llm_service
        self.fact_lock_builder = FactLockBuilder(bible_repository)
        self.bible_repository = bible_repository
        self.db_connection = db_connection
        self._runtime_settings = runtime_settings or get_memory_engine_runtime_settings()

        # 运行时内存缓存（优先读缓存，miss 再查 DB）。容量与 TTL 由 performance.yaml 管理。
        self._cache: "OrderedDict[str, MemoryState]" = OrderedDict()
        self._cache_loaded_at: Dict[str, float] = {}

        if self.db_connection is not None:
            # 与正式迁移一致；避免首次 _load_from_db 在表未建好时刷 WARNING
            self._ensure_table_exists()

    # ============================================================
    # T0 注入接口（生成前调用）
    # ============================================================

    def build_fact_lock_section(self, novel_id: str, chapter_number: int) -> str:
        """构建 T0-α: FACT_LOCK 不可篡改事实块"""
        return self.fact_lock_builder.build(novel_id, chapter_number)

    def get_completed_beats_section(self, novel_id: str) -> str:
        """构建 T0-β: 已完成节拍锁"""
        state = self._get_or_load_state(novel_id)
        if not state.completed_beats:
            return ""

        lines = ["【已完成节拍（以下事件已经发生过了，禁止在本章重复写一遍）】\n"]
        for beat in state.completed_beats:
            ch = beat.get("chapter", "?")
            summary = beat.get("summary", "")
            beat_id = beat.get("beat_id", "")
            lines.append(f"   [第{ch}章] {summary}")
        lines.append(
            "\n如果你需要'回顾'这些事件，用角色的回忆/一句话带过，不要重新展开写。"
        )
        return "\n".join(lines)

    def get_revealed_clues_section(self, novel_id: str) -> str:
        """构建 T0-γ: 已揭露线索清单"""
        state = self._get_or_load_state(novel_id)
        if not state.revealed_clues:
            return ""

        # 只显示 still_valid 的线索
        valid_clues = [c for c in state.revealed_clues if c.get("is_still_valid", True)]
        if not valid_clues:
            return ""

        lines = ["【截至目前已知的线索（读者和主角已经知道的信息）】\n"]
        for clue in valid_clues:
            ch = clue.get("revealed_at_chapter", "?")
            content = clue.get("content", "")
            category = clue.get("category", "")
            category_label = {
                "truth": "真相", "relationship": "关系",
                "identity": "身份", "ability": "能力", "other": "信息"
            }.get(category, "信息")
            lines.append(f"   [{category_label}] [第{ch}章] {content}")
        lines.append(
            "\n以上信息已经是'已知'的，不要再把它们当作'新发现'来写。你可以在此基础上推进，但不能推翻。"
        )
        return "\n".join(lines)

    # ============================================================
    # 章后状态回写（生成后调用，LLM 驱动）
    # ============================================================

    async def update_from_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        outline: str,
    ) -> Dict[str, Any]:
        """章后状态回写：调用 LLM 提取增量 + 去重合并 + 持久化
        
        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            content: 章节正文
            outline: 章节大纲
            
        Returns:
            delta dict: {
                "new_beats": int,
                "new_clues": int,
                "violations": int,
                "errors": list[str],
            }
        """
        result = {"new_beats": 0, "new_clues": 0, "violations": 0, "errors": []}

        try:
            # 1. 准备上下文
            state = self._get_or_load_state(novel_id)
            fact_lock = self.build_fact_lock_section(novel_id, chapter_number)
            existing_beats = self._summarize_beats_for_prompt(state.completed_beats)
            existing_clues = self._summarize_clues_for_prompt(state.revealed_clues)

            # 2. 通过 PromptGateway 渲染契约提示词，变量缺失时在请求 LLM 前失败
            try:
                rendered_prompt = get_prompt_gateway().render(
                    MEMORY_EXTRACTION_CONTRACT,
                    {
                        "chapter_content": content,
                        "chapter_number": chapter_number,
                        "outline": outline,
                        "fact_lock_text": fact_lock,
                        "existing_beats_summary": existing_beats,
                        "existing_clues_summary": existing_clues,
                    },
                )
            except PromptGatewayError as exc:
                msg = f"MemoryEngine 提示词渲染失败: {exc}"
                result["errors"].append(msg)
                logger.warning(msg)
                return result

            prompt = rendered_prompt.prompt
            config = generation_config_from_profile("memory_extraction")

            # 3. 调用 LLM
            llm_result = await self.llm_service.generate(prompt, config)
            raw_response = llm_result.content

            # 4. 解析响应
            data, parse_errors = parse_llm_json_to_dict(raw_response)
            if data is None:
                result["errors"].extend(parse_errors)
                logger.warning(
                    f"MemoryEngine LLM JSON 解析失败: {parse_errors}, "
                    f"raw response (first 200): {raw_response[:200]}"
                )
                return result

            try:
                payload = MemoryDeltaPayload.model_validate(data)
            except ValidationError as e:
                err_msg = "; ".join(
                    f"{'/'.join(str(x) for x in err.get('loc', ''))}: {err.get('msg', '')}"
                    for err in e.errors()[:10]
                )
                result["errors"].append(f"Contract 校验失败: {err_msg}")
                logger.warning(f"MemoryEngine contract validation failed: {err_msg}")
                return result

            # 5. 合并增量到状态
            new_beats = self._merge_beats(state, payload.completed_beats, chapter_number)
            new_clues = self._merge_clues(state, payload.revealed_clues, chapter_number)

            result["new_beats"] = new_beats
            result["new_clues"] = new_clues
            result["violations"] = len(payload.fact_violations)

            # 记录违反历史
            if payload.fact_violations:
                for v in payload.fact_violations:
                    state.fact_violations_history.append({
                        "chapter": chapter_number,
                        **v.model_dump(),
                    })
                logger.warning(
                    f"检测到 {len(payload.fact_violations)} 个事实违反 @ ch{chapter_number}"
                )

            # 6. 更新状态并持久化
            state.last_updated_chapter = chapter_number
            self._remember_state(novel_id, state)
            # ⚠ 第 67 批（B-4）：落库失败必须**进 errors** ——
            #   上游 `_run_memory_update` 只看 `result["errors"]`
            #   ⇒ 这里不报 ⇒ 上游判「已更新」⇒ 面板一条「未更新」都不写
            #   ⇒ 记忆一条没落库，系统却报「一切正常」。
            persist_error = self._persist_state(novel_id, state)
            if persist_error:
                result["errors"].append(persist_error)
                logger.error(persist_error)
                return result

            logger.info(
                f"MemoryEngine 更新完成 @ ch{chapter_number}: "
                f"+{new_beats} beats, +{new_clues} clues, "
                f"{result['violations']} violations"
            )

        except Exception as e:
            result["errors"].append(str(e))
            logger.error(f"MemoryEngine.update_from_chapter 失败: {e}", exc_info=True)

        return result

    # ============================================================
    # 内部方法：状态管理
    # ============================================================

    def _get_or_load_state(self, novel_id: str) -> MemoryState:
        """获取内存状态（缓存优先）"""
        cached = self._get_cached_state(novel_id)
        if cached is not None:
            return cached

        # 从 DB 加载
        state = self._load_from_db(novel_id)
        self._remember_state(novel_id, state)
        return state

    def _cache_enabled(self) -> bool:
        return (
            self._runtime_settings.state_cache_ttl_seconds > 0
            and self._runtime_settings.state_cache_max_size > 0
        )

    def _get_cached_state(self, novel_id: str) -> MemoryState | None:
        if not self._cache_enabled():
            self._forget_cached_state(novel_id)
            return None

        state = self._cache.get(novel_id)
        if state is None:
            return None

        loaded_at = self._cache_loaded_at.get(novel_id, 0.0)
        if time.monotonic() - loaded_at >= self._runtime_settings.state_cache_ttl_seconds:
            self._forget_cached_state(novel_id)
            return None

        self._cache.move_to_end(novel_id)
        return state

    def _remember_state(self, novel_id: str, state: MemoryState) -> None:
        if not self._cache_enabled():
            self._forget_cached_state(novel_id)
            return

        self._cache[novel_id] = state
        self._cache.move_to_end(novel_id)
        self._cache_loaded_at[novel_id] = time.monotonic()

        max_size = self._runtime_settings.state_cache_max_size
        while len(self._cache) > max_size:
            evicted_novel_id, _ = self._cache.popitem(last=False)
            self._cache_loaded_at.pop(evicted_novel_id, None)

    def _forget_cached_state(self, novel_id: str) -> None:
        self._cache.pop(novel_id, None)
        self._cache_loaded_at.pop(novel_id, None)

    def _load_from_db(self, novel_id: str) -> MemoryState:
        """从数据库加载持久化状态"""
        if not self.db_connection:
            return MemoryState(novel_id=novel_id)

        try:
            row = self.db_connection.execute(
                """
                SELECT state_json, last_updated_chapter 
                FROM memory_engine_state 
                WHERE novel_id = ? 
                ORDER BY last_updated_chapter DESC 
                LIMIT 1
                """,
                (novel_id,),
            ).fetchone()

            if row and row[0]:
                data = json.loads(row[0])
                state = MemoryState(
                    novel_id=novel_id,
                    last_updated_chapter=row[1] or 0,
                    completed_beats=data.get("completed_beats", []),
                    revealed_clues=data.get("revealed_clues", []),
                    fact_violations_history=data.get("fact_violations_history", []),
                )
                logger.debug(f"MemoryEngine 从 DB 加载状态: novel={novel_id}, ch={state.last_updated_chapter}")
                return state
        except Exception as e:
            logger.warning(f"MemoryEngine DB 加载失败: {e}")

        return MemoryState(novel_id=novel_id)

    def _memory_state_to_json(self, state: MemoryState) -> str:
        return json.dumps(
            {
                "completed_beats": state.completed_beats,
                "revealed_clues": state.revealed_clues,
                "fact_violations_history": state.fact_violations_history,
            },
            ensure_ascii=False,
        )

    def _upsert_state_row(self, novel_id: str, state: MemoryState) -> None:
        """将当前 MemoryState UPSERT 到 memory_engine_state（使用 DatabaseConnection / sqlite3 均支持的 execute API）"""
        state_json = self._memory_state_to_json(state)
        self.db_connection.execute(
            """
            INSERT INTO memory_engine_state (novel_id, state_json, last_updated_chapter, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(novel_id) DO UPDATE SET
                state_json = excluded.state_json,
                last_updated_chapter = excluded.last_updated_chapter,
                updated_at = datetime('now')
            """,
            (novel_id, state_json, state.last_updated_chapter),
        )
        self.db_connection.commit()

    def _persist_state(self, novel_id: str, state: MemoryState) -> str | None:
        """持久化状态到数据库。

        ⚠ 第 67 批（B-4）：**返回错误信息**（成功时 ``None``）——
          以前这里 catch 掉**全部**异常只 log、**不上报** ⇒ 落库失败被当成成功。
          ⇒ 现在失败**必须**让调用方看见（调用方把它放进 ``result["errors"]``）。

        返回值
        ------
        ``None``：已落库（并已回读确认）；``str``：失败原因（人话，可直接上报）。
        """
        if not self.db_connection:
            logger.debug("MemoryEngine 无 DB 连接，跳过持久化（仅内存模式）")
            return None

        try:
            self._upsert_state_row(novel_id, state)
            logger.debug(f"MemoryEngine 状态已持久化: novel={novel_id}, ch={state.last_updated_chapter}")

        except Exception as e:
            # 表可能还不存在，尝试自动建表
            if "no such table" in str(e).lower() or "table" in str(e).lower():
                self._ensure_table_exists()
                # 重试一次
                try:
                    self._upsert_state_row(novel_id, state)
                except Exception as retry_err:
                    logger.error(f"MemoryEngine 持久化重试失败: {retry_err}")
                    return f"MemoryEngine 状态持久化失败（建表重试后仍失败）: {retry_err}"
            else:
                logger.error(f"MemoryEngine 持久化失败: {e}")
                return f"MemoryEngine 状态持久化失败: {e}"

        # ⚠ 第 67 批（B-4）：队列模式下「execute 没抛异常」**什么都没证明** ——
        #   生产走写队列 ⇒ `execute()` 只**入队**就返回 ⇒ 真正的 INSERT 在
        #   writer 线程执行 ⇒ writer 线程里的失败**结构上到不了**这里。
        # ⇒ 必须**回读确认**（见 `_verify_persisted`）。
        return self._verify_persisted(novel_id, state)

    def _verify_persisted(self, novel_id: str, state: MemoryState) -> str | None:
        """确认状态**真的**落库了（返回错误信息；确认通过时 ``None``）。

        ⚠ 为什么必须确认（第 67 批 · B-4）：生产走**写队列** ⇒
          ``execute()`` 对 mutating SQL 只**入队**就返回 ⇒ 真正的 INSERT 在
          writer 线程执行 ⇒ writer 线程里的失败**结构上到不了**调用方
          ⇒ 「execute 没抛异常」**不等于**「落库成功」。

        做法：先等写队列排空（队列未启用时是 no-op），再**回读**该行核对章节号。
        """
        flush = getattr(self.db_connection, "flush_pending_writes", None)
        if callable(flush):
            try:
                if not flush(timeout=2.0):
                    return "MemoryEngine 状态持久化未确认（写队列未在超时内排空）"
            except Exception as exc:
                return f"MemoryEngine 状态持久化确认失败（刷新写队列出错）: {exc}"

        try:
            cursor = self.db_connection.execute(
                "SELECT last_updated_chapter FROM memory_engine_state WHERE novel_id = ?",
                (novel_id,),
            )
            row = cursor.fetchone()
        except Exception as exc:
            return f"MemoryEngine 状态持久化未确认（回读失败）: {exc}"

        if row is None:
            return "MemoryEngine 状态持久化未确认（回读不到该行）"

        stored = self._read_chapter_number(row)
        expected = int(state.last_updated_chapter)
        if stored != expected:
            return (
                "MemoryEngine 状态持久化未确认"
                f"（回读章节号={stored} ≠ 预期={expected}）"
            )
        return None

    @staticmethod
    def _read_chapter_number(row: Any) -> int | None:
        """从回读行里取章节号（兼容 tuple 行与 ``sqlite3.Row`` 行）。"""
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError, KeyError):
            pass
        try:
            return int(row["last_updated_chapter"])
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    def _ensure_table_exists(self) -> None:
        """自动创建 memory_engine_state 表"""
        if not self.db_connection:
            return
        try:
            self.db_connection.execute("""
                CREATE TABLE IF NOT EXISTS memory_engine_state (
                    novel_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    last_updated_chapter INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            self.db_connection.commit()
            logger.info("memory_engine_state 表已自动创建")
        except Exception as e:
            logger.error(f"创建 memory_engine_state 表失败: {e}")

    def _merge_beats(
        self, state: MemoryState, new_beats: List[CompletedBeatItem], chapter: int
    ) -> int:
        """合并新的已完成节拍（去重）"""
        count = 0
        existing_ids = {b.get("beat_id") for b in state.completed_beats}

        for beat in new_beats:
            beat_data = beat.model_dump()
            # 确保有 chapter
            if not beat_data.get("chapter"):
                beat_data["chapter"] = chapter

            beat_id = beat_data.get("beat_id", "")
            if beat_id and beat_id in existing_ids:
                continue  # 去重

            # 也按 summary 做模糊去重
            summary = beat_data.get("summary", "")
            if any(
                existing.get("summary") == summary
                for existing in state.completed_beats
            ):
                continue

            state.completed_beats.append(beat_data)
            count += 1

        return count

    def _merge_clues(
        self, state: MemoryState, new_clues: List[RevealedClueItem], chapter: int
    ) -> int:
        """合并新的已揭露线索（去重 + 证伪标记）"""
        count = 0
        existing_ids = {c.get("clue_id") for c in state.revealed_clues}

        for clue in new_clues:
            clue_data = clue.model_dump()
            if not clue_data.get("revealed_at_chapter"):
                clue_data["revealed_at_chapter"] = chapter

            clue_id = clue_data.get("clue_id", "")
            if clue_id and clue_id in existing_ids:
                continue

            # 按内容模糊去重
            content = clue_data.get("content", "")
            if any(
                existing.get("content") == content
                for existing in state.revealed_clues
            ):
                continue

            state.revealed_clues.append(clue_data)
            count += 1

        return count

    @staticmethod
    def _summarize_beats_for_prompt(beats: List[Dict]) -> str:
        """将已有节拍压缩为 prompt 摘要（避免 token 爆炸）"""
        if not beats:
            return ""
        lines = []
        # 只取最近 15 条
        for b in beats[-15:]:
            ch = b.get("chapter", "?")
            s = b.get("summary", "")[:80]
            lines.append(f"- [Ch{ch}] {s}")
        return "\n".join(lines)

    @staticmethod
    def _summarize_clues_for_prompt(clues: List[Dict]) -> str:
        """将已有线索压缩为 prompt 摘要"""
        if not clues:
            return ""
        valid = [c for c in clues if c.get("is_still_valid", True)]
        if not valid:
            return ""
        lines = []
        for c in valid[-20:]:  # 最近 20 条
            ch = c.get("revealed_at_chapter", "?")
            content = c.get("content", "")[:80]
            lines.append(f"- [Ch{ch}] {content}")
        return "\n".join(lines)

    # ============================================================
    # 管理接口
    # ============================================================

    def reset(self, novel_id: str) -> None:
        """重置某小说的记忆状态（谨慎使用）"""
        self._forget_cached_state(novel_id)

        if self.db_connection:
            try:
                self.db_connection.execute(
                    "DELETE FROM memory_engine_state WHERE novel_id = ?",
                    (novel_id,),
                )
                self.db_connection.commit()
                logger.info(f"MemoryEngine 状态已重置: novel={novel_id}")
            except Exception as e:
                logger.warning(f"MemoryEngine 重试失败: {e}")

    def get_state_summary(self, novel_id: str) -> Dict[str, Any]:
        """获取状态摘要（用于调试/监控）"""
        state = self._get_or_load_state(novel_id)
        return {
            "novel_id": novel_id,
            "last_updated_chapter": state.last_updated_chapter,
            "total_beats": len(state.completed_beats),
            "total_clues": len(state.revealed_clues),
            "valid_clues": sum(1 for c in state.revealed_clues if c.get("is_still_valid", True)),
            "total_violations": len(state.fact_violations_history),
        }
