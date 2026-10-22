"""E-06：题材节奏配额交 AI 判 + 修「张力配额在生产上到不了模型」的断链。

改造前
------
「本章该多紧、怎么写」是**代码代做**的：

- **黄金三章配额表**：第 1 章 ≥65「首章铁律」/ 第 2 章 ≥55 / 第 3 章 ≥60
  —— 一整套写死的创作指令 + 目标数字；
- **按前章张力的四档**：≤30 紧急升温 / ≤45 逐步升温 / ≥80 缓冲 / ≥65 维持
  —— 档位与目标张力区间也全是常量。

而且**它根本没到模型**：
``engine/pipeline/base.py`` 只从 bundle 里取 ``context`` / ``context_tokens`` /
``voice_anchors`` / ``hard_facts`` ⇒ ``plot_tension``（预设期望 + 前章实际 + 调整指令）
**算完即丢**。

改造后
------
· 新 CPMS 节点 ``planning-rhythm-quota``：AI 依题材/本章位置/前章实际张力
  给出**目标张力 + 节奏档 + 一句具体指令**；
· 代码表作**回落**（文案与目标数字**逐位一致**），来源可分辨（``rule``）；
· 配额文本经**独立变量** ``rhythm_quota`` 进生产正文提示词 ——
  但**受开关控制**（``rhythm_quota.enabled`` 默认**关闭**）⇒
  **行为与改造前逐位一致**（突然激活一个从未生效的硬约束会一次性改变全书节奏观）。
"""
from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.engine.services.rhythm_quota import (
    EARLY_CHAPTER_QUOTAS,
    RHYTHMS,
    SOURCE_AI,
    SOURCE_NONE,
    SOURCE_RULE,
    RhythmQuota,
    code_fallback_quota,
    normalize_quota,
    rhythm_quota_delivery_enabled,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROSE_PROMPT_REL = (
    "infrastructure/ai/prompt_packages/nodes/chapter-prose-generation"
)
WF_REL = "application/workflows/auto_novel_generation_workflow.py"
BASE_REL = "engine/pipeline/base.py"
COMPOSER_REL = "engine/pipeline/prose_composer.py"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


# ======================================================================
#  A. 代码表回落：逐位一致
# ======================================================================


def test_the_early_chapter_quota_is_unchanged():
    """黄金三章配额表：目标数字与文案**逐位一致**（改造前写死的那套）。"""
    assert EARLY_CHAPTER_QUOTAS[1][0] == 65
    assert "【首章铁律】" in EARLY_CHAPTER_QUOTAS[1][2]
    assert "综合张力目标：≥65。" in EARLY_CHAPTER_QUOTAS[1][2]

    assert EARLY_CHAPTER_QUOTAS[2][0] == 55
    assert "【次章加速】" in EARLY_CHAPTER_QUOTAS[2][2]
    assert "综合张力目标：≥55。" in EARLY_CHAPTER_QUOTAS[2][2]

    assert EARLY_CHAPTER_QUOTAS[3][0] == 60
    assert "【三章定乾坤】" in EARLY_CHAPTER_QUOTAS[3][2]
    assert "综合张力目标：≥60。" in EARLY_CHAPTER_QUOTAS[3][2]


def test_the_four_buckets_are_unchanged():
    """按前章张力的四档：文案与目标**逐位一致**。

    ⚠ 断言**完整句**（只断言前缀「⚠ 紧急指令」的话，改掉后半句照样绿 ——
    变异 M2 就这么漏过一次）。
    """
    urgent = code_fallback_quota(10, 30)
    assert urgent.instruction == (
        "⚠ 紧急指令：前章张力严重不足！本章必须制造至少一次核心冲突/反转/悬念，"
        "将综合张力拉升到 55 以上。建议：引入新威胁、暴露隐藏信息、"
        "让角色做出痛苦选择。"
    )

    rising = code_fallback_quota(10, 45)
    assert rising.instruction == (
        "⚡ 调整指令：前章张力偏低，读者可能正在流失。"
        "本章应逐步升温——增加信息不对称、加深角色矛盾、"
        "让阻碍变得更加紧迫。目标张力：50-65。"
    )

    cooling = code_fallback_quota(10, 80)
    assert cooling.instruction == (
        "📊 缓冲指令：前章已是高潮，本章应给读者喘息空间。"
        "可以写角色消化冲击、盟友互动、新线索浮现，"
        "但结尾要留一个钩子暗示更大的风暴即将到来。目标张力：40-55。"
    )

    holding = code_fallback_quota(10, 65)
    assert holding.instruction == (
        "📊 维持指令：前章张力较高，本章可以保持高压推进，"
        "也可以适当喘息后再次攀升。避免连续高压导致读者疲劳。"
    )


def test_the_fallback_is_sourced_as_rule():
    """代码表回落 ⇒ 来源是 ``rule``（可分辨）。"""
    assert code_fallback_quota(1, None).source == SOURCE_RULE
    assert code_fallback_quota(10, 30).source == SOURCE_RULE


def test_no_quota_when_nothing_is_known():
    """不在前三章且前章张力未评估 ⇒ **无配额**（不编）。"""
    quota = code_fallback_quota(10, None)

    assert quota.source == SOURCE_NONE
    assert quota.instruction == ""
    assert quota.target_tension is None


def test_the_middle_band_has_no_code_verdict():
    """45 < 前章张力 < 65 ⇒ 代码表**没有结论** ⇒ 不编（改造前也没有）。"""
    quota = code_fallback_quota(10, 55)

    assert quota.source == SOURCE_NONE
    assert quota.instruction == ""


# ======================================================================
#  B. AI 判定的校验
# ======================================================================


def test_a_valid_ai_quota_is_accepted():
    """合法判定 ⇒ 三个字段都留下，来源 ``ai``。"""
    quota = normalize_quota(
        {
            "target_tension": 68,
            "rhythm": "升温",
            "instruction": "综合张力目标：≥68。本章要让质疑先爆发再反打。",
            "reason": "前章偏低",
        }
    )

    assert quota.source == SOURCE_AI
    assert quota.target_tension == 68
    assert quota.rhythm == "升温"
    assert "综合张力目标" in quota.instruction


def test_the_rhythm_must_be_in_the_closed_enum():
    """节奏档必须在封闭枚举里。"""
    quota = normalize_quota(
        {"target_tension": 60, "rhythm": "随便", "instruction": "x"}
    )

    assert quota.source == SOURCE_NONE


def test_the_target_tension_must_be_an_int_in_range():
    """目标张力必须是 0-100 的**整数**。"""
    for bad in (-1, 101, 60.5, "60", True):
        quota = normalize_quota(
            {"target_tension": bad, "rhythm": "升温", "instruction": "x"}
        )
        assert quota.source == SOURCE_NONE, f"目标张力 {bad!r} 不该被接受"


def test_an_empty_instruction_is_rejected():
    """指令为空 ⇒ 未评估（不许给一个没有内容的判定）。"""
    quota = normalize_quota({"target_tension": 60, "rhythm": "升温", "instruction": "  "})

    assert quota.source == SOURCE_NONE


def test_a_non_dict_is_unassessed():
    """形状不对 ⇒ 未评估。"""
    assert normalize_quota(None).source == SOURCE_NONE
    assert normalize_quota({"target_tension": 60}).source == SOURCE_NONE


# ======================================================================
#  C. 交付开关：默认关闭 ⇒ 行为逐位不变
# ======================================================================


def test_the_delivery_is_off_by_default():
    """默认**不送进**正文提示词（那套配额从来没到过模型）。"""
    import yaml

    data = yaml.safe_load(_read("config/performance.yaml"))
    section = data.get("rhythm_quota") or {}

    assert section.get("enabled") is False
    assert rhythm_quota_delivery_enabled() is False


def test_the_delivery_text_is_empty_when_the_switch_is_off():
    """开关关闭 ⇒ 交付文本为空（提示词里整段不渲染）。"""
    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow,
    )

    quota = RhythmQuota(
        target_tension=68, rhythm="升温", instruction="综合张力目标：≥68", source=SOURCE_AI
    )

    assert AutoNovelGenerationWorkflow._rhythm_quota_delivery_text(quota) == ""


# ======================================================================
#  D. 接线：AI 优先 / 代码回落 / 真的到模型
# ======================================================================


def test_the_workflow_prefers_the_ai_quota():
    """给了 AI 的配额 ⇒ 用它（不回落代码表）。"""
    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow,
    )

    wf = AutoNovelGenerationWorkflow.__new__(AutoNovelGenerationWorkflow)
    ai_quota = RhythmQuota(
        target_tension=71, rhythm="爆发", instruction="AI 定的指令", source=SOURCE_AI
    )
    wf._get_prev_actual_tension = lambda *a, **k: None

    assert wf._resolve_rhythm_quota("n-1", 10, ai_quota) is ai_quota


def test_the_workflow_falls_back_to_the_code_table():
    """没有 AI 配额 ⇒ 回落代码表（与改造前逐位一致）。"""
    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow,
    )

    wf = AutoNovelGenerationWorkflow.__new__(AutoNovelGenerationWorkflow)
    wf._get_prev_actual_tension = lambda *a, **k: {"tension": 30.0}

    quota = wf._resolve_rhythm_quota("n-1", 10, None)

    assert quota.source == SOURCE_RULE
    assert "⚠ 紧急指令" in quota.instruction


def test_the_quota_reaches_the_prose_prompt():
    """**接线守卫**：bundle → ctx → composer → 正文提示词变量，三段都得在。

    ⚠ AST 判定（不判文本）。
    """
    base_src = ast.parse(_read(BASE_REL))
    assigns = False
    passes = False
    for node in ast.walk(base_src):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "rhythm_quota":
                    assigns = True
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "rhythm_quota":
                    passes = True
    assert assigns, "管线没把 bundle 里的 rhythm_quota 放进 ctx"
    assert passes, "管线没把 rhythm_quota 传给 composer ⇒ 它停在半路"


def test_the_composer_sends_it_as_its_own_variable():
    """节奏配额要**独立成变量**（它不是「硬事实」，不能塞进 chapter_context）。"""
    composer_src = ast.parse(_read(COMPOSER_REL))
    keys = set()
    for node in ast.walk(composer_src):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    assert "rhythm_quota" in keys


def test_the_prompt_package_and_template_carry_the_variable():
    """提示词包声明该变量 + 模板真的渲染它。"""
    package = _read(f"{PROSE_PROMPT_REL}/package.yaml")
    user = _read(f"{PROSE_PROMPT_REL}/user.md")

    assert "rhythm_quota" in package
    assert "{rhythm_quota}" in user
    assert "{% if rhythm_quota %}" in user


def test_the_node_is_registered():
    """新节点 key 必须登记 + 有内置包。"""
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_RHYTHM_QUOTA
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_RHYTHM_QUOTA in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_RHYTHM_QUOTA in package_ids


@pytest.mark.asyncio
async def test_the_planner_uses_the_ai_verdict(monkeypatch):
    """规划器：AI 的判定能落地（走真实契约校验）。

    ⚠ 第 3 批复审 S13：**开关关着就不该打模型**。
    此前 `rhythm_quota.enabled` 只管「文本送不送进正文提示词」，
    规划器照样**付费调一次模型**、拿回来的结果再被丢掉。
    ⇒ 现在规划器也看这个开关 ⇒ 本用例必须**把开关打开**
    （否则它测的是「开关关着时的行为」，不是「AI 的判定能落地」）。
    """
    import application.engine.services.rhythm_quota_planner as planner_mod

    monkeypatch.setattr(
        planner_mod, "rhythm_quota_delivery_enabled", lambda: True
    )

    class _FakeGateway:
        def render(self, contract, variables):
            return SimpleNamespace(
                prompt=SimpleNamespace(system="sys", user="usr"),
                node_key=contract.node_key,
            )

        def validate_output(self, contract, payload):
            return contract.output_schema.model_validate(payload)

    monkeypatch.setattr(planner_mod, "get_prompt_gateway", lambda: _FakeGateway())

    llm = Mock()
    llm.generate = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(
                {
                    "target_tension": 72,
                    "rhythm": "爆发",
                    "instruction": "综合张力目标：≥72。本章要有一次决定性反击。",
                    "reason": "题材是都市异能，前章偏低",
                },
                ensure_ascii=False,
            )
        )
    )

    quota = await planner_mod.RhythmQuotaPlanner(llm).plan(
        genre="都市异能", chapter_number=4, prev_tension=41.0, arc_tension=60
    )

    assert quota.source == SOURCE_AI
    assert quota.target_tension == 72
    assert quota.rhythm == "爆发"
    assert "决定性反击" in quota.instruction


@pytest.mark.asyncio
async def test_the_planner_does_not_call_the_model_when_the_switch_is_off(monkeypatch):
    """开关关着 ⇒ **模型一次都不许被调用**（第 3 批复审 S13）。

    此前只在**投递侧**看开关 ⇒ 开关关着仍然付费调一次模型、
    拿回来的结果再被丢掉 ⇒ 白花钱。
    """
    import application.engine.services.rhythm_quota_planner as planner_mod

    monkeypatch.setattr(planner_mod, "rhythm_quota_delivery_enabled", lambda: False)

    llm = Mock()
    llm.generate = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(
                {
                    "target_tension": 72,
                    "rhythm": "爆发",
                    "instruction": "综合张力目标：≥72。",
                    "reason": "不该被调用",
                },
                ensure_ascii=False,
            )
        )
    )

    quota = await planner_mod.RhythmQuotaPlanner(llm).plan(
        genre="都市异能", chapter_number=4, prev_tension=41.0, arc_tension=60
    )

    assert llm.generate.await_count == 0, "开关关着仍然调了模型"
    assert quota.source == SOURCE_NONE


@pytest.mark.asyncio
async def test_the_planner_is_unassessed_when_the_llm_fails(monkeypatch):
    """LLM 挂了 ⇒ 未评估（调用方回落代码表）。"""
    import application.engine.services.rhythm_quota_planner as planner_mod

    monkeypatch.setattr(
        planner_mod, "get_prompt_gateway", lambda: SimpleNamespace(render=None, validate_output=None)
    )
    llm = Mock()
    llm.generate = AsyncMock(side_effect=RuntimeError("upstream 502"))

    quota = await planner_mod.RhythmQuotaPlanner(llm).plan(
        genre="武侠", chapter_number=2, prev_tension=None, arc_tension=None
    )

    assert quota.source == SOURCE_NONE
    assert quota.instruction == ""
