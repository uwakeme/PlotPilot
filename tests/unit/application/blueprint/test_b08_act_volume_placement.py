"""B-08：建幕时由 **AI 声明**这一幕属于哪一卷。

改造前，「换卷时机」是**代码按幕数计数**的规则
（``daemon_host._find_parent_volume_for_new_act``：锚点卷幕数达到
``rec_acts_per_volume`` 才换卷，只向后不回填）—— 创作判断被写成了计数规则。

现在这一判断交回 AI（``planning-act-volume-placement``），但**两条不变量仍由
代码保证**：**卷号单调、不回填**（登记表 B-08「代码保留的不变量」列）。

本文件钉住：

- AI 声明的卷号落在允许集合内 ⇒ 用它。⚠ 用例刻意造出「代码策略会选另一卷」
  的局面，否则分不清是 AI 定的还是代码定的（守卫没有牙齿）；
- AI 声明越界（更早的卷）⇒ **拒收**，回落代码策略，且留痕写清「谁说了什么、
  为什么被拒」；
- AI 不可用 / 判不出来 ⇒ **未评估**（不许回落成某个卷），回落代码策略
  （与改造前逐位一致）；
- 首幕不问 AI（首幕的父卷是结构决定的，不是「换卷时机」）；
- 落库留痕：这一幕为什么挂在这卷，在库里可查、来源可分辨。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.act_volume_placement_contract import (
    REJECT_NOT_ALLOWED,
    REJECT_NOT_ASSESSED,
    SOURCE_AI,
    SOURCE_FALLBACK,
    SOURCE_NONE,
    VolumePlacement,
    allowed_volume_numbers,
    normalize_volume_placement,
    resolve_placed_volume_number,
    resolve_volume_anchor,
)
from engine.runtime import act_planning_delegate
from engine.runtime.daemon_host import DaemonHostMixin


def _vol(num: int, **extra):
    return SimpleNamespace(
        id=f"vol-{num}",
        number=num,
        title=f"第{num}卷",
        parent_id="part-1",
        node_type=SimpleNamespace(value="volume"),
        **extra,
    )


def _act(num: int, parent_id: str, **extra):
    return SimpleNamespace(
        id=f"act-{num}",
        number=num,
        parent_id=parent_id,
        node_type=SimpleNamespace(value="act"),
        **extra,
    )


def _novel():
    novel = Mock()
    novel.novel_id.value = "n-1"
    return novel


def _host(placement=None, *, boom: bool = False):
    """宿主替身：真实代码选卷策略 + 可控的 AI 声明通道。"""
    host = Mock()
    host._find_parent_volume_for_new_act = Mock(
        side_effect=lambda **kw: DaemonHostMixin._find_parent_volume_for_new_act(host, **kw)
    )
    if boom:
        host.planning_service.declare_act_volume = AsyncMock(
            side_effect=RuntimeError("endpoint down")
        )
    else:
        host.planning_service.declare_act_volume = AsyncMock(return_value=placement)
    return host


async def _resolve(host, volumes, acts, rec_acts_per_volume=3):
    return await act_planning_delegate._resolve_parent_volume_for_new_act(
        host,
        _novel(),
        volume_nodes=volumes,
        act_nodes=acts,
        rec_acts_per_volume=rec_acts_per_volume,
    )


# =====================================================================
# 一、代码保留的不变量（纯函数层）
# =====================================================================


def test_allowed_numbers_exclude_volumes_before_the_anchor():
    """锚点在卷3 ⇒ 只允许卷3 及之后（卷号单调 + 不回填）。"""
    volumes = [_vol(1), _vol(2), _vol(3), _vol(4)]
    acts = [_act(1, "vol-3")]

    assert allowed_volume_numbers(volumes, acts) == [3, 4]


def test_allowed_numbers_are_all_volumes_when_no_act_exists_yet():
    """还没有幕 ⇒ 没有锚点 ⇒ 允许全部卷号（与代码策略同口径）。"""
    volumes = [_vol(1), _vol(2)]

    assert allowed_volume_numbers(volumes, []) == [1, 2]


def test_allowed_numbers_are_all_volumes_when_anchor_is_dangling():
    """最新幕的父卷不在卷列表（脏数据）⇒ 不单调约束，允许全部。"""
    volumes = [_vol(1), _vol(2)]
    acts = [_act(9, "vol-ghost")]

    assert allowed_volume_numbers(volumes, acts) == [1, 2]


def test_placing_an_earlier_volume_is_rejected():
    """卷号单调/不回填：声明更早的卷 ⇒ 拒收（不是夹紧，是拒收）。"""
    effective, reason = resolve_placed_volume_number(1, [3, 4])

    assert effective is None
    assert reason == REJECT_NOT_ALLOWED


def test_placing_the_anchor_or_a_later_volume_is_accepted():
    effective, reason = resolve_placed_volume_number(3, [3, 4])

    assert effective == 3
    assert reason == ""


def test_not_assessed_is_distinguishable_from_rejected():
    """「AI 没判出来」与「AI 判了但越界」必须可分辨。"""
    assert resolve_placed_volume_number(None, [1, 2]) == (None, REJECT_NOT_ASSESSED)
    assert resolve_placed_volume_number(9, [1, 2]) == (None, REJECT_NOT_ALLOWED)


def test_anchor_is_the_volume_of_the_latest_act():
    volumes = [_vol(1), _vol(2)]
    acts = [_act(1, "vol-1"), _act(2, "vol-2")]

    anchor = resolve_volume_anchor(volumes, acts)

    assert anchor.anchor.id == "vol-2"


def test_anchor_is_the_first_volume_when_there_is_no_act():
    volumes = [_vol(1), _vol(2)]

    anchor = resolve_volume_anchor(volumes, [])

    assert anchor.anchor.id == "vol-1"


def test_anchor_is_none_when_the_parent_volume_is_missing():
    """父卷被删等脏数据 ⇒ 锚点为空（且要能分辨是脏数据，不是「还没有幕」）。"""
    volumes = [_vol(1)]
    acts = [_act(9, "vol-ghost")]

    anchor = resolve_volume_anchor(volumes, acts)

    assert anchor.anchor is None
    assert anchor.reason == "dangling"


@pytest.mark.parametrize(
    "raw",
    [
        {"volume_number": True},
        {"volume_number": 0},
        {"volume_number": -2},
        {"volume_number": "第二卷"},
        {"volume_number": None},
        {"reason": "只给了依据"},
    ],
)
def test_a_non_numeric_volume_is_not_assessed(raw):
    """含糊 / 非法卷号 ⇒ **未评估**，不许猜一个卷出来。"""
    placement = normalize_volume_placement(raw)

    assert placement.declared_volume_number is None
    assert placement.source == SOURCE_NONE
    assert placement.is_assessed is False


def test_a_numeric_volume_is_assessed_with_its_reason():
    placement = normalize_volume_placement({"volume_number": 2, "reason": "新阶段"})

    assert placement.declared_volume_number == 2
    assert placement.reason == "新阶段"
    assert placement.source == SOURCE_AI
    assert placement.is_assessed is True


# =====================================================================
# 二、AI 通道（服务层）
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
    import application.blueprint.services.act_volume_placement_service as mod

    gateway = _FakeGateway()
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: gateway)
    return mod.ActVolumePlacementService(_StubLLM(content)), gateway


@pytest.mark.asyncio
async def test_the_service_returns_the_declared_volume(monkeypatch):
    svc, _ = _service(monkeypatch, json.dumps({"volume_number": 2, "reason": "新阶段"}))

    placement = await svc.declare(novel_id="n-1", allowed_volume_numbers="2、3")

    assert placement.declared_volume_number == 2
    assert placement.source == SOURCE_AI


@pytest.mark.asyncio
async def test_the_service_passes_the_allowed_list_into_the_prompt(monkeypatch):
    """允许卷号必须**进提示词**（否则 AI 根本不知道不能选哪几卷）。"""
    svc, gateway = _service(
        monkeypatch, json.dumps({"volume_number": 2, "reason": "新阶段"})
    )

    await svc.declare(novel_id="n-1", allowed_volume_numbers="2、3")

    assert gateway.variables["allowed_volume_numbers"] == "2、3"


@pytest.mark.asyncio
async def test_the_service_reports_not_assessed_when_the_call_fails(monkeypatch):
    import application.blueprint.services.act_volume_placement_service as mod

    class _BoomLLM:
        async def generate(self, prompt, config):
            raise RuntimeError("endpoint down")

    gateway = _FakeGateway()
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: gateway)
    svc = mod.ActVolumePlacementService(_BoomLLM())

    placement = await svc.declare(novel_id="n-1")

    assert placement.declared_volume_number is None
    assert placement.source == SOURCE_NONE


@pytest.mark.asyncio
async def test_the_service_rejects_a_non_json_answer(monkeypatch):
    svc, _ = _service(monkeypatch, "第二卷")

    placement = await svc.declare(novel_id="n-1")

    assert placement.declared_volume_number is None
    assert placement.source == SOURCE_NONE


@pytest.mark.asyncio
async def test_the_service_rejects_a_non_numeric_volume(monkeypatch):
    """模型答「第二卷」⇒ 契约拒收 ⇒ 未评估（不许把含糊读成明确结论）。"""
    svc, _ = _service(monkeypatch, json.dumps({"volume_number": "第二卷"}))

    placement = await svc.declare(novel_id="n-1")

    assert placement.declared_volume_number is None
    assert placement.source == SOURCE_NONE


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [True, 2.0])
async def test_the_service_rejects_a_volume_that_is_not_a_plain_integer(
    monkeypatch, payload
):
    """pydantic 的 lax 模式会把 ``True`` 读成 ``1``、把 ``2.0`` 读成 ``2``。

    契约必须显式拒收：那不是模型说的卷号，是类型系统的顺手一猜。
    """
    svc, _ = _service(monkeypatch, json.dumps({"volume_number": payload}))

    placement = await svc.declare(novel_id="n-1")

    assert placement.declared_volume_number is None
    assert placement.source == SOURCE_NONE


# =====================================================================
# 三、接线：AI 声明优先，代码策略兜底
# =====================================================================


@pytest.mark.asyncio
async def test_ai_can_move_the_act_to_a_later_volume():
    """AI 说这一幕属于卷3 ⇒ 挂到卷3。

    ⚠ 隔离用例：此刻代码策略会**留守卷2**（锚点卷 1 幕 < 观测节奏 3），
    所以选到卷3 只可能是 AI 定的 —— 否则这条用例分不清谁做的决定。
    """
    volumes = [_vol(1), _vol(2), _vol(3)]
    acts = [_act(1, "vol-1"), _act(2, "vol-1"), _act(3, "vol-1"), _act(4, "vol-2")]
    host = _host(VolumePlacement(3, "开启新阶段", SOURCE_AI))

    chosen, meta = await _resolve(host, volumes, acts)

    assert chosen.id == "vol-3"
    assert meta["volume_placement_source"] == SOURCE_AI
    assert meta["volume_placement_volume"] == 3
    assert meta["volume_placement_declared"] == 3


@pytest.mark.asyncio
async def test_ai_can_keep_the_act_in_the_anchor_volume():
    """AI 说这一幕仍属于卷1 ⇒ 挂到卷1。

    ⚠ 隔离用例：此刻代码策略会**换到卷2**（锚点卷已满），
    所以选到卷1 只可能是 AI 定的。
    """
    volumes = [_vol(1), _vol(2)]
    acts = [_act(1, "vol-1")]
    host = _host(VolumePlacement(1, "还在同一条线上", SOURCE_AI))

    chosen, meta = await _resolve(host, volumes, acts)

    assert chosen.id == "vol-1"
    assert meta["volume_placement_source"] == SOURCE_AI


@pytest.mark.asyncio
async def test_an_earlier_volume_is_rejected_and_falls_back():
    """AI 声明更早的卷（违反不回填）⇒ 拒收 ⇒ 回落代码策略，且留痕写清原因。"""
    volumes = [_vol(1), _vol(2), _vol(3)]
    acts = [_act(1, "vol-1"), _act(2, "vol-3")]
    host = _host(VolumePlacement(1, "想回填", SOURCE_AI))

    chosen, meta = await _resolve(host, volumes, acts)

    assert chosen.id == "vol-3", "越界的卷号绝不能被采纳"
    assert meta["volume_placement_source"] == SOURCE_FALLBACK
    assert meta["volume_placement_declared"] == 1, "AI 说了什么必须留痕"
    assert meta["volume_placement_reject_reason"] == REJECT_NOT_ALLOWED


@pytest.mark.asyncio
async def test_unavailable_ai_falls_back_to_the_code_strategy():
    """AI 不可用 ⇒ 未评估 ⇒ 回落代码策略（与改造前逐位一致）。"""
    volumes = [_vol(1), _vol(2)]
    acts = [_act(1, "vol-1")]
    host = _host(boom=True)

    chosen, meta = await _resolve(host, volumes, acts)

    assert chosen.id == "vol-2", "与改造前的代码选卷结果一致"
    assert meta["volume_placement_source"] == SOURCE_FALLBACK
    assert meta["volume_placement_declared"] is None
    assert meta["volume_placement_reject_reason"] == REJECT_NOT_ASSESSED


@pytest.mark.asyncio
async def test_the_first_act_does_not_ask_the_ai():
    """首幕不问 AI：它的父卷由结构决定（第一卷），不是「换卷时机」。"""
    volumes = [_vol(1), _vol(2)]
    host = _host(VolumePlacement(2, "跳卷", SOURCE_AI))

    chosen, meta = await _resolve(host, volumes, [])

    host.planning_service.declare_act_volume.assert_not_called()
    assert chosen.id == "vol-1"
    assert meta["volume_placement_source"] == SOURCE_FALLBACK


# =====================================================================
# 四、真实服务方法 + 端到端建幕接线
# =====================================================================


@pytest.mark.asyncio
async def test_the_planning_service_builds_the_context_for_the_ai(monkeypatch):
    """真实服务方法必须把**卷况**与**允许卷号**喂给 AI。

    ⚠ delegate 那侧的用例把 ``declare_act_volume`` 打桩了 ——
    这里补的是「真方法」这一层，否则它写错了也没人发现。
    """
    import application.blueprint.services.act_volume_placement_service as svc_mod
    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    gateway = _FakeGateway()
    monkeypatch.setattr(svc_mod, "get_prompt_gateway", lambda: gateway)

    novel_repo = Mock()
    novel_repo.get_by_id = Mock(
        return_value=SimpleNamespace(
            title="测试书",
            premise="一个不该出现的人找上门",
            generation_prefs=SimpleNamespace(locked_genre="武侠"),
        )
    )

    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=_StubLLM(json.dumps({"volume_number": 3, "reason": "新阶段"})),
        novel_repository=novel_repo,
    )

    volumes = [_vol(1), _vol(2), _vol(3)]
    acts = [_act(1, "vol-2")]

    placement = await svc.declare_act_volume(
        novel_id="n-1", volume_nodes=volumes, act_nodes=acts, observed_acts_per_volume=3
    )

    assert placement.declared_volume_number == 3
    assert placement.source == SOURCE_AI
    # 锚点在卷2 ⇒ 允许卷号 = 2、3
    assert gateway.variables["allowed_volume_numbers"] == "2、3"
    assert "第3卷" in gateway.variables["volume_overview"]
    assert "不许回到更早的卷" in gateway.variables["anchor_note"]
    assert "第1幕" in gateway.variables["last_act"]
    assert gateway.variables["genre"] == "武侠"


@pytest.mark.asyncio
async def test_the_new_act_is_created_under_the_ai_declared_volume(monkeypatch):
    """端到端：AI 声明卷3 ⇒ 新幕真的建在卷3 下，且留痕随建幕一起落库。"""
    from domain.novel.entities.novel import NovelStage

    volumes = [_vol(1), _vol(2), _vol(3)]
    acts = [_act(1, "vol-1"), _act(2, "vol-1"), _act(3, "vol-1"), _act(4, "vol-2")]
    nodes = volumes + acts

    host = Mock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=nodes)
    host.story_node_repo.get_children_sync.return_value = []
    host._find_parent_volume_for_new_act = Mock(
        side_effect=lambda **kw: DaemonHostMixin._find_parent_volume_for_new_act(host, **kw)
    )
    host.planning_service.declare_act_volume = AsyncMock(
        return_value=VolumePlacement(3, "开启新阶段", SOURCE_AI)
    )
    create_next_act = AsyncMock(
        return_value={"success": True, "next_act": {"parent_id": "vol-3", "number": 5}}
    )
    host.planning_service.create_next_act_auto = create_next_act

    novel = Mock()
    novel.novel_id.value = "n-1"
    novel.current_act = 4
    novel.target_chapters = 300
    novel.current_stage = NovelStage.ACT_PLANNING

    monkeypatch.setattr(act_planning_delegate, "_read_shared_state", lambda _nid: {})

    await act_planning_delegate.run_act_planning(host, novel)

    create_next_act.assert_awaited_once()
    kwargs = create_next_act.await_args.kwargs
    assert kwargs["parent_volume_id"] == "vol-3"
    assert kwargs["placement"]["volume_placement_source"] == SOURCE_AI
    assert kwargs["placement"]["volume_placement_volume"] == 3


# =====================================================================
# 五、落库留痕与接线结构
# =====================================================================


@pytest.mark.asyncio
async def test_the_placement_is_stored_with_the_new_act(monkeypatch):
    """挂载留痕随新幕落库 ⇒ 「这一幕为什么挂在这卷」在库里可查。"""
    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    repo = Mock()
    repo.get_by_id = AsyncMock(
        side_effect=lambda node_id: SimpleNamespace(
            id=node_id,
            number=5,
            title="上一幕",
            description="",
            parent_id="vol-1",
            order_index=4,
            node_type=SimpleNamespace(value="act"),
        )
    )
    repo.save = AsyncMock()

    svc = ContinuousPlanningService(
        story_node_repo=repo,
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )

    async def _fake_next_act_info(*args, **kwargs):
        return {"title": "新幕", "description": "新阶段开始", "suggested_chapter_count": 6}

    monkeypatch.setattr(svc, "_generate_next_act_info", _fake_next_act_info)

    await svc.create_next_act_auto(
        novel_id="n-1",
        current_act_id="act-5",
        parent_volume_id="vol-2",
        placement={
            "volume_placement_source": SOURCE_AI,
            "volume_placement_declared": 2,
            "volume_placement_reason": "开启新阶段",
            "volume_placement_reject_reason": "",
            "volume_placement_volume": 2,
        },
    )

    saved = repo.save.await_args.args[0]
    assert saved.metadata["volume_placement_source"] == SOURCE_AI
    assert saved.metadata["volume_placement_declared"] == 2
    assert saved.metadata["volume_placement_reason"] == "开启新阶段"
    assert saved.metadata["volume_placement_volume"] == 2


def test_the_delegate_routes_through_the_ai_placement():
    """接线守卫：选卷必须经过 AI 声明通道，代码策略只许当兜底。

    ⚠ 用 **AST 判调用名**，不用源码 ``contains`` —— 文件里的说明注释会提到
    这些函数名，文本匹配会**命中自己的注释** ⇒ 恒绿或恒红。
    """
    import ast
    from pathlib import Path

    source = Path(act_planning_delegate.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    callers: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                if name:
                    names.add(name)
        callers[node.name] = names

    assert "_resolve_parent_volume_for_new_act" in callers.get("run_act_planning", set()), (
        "run_act_planning 必须走 AI 声明通道"
    )
    code_strategy_callers = {
        fn for fn, names in callers.items() if "_find_parent_volume_for_new_act" in names
    }
    assert code_strategy_callers == {"_resolve_parent_volume_for_new_act"}, (
        "代码选卷策略只许出现在兜底路径里，不许在 run_act_planning 里被直接调用"
    )


def test_the_node_and_profile_are_registered():
    """新节点 key 必须登记 + 有内置包 + 有生成档位（否则 seed 同步不到，调用必失败）。"""
    from infrastructure.ai.generation_profiles import generation_config_from_profile
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_ACT_VOLUME_PLACEMENT
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_ACT_VOLUME_PLACEMENT in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_ACT_VOLUME_PLACEMENT in package_ids
    assert generation_config_from_profile("act_volume_placement") is not None
