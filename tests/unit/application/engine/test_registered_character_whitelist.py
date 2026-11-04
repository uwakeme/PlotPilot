# -*- coding: utf-8 -*-
"""第 92 批：角色白名单放宽（Bible ∪ 记忆引擎已登记的角色）—— 行为级 + 结构守卫。

⚠ 这批要防的那类回归
--------------------
白名单**只有一处实现**（``registered_character_names``）。
一旦某个消费点改回 ``character_names(bible)``（只认 Bible 角色表），
口径就分叉 ⇒ 生产上表现为：**引擎一边禁止使用本章主角、一边把它判成 critical 越界**
（生产实证：556 章的书 Bible 只登记 5 个角色，而正文里实际有 40+ 个）。

守卫清单
--------
A. 行为级
   1. 白名单 = Bible ∪ 记忆引擎已登记的角色（**「谢迟」必须在**）
   2. 内部 id / 描述型名字 / Bible 地点**不进**白名单
   3. 生成侧事实锁文本**含**「谢迟」
   4. 审计侧基准事实**含**「谢迟」（两侧同源）
   5. 机检核对：K 类命中「谢迟」⇒ **驳回**（不是「已确认」）
   6. 机检核对：K 类命中「钱多多」（真·新角色）⇒ **仍确认**
   7. 查库失败 ⇒ 退回 Bible 那一半（不崩、不臆测）
B. 结构守卫
   8. 生产代码里**不许**有第二处白名单实现
   9. 三个消费点都必须走那个唯一实现
C. 常量
   10. ``MAX_REGISTERED_NAMES`` 用**字面量**钉死
"""
from __future__ import annotations

import pathlib
import re

import pytest

from application.audit.services.character_whitelist_check import (
    CONFIRMED,
    REJECTED,
    classify_rogue_character_hit,
)
from application.engine.services.memory_engine import FactLockBuilder
from application.engine.services.registered_character_names import (
    MAX_REGISTERED_NAMES,
    registered_character_names,
    registered_character_whitelist_variants,
)

ROOT = pathlib.Path(__file__).resolve().parents[4]

#: 「拿模型原始输出直接 json.loads」……（此处是白名单的唯一实现守卫）
#: 只匹配**裸**调用（前面不是 `_` / `.`）⇒ ``known_character_names(``、
#: ``_get_character_names(``、``registered_character_names(`` 都不算。
_CALL = re.compile(r"(?<![\w.])character_names\(")

#: 允许出现「Bible 那一半」的两个文件（**定义处**）
_ALLOWED_FILES = {
    "application/engine/services/memory_engine.py",
    "application/engine/services/registered_character_names.py",
}

#: 三个白名单消费点（生成侧 / 审计侧提示词 / 审计侧机检）
_CONSUMERS = (
    "application/engine/services/memory_engine.py",
    "engine/runtime/daemon_host.py",
    "application/ai_invocation/autopilot/continuations.py",
)


class _Char:
    def __init__(self, name, **kw):
        self.name = name
        self.description = kw.get("description", "")
        self.relationships = kw.get("relationships", [])
        self.public_profile = kw.get("public_profile", "")
        self.hidden_profile = kw.get("hidden_profile", "")
        self.reveal_chapter = kw.get("reveal_chapter", None)
        self.mental_state = kw.get("mental_state", "")


class _Loc:
    def __init__(self, name):
        self.name = name
        self.description = ""
        self.location_type = ""


class _Bible:
    def __init__(self, characters, locations=()):
        self.characters = list(characters)
        self.locations = list(locations)
        self.timeline_notes = []


def _bible() -> _Bible:
    """Bible 只登记了 2 个角色（**生产上真实的样子**：556 章的书只有 5 个）。"""
    return _Bible(
        [_Char("陆深"), _Char("宋扬")],
        locations=[_Loc("周队办公室")],
    )


def _rows() -> list[dict]:
    """记忆引擎（``character_states``）里已登记的角色。"""
    return [
        {"character_id": "novel-n-1-char-1", "last_chapter": 10},   # 内部 id
        {"character_id": "谢迟", "last_chapter": 555},              # 本书主角之一
        {"character_id": "老郑", "last_chapter": 540},
        {"character_id": "地下值班同事", "last_chapter": 530},       # 描述型（6 字）
        {"character_id": "周队办公室", "last_chapter": 520},        # 其实是地点
        {"character_id": "林姐丈夫", "last_chapter": 100},           # 带「夫」的角色名
    ]


class _FakeDB:
    def __init__(self, rows, boom: bool = False):
        self._rows = rows
        self._boom = boom

    def fetch_all(self, sql, params=()):
        if self._boom:
            raise RuntimeError("db down")
        return list(self._rows)


@pytest.fixture
def fake_db(monkeypatch):
    def _install(rows=None, boom: bool = False):
        db = _FakeDB(rows if rows is not None else _rows(), boom)
        monkeypatch.setattr(
            "infrastructure.persistence.database.connection.get_database",
            lambda: db,
        )
        return db

    return _install


# ======================================================================
#  A. 行为级
# ======================================================================


def test_the_whitelist_contains_the_bible_names_and_the_registered_ones(fake_db):
    """白名单 = Bible ∪ 记忆引擎已登记的角色（**「谢迟」必须在**）。"""
    fake_db()
    names = registered_character_names("n-1", _bible())

    assert "陆深" in names and "宋扬" in names, names
    assert "谢迟" in names, "记忆引擎已登记的角色不在白名单 ⇒ K 类又会把它判成越界"
    assert "老郑" in names, names
    assert names.index("陆深") < names.index("谢迟"), "Bible 的名字应排在前面"


def test_the_internal_ids_and_descriptions_are_not_in_the_whitelist(fake_db):
    """内部 id / 描述型名字 / Bible 地点**不进**白名单。"""
    fake_db()
    names = registered_character_names("n-1", _bible())

    assert "novel-n-1-char-1" not in names, "Bible 角色的内部 id 混进白名单了"
    assert "地下值班同事" not in names, "描述型短语混进白名单了"
    assert "周队办公室" not in names, "地点混进角色白名单了"
    assert "林姐丈夫" in names, "带「夫」的角色名被误伤了（它确实是个角色）"


class _Repo:
    """假 Bible 仓储（只回一本书）。"""

    def get_by_novel_id(self, novel_id):  # noqa: ANN001 - 假实现
        return _bible()


def test_the_generation_side_fact_lock_contains_the_registered_character(fake_db):
    """生成侧事实锁必须**含**「谢迟」。

    ⚠ 否则模型被要求「不许用」本章主角（生产上真实发生过）。

    ⚠ 走**公开**入口 ``build()``（不是私有 ``_build_from_bible``）——
    ⇒ 这样「``build()`` 没把 novel_id 传下去」这种回归也会被抓到。
    """
    fake_db()
    text = FactLockBuilder(_Repo()).build("n-1", 1)

    assert "谢迟" in text, text[:600]
    assert "陆深" in text


def test_the_audit_side_baseline_uses_the_same_whitelist(fake_db):
    """审计侧基准事实（``build_facts``）必须与生成侧**同一份**。

    ⚠ 同样走**公开**入口 ⇒ 「``build_facts()`` 没把 novel_id 传下去」也会被抓到。
    """
    fake_db()
    facts = FactLockBuilder(_Repo()).build_facts("n-1", 1)
    joined = "\n".join(facts)

    assert "谢迟" in joined, joined[:600]
    assert "陆深" in joined


def test_the_machine_check_rejects_a_registered_character(fake_db):
    """机检核对：K 类命中「谢迟」⇒ **驳回**（不是「已确认」）。

    ⚠ 生产上它被盖了 ``confirmed_out_of_whitelist`` ⇒ 误报看起来很权威。
    """
    fake_db()
    variants = registered_character_whitelist_variants("n-1", _bible())

    verdict, name = classify_rogue_character_hit("谢迟", variants)
    assert verdict == REJECTED, "「谢迟」是本书已登记的角色 ⇒ 不该判越界（%s / %s）" % (verdict, name)


def test_the_machine_check_still_catches_a_genuinely_new_character(fake_db):
    """机检核对：**真·新角色**仍要判越界（放宽不许变成放水）。"""
    fake_db()
    variants = registered_character_whitelist_variants("n-1", _bible())

    verdict, name = classify_rogue_character_hit("钱多多", variants)
    assert verdict == CONFIRMED, "凭空造的新名字没被抓到（%s / %s）" % (verdict, name)


def test_the_machine_check_also_rejects_a_short_form_of_a_registered_character(fake_db):
    """简称（「沈砚青」→「砚青」）也要驳回 —— 变体展开是白名单的一部分。"""
    fake_db()
    variants = registered_character_whitelist_variants("n-1", _Bible([_Char("沈砚青")]))

    verdict, name = classify_rogue_character_hit("砚青", variants)
    assert verdict == REJECTED, "白名单没展开简称 ⇒ 同一角色被当成新角色（%s / %s）" % (verdict, name)


def test_the_machine_check_rejects_a_hit_that_carries_context(fake_db):
    """生产实测：模型给的「名字」有时是**带上下文的片段**。

    ⚠ 生产库里 4 条 K 类命中有 3 条是这种形态（「谢迟掌中托过的墨」）
    ⇒ 旧口径会把它们判成 confirmed（**误报**，还被机检盖上「已确认」）。
    """
    fake_db()
    variants = registered_character_whitelist_variants("n-1", _bible())

    for pattern in ("谢迟掌中托过的墨", "谢迟（如\"谢迟掌心摊开\""):
        verdict, name = classify_rogue_character_hit(pattern, variants)
        assert verdict == REJECTED, "带上下文的命中被判成越界了（%s / %s）" % (verdict, name)


def test_a_broken_database_falls_back_to_the_bible_half(fake_db):
    """查库失败 ⇒ 退回 Bible 那一半（**不崩、不臆测**）。"""
    fake_db(boom=True)
    names = registered_character_names("n-1", _bible())

    assert names == ["陆深", "宋扬"], names


def test_without_a_novel_id_only_the_bible_half_is_used():
    """没有 novel_id ⇒ 只用 Bible 那一半（**不查库**）。"""
    assert registered_character_names("", _bible()) == ["陆深", "宋扬"]


# ======================================================================
#  B. 结构守卫
# ======================================================================


def test_no_second_whitelist_implementation_exists():
    """生产代码里**不许**有第二处白名单实现。

    ⚠ 只认 Bible 的那一版一旦被人抄走，口径就分叉 ⇒ 引擎自己打架。
    """
    offenders: list[str] = []
    for base in ("application", "engine", "interfaces", "infrastructure", "domain"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in _ALLOWED_FILES:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _CALL.search(line):
                    offenders.append("%s:%d  %s" % (rel, lineno, line.strip()[:80]))
    assert not offenders, (
        "这些地方还在用「只认 Bible」的白名单 ⇒ 口径与权威源分叉：\n  "
        + "\n  ".join(offenders)
    )


def test_every_whitelist_consumer_goes_through_the_single_source():
    """三个消费点都必须走那个**唯一实现**。"""
    for rel in _CONSUMERS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "registered_character_names" in text, (
            "%s 没有走 registered_character_names ⇒ 白名单口径与权威源分叉" % rel
        )


# ======================================================================
#  C. 常量
# ======================================================================


def test_the_whitelist_size_limit_is_pinned():
    """⚠ **字面量**钉死（不从被测模块 import 回来比较 ⇒ 不自指）。"""
    assert MAX_REGISTERED_NAMES == 80
