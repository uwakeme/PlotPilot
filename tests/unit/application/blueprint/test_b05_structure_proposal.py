"""B-05：结构网格（部/卷/幕的**数量**）由 **AI 提案**，不再由代码公式铺死。

改造前
------
「精密」模式的结构网格是**代码/前端定的**：``StructurePreference`` 默认 3×3×3
⇒ ``_build_precise_structure_skeleton`` 按 parts×vpp×apv 铺死整棵树，
提示词还双重锁定它（「结构网格绝对服从…不得新增、删除、合并或拆分节点」
+「你的任务只是为固定节点填写标题、描述和幕级字段」）。

⇒ 「这本书该分几部、每部几卷、每卷几幕」这个**创作判断**被写成了一条常量；
而精密模式在生产上**根本没有前端入口**（SSE 路由固定传
``structure_preference=None`` ⇒ 走的是快捷模式）。

现在
----
AI 先**提案**网格（看题材、梗概、目标章数），代码**夹紧**提案，
精密模式按这个网格填充内容 ⇒ 最后由用户在规划弹窗里**确认**后落库。

不变量（登记表 B-05「代码保留的不变量」）
----------------------------------------
- **结构深度上限**：三层各自的层数 ∈ [1, 10]；
- **防无限嵌套**：幕数总数 ≤ 目标章数（每幕至少 1 章）；
- 被夹过时 ``clamped=True``，**不许静默**。

降级纪律
--------
AI 不可用 / 输出不合契约 / 没给出可用数字 ⇒ **未评估**
⇒ 回落**代码兜底网格**（3×3×3，与改造前**逐位一致**）。
"""
from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.structure_proposal import (
    FALLBACK_GRID,
    MAX_ACTS_PER_VOLUME,
    MAX_PARTS,
    MAX_VOLUMES_PER_PART,
    SOURCE_AI,
    SOURCE_FALLBACK,
    SOURCE_NONE,
    StructureProposalService,
    clamp_structure_grid,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SERVICE_REL = "application/blueprint/services/continuous_planning_service.py"
ROUTES_REL = "interfaces/api/v1/blueprint/continuous_planning_routes.py"
PRECISE_SYSTEM = (
    REPO_ROOT
    / "infrastructure/ai/prompt_packages/nodes/planning-precise-macro/system.md"
)
PRECISE_USER = (
    REPO_ROOT
    / "infrastructure/ai/prompt_packages/nodes/planning-precise-macro/user.md"
)
FRONTEND = REPO_ROOT / "frontend/src"


def _read(path: pathlib.Path) -> str:
    return path.read_bytes().decode("utf-8")


# =====================================================================
# 一、夹紧：登记表的两条不变量
# =====================================================================


def test_the_grid_is_clamped_to_the_depth_limit():
    """结构深度上限：三层各自的层数 ∈ [1, 10]。"""
    proposal = clamp_structure_grid(
        {"parts": 50, "volumes_per_part": 50, "acts_per_volume": 50},
        target_chapters=100000,
    )

    assert proposal.parts == 10
    assert proposal.volumes_per_part == 10
    assert proposal.acts_per_volume == 10
    assert proposal.clamped is True, "被夹过必须说出口，不许静默"


def test_clamping_is_reported_even_when_only_one_layer_is_out_of_range():
    """⚠ 隔离用例：只有「部数」超限时，别的分支不该顺手把 clamped 设上。

    否则「夹紧过没有说出口」这条守卫会被**另一条分支**挡住
    （三个数都超限时，任一分支设了 clamped 都算过）。
    """
    proposal = clamp_structure_grid(
        {"parts": 50, "volumes_per_part": 3, "acts_per_volume": 3},
        target_chapters=100000,
    )

    assert proposal.parts == 10
    assert proposal.volumes_per_part == 3
    assert proposal.acts_per_volume == 3
    assert proposal.clamped is True, "夹紧过必须说出口"


def test_the_grid_never_exceeds_the_target_chapters():
    """防无限嵌套：幕数总数 ≤ 目标章数（每幕至少 1 章）。"""
    proposal = clamp_structure_grid(
        {"parts": 10, "volumes_per_part": 10, "acts_per_volume": 10},
        target_chapters=50,
    )

    assert proposal.total_acts <= 50
    assert proposal.clamped is True


def test_a_reasonable_proposal_is_kept_untouched():
    proposal = clamp_structure_grid(
        {"parts": 3, "volumes_per_part": 4, "acts_per_volume": 5, "reason": "三条主线"},
        target_chapters=300,
    )

    assert (proposal.parts, proposal.volumes_per_part, proposal.acts_per_volume) == (3, 4, 5)
    assert proposal.source == SOURCE_AI
    assert proposal.clamped is False
    assert proposal.reason == "三条主线"


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"parts": 3},
        {"parts": "三", "volumes_per_part": 4, "acts_per_volume": 5},
        {"parts": True, "volumes_per_part": 4, "acts_per_volume": 5},
        "不是对象",
    ],
)
def test_an_unusable_proposal_falls_back_to_the_code_grid(raw):
    """模型没给出**三个**可用数字 ⇒ 未评估 ⇒ 回落代码兜底（3×3×3）。"""
    proposal = clamp_structure_grid(raw, target_chapters=300)

    assert (
        proposal.parts,
        proposal.volumes_per_part,
        proposal.acts_per_volume,
    ) == FALLBACK_GRID
    assert proposal.source == SOURCE_FALLBACK


# =====================================================================
# 二、AI 通道
# =====================================================================


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


def _service(monkeypatch, content: str):
    import application.blueprint.services.structure_proposal as mod

    gateway = _FakeGateway()
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: gateway)
    return StructureProposalService(_StubLLM(content)), gateway


@pytest.mark.asyncio
async def test_the_service_returns_the_proposed_grid(monkeypatch):
    svc, gateway = _service(
        monkeypatch,
        json.dumps(
            {"parts": 3, "volumes_per_part": 4, "acts_per_volume": 5, "reason": "三条主线"},
            ensure_ascii=False,
        ),
    )

    proposal = await svc.propose(novel_id="n-1", target_chapters=300)

    assert (proposal.parts, proposal.volumes_per_part, proposal.acts_per_volume) == (3, 4, 5)
    assert proposal.source == SOURCE_AI
    # 上限要进提示词（模型也得知道边界）
    assert gateway.variables["max_parts"] == "10"
    assert gateway.variables["target_chapters"] == "300"


@pytest.mark.asyncio
async def test_the_service_reports_not_assessed_when_the_call_fails(monkeypatch):
    import application.blueprint.services.structure_proposal as mod

    class _BoomLLM:
        async def generate(self, prompt, config):
            raise RuntimeError("endpoint down")

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway())
    svc = StructureProposalService(_BoomLLM())

    proposal = await svc.propose(novel_id="n-1", target_chapters=300)

    assert proposal.source == SOURCE_NONE
    assert (
        proposal.parts,
        proposal.volumes_per_part,
        proposal.acts_per_volume,
    ) == FALLBACK_GRID


@pytest.mark.asyncio
async def test_the_service_rejects_a_non_numeric_grid(monkeypatch):
    """模型答「三」⇒ 契约拒收 ⇒ 未评估（不许把文字当数字）。"""
    svc, _ = _service(
        monkeypatch,
        json.dumps(
            {"parts": "三", "volumes_per_part": 4, "acts_per_volume": 5}, ensure_ascii=False
        ),
    )

    proposal = await svc.propose(novel_id="n-1", target_chapters=300)

    assert proposal.source == SOURCE_NONE


@pytest.mark.asyncio
async def test_the_service_rejects_a_boolean_grid_number(monkeypatch):
    """⚠ pydantic 的 lax 模式会把 ``True`` 读成 ``1`` —— 那不是模型说的部数。"""
    svc, _ = _service(
        monkeypatch,
        json.dumps(
            {"parts": True, "volumes_per_part": 4, "acts_per_volume": 5}, ensure_ascii=False
        ),
    )

    proposal = await svc.propose(novel_id="n-1", target_chapters=300)

    assert proposal.source == SOURCE_NONE


# =====================================================================
# 三、接线：精密模式走 AI 提案；快捷模式不变
# =====================================================================


def _called_names(source: str, func_name: str) -> set[str]:
    tree = ast.parse(source)
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                if name:
                    out.add(name)
    return out


def test_the_precise_path_routes_through_the_proposal():
    """接线守卫：精密模式的网格必须经过 AI 提案（不许直接用公式网格）。"""
    source = _read(REPO_ROOT / SERVICE_REL)
    names = _called_names(source, "generate_macro_plan")

    assert "_propose_structure_grid" in names, "精密模式必须先让 AI 提案网格"


def test_the_quick_mode_is_untouched():
    """快捷模式（``structure_preference=None`` 且不提案）仍然走原路径。"""
    source = _read(REPO_ROOT / SERVICE_REL)
    names = _called_names(source, "generate_macro_plan")

    assert "_build_macro_planning_prompt" in names
    assert "_generate_precise_macro_plan" in names


def test_the_sse_route_exposes_the_precise_mode():
    """UI 契约：SSE 路由要能切到精密模式（否则精密模式仍无前端入口）。"""
    source = _read(REPO_ROOT / ROUTES_REL)
    tree = ast.parse(source)

    mode_params = []
    propose_flags = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            args = node.args
            for arg in list(args.args) + list(args.kwonlyargs):
                if arg.arg == "mode":
                    mode_params.append(node.name)
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "generate_macro_plan":
                for kw in node.keywords:
                    if kw.arg == "propose_structure":
                        propose_flags.append(
                            ast.unparse(kw.value)
                            if hasattr(ast, "unparse")
                            else ""
                        )
    assert "stream_macro_plan_sse" in mode_params, "SSE 路由没有 mode 参数"
    assert any("precise" in flag for flag in propose_flags), (
        "SSE 路由没有把 precise 模式转成 propose_structure"
    )


def test_the_merge_does_not_silently_drop_unknown_nodes():
    """未知 node_id 不许静默丢弃（模型可能以为网格是自己定的）。"""
    source = _read(REPO_ROOT / SERVICE_REL)
    names = _called_names(source, "_merge_precise_structure_updates")

    assert "_apply_precise_node_update" in names
    body = source.split("def _merge_precise_structure_updates")[1][:3000]
    assert "丢弃未知节点" in body, "合并未知节点时必须留下告警（不许静默丢弃）"


# =====================================================================
# 四、提示词契约：网格的来源改了
# =====================================================================


def test_the_precise_prompt_says_the_grid_is_ai_proposed():
    """提示词要讲清「网格由你提议」，否则模型仍以为网格是外部强加的。"""
    system_text = _read(PRECISE_SYSTEM)
    user_text = _read(PRECISE_USER)

    assert "由你依据" in system_text
    assert "你提议的数量" in user_text


# =====================================================================
# 五、前端：模式切换 + 请求不再钉死网格
# =====================================================================


def test_the_frontend_can_choose_the_precise_mode():
    planning = _read(FRONTEND / "api/planning.ts")
    modal = _read(FRONTEND / "components/workbench/MacroPlanModal.vue")

    assert "mode?: 'quick' | 'precise'" in planning
    assert "macro/stream?mode=" in planning
    assert "n-radio-group" in modal
    assert "planMode" in modal


def test_the_request_no_longer_pins_the_grid():
    """请求里的 structure 变成**可选**（此前默认 3×3×3 = 代码替用户做决定）。"""
    routes = _read(REPO_ROOT / ROUTES_REL)
    planning = _read(FRONTEND / "api/planning.ts")

    assert "structure: Optional[StructurePreference] = None" in routes
    assert "structure?: StructurePreference" in planning


# =====================================================================
# 六、注册
# =====================================================================


def test_the_node_and_profile_are_registered():
    from infrastructure.ai.generation_profiles import generation_config_from_profile
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_STRUCTURE_PROPOSAL
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_STRUCTURE_PROPOSAL in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_STRUCTURE_PROPOSAL in package_ids
    assert generation_config_from_profile("structure_proposal") is not None


def test_the_structure_limits_are_pinned_by_value():
    """常量本身的值必须被钉住（否则上面的字面量断言会在常量改动时假红）。"""
    assert (MAX_PARTS, MAX_VOLUMES_PER_PART, MAX_ACTS_PER_VOLUME) == (10, 10, 10)
