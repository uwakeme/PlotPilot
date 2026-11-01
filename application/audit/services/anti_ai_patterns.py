# -*- coding: utf-8 -*-
"""反 AI 俗套检测的**规则清单**（唯一来源：``config/anti_ai_patterns.yaml``）。

第 77 批（A）
------------
51 条正则从 ``cliche_scanner.py`` 搬到这里 ⇒

* **规则可调**：改 YAML 就行，不用改代码（原先要动 Python 源码）；
* **清单只有一个来源**：代码里不再留第二份（守卫会查）；
* **搬运无损**：守卫钉住清单的**校验和**
  （``tests/unit/application/audit/test_anti_ai_patterns_datafile.py``）
  ⇒ 改 YAML 里的任何一条 ⇒ 守卫立刻红 ⇒ 不会被「顺手改坏」。

⚠ **两份 SSOT 的分工**（别混）：

* 这里 = 「**怎么判**」（正则、严重性、替换建议）；
* ``config/anti_ai_taxonomy.yaml`` = 「**判出来算什么**」（A–K 分类学）。

⇒ 分类字段必须能在词表里找到对应（守卫双向核对）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Tuple

import yaml

#: 规则清单文件（仓库内唯一来源）
PATTERNS_PATH = Path(__file__).resolve().parents[3] / "config" / "anti_ai_patterns.yaml"

_CACHE: List[Tuple[str, str, str, str, str]] | None = None
_LOCK = threading.Lock()


def load_cliche_patterns() -> List[Tuple[str, str, str, str, str]]:
    """读规则清单（**只读一次** ⇒ 之后都是内存里的）。"""
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            raw = yaml.safe_load(PATTERNS_PATH.read_text(encoding="utf-8"))
            items: List[Tuple[str, str, str, str, str]] = []
            for entry in (raw or {}).get("patterns") or []:
                items.append(
                    (
                        str(entry["regex"]),
                        str(entry["name"]),
                        str(entry["severity"]),
                        str(entry["category"]),
                        str(entry.get("hint") or ""),
                    )
                )
            if not items:
                raise ValueError("反 AI 规则清单是空的：%s" % PATTERNS_PATH)
            _CACHE = items
        return list(_CACHE)


#: 规则清单（**保持原名** ⇒ 既有调用方零改动）
#: 格式：(正则, 模式名, 严重性, 分类, 替换建议)
AI_CLICHE_PATTERNS_ENHANCED: List[Tuple[str, str, str, str, str]] = load_cliche_patterns()
