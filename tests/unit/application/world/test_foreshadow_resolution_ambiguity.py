"""第 37 批 · 登记表 G-02：伏笔回收**歧义**不许静默取第一个。

⚠ 病灶（按代码核实，2026-10-28）
--------------------------------
``chapter_narrative_sync._fuzzy_match_foreshadow`` **命中就返回第一个**
⇒ 多个待回收伏笔都匹配同一条描述时 ⇒ **静默取第一个**
⇒ ⇒ 另一条伏笔**静默漏掉**（而日志写「伏笔已消费」⇒ **看起来成功了**）
⇒ ⇒ 故事的伏笔账从此对不上（一条被误标成已回收、另一条永远挂着）。

⚠ 本批的修法（**降级不许编**）
------------------------------
* 匹配器改成返回**全部**候选（``_foreshadow_candidates``）；
* **1 个**候选 ⇒ 照旧回收（行为不变）；
* **≥2 个**候选（歧义）⇒ **不静默取第一个** ⇒ **一条都不回收**
  ⇒ **宁可挂账**（伏笔继续挂着 = 诚实的「还没回收」）
  ⇒ 也不 resolve 错的那条（那会让**伏笔账**彻底对不上）；
* **0 个**候选 ⇒ AI 说回收了它，但它**不在**待回收清单里
  ⇒ 此前**完全静默** ⇒ 现在有 WARNING 登记。

⚠ 登记通道：日志（WARNING）+ 章末汇总行（``ambiguous=`` / ``unmatched=``）
⇒ ⚠ **没有**新增 DB 列（章后 flags 是**逐字段映射**，加字段要迁移 ⇒ 代价与收益不匹配）。

⚠ 守卫纪律：走**真实**的落库函数（不是重写一遍它的形状）。
"""
from __future__ import annotations

import logging

from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.value_objects.foreshadowing import (
    Foreshadowing,
    ForeshadowingStatus,
    ImportanceLevel,
)
from domain.novel.value_objects.novel_id import NovelId

from application.world.services.chapter_narrative_sync import (
    _foreshadow_candidates,
    persist_bundle_triples_and_foreshadows,
)

#: 两条**描述互相包含**的待回收伏笔 ⇒ 同一条 AI 描述会**同时**命中它们
DESC_A = "封蜡离案即融、融尽即启即投递"
DESC_B = "封蜡离案即融、融尽即启即投递，内里是郑铁珊自己的命账"

#: ⚠ 造成**歧义**的那条 AI 描述：**两条候选的公共前缀**
#: ⇒ 它**不等于**任何一条的完整描述（否则会落进「精确匹配」那一档 ⇒ 那一档本就无歧义）
#: ⇒ 但两条都**包含**它 ⇒ 「包含关系」那一档返回**两个**候选 ⇒ 歧义。
CONSUMED_AMBIGUOUS = "封蜡离案即融、融尽即启"


class _ForeshadowRepo:
    def __init__(self, registry: ForeshadowingRegistry) -> None:
        self.registry = registry

    def get_by_novel_id(self, novel_id):
        return self.registry

    def save(self, registry):
        self.registry = registry


def _foreshadow(fid: str, desc: str) -> Foreshadowing:
    return Foreshadowing(
        id=fid,
        planted_in_chapter=1,
        description=desc,
        importance=ImportanceLevel.HIGH,
        status=ForeshadowingStatus.PLANTED,
    )


def _registry_with(*foreshadowings: Foreshadowing) -> ForeshadowingRegistry:
    registry = ForeshadowingRegistry(id="fr-1", novel_id=NovelId("novel-1"))
    for f in foreshadowings:
        registry.register(f)
    return registry


def _run(registry: ForeshadowingRegistry, consumed_desc: str) -> None:
    bundle = {"consumed_foreshadows": [consumed_desc]}
    persist_bundle_triples_and_foreshadows(
        "novel-1", 4, bundle, None, _ForeshadowRepo(registry)
    )


# ── ① 匹配器：返回**全部**候选 ──────────────────────────────────────────


def test_the_matcher_returns_every_candidate():
    """⚠ 多个候选都匹配时 ⇒ **全部返回**（不是只给第一个）。"""
    a = _foreshadow("f1", DESC_A)
    b = _foreshadow("f2", DESC_B)

    candidates = _foreshadow_candidates(CONSUMED_AMBIGUOUS, [a, b])

    assert [c.id for c in candidates] == ["f1", "f2"]


def test_the_exact_tier_beats_the_looser_tiers():
    """⚠ 精确命中时**不**把更松档位的候选混进来（否则精确匹配被淹没）。"""
    exact = _foreshadow("f1", "铜铃将在雨夜响起")
    loose = _foreshadow("f2", "铜铃将在雨夜响起之后很久很久才响")

    candidates = _foreshadow_candidates("铜铃将在雨夜响起", [exact, loose])

    assert [c.id for c in candidates] == ["f1"]


# ── ② 歧义：**一条都不回收**（不静默取第一个） ──────────────────────────


def test_a_chapter_that_only_closes_foreshadows_still_resolves_them():
    """⚠⚠ **只回收、不种植**的一章 ⇒ 伏笔**必须**被回收。

    ⚠ 第 37 批实测（写这条守卫时才发现的**更大**的洞）：
      消费处理此前**整块**嵌在 ``if foreshadowing_repo and hints:``（**种植**）里
      ⇒ 这一章**没种新伏笔** ⇒ 回收逻辑**压根不执行**
      ⇒ ⇒ 伏笔**永远回收不了**（除非那一章碰巧也种了新伏笔）⇒ **静默失效**。
    ⇒ 消费必须与种植**解耦**。
    """
    registry = _registry_with(_foreshadow("f1", DESC_A))
    bundle = {
        "consumed_foreshadows": [DESC_A],
        # ⚠ **故意不给** "foreshadow_hints" ⇒ 这一章**不种植**新伏笔
    }

    persist_bundle_triples_and_foreshadows(
        "novel-1", 4, bundle, None, _ForeshadowRepo(registry)
    )

    statuses = {f.id: f.status for f in registry.foreshadowings}
    assert statuses == {"f1": ForeshadowingStatus.RESOLVED}, (
        "只回收、不种植的一章 ⇒ 伏笔没被回收（回收逻辑被种植门控了）"
    )


def test_an_ambiguous_match_resolves_nothing(caplog):
    """⚠ 歧义 ⇒ **不 resolve**（宁可挂账也不 resolve 错的那条）。"""
    registry = _registry_with(
        _foreshadow("f1", DESC_A),
        _foreshadow("f2", DESC_B),
    )

    with caplog.at_level(logging.WARNING):
        _run(registry, CONSUMED_AMBIGUOUS)

    statuses = {f.id: f.status for f in registry.foreshadowings}
    assert statuses == {"f1": ForeshadowingStatus.PLANTED, "f2": ForeshadowingStatus.PLANTED}, (
        "歧义时不该回收任何一条（此前会静默回收第一条）"
    )
    assert "歧义" in caplog.text, "歧义必须有登记（否则就是静默失效）"
    assert "f1" in caplog.text and "f2" in caplog.text, "登记必须点名**候选**（要能查）"


def test_a_single_match_is_still_resolved():
    """⚠ 唯一候选 ⇒ **照旧回收**（行为不变，别把正常路径也堵死）。"""
    registry = _registry_with(_foreshadow("f1", DESC_A))

    _run(registry, DESC_A)

    statuses = {f.id: f.status for f in registry.foreshadowings}
    assert statuses == {"f1": ForeshadowingStatus.RESOLVED}


# ── ③ 未匹配：不再**完全静默** ─────────────────────────────────────────


def test_an_unmatched_consumption_is_logged(caplog):
    """⚠ AI 说回收了它，但它**不在**待回收清单里 ⇒ 必须有登记（此前完全静默）。"""
    registry = _registry_with(_foreshadow("f1", DESC_A))

    with caplog.at_level(logging.WARNING):
        _run(registry, "某个我们根本没埋过的伏笔")

    assert "不在" in caplog.text, "未匹配必须有登记"
    assert all(f.status == ForeshadowingStatus.PLANTED for f in registry.foreshadowings)
