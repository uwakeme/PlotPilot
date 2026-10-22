"""V1 体量档位（T 恤尺码）：用户只选篇幅档，章数/每章字数/宏观结构由服务端推导并写入梗概黑盒。"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from application.core.chapter_target_limits import clamp_chapter_target_words

# 档位 id → 约总字数（规划目标，非公证成稿字数）
V1_LENGTH_TIERS: Dict[str, Dict[str, Any]] = {
    "short": {
        "label_zh": "短篇快穿 / 脑洞文",
        "approx_total_words": 300_000,
        "default_chapter_words": 2000,
    },
    "standard": {
        "label_zh": "标准商业连载",
        "approx_total_words": 1_000_000,
        "default_chapter_words": 2000,
    },
    "epic": {
        "label_zh": "宏大史诗巨著",
        "approx_total_words": 3_000_000,
        "default_chapter_words": 2000,
    },
}


def resolve_v1_length_params(
    length_tier: Optional[str],
    target_chapters: int,
    target_words_per_chapter: Optional[int],
) -> Tuple[int, int, Optional[str]]:
    """解析建档时的目标章数与每章字数。

    - 若提供合法 ``length_tier``：按档位推导章数（ceil(总字数/每章字)），每章字数取请求值或档位默认。
    - 否则：沿用 ``target_chapters``（默认至少 1）与 ``target_words_per_chapter``（默认 2500）。

    Returns:
        (target_chapters, target_words_per_chapter, normalized_tier_or_none)
    """
    tier = (length_tier or "").strip().lower()
    if tier in V1_LENGTH_TIERS:
        meta = V1_LENGTH_TIERS[tier]
        wpc = target_words_per_chapter if target_words_per_chapter and target_words_per_chapter > 0 else int(
            meta["default_chapter_words"]
        )
        wpc = clamp_chapter_target_words(wpc)
        total = int(meta["approx_total_words"])
        chapters = max(1, math.ceil(total / wpc))
        return chapters, wpc, tier

    tc = target_chapters if target_chapters and target_chapters > 0 else 100
    tw = target_words_per_chapter if target_words_per_chapter and target_words_per_chapter > 0 else 2500
    tw = clamp_chapter_target_words(tw)
    return tc, tw, None


def strip_generated_premise_prefixes(premise: str) -> str:
    """Remove server-generated prefixes from user premise text.

    A-07：``build_v1_structure_black_box_hint`` 此前**在这里**拼出一整段
    结构论（卷数公式 + 「每卷宜安排 2~3 个大高潮节点」+ 「卷末留强钩子」）
    并拼进用户梗概前缀。

    但它的**写路径**早已删除（``novel_service`` 现在只存作者原文）⇒
    该函数是**零生产调用方的死函数**，而那段结构论也从未真正进过模型。
    ⇒ 现在**卷内节奏建议由 AI 提案**（见
    ``application/blueprint/services/volume_pacing_proposal.py``），
    本模块只保留**读侧**的历史数据清理（存量库里仍有旧前缀）。

    ⚠ ``strip_*`` 必须留着：存量库的 ``novels.premise`` 仍可能含旧前缀。
    """
    text = str(premise or "").strip()
    if "【系统内部·叙事结构规划" in text:
        idx = text.find("\n\n")
        if idx != -1:
            text = text[idx + 2 :].strip()
    if text.startswith("【") and ("类型：" in text[:120] or "世界观基调：" in text[:120]):
        idx = text.find("\n\n")
        if idx != -1:
            text = text[idx + 2 :].strip()
    return text


def strip_v1_structure_black_box_hint(premise: str) -> str:
    """Backward-compatible alias for callers that clean generated premise text."""
    return strip_generated_premise_prefixes(premise)
