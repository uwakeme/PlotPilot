"""J-02：文风预设清单的**唯一真相**（SSOT）。

背景
----
这 6 个市场向「文风公约」预设此前**只手写在前端**
（``frontend/src/constants/marketStylePresets.ts`` 的 ``MARKET_STYLE_PRESETS``），
而「让 AI 按语义把题材匹配到某个预设」这件事**要在后端做**
⇒ 后端拿不到清单 ⇒ 要么再抄一份（两份真相），
要么让前端把清单传给后端（⇒ AI 的判读输入由前端决定，不可信也不可控）。

⇒ 现在清单下沉到 ``shared/taxonomy/style_presets_cn_v1.yaml``：
- 后端（本模块）**直接读 YAML**；
- 前端从**同一份 YAML 生成的 bundle** 读（``frontend/scripts/sync-builtin-taxonomy.mjs``）。
⇒ 仍然是一份真相，只是换了个位置。

降级纪律
--------
清单读不到 / 条目残缺 ⇒ **跳过并告警**，不编造预设；
整份清单为空 ⇒ 调用方拿到的是「没有候选」⇒ 判不出 ⇒ 返回「未评估」。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "shared"
    / "taxonomy"
    / "style_presets_cn_v1.yaml"
)


@dataclass(frozen=True)
class StylePreset:
    """一个文风公约预设。"""

    #: 稳定标识符（前端用它做 key / 存进 Bible 的就是 ``body``）
    value: str
    #: 面板上显示的名字（如「修仙·升级打脸」）
    label: str
    #: 面板上显示的单字图标
    icon: str
    #: 会被写进 Bible 的**文风公约正文**
    body: str
    #: 题材别名（模型匹配时用）
    aliases: Tuple[str, ...] = ()
    #: 题材关键词（模型匹配时用）
    keywords: Tuple[str, ...] = ()

    def as_candidate_line(self) -> str:
        """给模型看的**候选项**一行。

        ⚠ 只给「题材特征」（别名 + 关键词），**不给完整 body** ——
        body 是要写进 Bible 的成文公约，让模型逐字读六段长文会：
        ① 撑爆上下文；② 让模型去「抄公约」而不是「判题材」。
        ⇒ 模型的活是**判题材归属**，成文交给预设本身。
        """
        parts = [f"- {self.value}（{self.label}）"]
        if self.aliases:
            parts.append("题材别名：" + "、".join(self.aliases))
        if self.keywords:
            parts.append("题材特征：" + "、".join(self.keywords))
        return "；".join(parts)


def _coerce_str_list(value: object) -> Tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


@lru_cache(maxsize=1)
def get_style_presets() -> Tuple[StylePreset, ...]:
    """读取全部文风预设（顺序 = YAML 里的顺序）。"""
    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # 清单读不到 ⇒ 没有候选（不许编）
        logger.warning("文风预设清单读取失败，按无候选处理: %s", exc)
        return ()

    items = raw.get("presets") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        logger.warning("文风预设清单格式异常（presets 不是列表），按无候选处理")
        return ()

    presets: list[StylePreset] = []
    seen: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            logger.warning("文风预设条目不是映射，跳过")
            continue
        value = str(entry.get("value") or "").strip()
        label = str(entry.get("label") or "").strip()
        body = str(entry.get("body") or "").strip()
        if not value or not label or not body:
            logger.warning("文风预设条目残缺（value/label/body 缺一），跳过: %r", value)
            continue
        if value in seen:
            logger.warning("文风预设 value 重复，跳过后一个: %s", value)
            continue
        seen.add(value)
        presets.append(
            StylePreset(
                value=value,
                label=label,
                icon=str(entry.get("icon") or "").strip(),
                body=body,
                aliases=_coerce_str_list(entry.get("aliases")),
                keywords=_coerce_str_list(entry.get("keywords")),
            )
        )
    return tuple(presets)


def get_style_preset(value: str) -> Optional[StylePreset]:
    """按 ``value`` 取预设；取不到 ⇒ ``None``（不回落成第一个）。"""
    target = str(value or "").strip()
    if not target:
        return None
    for preset in get_style_presets():
        if preset.value == target:
            return preset
    return None


def style_preset_values() -> Tuple[str, ...]:
    """全部合法的 ``value``（AI 返回值必须在里面，否则判不出来）。"""
    return tuple(preset.value for preset in get_style_presets())
