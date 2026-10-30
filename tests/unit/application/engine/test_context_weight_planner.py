"""D-02 上游规划调用：契约完整性 + 三态降级链。

红线
----
**降级路径不得返回看起来正常的权重。** 全套默认值（如「全 0.5」）会被读成
「AI 看过本章，认为各槽位同等重要」，与「没问过」不是一回事。
所以本模块所有失败分支都必须返回 ``None``。

这几条是登记表第九节「不可交给 AI 的红线」的同类：
判断不出来 ≠ 判断为平均。
"""
import json

import pytest

from application.engine.services.context_weight_catalog import (
    SLOT_CATALOG,
    catalog_lines,
    sanitize_weights,
)
from application.engine.services.context_weight_planner import ContextWeightPlanner
from application.engine.services.context_weight_schema import (
    ContextWeightsLlmPayload,
)
from infrastructure.ai.prompt_contracts.context_weights import (
    CONTEXT_WEIGHTS_CONTRACT,
)
from infrastructure.ai.prompt_keys import PLANNING_CONTEXT_WEIGHTS


# ── 契约完整性 ──────────────────────────────────────────────────────────

def test_node_key_is_registered():
    """契约 key 必须先在 prompt_keys 注册，否则 validate_key 会拒绝。"""
    from infrastructure.ai.prompt_keys import is_valid_key

    assert PLANNING_CONTEXT_WEIGHTS == "planning-context-weights"
    assert is_valid_key(PLANNING_CONTEXT_WEIGHTS)


def test_contract_wires_schema_and_profile():
    assert CONTEXT_WEIGHTS_CONTRACT.node_key == PLANNING_CONTEXT_WEIGHTS
    assert CONTEXT_WEIGHTS_CONTRACT.output_schema is ContextWeightsLlmPayload
    assert CONTEXT_WEIGHTS_CONTRACT.generation_profile == "context_weights"


def test_generation_profile_exists():
    """profile 必须在 generation_profiles.yaml 里真的存在。"""
    from infrastructure.ai.generation_profiles import generation_config_from_profile

    cfg = generation_config_from_profile("context_weights")
    assert cfg is not None


def test_prompt_package_is_present():
    """包目录必须存在，否则网关渲染时才发现 —— 那已经在热路径上了。"""
    from infrastructure.ai.prompt_gateway import get_prompt_gateway

    rendered = get_prompt_gateway().render(
        CONTEXT_WEIGHTS_CONTRACT,
        {
            "novel_id": "n1",
            "chapter_number": 3,
            "chapter_outline": "回收第二幕线索",
            "phase_hint": "发展",
            "slot_catalog": catalog_lines(),
            "recent_digest": "上章在旧宅发现半册手稿",
        },
    )
    assert rendered.node_key == PLANNING_CONTEXT_WEIGHTS
    # rendered.prompt 是 Prompt 对象（system/user），不是 str
    user_text = rendered.prompt.user
    assert "回收第二幕线索" in user_text
    assert "vector_recall" in user_text, "槽位清单应进入 prompt"
    assert "第 3 章" in user_text


def test_validate_output_accepts_a_legal_payload():
    from infrastructure.ai.prompt_gateway import get_prompt_gateway

    parsed = get_prompt_gateway().validate_output(
        CONTEXT_WEIGHTS_CONTRACT,
        {"weights": {"recent_chapters": 0.9}, "evaluated": True, "reason": "承接现场"},
    )
    assert parsed.evaluated is True
    assert parsed.weights == {"recent_chapters": 0.9}


# ── 槽位目录：必须与真实槽位一致 ────────────────────────────────────────

def test_catalog_keys_are_stable_identifiers_not_display_names():
    """键必须是 dict key，不能是带括号后缀的展示名。

    真实槽位形如 ``slots["vector_recall"]`` / ``name="向量召回"``，
    两者从不相同 —— 契约必须用前者。
    """
    assert "vector_recall" in SLOT_CATALOG
    assert "recent_chapters" in SLOT_CATALOG
    for key, spec in SLOT_CATALOG.items():
        assert spec.key == key, f"{key} 的 spec.key 不一致"
        assert spec.key.isascii(), f"槽位键必须 ASCII：{key}"
        assert "(" not in key, f"槽位键不该带展示用括号后缀：{key}"


def test_catalog_covers_every_registered_slot():
    """目录漏一个槽位，那个槽位就永远拿不到权重 —— 而没人会发现。"""
    import ast
    from pathlib import Path

    src = Path(
        r"D:\WorkSpace\projects\PlotPilot"
        r"\application\engine\services\context_budget_allocator.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    real = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) and isinstance(node.value, ast.Call):
                    if "ContextSlot" in ast.unparse(node.value.func):
                        real.add(ast.literal_eval(tgt.slice))

    missing = real - set(SLOT_CATALOG)
    extra = set(SLOT_CATALOG) - real
    assert not missing, f"这些真实槽位不在目录里（永远拿不到权重）：{sorted(missing)}"
    assert not extra, f"目录里有不存在的槽位（AI 报了也无效）：{sorted(extra)}"


def test_sanitize_drops_unknown_and_rejects_out_of_range():
    """⚠ 第 68 批（S-2）**更正**了本用例的旧期望。

    旧写法是「越界**钳到** [0,1]」⇒ 负数被钳成 0 ⇒ 权重 0 ⇒ 槽位被**整块清空**
    ⇒ AI 只要返回**一个负数**，那一节就没了，而且**模型侧无痕**。

    ⇒ 现在：越界值**整个键被丢弃**（不生效）⇒ 该槽位按「没有权重」处理
    ⇒ 保留完整上限 ⇒ **坏值不能抹掉内容**（fail-closed）。
    """
    assert sanitize_weights({"不存在": 1.0}) == {}
    assert sanitize_weights({"vector_recall": "垃圾"}) == {}
    assert sanitize_weights({"vector_recall": 5.0}) == {}, "越界权重不该被钳成 1.0"
    assert sanitize_weights({"vector_recall": -1.0}) == {}, "越界权重不该被钳成 0.0"
    assert sanitize_weights({"vector_recall": True}) == {}, "JSON 的 true 不是权重"
    assert sanitize_weights({"vector_recall": 0.25}) == {"vector_recall": 0.25}
    assert sanitize_weights("不是字典") == {}
    assert sanitize_weights(None) == {}


# ── 三态降级链 ──────────────────────────────────────────────────────────

class _FakeLLM:
    def __init__(self, content: str = "", raise_exc: Exception = None):
        self._content = content
        self._raise = raise_exc
        self.calls = 0

    async def generate(self, prompt, config):
        self.calls += 1
        if self._raise:
            raise self._raise
        return type("R", (), {"content": self._content})()


async def _plan(llm):
    return await ContextWeightPlanner(llm).plan_weights(
        novel_id="n1", chapter_number=3, chapter_outline="回收线索",
    )


@pytest.mark.asyncio
async def test_valid_weights_are_returned():
    llm = _FakeLLM(json.dumps({
        "weights": {"recent_chapters": 0.9, "vector_recall": 0.3},
        "evaluated": True,
        "reason": "承接现场 + 回收线索",
    }, ensure_ascii=False))
    got = await _plan(llm)

    assert got == {"recent_chapters": 0.9, "vector_recall": 0.3}
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_llm_failure_returns_none_not_defaults():
    """红线：调用失败不得回填一套「合理默认」。"""
    llm = _FakeLLM(raise_exc=RuntimeError("上游 503"))
    assert await _plan(llm) is None


@pytest.mark.asyncio
async def test_non_json_returns_none():
    assert await _plan(_FakeLLM("模型返回了一段解释文字")) is None


@pytest.mark.asyncio
async def test_json_array_returns_none():
    assert await _plan(_FakeLLM("[1, 2, 3]")) is None


@pytest.mark.asyncio
async def test_evaluated_false_returns_none():
    """AI 明说「判不出来」⇒ 必须是 None，而不是退回平均。"""
    llm = _FakeLLM(json.dumps({
        "weights": {"recent_chapters": 0.5, "vector_recall": 0.5},
        "evaluated": False,
        "reason": "信息不足",
    }, ensure_ascii=False))
    assert await _plan(llm) is None


@pytest.mark.asyncio
async def test_evaluated_true_but_no_valid_slots_returns_none():
    """声称评估了却一个合法槽位都没给 ⇒ 当作没判出来，不退回默认值。"""
    llm = _FakeLLM(json.dumps({
        "weights": {"我编的槽位": 0.9},
        "evaluated": True,
        "reason": "",
    }, ensure_ascii=False))
    assert await _plan(llm) is None


@pytest.mark.asyncio
async def test_unknown_slots_alone_return_none():
    llm = _FakeLLM(json.dumps({
        "weights": {"不存在的": 0.8, "也不存在": 0.6},
        "evaluated": True,
    }, ensure_ascii=False))
    assert await _plan(llm) is None


@pytest.mark.asyncio
async def test_out_of_range_weights_are_rejected_not_clamped():
    """⚠ 第 68 批（S-2）**更正**了本用例的旧期望。

    旧写法是「越界**钳到** [0,1]、不拒绝」⇒ 9.0 被当成 1.0（= 满分）
    ⇒ 分配器照着这个权重裁上下文 ⇒ **一个坏值直接影响正文上下文**。

    ⇒ 现在：越界值**被丢弃** ⇒ 合法权重一个都不剩 ⇒ 规划器走它**本来就有**的
    「声称已评估但无可用槽位 ⇒ 按未评估处理」分支 ⇒ 返回 ``None``
    ⇒ 分配器走旧路径（**不施加任何权重**）⇒ 坏值不许生效（fail-closed）。
    """
    llm = _FakeLLM(json.dumps({
        "weights": {"vector_recall": 9.0},
        "evaluated": True,
    }, ensure_ascii=False))
    assert await _plan(llm) is None, (
        "越界权重被当成了 1.0（满分）⇒ 一个坏值直接影响正文上下文"
    )


def test_schema_defaults_are_none_not_midpoint():
    """schema 本身也不能带「看起来正常」的默认权重。"""
    payload = ContextWeightsLlmPayload()
    assert payload.weights is None
    assert payload.evaluated is False
