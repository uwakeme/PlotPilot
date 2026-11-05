# -*- coding: utf-8 -*-
"""第 95 批：「未登记角色」提示 —— 行为级 + 接线守卫。

⚠ 要解决的缺口（生产实证）
--------------------------
第 92 批把角色白名单放宽成「Bible ∪ 记忆引擎已登记的角色」（49 个名字），
但事实锁的「角色设定 / 身份锁 / 关系锁」与**设定漂移审计的基准**仍只覆盖
Bible 那 5 个角色 ⇒ 生产上送进审计的基准全文里「谢迟」只出现 2 次
（都在白名单那一行），**没有任何一条关于他的事实**
⇒ 这些人的人设漂移**审计根本看不见**
（近 25 次 A1 的 issue 全是 Bible 角色或地点，**没有一个**是非 Bible 角色的）。

⚠ 用户 2026-11-04 拍板：**做提示，不自动改 Bible**
（Bible 是设定真源 ⇒ 让模型改它，错了就污染事实锁与审计基准）。

守卫清单
--------
1. 有新角色 ⇒ 加一条 info 提示（描述里带名字与「不在事实锁」的说明）；
2. 没有新角色 ⇒ **一条都不加**；
3. 没扫出来 ⇒ 加一条**可见**的「未评估」条目（不许把「没查」读成「没有」）；
4. 重扫不堆积旧条目（同一 type 只留一条）；
5. 扫描函数：**记忆引擎此前已登记**的名字被过滤（否则每章一条长名单 = 纯噪音）；
6. 扫描函数：没有章纲 ⇒ 判不了（assessed=False）；
7. 接线：章后流程**真的**调了它（按函数边界切片，不是全文 contains）。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from engine.runtime.audit_delegate import (
    UNREGISTERED_CHARACTER_ISSUE_TYPE,
    _merge_unregistered_character_issue,
    run_chapter_audit,
)
from engine.runtime.daemon_host import DaemonHostMixin


# ── 1) 合并函数 ────────────────────────────────────────────────────────


def test_a_new_character_produces_a_visible_hint():
    """有新角色 ⇒ 加一条 info 提示（描述里要有名字 + 说清后果）。"""
    items = _merge_unregistered_character_issue(
        [], {"assessed": True, "names": ["谢迟"]}
    )
    added = [i for i in items if str(i.get("type", "")).startswith(UNREGISTERED_CHARACTER_ISSUE_TYPE)]
    assert len(added) == 1, items
    issue = added[0]
    assert issue["severity"] == "info", "提示不该是告警级别（它不是违规）"
    assert "谢迟" in issue["description"], issue
    assert "事实锁" in issue["description"], (
        "提示要说明后果（不在事实锁/审计基准里 ⇒ 人设漂移审不出来）：%s" % issue["description"]
    )
    assert issue["suggestion"], "提示要给出下一步（去哪儿登记）"


def test_no_new_character_adds_nothing():
    """没有新角色 ⇒ **一条都不加**（issues 为空才真的意味着「没有」）。"""
    assert _merge_unregistered_character_issue([], {"assessed": True, "names": []}) == []


def test_an_unassessed_scan_produces_a_visible_unassessed_entry():
    """没扫出来 ⇒ 加一条**可见**的「未评估」条目（不许假装「没有新角色」）。"""
    items = _merge_unregistered_character_issue(
        [], {"assessed": False, "reason": "扫描超时", "names": []}
    )
    added = [i for i in items if str(i.get("type", "")).startswith(UNREGISTERED_CHARACTER_ISSUE_TYPE)]
    assert len(added) == 1, items
    assert "未评估" in added[0]["description"] or "未完成" in added[0]["description"], added[0]
    assert "扫描超时" in added[0]["description"], added[0]


def test_rescanning_does_not_stack_entries():
    """重扫（下一章）⇒ 同一 type 只留**一条**（旧的被替换，不堆积）。"""
    first = _merge_unregistered_character_issue(
        [], {"assessed": True, "names": ["谢迟"]}
    )
    second = _merge_unregistered_character_issue(
        first, {"assessed": True, "names": ["潮爷"]}
    )
    added = [i for i in second if str(i.get("type", "")).startswith(UNREGISTERED_CHARACTER_ISSUE_TYPE)]
    assert len(added) == 1, second
    assert "潮爷" in added[0]["description"], added[0]


# ── 2) 扫描函数 ────────────────────────────────────────────────────────


class _FakeBible:
    def __init__(self, characters, locations):
        self.characters = [SimpleNamespace(name=n) for n in characters]
        self.locations = [SimpleNamespace(name=n) for n in locations]


class _FakeDB:
    def __init__(self, known_before):
        self._known_before = known_before

    def fetch_all(self, sql, params=()):
        # 只回答 character_states 的那条查询
        return [{"character_id": n} for n in self._known_before]


class _FakeNerResult:
    def __init__(self, names):
        self.names = names
        self.rejected = []
        self.source = "ai"

    @property
    def is_assessed(self):
        return True





CAPTURED = {}


def _patch(monkeypatch, *, ner_names, known_before=(), bible=None):
    """把扫描函数依赖的三样东西都换成假的。"""
    import infrastructure.persistence.database.connection as conn_mod
    import infrastructure.persistence.database.sqlite_bible_repository as repo_mod
    import application.character.services.character_ner as ner_mod

    CAPTURED.clear()
    fake_bible = bible if bible is not None else _FakeBible(["陆深", "宋扬"], ["市档案馆"])

    class _FakeService:
        """⚠ 真服务的 ``extract`` 是 **async** ⇒ 假的也得是 async。"""

        def __init__(self, llm):
            self.llm = llm

        async def extract(self, **kw):
            CAPTURED.update(kw)
            return _FakeNerResult(list(ner_names))

    monkeypatch.setattr(ner_mod, "CharacterNerService", _FakeService)
    monkeypatch.setattr(
        conn_mod, "get_database", lambda: _FakeDB(list(known_before))
    )
    monkeypatch.setattr(
        repo_mod, "SqliteBibleRepository",
        lambda db: SimpleNamespace(get_by_novel_id=lambda nid: fake_bible),
    )


def _host():
    host = DaemonHostMixin.__new__(DaemonHostMixin)
    host.llm_service = object()
    return host


@pytest.mark.asyncio
async def test_the_scan_keeps_only_genuinely_new_characters(monkeypatch):
    """⚠ 记忆引擎**此前**已登记的名字 ⇒ 不是新面孔 ⇒ 不报（否则每章一条长名单）。"""
    _patch(
        monkeypatch,
        ner_names=["老郑", "谢迟"],
        known_before=["老郑"],   # 老郑早就在册了
    )
    result = await _host()._run_unregistered_character_scan(
        "n-1", 528, "本章章纲：谢迟执册，老郑推门。"
    )
    assert result["assessed"] is True, result
    assert result["names"] == ["谢迟"], result


@pytest.mark.asyncio
async def test_the_scan_reports_unassessed_without_an_outline(monkeypatch):
    """没有章纲 ⇒ 判不了（assessed=False）⇒ 上游写「未评估」条目。"""
    _patch(monkeypatch, ner_names=[])
    result = await _host()._run_unregistered_character_scan("n-1", 528, "")
    assert result["assessed"] is False, result
    assert result["reason"], result


@pytest.mark.asyncio
async def test_the_known_names_are_the_bible_ones_not_the_broadened_whitelist(monkeypatch):
    """⚠ 已知名单必须是 **Bible 角色表**，不是第 92 批那个放宽后的白名单。

    ⇒ 若传放宽后的白名单（49 个名字），「已登记但不在 Bible」的角色
    会被当成**已知** ⇒ 新角色**一个都发现不了** ⇒ 这个提示形同虚设。
    """
    _patch(monkeypatch, ner_names=["谢迟"])
    await _host()._run_unregistered_character_scan("n-1", 528, "本章章纲：谢迟执册。")

    known = list(CAPTURED.get("known_names") or [])
    assert known == ["陆深", "宋扬"], (
        "传给角色识别的已知名单不是 Bible 角色表（%r）⇒ 新角色发现不到" % known
    )


# ── 3) 接线守卫 ────────────────────────────────────────────────────────


def test_the_chapter_after_flow_really_calls_the_scan():
    """⚠ 接线：章后流程**真的**调了它。

    ⚠ 用「按函数边界切片」查（``inspect.getsource``），不是全文 contains ——
    全文 contains 会命中别的函数里的同名字符串。
    """
    src = inspect.getsource(run_chapter_audit)
    assert "_run_unregistered_character_scan" in src, (
        "章后流程没有调未登记角色扫描 ⇒ 这个提示永远不会出现"
    )
    assert "_merge_unregistered_character_issue" in src, (
        "章后流程没有把扫描结果并进 issues ⇒ 面板上看不到"
    )
