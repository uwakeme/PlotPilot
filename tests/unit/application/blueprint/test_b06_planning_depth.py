"""B-06：规划深度档位由 **AI 自评**（章数上限仍由代码夹紧）。

改造前
------
规划深度是**目标章数的纯函数**：

- ``>500`` → ``framework``（只铺到部/卷）
- ``>100`` → ``partial``
- 否则 → ``full``（铺到幕）

而深度决定**要求模型输出的结构精细度**（三套 JSON schema）
⇒ 档位既不是创作判断，也没法按这本书的实际形态调。

改造后
------
· 新 CPMS 节点 ``planning-depth-assessment``：AI 看题材 / 梗概 / 目标章数，
  自评「这本书的结构要铺到多细」；
· **硬约束保留**：章数给出的档位变成**上限**（能到多细）——
  **不许**选比上限更细的档（那会让输出规模随章数一起炸），
  被夹时 ``clamped=True`` ⇒ **不许静默**；
· AI 不可用 ⇒ 用章数阈值那档（与改造前**逐位一致**），来源 ``threshold``。
"""
from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.planning_depth import (
    DEPTH_FRAMEWORK,
    DEPTH_FULL,
    DEPTH_PARTIAL,
    SOURCE_AI,
    SOURCE_THRESHOLD,
    depth_cap_for,
    resolve_planning_depth,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
DELEGATE_REL = "engine/runtime/macro_planning_delegate.py"


def _svc():
    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    return ContinuousPlanningService(
        story_node_repo=Mock(), chapter_element_repo=Mock(), llm_service=Mock()
    )


# ======================================================================
#  A. 口径层
# ======================================================================


def test_the_chapter_cap_is_unchanged():
    """章数上限与改造前的阈值**逐位一致**（只是语义从「档位」变成「上限」）。"""
    assert depth_cap_for(501) == DEPTH_FRAMEWORK
    assert depth_cap_for(101) == DEPTH_PARTIAL
    assert depth_cap_for(100) == DEPTH_FULL
    assert depth_cap_for(5) == DEPTH_FULL


def test_a_finer_request_is_clamped():
    """请求比上限**更细** ⇒ 夹回上限，并标出被夹过。"""
    resolved = resolve_planning_depth(600, DEPTH_FULL)

    assert resolved.depth == DEPTH_FRAMEWORK
    assert resolved.clamped is True
    assert resolved.source == SOURCE_AI


def test_a_coarser_request_is_kept():
    """请求比上限**更粗** ⇒ 保留（更省输出，不是安全风险）。"""
    resolved = resolve_planning_depth(5, DEPTH_FRAMEWORK)

    assert resolved.depth == DEPTH_FRAMEWORK
    assert resolved.clamped is False
    assert resolved.source == SOURCE_AI


def test_no_request_uses_the_threshold():
    """没给 ⇒ 用章数阈值那档（与改造前**逐位一致**）。"""
    resolved = resolve_planning_depth(5)

    assert resolved.depth == DEPTH_FULL
    assert resolved.source == SOURCE_THRESHOLD
    assert resolved.clamped is False


def test_an_invalid_request_falls_back():
    """给了非法档位 ⇒ 回落阈值那档（不编）。"""
    resolved = resolve_planning_depth(5, "ultra_deep")

    assert resolved.depth == DEPTH_FULL
    assert resolved.source == SOURCE_THRESHOLD


# ======================================================================
#  B. 服务层
# ======================================================================


def test_the_service_uses_the_ai_depth():
    """服务用 AI 判的档位（更粗的那一档）。"""
    svc = _svc()

    variables = svc.build_quick_macro_variables({}, 5, ai_depth=DEPTH_FRAMEWORK)

    assert variables["planning_depth"] == DEPTH_FRAMEWORK


def test_the_service_clamps_a_finer_ai_depth():
    """服务夹住比上限更细的 AI 判定。"""
    svc = _svc()

    variables = svc.build_quick_macro_variables({}, 600, ai_depth=DEPTH_FULL)

    assert variables["planning_depth"] == DEPTH_FRAMEWORK


def test_the_service_keeps_the_old_behaviour_without_ai():
    """没有 AI 判定 ⇒ 行为与改造前**逐位一致**。"""
    svc = _svc()

    assert svc.build_quick_macro_variables({}, 5)["planning_depth"] == DEPTH_FULL
    assert svc.build_quick_macro_variables({}, 200)["planning_depth"] == DEPTH_PARTIAL
    assert svc.build_quick_macro_variables({}, 900)["planning_depth"] == DEPTH_FRAMEWORK


# ======================================================================
#  C. AI 层
# ======================================================================


class _FakeGateway:
    def render(self, contract, variables):
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"),
            node_key=contract.node_key,
        )

    def validate_output(self, contract, payload):
        return contract.output_schema.model_validate(payload)


@pytest.fixture
def stub_gateway(monkeypatch):
    import application.engine.services.planning_depth_assessor as mod

    gateway = _FakeGateway()
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: gateway)
    return gateway


@pytest.mark.asyncio
async def test_the_assessor_uses_the_ai_verdict(stub_gateway):
    """AI 判的档位能落地。"""
    from application.engine.services.planning_depth_assessor import (
        PlanningDepthAssessor,
    )

    llm = Mock()
    llm.generate = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(
                {"recommended_depth": DEPTH_PARTIAL, "reason": "双线叙事"},
                ensure_ascii=False,
            )
        )
    )

    depth = await PlanningDepthAssessor(llm).assess(
        genre="悬疑", premise="一桩旧案", target_chapters=80
    )

    assert depth == DEPTH_PARTIAL


@pytest.mark.asyncio
async def test_the_assessor_is_unassessed_when_the_llm_fails(stub_gateway):
    """LLM 挂了 ⇒ 未评估（调用方回落章数阈值）。"""
    from application.engine.services.planning_depth_assessor import (
        PlanningDepthAssessor,
    )

    llm = Mock()
    llm.generate = AsyncMock(side_effect=RuntimeError("upstream 502"))

    depth = await PlanningDepthAssessor(llm).assess(
        genre="武侠", premise="复仇", target_chapters=300
    )

    assert depth is None


@pytest.mark.asyncio
async def test_the_assessor_rejects_an_invalid_depth(stub_gateway):
    """AI 给了非法档位 ⇒ 未评估（不编）。"""
    from application.engine.services.planning_depth_assessor import (
        PlanningDepthAssessor,
    )

    llm = Mock()
    llm.generate = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(
                {"recommended_depth": "ultra", "reason": ""}, ensure_ascii=False
            )
        )
    )

    depth = await PlanningDepthAssessor(llm).assess(
        genre="武侠", premise="复仇", target_chapters=300
    )

    assert depth is None


# ======================================================================
#  D. 接线 + 节点登记
# ======================================================================


def test_the_delegate_calls_the_assessor_and_forwards_the_result():
    """守护进程要**真的**调 AI 自评，并把结果下传给变量构造（AST 判调用）。"""
    src = (REPO_ROOT / DELEGATE_REL).read_bytes().decode("utf-8")
    tree = ast.parse(src)

    called_assessor = False
    forwards = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name == "_assess_planning_depth":
            called_assessor = True
        if name == "build_quick_macro_variables":
            for kw in node.keywords:
                if kw.arg == "ai_depth":
                    forwards = True
    assert called_assessor, "守护进程没调 AI 深度自评"
    assert forwards, "AI 的档位没下传给变量构造"


def test_the_node_is_registered():
    """新节点 key 必须登记 + 有内置包。"""
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_DEPTH_ASSESSMENT
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_DEPTH_ASSESSMENT in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_DEPTH_ASSESSMENT in package_ids
