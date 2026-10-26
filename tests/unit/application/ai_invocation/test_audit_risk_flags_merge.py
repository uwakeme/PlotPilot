"""C：审计的 ``risk_flags`` 必须**真的有内容**，且**两条通道的结论汇总到一处**。

背景 —— 第 16 批生产跑章实测：
``continuations._audit`` 读 ``payload.get("risk_flags", [])``，
而 LLM 的 JSON 契约里**从来没有这个键** ⇒ 面板上的「风险标记」**恒为 0**
（实测日志：「审计 Invocation 已提交 chapter=1 risk_flags=0」）。

而代码通道（``ClicheScanner``，可机检的那些）算出来的分数/命中走的是**另一条路**
（``anti_ai_audits`` 表 + 共享状态）⇒ 两条通道的结论**没有汇总到一处**。

⇒ 现在 ``risk_flags`` 由**两条通道派生**：LLM 的 hits + 代码通道的命中。
"""
from __future__ import annotations

import sqlite3

import pytest

from application.ai_invocation.autopilot.continuations import (
    _code_channel_risk_flags,
    _merge_risk_flags,
)


# ======================================================================
#  A. 合并口径
# ======================================================================


def test_the_two_channels_are_merged():
    """LLM 的命中与代码通道的命中都要进 ``risk_flags``，且各自标了来源。"""
    payload = {
        "total_hits": 1,
        "hits": [
            {
                "category": "D",
                "pattern": "指尖泛白",
                "position": "中段",
                "severity": "critical",
                "replacement": "改用动作",
            }
        ],
    }
    code_flags = [
        {
            "source": "code",
            "category": "语言混杂",
            "pattern": "中文正文夹带英文词",
            "text": "notebook",
            "position": "前段",
            "severity": "info",
            "replacement": "改用中文",
        }
    ]

    merged = _merge_risk_flags(payload, code_flags)

    sources = {item["source"] for item in merged}
    assert sources == {"llm", "code"}, merged
    assert any("notebook" in str(item.get("text")) for item in merged), merged
    assert any("指尖泛白" in str(item.get("text")) for item in merged), merged


def test_the_same_finding_is_not_counted_twice():
    """同一条（类别 + 原文）只进一次。"""
    payload = {
        "hits": [
            {"category": "语言混杂", "pattern": "notebook", "position": "前段", "severity": "info"}
        ]
    }
    code_flags = [
        {
            "source": "code",
            "category": "语言混杂",
            "pattern": "中文正文夹带英文词",
            "text": "notebook",
            "position": "前段",
            "severity": "info",
        }
    ]

    merged = _merge_risk_flags(payload, code_flags)

    assert len(merged) == 1, merged


def test_the_llm_channel_alone_still_produces_flags():
    """LLM 通道有命中时，即使代码通道空，也要有内容（这正是之前恒空的场景）。"""
    payload = {
        "hits": [
            {"category": "A", "pattern": "嘴角勾起", "position": "后段", "severity": "critical"}
        ]
    }

    merged = _merge_risk_flags(payload, [])

    assert len(merged) == 1, merged
    assert merged[0]["source"] == "llm"


# ======================================================================
#  B. 代码通道：真的去扫这一章
# ======================================================================


class _FakeDb:
    def __init__(self, content: str):
        self._content = content

    def fetch_one(self, sql, params=()):
        class _Row(dict):
            pass

        row = _Row()
        row["content"] = self._content
        return row


def test_the_code_channel_scans_the_stored_chapter(monkeypatch):
    """代码通道会去读这一章的正文并扫描它（不是空转）。"""
    import infrastructure.persistence.database.connection as connection_module

    content = "她膝头压着 notebook，屏幕亮起时连片名都像隔着一层水。" + "灯影摇曳。" * 40
    monkeypatch.setattr(
        connection_module, "get_database", lambda *a, **k: _FakeDb(content)
    )

    flags = _code_channel_risk_flags("novel-1", 1)

    assert flags, "代码通道没有产出任何命中"
    assert any("notebook" in str(f.get("text")) for f in flags), flags
    assert all(f.get("source") == "code" for f in flags), flags


def test_the_code_channel_survives_a_broken_database(monkeypatch):
    """取不到正文不许把审计搞崩（返回空即可）。"""
    import infrastructure.persistence.database.connection as connection_module

    def _boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(connection_module, "get_database", _boom)

    assert _code_channel_risk_flags("novel-1", 1) == []
