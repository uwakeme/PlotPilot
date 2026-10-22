"""C-07：全书首幕由 AI 决定（不可用时落代码兜底，且**如实**标来源）。

改造前：daemon 动态补首幕时，标题/描述/建议章数**全部代码写死**
（「第一幕 · 开端」「故事起始，建立世界观与主角目标」+ 公式章数）——
创作判断被写成了常量（登记表 C-07）。

本文件钉住：
- AI 给出 title + description ⇒ 用它，落库来源 ``AI_MACRO`` / ``CONFIRMED``；
- AI 不可用 / 缺字段 / 输出不合契约 ⇒ 落**代码兜底**文案，
  且落库来源保持 ``DRAFT`` / ``MANUAL``（**不**把兜底文案标成「AI 规划好的」）；
- 章数：模型没给 ⇒ 退到代码兜底值；
- **接线守卫**：守护进程的首幕分支必须走服务（不得再自己拼占位文案）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from domain.structure.story_node import PlanningSource, PlanningStatus


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

    def __init__(self):
        self.variables = {}

    def render(self, contract, variables):
        self.variables = dict(variables)
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"), node_key=contract.node_key
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


def _svc(monkeypatch, content: str) -> ContinuousPlanningService:
    import application.blueprint.services.first_act_planner as planner_mod

    gateway = _FakeGateway()
    monkeypatch.setattr(planner_mod, "get_prompt_gateway", lambda: gateway)

    repo = Mock()
    repo.get_by_id = AsyncMock(
        return_value=SimpleNamespace(
            id="vol-1", title="第一卷", node_type=SimpleNamespace(value="volume")
        )
    )
    repo.save = AsyncMock()

    novel_repo = Mock()
    novel_repo.get_by_id = Mock(
        return_value=SimpleNamespace(
            title="测试书", genre="武侠", premise="一个不该出现的人找上门"
        )
    )

    return ContinuousPlanningService(
        story_node_repo=repo,
        chapter_element_repo=Mock(),
        llm_service=_StubLLM(content),
        bible_service=Mock(**{"get_bible_by_novel.return_value": None}),
        novel_repository=novel_repo,
    )


def _payload(**overrides) -> str:
    data = {
        "title": "雨夜叩门",
        "description": "主角在雨夜客栈被一个不该出现的人找上门",
        "suggested_chapter_count": 6,
        "reason": "梗概里的核心冲突要在第一幕就露面",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.asyncio
async def test_ai_decides_the_first_act(monkeypatch):
    """AI 给出首幕 ⇒ 落库用 AI 的标题/描述，来源标成 AI 规划。"""
    svc = _svc(monkeypatch, _payload())

    result = await svc.create_first_act_auto(
        novel_id="n-1", parent_volume_id="vol-1", fallback_chapter_count=8
    )

    node = result["next_act"]
    assert result["source"] == "ai"
    assert node["title"] == "雨夜叩门"
    assert node["description"] == "主角在雨夜客栈被一个不该出现的人找上门"
    assert node["suggested_chapter_count"] == 6
    assert node["metadata"]["first_act_source"] == "ai"
    # 来源必须**如实**标成 AI 规划（内容确实是 AI 写的）
    assert node["planning_source"] == PlanningSource.AI_MACRO.value
    assert node["planning_status"] == PlanningStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_ai_unavailable_falls_back_with_honest_source(monkeypatch):
    """AI 不可用 ⇒ 落代码兜底文案，且来源**如实**是 DRAFT / MANUAL。"""
    import application.blueprint.services.first_act_planner as planner_mod

    class _BoomLLM:
        async def generate(self, prompt, config):
            raise RuntimeError("endpoint down")

    svc = _svc(monkeypatch, _payload())
    svc.llm_service = _BoomLLM()
    monkeypatch.setattr(planner_mod, "get_prompt_gateway", lambda: _FakeGateway())

    result = await svc.create_first_act_auto(
        novel_id="n-1", parent_volume_id="vol-1", fallback_chapter_count=8
    )

    node = result["next_act"]
    assert result["source"] == "fallback"
    assert node["title"] == "第一幕 · 开端"
    assert node["planning_status"] == PlanningStatus.DRAFT.value
    assert node["planning_source"] == PlanningSource.MANUAL.value
    assert node["metadata"]["first_act_source"] == "fallback"


@pytest.mark.asyncio
async def test_missing_title_or_description_is_not_accepted(monkeypatch):
    """缺 title / description ⇒ 整次作废（不许用占位文案冒充创作内容）。"""
    svc = _svc(monkeypatch, _payload(title=""))

    result = await svc.create_first_act_auto(
        novel_id="n-1", parent_volume_id="vol-1", fallback_chapter_count=8
    )

    assert result["source"] == "fallback"
    assert result["next_act"]["title"] == "第一幕 · 开端"


@pytest.mark.asyncio
async def test_chapter_count_falls_back_when_model_gives_none(monkeypatch):
    """模型没给章数 ⇒ 退到代码兜底值（章数是预算，不是创作内容）。"""
    svc = _svc(monkeypatch, _payload(suggested_chapter_count=None))

    result = await svc.create_first_act_auto(
        novel_id="n-1", parent_volume_id="vol-1", fallback_chapter_count=8
    )

    assert result["source"] == "ai"
    assert result["next_act"]["suggested_chapter_count"] == 8


@pytest.mark.asyncio
async def test_non_integer_chapter_count_is_rejected(monkeypatch):
    """模型给字符串章数 ⇒ 契约拒收 ⇒ 走兜底（不许把 "6" 静默当成 6）。"""
    svc = _svc(monkeypatch, _payload(suggested_chapter_count="6"))

    result = await svc.create_first_act_auto(
        novel_id="n-1", parent_volume_id="vol-1", fallback_chapter_count=8
    )

    assert result["source"] == "fallback"


def test_delegate_no_longer_hardcodes_the_first_act():
    """接线守卫：守护进程的首幕分支**不许**再自己拼占位文案。

    ⚠ 用 **AST 判字符串常量 / 调用名**，不用源码 `contains` ——
    文件里的说明注释会提到这个占位文案和这个函数名，
    文本匹配会**命中自己的注释** ⇒ 恒绿或恒红。
    """
    import ast
    from pathlib import Path

    import engine.runtime.act_planning_delegate as delegate_mod

    source = Path(delegate_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "第一幕 · 开端" not in literals, (
        "守护进程里还写死了首幕文案（创作判断被代码代写）"
    )

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name:
                called.add(name)
    assert "create_first_act_auto" in called, (
        "守护进程的首幕分支必须走服务（AI 决定）"
    )


def test_prompt_key_and_package_are_registered():
    """新节点 key 必须登记 + 有内置包（否则 seed 同步不到，调用必失败）。"""
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_FIRST_ACT
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_FIRST_ACT in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_FIRST_ACT in package_ids
