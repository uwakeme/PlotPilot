"""J-01：目标章数 / 终局预算的提案改由 AI 给（不可用时回落公式，来源可分辨）。

改造前：「再加多少章」「终局留几章」是**代码写死的常量公式**
（``max(20, round(target * 0.2))``）—— 创作判断被公式代答。

本文件钉住：
- AI 给出**完整且自洽**的提案 ⇒ 用它，来源 ``ai``；
- 终局预算的 [5, 200] 上限**仍由 domain 钳制**（AI 动不了这个产品常量）；
- AI 说「给不出」/ 调用失败 / 输出不合契约 ⇒ 回落**引擎默认公式**
  （与改造前逐位相同），来源 ``fallback``；
- 出口（HTTP 端点）必须把**来源**透出去（读端能分辨「AI 说的」与「公式兜的」）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from application.blueprint.services.target_budget import (
    FINALE_BUDGET_MAX,
    FINALE_BUDGET_MIN,
    propose_continue_block,
)
from application.blueprint.services.target_budget_proposal import (
    TargetBudgetProposalService,
)
from application.blueprint.services.target_budget_schema import (
    SOURCE_AI,
    SOURCE_FALLBACK,
)
from domain.novel.value_objects.generation_preferences import clamp_finale_budget


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

    def __init__(self, captured: dict):
        self._captured = captured

    def render(self, contract, variables):
        self._captured["variables"] = dict(variables)
        self._captured["node_key"] = contract.node_key
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"),
            node_key=contract.node_key,
        )

    def validate_output(self, contract, payload):
        return contract.output_schema.model_validate(payload)


class _StubLLM:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    async def generate(self, prompt, config):
        self.calls += 1
        return SimpleNamespace(content=self._content)


def _service(monkeypatch, content: str, captured: dict | None = None) -> TargetBudgetProposalService:
    import application.blueprint.services.target_budget_proposal as mod

    captured = captured if captured is not None else {}
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway(captured))
    return TargetBudgetProposalService(_StubLLM(content))


def _payload(**overrides) -> str:
    data = {
        "continue_block": 24,
        "finale_budget": 12,
        "finale_budget_min": 8,
        "finale_budget_max": 18,
        "reason": "主线还剩两处伏笔未回收",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.asyncio
async def test_ai_proposal_is_used_when_complete(monkeypatch):
    """AI 给出完整提案 ⇒ 用它，来源 ai。"""
    captured: dict = {}
    svc = _service(monkeypatch, _payload(), captured)

    result = await svc.propose(
        novel_id="n-1",
        title="测试书",
        genre="武侠",
        target_chapters=100,
        completed_chapters=100,
        current_finale_budget=12,
    )

    assert result["source"] == SOURCE_AI
    assert result["continue_block"] == 24
    assert result["finale_budget"] == 12
    assert result["finale_budget_min"] == 8
    assert result["finale_budget_max"] == 18
    assert result["continue_target"] == 124  # 完成数 + 加写块
    assert result["reason"]
    # 变量真的送进模板了（出口存在）
    assert captured["variables"]["completed_chapters"] == 100
    assert captured["variables"]["current_finale_budget"] == 12


@pytest.mark.asyncio
async def test_finale_budget_upper_bound_is_code_guaranteed(monkeypatch):
    """终局预算的 [5, 200] 上限**AI 动不了**（那是产品常量，不是创作判断）。"""
    svc = _service(monkeypatch, _payload(finale_budget=9999, finale_budget_max=99999))

    result = await svc.propose(
        novel_id="n-1",
        target_chapters=100,
        completed_chapters=100,
        current_finale_budget=12,
    )

    assert result["finale_budget"] == FINALE_BUDGET_MAX
    assert result["finale_budget_max"] == FINALE_BUDGET_MAX


@pytest.mark.asyncio
async def test_null_proposal_falls_back_to_engine_formula(monkeypatch):
    """AI 明确说「给不出」（全 null）⇒ 回落公式，来源 fallback。"""
    svc = _service(
        monkeypatch,
        json.dumps(
            {
                "continue_block": None,
                "finale_budget": None,
                "finale_budget_min": None,
                "finale_budget_max": None,
                "reason": "信息不足，无法判断",
            },
            ensure_ascii=False,
        ),
    )

    result = await svc.propose(
        novel_id="n-1",
        target_chapters=100,
        completed_chapters=100,
        current_finale_budget=12,
    )

    assert result["source"] == SOURCE_FALLBACK
    # 回落值与「改造前逐位相同」的公式一致
    assert result["continue_block"] == propose_continue_block(
        target_chapters=100, completed_chapters=100
    )
    assert result["finale_budget"] == clamp_finale_budget(12)
    assert result["finale_budget_min"] == FINALE_BUDGET_MIN
    assert result["finale_budget_max"] == FINALE_BUDGET_MAX


@pytest.mark.asyncio
async def test_inconsistent_proposal_is_rejected_and_falls_back(monkeypatch):
    """自相矛盾的区间（min > 值）⇒ 契约拒收 ⇒ 回落公式。"""
    svc = _service(monkeypatch, _payload(finale_budget=5, finale_budget_min=9))

    result = await svc.propose(
        novel_id="n-1",
        target_chapters=100,
        completed_chapters=100,
        current_finale_budget=12,
    )

    assert result["source"] == SOURCE_FALLBACK


@pytest.mark.asyncio
async def test_llm_failure_falls_back(monkeypatch):
    """调用失败 ⇒ 回落公式（不许把异常透给前端）。"""
    import application.blueprint.services.target_budget_proposal as mod

    class _BoomLLM:
        async def generate(self, prompt, config):
            raise RuntimeError("endpoint down")

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway({}))
    svc = TargetBudgetProposalService(_BoomLLM())

    result = await svc.propose(
        novel_id="n-1",
        target_chapters=100,
        completed_chapters=100,
        current_finale_budget=12,
    )

    assert result["source"] == SOURCE_FALLBACK
    assert result["continue_block"] == propose_continue_block(
        target_chapters=100, completed_chapters=100
    )


@pytest.mark.asyncio
async def test_partial_proposal_is_treated_as_unassessed(monkeypatch):
    """只给了一部分数字 ⇒ 当「没给全」⇒ 回落公式（不许拼凑）。"""
    svc = _service(
        monkeypatch,
        json.dumps(
            {"continue_block": 24, "finale_budget": 12, "reason": "只说了两个"},
            ensure_ascii=False,
        ),
    )

    result = await svc.propose(
        novel_id="n-1",
        target_chapters=100,
        completed_chapters=100,
        current_finale_budget=12,
    )

    assert result["source"] == SOURCE_FALLBACK


def test_prompt_key_and_package_are_registered():
    """新节点 key 必须登记 + 有内置包（否则 seed 同步不到，调用必失败）。"""
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_TARGET_BUDGET_PROPOSAL
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_TARGET_BUDGET_PROPOSAL in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_TARGET_BUDGET_PROPOSAL in package_ids


@pytest.mark.asyncio
async def test_http_endpoint_surfaces_the_proposal_source(monkeypatch):
    """出口守卫：HTTP 端点必须把**来源**透出去（读端能分辨 ai / fallback）。"""
    from interfaces.api.v1.core.novels import get_target_budget_proposal

    captured: dict = {}

    async def _fake_propose(**kwargs):
        captured.update(kwargs)
        return {
            "continue_target": 124,
            "continue_block": 24,
            "finale_budget": 12,
            "finale_budget_min": 8,
            "finale_budget_max": 18,
            "source": SOURCE_AI,
            "reason": "主线还剩两处伏笔未回收",
        }

    monkeypatch.setattr(
        "interfaces.api.v1.core.novels._propose_target_budget", _fake_propose
    )

    novel = SimpleNamespace(
        title="测试书",
        genre="武侠",
        target_chapters=100,
        generation_prefs=SimpleNamespace(finale_chapter_budget=12),
    )
    service = SimpleNamespace(get_novel=lambda _nid: novel)

    payload = await get_target_budget_proposal("n-1", service)

    assert payload["source"] == SOURCE_AI
    assert payload["continue_target"] == 124
    assert payload["finale_budget_min"] == 8
    assert payload["target_chapters_max"] > 0
    # 端点真的把「本书目标 / 已完成 / 当前终局预算」送进了提案
    assert captured["target_chapters"] == 100
    assert captured["current_finale_budget"] == 12
