"""第 25 批：**世界规则**进 A1 审计基准（与生成侧同源）。

背景（生产实测，第 24 批核实）
------------------------------
A1（设定漂移审计）的判据写着「世界观规则被违反」，而它的基准**只取**
``bible.world_settings`` —— 那张表**永远为空**：

* Bible 生成流程只把五维写进 ``worldbuilding`` 表，**从不**生成 world_settings
  （``bible_setup_continuation`` 三个 handler 都是把 ``bible.world_settings`` 原样传回）；
* 实测生产书 ``prodrealtest-001``：``bible_world_settings`` **0 条**，
  而 ``worldbuilding`` **1 行**、核心法则维度的内容极其丰富
  （聆音三途 / 声痕留存三条件 / 蜡筒三放即碎……）。

⇒ 而生成侧的世界核心规则走的是 ``worldbuilding_core`` 槽位
（``context_budget_allocator._build_worldbuilding_core_slot``）
⇒ **审计审「世界观规则」，却自己一条规则都不知道** ⇒ 那一类漂移**永远审不到**。

⚠ 修法的关键：**同源**。
审计侧若自己另写一份「世界观规则怎么渲染」，两边迟早漂移
⇒ 又变成「审计审模型没被告知过的东西」（第 24 批的同一个病灶）
⇒ 这里直接复用**生成侧那个函数**，只把它的块切成事实条。

⚠ 守卫纪律：走**真实**函数（生成侧的槽位内容函数），不复刻任何一层的形状。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from application.audit.services.setting_drift_audit import (
    _FACT_TEXT_LIMIT,
    SettingDriftAuditService,
)
from application.engine.services.context_slot_providers import (
    build_worldbuilding_core_slot_content,
)

ROOT = Path(__file__).resolve().parents[4]

#: 载荷：与仓库里其它用例都不同的名字（避免「碰巧命中」）
POWER_RULE = "聆音分三途：听痕、借声、封声，代价是拿记忆与听力去抵"
PHYSICS_RULE = "声痕留存要同时满足封闭空间、潮湿空气与少有人惊动的夜晚"
TECH_RULE = "蜡筒每放一次便多一道裂纹，三放即碎"


class _WorldbuildingRepository:
    def __init__(self, worldbuilding):
        self.worldbuilding = worldbuilding

    def get_by_novel_id(self, novel_id):
        return self.worldbuilding


class _RaisingRepository:
    def get_by_novel_id(self, novel_id):
        raise RuntimeError("db down")


def _worldbuilding():
    return SimpleNamespace(
        power_system=POWER_RULE,
        physics_rules=PHYSICS_RULE,
        magic_tech=TECH_RULE,
    )


def _bible():
    return SimpleNamespace(
        world_settings=[],
        locations=[SimpleNamespace(name="临渊城", description="北境边城", location_type="city")],
    )


def _service(worldbuilding_repository=..., bible=None):
    repo = _WorldbuildingRepository(_worldbuilding()) if worldbuilding_repository is ... else worldbuilding_repository
    svc = SettingDriftAuditService(
        llm_service=MagicMock(),
        bible_repository=MagicMock(),
        worldbuilding_repository=repo,
    )
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=bible if bible else _bible())
    return svc


# ── ① 世界规则**真的进了基准** ─────────────────────────────────────────


def test_the_world_rules_are_in_the_audit_baseline():
    """⚠ 审计的判据写着「世界观规则被违反」⇒ 基准里**必须有**世界规则。

    ⚠ 此前基准只取 ``bible.world_settings``（永远为空）⇒ 这一类**永远审不到**。
    """
    svc = _service()

    facts = svc._collect_setting_facts("n-1", 3)

    joined = "\n".join(facts)
    assert POWER_RULE[:40] in joined, "审计基准里没有世界核心规则 ⇒ 那一类漂移永远审不到"
    assert PHYSICS_RULE[:40] in joined, "物理规则没进基准"
    assert TECH_RULE[:40] in joined, "技术机制没进基准"


# ── ② **同源**：基准内容逐字来自生成侧那个函数 ──────────────────────────


def test_the_baseline_comes_from_the_same_function_as_the_generation_side():
    """⚠ 审计侧若自己另写一份渲染 ⇒ 两边漂移 ⇒ 又审「模型没被告知过的东西」。

    ⇒ 断言：基准里的每条世界规则，其正文都**逐字**出现在
    **生成侧槽位函数**的输出里。
    """
    repo = _WorldbuildingRepository(_worldbuilding())
    svc = _service(worldbuilding_repository=repo)

    facts = svc._collect_setting_facts("n-1", 3)
    world_facts = [f for f in facts if f.startswith("[世界观/")]

    assert world_facts, "基准里没有世界规则条目"
    slot_text = build_worldbuilding_core_slot_content(repo, "n-1")
    for fact in world_facts:
        body = fact.split("]", 1)[1].strip()
        assert body[:30] in slot_text, (
            f"这条基准不是生成侧那块内容 ⇒ 两边不同源：{fact[:60]!r}"
        )


def test_the_audit_side_calls_the_generation_side_function(monkeypatch):
    """⚠ **同源**要钉在**行为**上，不是只比文字。

    ⚠ 只比文字 ⇒ 「自己另写一份渲染、但碰巧文字一样」会**假绿**
    ⇒ 变异验证就是这么钻空子的。
    ⇒ 这里**探针**：断言审计侧**真的调用了**生成侧那个函数，
    且参数就是（同一个仓储、同一个 novel_id）。
    """
    from application.engine.services import context_slot_providers

    calls = []
    original = context_slot_providers.build_worldbuilding_core_slot_content

    def _spy(repo, novel_id):
        calls.append((repo, novel_id))
        return original(repo, novel_id)

    monkeypatch.setattr(
        context_slot_providers, "build_worldbuilding_core_slot_content", _spy
    )
    repo = _WorldbuildingRepository(_worldbuilding())
    svc = _service(worldbuilding_repository=repo)

    facts = svc._collect_setting_facts("n-1", 3)

    assert calls, "审计侧没有调用生成侧那个函数 ⇒ 两边迟早漂移"
    assert calls[0][0] is repo, "传给生成侧的仓储不是审计侧那个"
    assert calls[0][1] == "n-1", "传给生成侧的 novel_id 不对"


def test_the_baseline_covers_every_core_rule_field():
    """核心法则三个字段都要在基准里（不能只给一条就当「有世界观规则」）。"""
    svc = _service()

    facts = svc._collect_setting_facts("n-1", 3)
    world_facts = [f for f in facts if f.startswith("[世界观/")]

    assert len(world_facts) >= 3, world_facts


# ── ③ 降级：缺仓储 / 仓储炸了 ⇒ 不崩、不假装有 ─────────────────────────


def test_a_missing_worldbuilding_repository_degrades_without_crashing():
    """没配仓储 ⇒ 世界规则这一类**审不到**（缺覆盖），但其余基准照常、且不崩。"""
    svc = _service(worldbuilding_repository=None)

    facts = svc._collect_setting_facts("n-1", 3)

    assert not [f for f in facts if f.startswith("[世界观/")], "没配仓储却出现了世界规则"
    assert any("临渊城" in f for f in facts), "其它基准（地点）也不见了 ⇒ 降级过头了"


def test_a_broken_worldbuilding_repository_degrades_without_crashing():
    """仓储抛异常 ⇒ 降级（不崩、不假装有）⇒ 其余基准照常。"""
    svc = _service(worldbuilding_repository=_RaisingRepository())

    facts = svc._collect_setting_facts("n-1", 3)

    assert not [f for f in facts if f.startswith("[世界观/")], "仓储炸了却出现了世界规则"
    assert any("临渊城" in f for f in facts), "其它基准也不见了"


# ── ④ 单条限长（prompt 预算）仍然生效 ──────────────────────────────────


def test_each_world_rule_fact_is_length_limited():
    """单条上限仍在 ⇒ 世界规则不会把审计 prompt 撑爆。"""
    long_rule = "力量体系" + "描述" * 400
    repo = _WorldbuildingRepository(SimpleNamespace(power_system=long_rule))
    svc = _service(worldbuilding_repository=repo)

    facts = svc._collect_setting_facts("n-1", 3)
    world_facts = [f for f in facts if f.startswith("[世界观/")]

    assert world_facts
    for fact in world_facts:
        assert len(fact) <= _FACT_TEXT_LIMIT, f"这条基准超长：{len(fact)}"


# ── ⑤ 两处装配都要有（手动审稿 + 自动驾驶） ────────────────────────────


def test_both_call_sites_wire_the_worldbuilding_repository():
    """⚠ **两处**调用点都要传（手动审稿走 API，自动驾驶走守护进程）。

    ⚠ 只装一处 ⇒ 两条路径的审计口径不一样 ⇒ 「手动审得出、自动审不出」。
    """
    for rel in (
        "interfaces/api/dependencies.py",
        "engine/runtime/daemon_host.py",
    ):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "SettingDriftAuditService(" in source, rel
        # 取构造调用附近的一段
        idx = source.index("SettingDriftAuditService(")
        window = source[idx : idx + 600]
        assert "worldbuilding_repository=" in window, (
            f"{rel} 没有装配 worldbuilding_repository ⇒ 该路径审不到世界规则"
        )


def test_the_world_rule_facts_are_recognisable_in_the_prompt_shape():
    """基准条目的格式与其它事实**一致**（``[类别/子类] 正文``）⇒ 模型能读懂。"""
    svc = _service()

    facts = svc._collect_setting_facts("n-1", 3)
    world_facts = [f for f in facts if f.startswith("[世界观/")]

    assert world_facts
    for fact in world_facts:
        assert re.match(r"^\[世界观/[^\]]+\] .+", fact), fact
