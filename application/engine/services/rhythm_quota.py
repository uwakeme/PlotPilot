"""H-06 / E-06：题材节奏配额的**口径**、三态与代码表回落。

改造前
------
「本章该多紧、该怎么写」是**代码代做**的（``auto_novel_generation_workflow``
的 ``_get_plot_tension`` 第 3 段）：

- **黄金三章配额表**：第 1 章 ≥65「首章铁律」/ 第 2 章 ≥55 / 第 3 章 ≥60
  —— 一整套写死的创作指令 + 目标数字；
- **按前章张力的四档**：≤30 紧急升温 / ≤45 逐步升温 / ≥80 缓冲 / ≥65 维持
  —— 档位与目标张力区间也全是常量。

⇒ 题材不同（玄幻 vs 都市 vs 悬疑），该多紧完全不是同一套；
而「谁该来定」这件事，本该看题材与当下状态。

现在由 **AI 定**（``planning-rhythm-quota`` 节点）：它看到题材、本章位置、
预设期望张力与前章实际张力，给出**目标张力 + 节奏档 + 一句具体指令**。

降级纪律
--------
AI 不可用 / 输出不合契约 ⇒ 回落**代码表**（改造前的行为，**逐位一致**），
来源可分辨（``rule``）；代码表自己也没结论（前章张力未评估且不在前三章）
⇒ **无配额**（``source=""``），**不编**。

⚠ **这不等于「送进模型」**：配额文本要不要真的进生产正文提示词，
由 ``config/performance.yaml`` 的 ``rhythm_quota.enabled`` 控制
（**默认关闭** ⇒ 行为与改造前逐位一致）—— 突然激活一个从未生效的
硬约束会一次性改变全书节奏观 ⇒ 必须开关 + 灰度。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from application.core.config.config_loader import get_config
from application.core.config.runtime_settings_utils import (
    non_negative_int,
    section_value,
)

from pydantic import BaseModel, Field

#: 来源标记
SOURCE_AI = "ai"
SOURCE_RULE = "rule"
SOURCE_NONE = ""

#: 节奏档（封闭枚举 —— AI 只能在这四个里选）
RHYTHM_BURST = "爆发"
RHYTHM_RISE = "升温"
RHYTHM_HOLD = "维持"
RHYTHM_COOL = "缓冲"
RHYTHMS: Tuple[str, ...] = (RHYTHM_BURST, RHYTHM_RISE, RHYTHM_HOLD, RHYTHM_COOL)

#: 指令文本长度上限
MAX_INSTRUCTION_CHARS = 600
#: 送进正文提示词的配额文本上限（字符）
DEFAULT_MAX_DELIVERY_CHARS = 600


@dataclass(frozen=True)
class RhythmQuota:
    """一章的节奏配额。"""

    #: 目标张力（0-100）；``None`` = 没有目标
    target_tension: Optional[int] = None
    #: 节奏档（爆发 / 升温 / 维持 / 缓冲）；空 = 没有档
    rhythm: str = ""
    #: 给模型的具体指令（**逐位沿用**代码表的措辞 ⇒ 回落时行为不变）
    instruction: str = ""
    #: 来源（ai / rule / ""=未评估）
    source: str = SOURCE_NONE

    @property
    def is_usable(self) -> bool:
        """是否有可用的配额（有任何一项结论）。"""
        return bool(self.instruction or self.target_tension is not None)


#: 黄金三章配额表（**逐位复刻**改造前的文案与目标数字）
EARLY_CHAPTER_QUOTAS: Dict[int, Tuple[int, str, str]] = {
    1: (
        65,
        RHYTHM_BURST,
        "🔥【首章铁律】本章是读者决定是否继续阅读的关键！"
        "综合张力目标：≥65。"
        "必须在第一个节拍就制造强烈冲突/悬念/反转——"
        "绝对禁止大段背景介绍、设定灌输、平淡日常！"
        "主角必须在被压制/轻视后展露底牌，给读者第一次爽感冲击！",
    ),
    2: (
        55,
        RHYTHM_RISE,
        "⚡【次章加速】读者已被首章吸引，但不能松劲！"
        "综合张力目标：≥55。"
        "本章必须有至少一次实力验证/身份暗示/新冲突爆发。"
        "继续升温，扩大主角展现出的特殊之处引发的反响。",
    ),
    3: (
        60,
        RHYTHM_BURST,
        "⚡【三章定乾坤】前三章决定了读者是否追读！"
        "综合张力目标：≥60。"
        "本章必须有一次真正的高潮场景——"
        "第一次正式对抗/博弈/危机中的大逆转！"
        "读者在这里应该获得明确的'这书好看'的正反馈！",
    ),
}


def code_fallback_quota(
    chapter_number: int,
    prev_tension: Optional[float],
) -> RhythmQuota:
    """代码表回落（**逐位复刻**改造前的逻辑与文案）。

    - 前三章 ⇒ 黄金三章配额表；
    - 否则按前章实际张力分四档（≤30 / ≤45 / ≥80 / ≥65）；
    - 前章张力未评估且不在前三章 ⇒ **无配额**（``source=""``）⇒ 不编。
    """
    if chapter_number <= 3:
        entry = EARLY_CHAPTER_QUOTAS.get(chapter_number)
        if entry is None:
            return RhythmQuota(source=SOURCE_NONE)
        target, rhythm, instruction = entry
        return RhythmQuota(
            target_tension=target,
            rhythm=rhythm,
            instruction=instruction,
            source=SOURCE_RULE,
        )

    if prev_tension is None:
        return RhythmQuota(source=SOURCE_NONE)

    if prev_tension <= 30:
        return RhythmQuota(
            target_tension=55,
            rhythm=RHYTHM_RISE,
            instruction=(
                "⚠ 紧急指令：前章张力严重不足！本章必须制造至少一次核心冲突/反转/悬念，"
                "将综合张力拉升到 55 以上。建议：引入新威胁、暴露隐藏信息、"
                "让角色做出痛苦选择。"
            ),
            source=SOURCE_RULE,
        )
    if prev_tension <= 45:
        return RhythmQuota(
            target_tension=50,
            rhythm=RHYTHM_RISE,
            instruction=(
                "⚡ 调整指令：前章张力偏低，读者可能正在流失。"
                "本章应逐步升温——增加信息不对称、加深角色矛盾、"
                "让阻碍变得更加紧迫。目标张力：50-65。"
            ),
            source=SOURCE_RULE,
        )
    if prev_tension >= 80:
        return RhythmQuota(
            target_tension=40,
            rhythm=RHYTHM_COOL,
            instruction=(
                "📊 缓冲指令：前章已是高潮，本章应给读者喘息空间。"
                "可以写角色消化冲击、盟友互动、新线索浮现，"
                "但结尾要留一个钩子暗示更大的风暴即将到来。目标张力：40-55。"
            ),
            source=SOURCE_RULE,
        )
    if prev_tension >= 65:
        return RhythmQuota(
            target_tension=None,
            rhythm=RHYTHM_HOLD,
            instruction=(
                "📊 维持指令：前章张力较高，本章可以保持高压推进，"
                "也可以适当喘息后再次攀升。避免连续高压导致读者疲劳。"
            ),
            source=SOURCE_RULE,
        )
    # 45 < prev < 65 ⇒ 代码表没有结论 ⇒ 不编
    return RhythmQuota(source=SOURCE_NONE)


def rhythm_quota_delivery_enabled() -> bool:
    """配额文本是否真的送进**生产正文提示词**。

    默认**关闭** ⇒ 行为与改造前逐位一致（那套配额从来没到过模型）。
    突然激活一个从未生效的硬约束会一次性改变全书节奏观 ⇒ 必须开关 + 灰度。
    """
    section = getattr(get_config(), "rhythm_quota", None)
    return bool(section_value(section, "enabled", False))


def rhythm_quota_max_delivery_chars() -> int:
    """送进正文提示词的配额文本上限（字符）。"""
    section = getattr(get_config(), "rhythm_quota", None)
    return non_negative_int(
        section_value(section, "max_chars", DEFAULT_MAX_DELIVERY_CHARS),
        DEFAULT_MAX_DELIVERY_CHARS,
    )


class RhythmQuotaLlmPayload(BaseModel):
    """planning-rhythm-quota 节点的 LLM 输出。"""

    target_tension: int = Field(
        default=0, ge=0, le=100, description="本章目标张力（0-100）"
    )
    rhythm: str = Field(default="", description="爆发 | 升温 | 维持 | 缓冲")
    instruction: str = Field(default="", description="给本章的具体指令（不得为空）")
    reason: str = Field(default="", description="判断依据（可空）")


def normalize_quota(data: Optional[Dict[str, Any]]) -> RhythmQuota:
    """把 AI 的输出收成可用的配额。

    - 形状不对 ⇒ 未评估（``source=""``）；
    - ``target_tension`` 不是 0-100 的整数 ⇒ 未评估；
    - ``rhythm`` 不在封闭枚举里 ⇒ 未评估；
    - ``instruction`` 为空 ⇒ 未评估。
    """
    if not isinstance(data, dict):
        return RhythmQuota(source=SOURCE_NONE)

    target = data.get("target_tension")
    if isinstance(target, bool) or not isinstance(target, int):
        return RhythmQuota(source=SOURCE_NONE)
    if not 0 <= target <= 100:
        return RhythmQuota(source=SOURCE_NONE)

    rhythm = str(data.get("rhythm") or "").strip()
    if rhythm not in RHYTHMS:
        return RhythmQuota(source=SOURCE_NONE)

    instruction = str(data.get("instruction") or "").strip()
    if not instruction:
        return RhythmQuota(source=SOURCE_NONE)

    return RhythmQuota(
        target_tension=target,
        rhythm=rhythm,
        instruction=instruction[:MAX_INSTRUCTION_CHARS],
        source=SOURCE_AI,
    )
