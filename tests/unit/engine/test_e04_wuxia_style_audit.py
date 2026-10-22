"""E-04：武侠题材的战斗逻辑 / 现代口语判定交 AI 审 + 死常量清理。

改造前
------
1. ``engine/pipelines/wuxia_pipeline.py`` 声明了 4 个**从未生效**的常量：
   - ``DEFAULT_TARGET_WORDS = 3000`` ⇒ 生产的章节目标字数取自
     ``StoryPipelineRunner``（基类的 2500），**不是**这个 3000；
   - ``MIN_PASS_SCORE = 0.55`` ⇒ 真阈值在 ``policy_validator.MIN_PASS_SCORE``
     与 ``quality_guardrail.MIN_PASS_SCORE``（都是 0.6），
     这个 0.55 **从未被读** ⇒「武侠门槛更低」是假的；
   - ``COMBAT_SCENE_MAX_WORDS`` / ``CULTIVATION_DETAIL_LEVEL`` 零引用。
   （``BATCH_WRITE_INTERVAL = 3`` 与基类同值 ⇒ 冗余覆写。）
2. 两条**代码代做的语义判断**：
   - 「一招秒杀」两条正则；
   - 现代口语**恰 7 个词**的黑名单。
   ⇒ 词表外的写法一律漏报，而真正违反战斗逻辑的写法反而抓不到。

改造后
------
· 死常量清掉（行为不变 —— 它们本来就没有行为）；
· 判定改由 **AI** 审（``wuxia-style-audit``）：代码只做不越界校验
  （type 封闭枚举 / **证据必须逐字出现在正文里** / 有结论必须有依据）；
· AI 不可用 ⇒ 回落**代码表**（改造前逐位一致），来源可分辨。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.audit.services.wuxia_style_audit import (
    FALLBACK_COMBAT_PATTERNS,
    FALLBACK_MODERN_WORDS,
    WuxiaStyleAuditor,
    fallback_audit,
)
from application.audit.services.wuxia_style_audit_schema import (
    TYPE_COMBAT_LOGIC,
    TYPE_MODERN_SLANG,
    normalize_style_audit,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WUXIA_REL = "engine/pipelines/wuxia_pipeline.py"


# ======================================================================
#  A. 死常量
# ======================================================================


def _wuxia_class_assignments():
    src = (REPO_ROOT / WUXIA_REL).read_bytes().decode("utf-8")
    tree = ast.parse(src)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "WuxiaPipeline":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            names.append(target.id)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names.append(stmt.target.id)
    return names


@pytest.mark.parametrize(
    "name",
    [
        "DEFAULT_TARGET_WORDS",
        "MIN_PASS_SCORE",
        "COMBAT_SCENE_MAX_WORDS",
        "CULTIVATION_DETAIL_LEVEL",
        "BATCH_WRITE_INTERVAL",
    ],
)
def test_the_dead_wuxia_constants_are_gone(name):
    """武侠管线不再声明那些**从未生效**的常量。

    保留它们 = 继续对外宣称「武侠目标 3000 字」「武侠门槛 0.55」
    —— 而生产上都不是。
    """
    assert name not in _wuxia_class_assignments(), (
        f"武侠管线还在声明 {name} —— 它从未生效，留着就是对外说假话"
    )


def test_the_real_thresholds_are_the_ones_that_actually_run():
    """真阈值只有一个来源（0.6），且武侠没有更低的那份。"""
    from engine.runtime.policy_validator import PolicyValidator
    from engine.runtime.quality_guardrails.quality_guardrail import QualityGuardrail

    assert PolicyValidator.MIN_PASS_SCORE == 0.6
    assert QualityGuardrail.MIN_PASS_SCORE == 0.6


def test_the_wuxia_target_words_come_from_the_runner_not_the_pipeline():
    """章节目标字数取自 runner（基类 2500），不是武侠管线的 3000。"""
    from engine.pipeline.base import BaseStoryPipeline

    assert BaseStoryPipeline.DEFAULT_TARGET_WORDS == 2500


# ======================================================================
#  B. schema 层
# ======================================================================


def _norm(violations, content="他随手一掌，段无痕便死在掌下。"):
    return normalize_style_audit({"violations": violations}, content=content)


def test_type_must_be_in_the_closed_enum():
    """type 必须是封闭枚举里的那两种之一。"""
    result = _norm(
        [
            {
                "type": "ooc",
                "evidence": "他随手一掌，段无痕便死在掌下。",
                "description": "不在枚举里",
            }
        ]
    )

    assert result.source == ""
    assert result.violations == []


def test_evidence_must_appear_verbatim_in_the_content():
    """证据必须**逐字**出现在正文里（AI 编的证据一律丢弃）。"""
    result = _norm(
        [
            {
                "type": TYPE_COMBAT_LOGIC,
                "evidence": "他反手一剑斩尽满堂宾客",  # 正文里没有
                "description": "编出来的证据",
            }
        ]
    )

    assert result.source == ""
    assert result.violations == []


def test_a_violation_without_a_description_is_rejected():
    """有结论必须有依据（description 为空 ⇒ 丢弃）。"""
    result = _norm(
        [
            {
                "type": TYPE_COMBAT_LOGIC,
                "evidence": "他随手一掌，段无痕便死在掌下。",
                "description": "",
            }
        ]
    )

    assert result.source == ""


def test_all_unusable_means_unassessed():
    """AI 给了违规但一条都用不了 ⇒ 未评估（调用方回落代码表）。"""
    result = _norm(
        [
            {
                "type": TYPE_COMBAT_LOGIC,
                "evidence": "正文里根本没有的片段",
                "description": "证据不在正文里",
            }
        ]
    )

    assert result.source == ""
    assert result.violations == []


def test_ai_saying_no_violations_counts_as_answered():
    """AI 合法地说「没有违规」⇒ 算审过了（**不回退**）。"""
    result = _norm([])

    assert result.source == "ai"
    assert result.violations == []


def test_a_valid_violation_is_kept_with_its_suggestion():
    """合法违规要留下，且带上 suggestion（与改造前的字典形状一致）。"""
    result = _norm(
        [
            {
                "type": TYPE_COMBAT_LOGIC,
                "evidence": "他随手一掌，段无痕便死在掌下。",
                "description": "没有铺垫就秒杀",
                "severity": 0.75,
            }
        ]
    )

    assert result.source == "ai"
    assert len(result.violations) == 1
    payload = result.violations[0].to_dict()
    assert payload["dimension"] == "wuxia"
    assert payload["type"] == TYPE_COMBAT_LOGIC
    assert payload["suggestion"]
    assert payload["severity"] == 0.75


# ======================================================================
#  C. 代码表回落（逐位一致）
# ======================================================================


def test_the_fallback_keeps_the_original_two_regexes():
    """代码兜底仍是原来那两条正则（逐位一致）。

    ⚠ 不写成「两条都命中同一段文本」—— ``一招.{0,4}(秒杀|击杀|击毙|毙命)``
    本来就不该命中「随手一掌…死」那段（那是另一条正则管的事）。
    """
    assert len(FALLBACK_COMBAT_PATTERNS) == 2
    assert any(
        re.search(p, "他随手一杀，敌人便倒下了。")
        for p in FALLBACK_COMBAT_PATTERNS
    )
    assert any(
        re.search(p, "他一招秒杀对手")
        for p in FALLBACK_COMBAT_PATTERNS
    )


def test_the_fallback_keeps_the_original_seven_words():
    """代码兜底仍是原来那 7 个词（逐位一致）。"""
    assert FALLBACK_MODERN_WORDS == (
        "搞定",
        "没毛病",
        "杠杠的",
        "绝绝子",
        "yyds",
        "666",
        "离谱",
    )


def test_the_fallback_reports_the_same_things_as_the_original_code():
    """代码兜底的行为与改造前**逐位一致**（同样的输入 ⇒ 同样的违规）。"""
    content = "他随手一杀，段无痕便死在掌下。这事搞定。"

    audit = fallback_audit(content)
    types = [v.type for v in audit.violations]

    assert audit.source == "fallback"
    assert TYPE_COMBAT_LOGIC in types
    assert TYPE_MODERN_SLANG in types

    # 改造前的实现：同样的两条正则 + 同样的 7 个词
    original_combat = any(
        re.search(p, content) for p in FALLBACK_COMBAT_PATTERNS
    )
    original_modern = [w for w in FALLBACK_MODERN_WORDS if w in content]
    assert (TYPE_COMBAT_LOGIC in types) == original_combat
    assert types.count(TYPE_MODERN_SLANG) == len(original_modern)


def test_the_fallback_finds_nothing_in_clean_text():
    """干净的正文不该被代码表误报。"""
    audit = fallback_audit("他一剑刺出，剑光如练，对方后退三步。")

    assert audit.violations == []


# ======================================================================
#  D. 接线
# ======================================================================


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

    def __init__(self):
        self.variables = {}

    def render(self, contract, variables):
        self.variables = dict(variables)
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"),
            node_key=contract.node_key,
        )

    def validate_output(self, contract, payload):
        return contract.output_schema.model_validate(payload)


def _llm(payload):
    llm = Mock()
    llm.generate = AsyncMock(
        return_value=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    )
    return llm


@pytest.fixture
def stub_gateway(monkeypatch):
    import application.audit.services.wuxia_style_audit as audit_mod

    gateway = _FakeGateway()
    monkeypatch.setattr(audit_mod, "get_prompt_gateway", lambda: gateway)
    return gateway


@pytest.mark.asyncio
async def test_the_auditor_uses_the_ai_verdict(stub_gateway):
    """AI 判出的违规要能落地（且证据逐字来自正文）。"""
    llm = _llm(
        {
            "violations": [
                {
                    "type": TYPE_COMBAT_LOGIC,
                    "evidence": "他随手一掌，段无痕便死在掌下。",
                    "description": "没有铺垫就秒杀",
                    "severity": 0.75,
                }
            ],
            "reason": "",
        }
    )

    audit = await WuxiaStyleAuditor(llm).audit(
        content="他随手一掌，段无痕便死在掌下。"
    )

    assert audit.source == "ai"
    assert len(audit.violations) == 1
    assert audit.violations[0].type == TYPE_COMBAT_LOGIC


@pytest.mark.asyncio
async def test_the_auditor_is_unassessed_when_the_llm_fails(stub_gateway):
    """LLM 挂了 ⇒ 未评估（调用方回落代码表）。"""
    llm = Mock()
    llm.generate = AsyncMock(side_effect=RuntimeError("upstream 502"))

    audit = await WuxiaStyleAuditor(llm).audit(content="他随手一掌，段无痕便死在掌下。")

    assert audit.source == ""
    assert audit.violations == []


def test_the_pipeline_delegates_to_the_ai_auditor():
    """武侠管线的校验步骤必须走 AI 审计（AST 判调用名，不用文本匹配）。"""
    src = (REPO_ROOT / WUXIA_REL).read_bytes().decode("utf-8")
    tree = ast.parse(src)

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name:
                called.add(name)

    assert "_audit_wuxia_style" in called, "校验步骤没走武侠风格审计"
    assert "WuxiaStyleAuditor" in called, "武侠风格审计没接 AI"
    assert "fallback_audit" in called, "AI 不可用时必须回落代码表"
    # 旧的两条正则 / 词表不该再留在管线里
    assert "re" not in called, "管线里不该再有正则调用（已交 AI）"


# ======================================================================
#  E. 节点注册
# ======================================================================


def test_prompt_key_and_package_are_registered():
    """新节点 key 必须登记 + 有内置包（否则 seed 同步不到，调用必失败）。"""
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_WUXIA_STYLE_AUDIT
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_WUXIA_STYLE_AUDIT in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_WUXIA_STYLE_AUDIT in package_ids
