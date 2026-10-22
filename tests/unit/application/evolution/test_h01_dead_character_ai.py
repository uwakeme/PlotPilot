"""H-01：死亡角色在大纲里的出场方式由 **AI** 判（活跃登场 vs 回忆/遗物/传闻）。

改造前
------
``DEAD_CHARACTER_REAPPEARS`` 规则只看「名字有没有出现在大纲里」：

```python
hit = _outline_mentions(outline, labels)
if status == "dead" and hit:
    → blocking，消息写着「当前大纲仍让其以活跃人物出现」
```

于是「回忆《林澈》当年的约定」「桌上还摆着林澈的旧剑」这类**合法的回忆 / 遗物**
同样被拦 ⇒ 误报。而且这条规则**自相矛盾**：它给的建议就是「改为回忆/遗物/传闻」，
可改了之后**照样**被拦（判据就是「名字出现了」）。

改造后
------
· 新 CPMS 节点 ``evolution-dead-character-check``：AI 看大纲上下文判「在场行动」
  还是「只是被想起」；
· 代码只做不越界校验（角色白名单 / verdict 封闭枚举 / 有结论必须有依据）；
· **「判不出」显式落** ``level="unassessed"``（≠ 没违规，报告里
  ``unassessed_count`` 可数）；
· **AI 不可用 ⇒ 回落规则判定**（命中即拦，与改造前逐位一致 —— 不放宽闸门）；
· ``check()`` **保持同步**（三个调用点里有一个在纯同步方法里），
  AI 判定走 ``check_with_ai`` / ``classify_dead_characters``（async 侧调用方下传）。
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.evolution.services.dead_character_check_schema import (
    VERDICT_ACTIVE,
    VERDICT_MEMORY,
    VERDICT_UNCLEAR,
    normalize_dead_character_check,
)
from application.evolution.services.gate_service import EvolutionGateService
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage
from domain.evolution.models import EvolutionState

REPO_ROOT = Path(__file__).resolve().parents[4]


def _dead_state(name="顾言之"):
    state = EvolutionState.empty()
    state.characters[name] = {"status": "dead", "location": "悬崖底", "inventory": []}
    return state


# ======================================================================
#  A. 闸门层
# ======================================================================


def test_hit_without_verdict_is_still_blocking_by_the_rule():
    """没传 AI 判定 ⇒ **规则兜底**：命中即拦（不放宽闸门）。"""
    report = EvolutionGateService().check(
        novel_id="n1",
        chapter_number=2,
        outline_content="顾言之在客栈醒来。",
        state=_dead_state(),
    )

    assert report.is_pass is False
    assert report.violations[0].type == "DEAD_CHARACTER_REAPPEARS"
    assert report.violations[0].source == "rule"


def test_ai_saying_memory_is_not_a_violation():
    """AI 判「只是回忆/遗物」⇒ **不是违规**（这条误报就是本笔要消掉的）。"""
    report = EvolutionGateService().check(
        novel_id="n1",
        chapter_number=2,
        outline_content="纪寒想起顾言之临终前的那句话。",
        state=_dead_state(),
        dead_character_verdicts={"顾言之": VERDICT_MEMORY},
    )

    assert report.violations == []
    assert report.is_pass is True


def test_ai_saying_active_is_a_blocking_violation():
    """AI 判「活跃登场」⇒ 真违规（blocking，来源 ai）。"""
    report = EvolutionGateService().check(
        novel_id="n1",
        chapter_number=2,
        outline_content="顾言之推门而入，开口问你们来做什么。",
        state=_dead_state(),
        dead_character_verdicts={"顾言之": VERDICT_ACTIVE},
    )

    assert report.is_pass is False
    assert report.violations[0].level == "blocking"
    assert report.violations[0].source == "ai"


def test_ai_unclear_is_explicitly_unassessed_not_silently_ok():
    """AI 判「判不出来」⇒ 显式未评估（**不**悄悄当成「没违规」）。"""
    report = EvolutionGateService().check(
        novel_id="n1",
        chapter_number=2,
        outline_content="顾言之的名字被提到了一句。",
        state=_dead_state(),
        dead_character_verdicts={"顾言之": VERDICT_UNCLEAR},
    )

    assert report.violations, "未评估必须**显式**出现在报告里，不能悄悄消失"
    assert report.violations[0].level == "unassessed"
    assert report.unassessed_count == 1
    assert "未评估" in report.violations[0].message
    # 未评估 ≠ 违规 ⇒ 不 blocking
    assert report.is_pass is True
    assert report.repair_plan == []


def test_unassessed_count_is_exported_in_the_report_dict():
    """未评估条数要进报告（出口：API / ctx.evolution_continuity_report）。"""
    report = EvolutionGateService().check(
        novel_id="n1",
        chapter_number=2,
        outline_content="顾言之的名字被提到了一句。",
        state=_dead_state(),
        dead_character_verdicts={"顾言之": VERDICT_UNCLEAR},
    )

    payload = report.to_dict()
    assert payload["unassessed_count"] == 1
    assert payload["violations"][0]["source"] == "ai"


def test_alive_character_is_never_a_violation():
    """活着的角色被提到 ⇒ 不报（这条不该被 AI 判）。"""
    state = EvolutionState.empty()
    state.characters["顾言之"] = {"status": "alive", "location": "", "inventory": []}

    report = EvolutionGateService().check(
        novel_id="n1",
        chapter_number=2,
        outline_content="顾言之在客栈醒来。",
        state=state,
    )

    assert report.violations == []


def test_check_must_stay_synchronous():
    """``check()`` **必须**保持同步（有一个调用点在纯同步方法里）。

    改成 async ⇒ 那个同步调用点直接坏掉。
    """
    assert not inspect.iscoroutinefunction(EvolutionGateService.check)
    assert inspect.iscoroutinefunction(EvolutionGateService.check_with_ai)


# ======================================================================
#  B. schema 层
# ======================================================================


def _norm(verdicts, characters=("char-1",)):
    return normalize_dead_character_check(
        {"verdicts": verdicts},
        valid_character_ids=list(characters),
    )


def test_ai_cannot_invent_characters():
    """AI 不得凭空造人（不在候选清单里的 character_id 一律丢弃）。"""
    result = _norm(
        [
            {
                "character_id": "char-not-in-list",
                "verdict": VERDICT_ACTIVE,
                "reason": "编出来的角色",
            }
        ]
    )

    assert result.source == ""
    assert result.verdicts == {}


def test_verdict_must_be_in_the_closed_enum():
    """verdict 必须是封闭枚举里的那三个之一。"""
    result = _norm(
        [
            {
                "character_id": "char-1",
                "verdict": "he's_dead_but_shows_up",
                "reason": "不在枚举里",
            }
        ]
    )

    assert result.source == ""


def test_a_verdict_without_a_reason_is_rejected():
    """有结论必须有依据（reason 为空 ⇒ 丢弃）。"""
    result = _norm(
        [{"character_id": "char-1", "verdict": VERDICT_ACTIVE, "reason": ""}]
    )

    assert result.source == ""


def test_unclear_verdict_may_have_no_reason():
    """``unclear`` 是「不下结论」，允许没有依据。"""
    result = _norm([{"character_id": "char-1", "verdict": VERDICT_UNCLEAR, "reason": ""}])

    assert result.source == "ai"
    assert result.verdicts["char-1"].verdict == VERDICT_UNCLEAR


def test_ai_saying_memory_counts_as_answered():
    """AI 合法地说「只是回忆」⇒ 算判过了（**不回退**）。"""
    result = _norm(
        [
            {
                "character_id": "char-1",
                "verdict": VERDICT_MEMORY,
                "reason": "大纲写的是回忆",
            }
        ]
    )

    assert result.source == "ai"
    assert result.verdicts["char-1"].verdict == VERDICT_MEMORY


# ======================================================================
#  C. 接线层
# ======================================================================


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

    def __init__(self):
        self.variables = {}
        self.rendered_contracts = []

    def render(self, contract, variables):
        self.variables = dict(variables)
        self.rendered_contracts.append(contract)
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"),
            node_key=contract.node_key,
        )

    def validate_output(self, contract, payload):
        return contract.output_schema.model_validate(payload)


def _llm(payload):
    llm = Mock()
    llm.generate = AsyncMock(
        return_value=GenerationResult(
            json.dumps(payload, ensure_ascii=False),
            TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    return llm


@pytest.fixture
def stub_gateway(monkeypatch):
    import application.evolution.services.dead_character_checker as checker_mod

    gateway = _FakeGateway()
    monkeypatch.setattr(checker_mod, "get_prompt_gateway", lambda: gateway)
    return gateway


@pytest.mark.asyncio
async def test_check_with_ai_uses_the_ai_verdict(stub_gateway):
    """AI 判「只是回忆」⇒ 闸门**不**报违规（误报被消掉）。"""
    llm = _llm(
        {
            "verdicts": [
                {
                    "character_id": "顾言之",
                    "verdict": VERDICT_MEMORY,
                    "reason": "大纲写的是回忆，顾言之没有在场",
                }
            ],
            "reason": "",
        }
    )
    service = EvolutionGateService(llm_service=llm)

    report = await service.check_with_ai(
        novel_id="n1",
        chapter_number=2,
        outline_content="纪寒想起顾言之临终前的那句话。",
        state=_dead_state(),
    )

    assert report.violations == []
    assert report.is_pass is True


@pytest.mark.asyncio
async def test_check_with_ai_falls_back_to_the_rule_when_ai_fails(stub_gateway):
    """AI 调用失败 ⇒ 回落规则判定（命中即拦，**不放宽**）。"""
    llm = Mock()
    llm.generate = AsyncMock(side_effect=RuntimeError("upstream 502"))
    service = EvolutionGateService(llm_service=llm)

    report = await service.check_with_ai(
        novel_id="n1",
        chapter_number=2,
        outline_content="顾言之在客栈醒来。",
        state=_dead_state(),
    )

    assert report.is_pass is False
    assert report.violations[0].source == "rule"


@pytest.mark.asyncio
async def test_check_with_ai_marks_unclear_as_unassessed(stub_gateway):
    """AI 判不出 ⇒ 显式未评估（不 blocking，但报告里看得见）。"""
    llm = _llm(
        {
            "verdicts": [
                {
                    "character_id": "顾言之",
                    "verdict": VERDICT_UNCLEAR,
                    "reason": "只提了一句，看不出在场还是被想起",
                }
            ],
            "reason": "",
        }
    )
    service = EvolutionGateService(llm_service=llm)

    report = await service.check_with_ai(
        novel_id="n1",
        chapter_number=2,
        outline_content="顾言之的名字被提到了一句。",
        state=_dead_state(),
    )

    assert report.violations[0].level == "unassessed"
    assert report.unassessed_count == 1


def test_candidate_discovery_has_only_one_implementation():
    """候选发现只有**一份**（不写两份）。

    ⚠ 用 AST 判调用名，不用源码 ``contains`` —— 注释会命中自己。
    """
    src = (
        REPO_ROOT / "application/evolution/services/gate_service.py"
    ).read_bytes().decode("utf-8")
    tree = ast.parse(src)

    def calls_in(name: str):
        found = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        attr = getattr(fn, "attr", None) or getattr(fn, "id", None)
                        found.append(attr)
        return found

    # ① 候选发现只定义一次（不写两份）
    definitions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_dead_character_candidates"
    ]
    assert len(definitions) == 1, (
        f"候选发现被定义了 {len(definitions)} 次 ⇒ 同一逻辑写了两份"
    )

    # ② 闸门判定与 AI 分类都走它
    assert "_dead_character_candidates" in calls_in("check")
    assert "_dead_character_candidates" in calls_in("classify_dead_characters")

    # ③ check_with_ai 走 classify_dead_characters（不自己再实现一遍）
    assert "classify_dead_characters" in calls_in("check_with_ai")


def test_production_call_sites_use_the_async_path():
    """生产调用点必须走 async 版（AI 判定在那儿算）。

    ⚠ AST 判调用名（不用文本匹配）。
    """
    for rel in (
        "engine/pipeline/base.py",
        "interfaces/api/v1/engine/generation.py",
        "engine/runtime/legacy_writing_delegate.py",
    ):
        src = (REPO_ROOT / rel).read_bytes().decode("utf-8")
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in (
                    "prepare_chapter_generation",
                    "prepare_chapter_generation_async",
                ):
                    names.add(node.func.attr)
        assert "prepare_chapter_generation_async" in names, (
            f"{rel} 没走 async 版 ⇒ AI 判定跑不到（闸门会一直用规则兜底）"
        )


def test_the_gate_factory_injects_the_llm_service():
    """闸门工厂必须注入 LLM 服务（不注入 ⇒ AI 通道永不触发）。"""
    src = (REPO_ROOT / "interfaces/api/dependencies.py").read_bytes().decode("utf-8")
    tree = ast.parse(src)

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "EvolutionGateService":
            continue
        kwargs = {kw.arg for kw in node.keywords}
        if "llm_service" not in kwargs:
            missing.append(f"dependencies.py:{node.lineno}")
    assert not missing, "这些构造点没传 llm_service：\n" + "\n".join(missing)


# ======================================================================
#  D. 节点注册
# ======================================================================


def test_prompt_key_and_package_are_registered():
    """新节点 key 必须登记 + 有内置包（否则 seed 同步不到，调用必失败）。"""
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_DEAD_CHARACTER_CHECK
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_DEAD_CHARACTER_CHECK in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_DEAD_CHARACTER_CHECK in package_ids
