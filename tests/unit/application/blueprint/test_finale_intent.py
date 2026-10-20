"""B-07 / B-13：终局意图通道的行为守卫。

改造前
------
1. 幕级 brief 里的收束口径是**代码写死的两段创作文案**：收束分支的
   「本幕必须完成结局：核心冲突正面了结……最后一章必须落在结局而非新悬念上」，
   以及非终局分支的对应文案。它们每次幕级规划与每次建幕都会进模型 ——
   创作判断被写成了常量。
2. 「进入终局」这件事只写进 ``novel.generation_prefs.finale_mode``，
   **全仓无人读** ⇒ 终局章与普通章走同一条管线，收尾质量无人负责。
3. ``anti_ai_finale`` 节点挂着 ``is_final_chapter`` 输入端口却无人供给 ——
   但整个 DAG 执行层早在 2026-10-17 就被删除，给它接线等于修死代码。

改造后
------
· 本幕方向由 AI 判定并写下（``planning-finale-intent`` 节点），代码只留结构事实；
  模型没给方向就是没有，**代码绝不代写**。
· 方向落库到幕节点 metadata，章节级终局注入经 ``chapter_node.parent_id`` 读它。

全部是行为断言：不碰源码文本 —— 断言的是产物内容与调用链，
不是「源码里没有这几个字」。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from application.blueprint.services.finale_intent_contract import (
    SOURCE_AI,
    SOURCE_NONE,
    normalize_finale_intent,
)
from domain.structure.story_node import NodeType, StoryNode


# ======================================================================
#  替身
# ======================================================================


class _TreeRepo:
    def __init__(self, nodes):
        self._nodes = list(nodes)
        self.updated = []

    def get_tree_sync(self, novel_id):
        return SimpleNamespace(novel_id=novel_id, nodes=self._nodes)

    def get_tree(self, novel_id):
        return SimpleNamespace(novel_id=novel_id, nodes=self._nodes)

    async def update(self, node):
        self.updated.append(node)
        return node


class _FakeLLM:
    """只实现 FinaleIntentService 用到的那一个方法。"""

    def __init__(self, payload: str):
        self._payload = payload
        self.calls = []

    async def generate(self, prompt, config):
        self.calls.append(prompt)
        return SimpleNamespace(content=self._payload)


def _act(number, *, novel_id="n-1", title=None, metadata=None):
    return StoryNode(
        id=f"act-{number}",
        novel_id=novel_id,
        parent_id="vol-1",
        node_type=NodeType.ACT,
        number=number,
        title=title if title is not None else f"第{number}幕标题",
        order_index=number,
        metadata=dict(metadata or {}),
    )


def _svc(nodes, llm_payload='{"is_finale_act": true, "act_direction": "AI 写的方向"}'):
    repo = _TreeRepo(nodes)
    svc = ContinuousPlanningService(
        story_node_repo=repo,
        chapter_element_repo=Mock(),
        llm_service=_FakeLLM(llm_payload),
    )
    return svc, repo


# ======================================================================
#  A. 归一化：兜底标记而不编造
# ======================================================================


def test_normalize_marks_ai_source_only_when_flag_and_direction_both_present():
    intent = normalize_finale_intent(
        {"is_finale_act": True, "act_direction": "本幕了结主线"}
    )
    assert intent.source == SOURCE_AI
    assert intent.is_finale_act is True
    assert intent.act_direction == "本幕了结主线"
    assert intent.is_usable is True


def test_normalize_never_invents_direction():
    """模型没给方向 ⇒ 方向为空串。**代码不许补一句像样的收束方案**。"""
    intent = normalize_finale_intent({"is_finale_act": True})
    assert intent.act_direction == ""
    assert intent.is_usable is False


def test_normalize_treats_string_boolean_as_undecided():
    """``"true"`` 是含糊输出，不是明确结论 —— 不得读成 True。"""
    intent = normalize_finale_intent(
        {"is_finale_act": "true", "act_direction": "方向"}
    )
    assert intent.is_finale_act is None
    assert intent.source == SOURCE_NONE


def test_normalize_drops_non_string_direction():
    """非字符串方向一律丢弃 —— 不 str() 转成假文本。"""
    intent = normalize_finale_intent({"is_finale_act": True, "act_direction": 123})
    assert intent.act_direction == ""


def test_normalize_non_dict_payload_is_none_source():
    for payload in (None, [], "文本", 7):
        intent = normalize_finale_intent(payload)
        assert intent.source == SOURCE_NONE
        assert intent.is_finale_act is None
        assert intent.act_direction == ""


# ======================================================================
#  B. 代码代写的创作文案必须从产物里消失
# ======================================================================


_CODE_WRITTEN_CREATIVE_PHRASES = (
    "必须完成结局",
    "核心冲突正面了结",
    "尘埃落定",
    "留出推进空间",
    "提前解决全书主线",
    "不要再埋新的大悬念",
)


def test_act_budget_context_brief_carries_no_code_authored_creative_text():
    """收束 brief 里只剩结构事实。

    旧实现把两段创作文案写死在 brief 里，每次幕级规划与每次建幕都进模型。
    """
    svc, _ = _svc([_act(1), _act(2), _act(3)])

    final_brief = svc._act_budget_context("n-1", 3)["brief"]
    middle_brief = svc._act_budget_context("n-1", 2)["brief"]

    for brief in (final_brief, middle_brief):
        for phrase in _CODE_WRITTEN_CREATIVE_PHRASES:
            assert phrase not in brief, f"brief 里仍有代码代写的创作内容：{phrase!r}"

    # 结构事实与标签仍在
    assert "【全书收束】" in final_brief
    assert "【全书进度】" in middle_brief
    assert "全书" in final_brief


def test_act_budget_context_injects_ai_direction():
    svc, _ = _svc([_act(1), _act(2), _act(3)])

    brief = svc._act_budget_context(
        "n-1", 3, direction="本幕了结主线，结尾落在城门落锁"
    )["brief"]

    assert "本幕了结主线，结尾落在城门落锁" in brief
    assert "【全书收束】" in brief


def test_act_budget_context_without_direction_has_no_direction_section():
    svc, _ = _svc([_act(1), _act(2), _act(3)])

    brief = svc._act_budget_context("n-1", 3)["brief"]

    assert "本幕创作方向" not in brief


# ======================================================================
#  C. 方向真的进了幕级提示词
# ======================================================================


def test_act_planning_prompt_carries_ai_direction():
    svc, _ = _svc([_act(1), _act(2), _act(3)])

    prompt = svc._build_act_planning_prompt(
        _act(3, title="落幕"),
        {},
        None,
        8,
        direction="本幕了结主线，结尾落在城门落锁",
    )

    assert "本幕了结主线，结尾落在城门落锁" in str(prompt)


# ======================================================================
#  D. 方向落库（有读取方 ⇒ 不是死字段）
# ======================================================================


async def test_act_direction_is_persisted_on_the_act_node():
    svc, repo = _svc(
        [_act(1), _act(2), _act(3)],
        llm_payload=json.dumps(
            {"is_finale_act": True, "act_direction": "本幕了结主线", "reason": "最后一幕"}
        ),
    )
    act = _act(3, title="落幕")

    intent = await svc._resolve_act_direction(
        novel_id="n-1",
        act_number=3,
        act_title="落幕",
        act_description="潮爷身份揭破",
        previous_summary="前两幕",
        store_node=act,
    )

    assert intent.is_finale_act is True
    assert intent.act_direction == "本幕了结主线"
    assert act.metadata["finale_intent"] == "本幕了结主线"
    assert act.metadata["ai_is_finale_act"] is True
    assert act.metadata["finale_intent_source"] == SOURCE_AI
    assert repo.updated, "方向落库必须有写入动作（否则字段是死的）"


async def test_undecided_direction_is_stored_as_none_not_false():
    """AI 没判定 ⇒ 落库的是 null，不是 false —— 两者必须可分辨。"""
    svc, _ = _svc([_act(3)], llm_payload=json.dumps({"reason": "信息不足"}))
    act = _act(3)

    await svc._resolve_act_direction(novel_id="n-1", act_number=3, store_node=act)

    assert act.metadata["ai_is_finale_act"] is None
    assert act.metadata["finale_intent"] == ""
    assert act.metadata["finale_intent_source"] == SOURCE_NONE


async def test_ai_judgement_conflicting_with_structure_is_recorded_not_silent():
    """AI 说该收束、但结构上不是最后一幕 ⇒ 必须留痕，不许悄悄改结构。"""
    svc, _ = _svc(
        [_act(1), _act(2), _act(3)],
        llm_payload=json.dumps(
            {"is_finale_act": True, "act_direction": "这一幕就收束"}
        ),
    )

    intent = await svc._resolve_act_direction(novel_id="n-1", act_number=2)

    # 结构口径没被 AI 改掉：第二幕不是结构收束幕
    assert intent.is_finale_act is True, "AI 的判定必须被保留（它决定方向口径）"
    budget = svc._act_budget_context("n-1", 2)
    assert budget["is_final_act"] is False, "结构收束幕口径不得被 AI 判定改写"


# ======================================================================
#  E. B-13：终局事实进章节级规划
# ======================================================================


def test_preplan_contract_declares_finale_directive():
    from infrastructure.ai.prompt_contracts.continuous_planning import (
        PLANNING_CHAPTER_PREPLAN_CONTRACT,
    )

    fields = PLANNING_CHAPTER_PREPLAN_CONTRACT.variables_schema.model_fields
    assert "finale_directive" in fields
    assert fields["finale_directive"].default == ""


def test_preplan_prompt_renders_finale_directive_only_when_present():
    from infrastructure.ai.prompt_contracts.continuous_planning import (
        PLANNING_CHAPTER_PREPLAN_CONTRACT,
    )
    from infrastructure.ai.prompt_gateway import get_prompt_gateway

    def _render(directive):
        rendered = get_prompt_gateway().render(
            PLANNING_CHAPTER_PREPLAN_CONTRACT,
            {
                "chapter_number": 120,
                "chapter_title": "最后一章",
                "act_chapter_plan": "{}",
                "continuity_context": "（无）",
                "pacing_directive": "",
                "finale_directive": directive,
            },
        )
        return str(rendered.prompt)

    with_directive = _render("【终局阶段】全书目标章数已达成，当前处于终局阶段。")
    without = _render("")

    assert "终局阶段" in with_directive
    assert "终局阶段" not in without, "空串时整段必须不渲染（行为逐位一致）"


def _ctx(*, finale_mode: bool, act_direction: str = ""):
    from engine.pipeline.base import PipelineContext

    novel = SimpleNamespace(
        generation_prefs=SimpleNamespace(finale_mode=finale_mode),
    )
    act = _act(3, metadata={"finale_intent": act_direction} if act_direction else {})

    class _Repo:
        async def get_by_id(self, node_id):
            return act if node_id == act.id else None

    ctx = PipelineContext(novel_id="n-1", chapter_number=120)
    ctx.inject(
        novel_repository=SimpleNamespace(get_by_id=lambda nid: novel),
        story_node_repo=_Repo(),
    )
    ctx.chapter_node = SimpleNamespace(parent_id=act.id)
    return ctx


async def test_finale_directive_is_empty_when_not_in_finale_mode():
    """没进终局 ⇒ 空串 ⇒ 模板整段不渲染，行为与改造前逐位一致。"""
    from engine.pipeline.base import _resolve_finale_directive

    directive = await _resolve_finale_directive(
        _ctx(finale_mode=False, act_direction="本幕了结主线")
    )

    assert directive == ""


async def test_finale_directive_carries_the_finale_fact_and_the_ai_direction():
    from engine.pipeline.base import _resolve_finale_directive

    directive = await _resolve_finale_directive(
        _ctx(finale_mode=True, act_direction="本幕了结主线，结尾落在城门落锁")
    )

    assert "终局阶段" in directive
    assert "本幕了结主线，结尾落在城门落锁" in directive


async def test_finale_directive_has_no_direction_when_the_act_has_none():
    """幕上没落库方向 ⇒ 只有事实，没有方向 —— 不许代写。"""
    from engine.pipeline.base import _resolve_finale_directive

    directive = await _resolve_finale_directive(_ctx(finale_mode=True))

    assert "终局阶段" in directive
    assert "本幕创作方向" not in directive


# ======================================================================
#  F. 通道本身存在（防止「建了节点没接线」）
# ======================================================================


def test_cpms_node_and_profile_exist():
    from infrastructure.ai.generation_profiles import generation_config_from_profile
    from infrastructure.ai.prompt_manager import PromptManager

    node = PromptManager().get_node("planning-finale-intent")
    assert node is not None, "planning-finale-intent 节点没进 CPMS"

    assert generation_config_from_profile("finale_intent") is not None


def test_bundle_version_matches_the_changelog_head():
    """bundle 版本号必须与 changelog 首条对得上。

    早先这条守卫把版本号**写死**成某个字面量，结果下一次 bump 就把它顶红
    —— 它守的是「改了 prompt 包要 bump」，不是「版本号等于某个字面量」。
    改成「changelog 首条与版本号一致」之后，任何一次 bump 都不会误红，
    而「版本号改了却没在 changelog 里记」仍会被抓住。

    「改了包却忘了 bump」这件事单测守不住（它需要一个基线版本），
    那是提交前的检查，记账在 ledger。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[4]
    meta = (
        root / "infrastructure/ai/prompt_packages/bundle_meta.yaml"
    ).read_bytes().decode("utf-8")
    version_line = [ln for ln in meta.split("\n") if ln.startswith("version:")][0]
    version = version_line.split(":", 1)[1].strip().strip("'\"")

    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    assert match, f"bundle 版本号格式不对：{version_line}"
    parsed = tuple(int(x) for x in match.groups())
    assert parsed >= (5, 14, 0), f"bundle 版本低于本轮起点：{version_line}"

    changelog_line = [ln for ln in meta.split("\n") if ln.startswith("changelog:")][0]
    head_text = changelog_line.split("changelog:", 1)[1].lstrip(" '\"")
    head_version = re.match(r"^v(\d+)\.(\d+)\.(\d+)", head_text)
    assert head_version, f"changelog 首条没有版本号：{head_text[:60]}"
    assert tuple(int(x) for x in head_version.groups()) == parsed, (
        f"changelog 首条版本与 version 不一致：{version_line} vs {head_text[:60]}"
    )