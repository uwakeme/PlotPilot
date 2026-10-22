"""E-03：声线定向改写的输出解析（正文 + 本轮诊断 + **下一轮换什么手法**）。

改造前
------
``voice-rewrite`` 节点**只输出改写后的正文**（纯文本）⇒
第 2 轮改写时，模型**不知道上一轮为什么没达标**，只能换个说法再改一遍
⇒ 最多 3 轮 = 最多 3 次**盲改**。

现在节点输出 JSON：

- ``rewritten``：改写后的正文；
- ``diagnosis``：**这一轮为什么没达标**（模型自评）；
- ``next_technique``：**下一轮换什么手法**。

两条改写链（StoryPipeline 的 ``engine/pipeline/base.py`` 与
daemon 的 ``engine/runtime/daemon_host.py``）都把上一轮这两个字段
回传给下一轮（新增提示词变量 ``previous_diagnosis`` /
``previous_next_technique``）。

降级纪律
--------
- 模型没给 JSON ⇒ **原样当正文**（改造前的行为），
  结构化字段为空 ⇒ 下一轮没有可回传的内容 ⇒ **如实为空，不编**；
- JSON 里有 ``rewritten`` 但它是空的 ⇒ 视为「没改出来」⇒ 交由调用方按
  「未产生有效变化」处理（与改造前的空正文处理一致）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

#: 单条诊断 / 手法的长度上限（防模型写长篇）
MAX_DIAGNOSIS_CHARS = 400
MAX_TECHNIQUE_CHARS = 400


@dataclass(frozen=True)
class VoiceRewriteOutcome:
    """一次定向改写的产出。"""

    rewritten: str = ""
    #: 本轮为什么没达标（模型自评；拿不到就是空）
    diagnosis: str = ""
    #: 下一轮换什么手法（拿不到就是空）
    next_technique: str = ""
    #: 是否真的拿到了结构化输出（False ⇒ 模型没给 JSON）
    structured: bool = False


def _clip(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip()[:limit]


def parse_voice_rewrite(raw: str) -> VoiceRewriteOutcome:
    """解析 ``voice-rewrite`` 的输出。

    - JSON 且含非空 ``rewritten`` ⇒ 取正文 + 诊断 + 下一轮手法；
    - 不是 JSON ⇒ **原样当正文**（改造前的行为），结构化字段为空
      ⇒ ``structured=False``（调用方据此知道「下一轮没有可回传的内容」）。
    """
    text = (raw or "").strip()
    if not text:
        return VoiceRewriteOutcome()

    payload: Optional[Dict[str, Any]] = None
    try:
        candidate = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        candidate = None
    if isinstance(candidate, dict):
        payload = candidate

    if payload is None:
        # 模型没给 JSON ⇒ 整段当正文（改造前的行为）
        return VoiceRewriteOutcome(rewritten=text, structured=False)

    rewritten = payload.get("rewritten")
    if not isinstance(rewritten, str) or not rewritten.strip():
        # 给了 JSON 但没有可用正文 ⇒ 交由调用方按「未产生有效变化」处理
        return VoiceRewriteOutcome(
            rewritten="",
            diagnosis=_clip(payload.get("diagnosis"), MAX_DIAGNOSIS_CHARS),
            next_technique=_clip(payload.get("next_technique"), MAX_TECHNIQUE_CHARS),
            structured=True,
        )
    return VoiceRewriteOutcome(
        rewritten=rewritten.strip(),
        diagnosis=_clip(payload.get("diagnosis"), MAX_DIAGNOSIS_CHARS),
        next_technique=_clip(payload.get("next_technique"), MAX_TECHNIQUE_CHARS),
        structured=True,
    )
