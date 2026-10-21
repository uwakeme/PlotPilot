"""H-03：角色行为一致性判定交回 AI（语义判断不该由关键词表 + 正则代劳）。

红线（本文件负责钉死）：

1. **关键词表与正则必须消失** —— 那是「语义判断被写成字符串匹配」的残留。
2. **AI 判不出来 ⇒ 未评估**（``valid`` 必为 ``None``）——
   旧实现失败时返回 ``valid=True``，那是**假通过**（「没查过」被说成「没问题」）。
3. **AI 说了什么就透出什么** —— 代码不替它裁决、不加工。
4. 角色设定的事实（信念/禁忌/旧伤/语体）由**代码**从角色面具取出交给 AI，
   AI 不负责「这个角色是什么样的人」。

AST 判据：关键词表用 AST 扫字符串常量（不做文本匹配，注释/docstring 不会撑住判据）。
"""
from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.character.services.character_belief_check import CharacterBeliefChecker
from application.character.services.character_belief_check_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    UNASSESSED,
    normalize_verdict,
)

#: 旧实现里那套关键词表的字面量
_LEGACY_KEYWORDS = {
    "不信任", "警惕", "谨慎", "轻信", "防备", "不信",
    "软肋", "致命", "危险", "不可信",
    "力量", "强者为尊", "弱肉强食",
    "毫不犹豫地相信", "完全信任", "放心地", "毫无戒备", "毫无防备",
    "退缩", "害怕", "求饶", "示弱", "妥协",
}

_MASK_PATH = pathlib.Path(__file__).resolve().parents[4] / "engine/core/value_objects/character_mask.py"


def _mask_source() -> str:
    return _MASK_PATH.read_bytes().decode("utf-8")


# ── 1. 关键词表与正则已删除 ─────────────────────────────────────────


def test_legacy_keyword_table_is_gone():
    """AST 扫字符串常量 —— 关键词表必须不在代码里（注释里出现不算）。"""
    tree = ast.parse(_mask_source())
    literals = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
    for keyword in _LEGACY_KEYWORDS:
        assert keyword not in literals, f"旧关键词表还在代码里：{keyword}"


def test_character_mask_no_longer_judges_behavior():
    """判断已移到 AI 层；值对象只负责提供事实。"""
    from engine.core.value_objects.character_mask import CharacterMask

    assert not hasattr(CharacterMask, "validate_behavior")


# ── 2. 三态：判不出来 ⇒ 未评估，绝不 valid=True ────────────────────


def test_unassessed_is_not_valid():
    assert UNASSESSED.evaluated is False
    assert UNASSESSED.valid is None
    assert UNASSESSED.source == SOURCE_NONE


def test_missing_consistent_field_is_unassessed_not_true():
    """旧实现 `result.get("valid", True)` 把「没给」当成「没问题」。"""
    verdict = normalize_verdict({"findings": []})

    assert verdict.evaluated is False
    assert verdict.valid is None


def test_non_dict_payload_is_unassessed():
    assert normalize_verdict(None).evaluated is False
    assert normalize_verdict("不是对象").evaluated is False


# ── 3. AI 说了什么就透出什么 ───────────────────────────────────────


def test_consistent_verdict_is_usable():
    verdict = normalize_verdict({"consistent": True, "findings": []})

    assert verdict.evaluated is True
    assert verdict.valid is True
    assert verdict.source == SOURCE_AI
    assert verdict.warnings == []


def test_inconsistent_verdict_carries_findings():
    verdict = normalize_verdict({
        "consistent": False,
        "findings": [
            {
                "type": "belief",
                "severity": "critical",
                "message": "他从不轻信，却毫不犹豫地相信了陌生人",
                "suggestion": "让他先试探",
            }
        ],
    })

    assert verdict.evaluated is True
    assert verdict.valid is False
    assert len(verdict.warnings) == 1
    assert "毫不犹豫地相信" in verdict.warnings[0]
    assert verdict.suggestions == ["让他先试探"]


def test_unknown_finding_type_is_skipped_but_others_survive():
    verdict = normalize_verdict({
        "consistent": False,
        "findings": [
            {"type": "外星生物", "severity": "critical", "message": "未知类型"},
            {"type": "taboo", "severity": "warning", "message": "触碰了禁忌"},
        ],
    })

    assert len(verdict.warnings) == 1
    assert "触碰了禁忌" in verdict.warnings[0]


# ── 4. 调用链：失败即未评估 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_checker_returns_unassessed_when_the_llm_fails():
    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    verdict = await CharacterBeliefChecker(_Boom()).check(
        character_name="沈砚", action="沈砚毫不犹豫地相信了陌生人",
    )

    assert verdict.evaluated is False
    assert verdict.valid is None


@pytest.mark.asyncio
async def test_checker_returns_unassessed_when_the_output_is_not_json():
    class _Garbage:
        async def generate(self, prompt, config=None):
            return SimpleNamespace(content="我觉得写得还不错！")

    verdict = await CharacterBeliefChecker(_Garbage()).check(
        character_name="沈砚", action="沈砚轻信了陌生人",
    )

    assert verdict.evaluated is False
    assert verdict.valid is None


@pytest.mark.asyncio
async def test_checker_passes_the_character_facts_to_the_model():
    captured = {}

    class _LLM:
        async def generate(self, prompt, config=None):
            captured["prompt"] = prompt
            return SimpleNamespace(content=json.dumps({
                "consistent": False,
                "findings": [{"type": "belief", "severity": "critical", "message": "违背信念"}],
            }))

    verdict = await CharacterBeliefChecker(_LLM()).check(
        character_name="沈砚",
        action="沈砚毫不犹豫地相信了陌生人",
        context="堂上无人",
        core_belief="只有力量能保护自己，轻信必死",
        moral_taboos=["杀手无寸铁之人"],
        active_wounds=[{"description": "左肩被刺", "trigger": "左后方", "effect": "紧绷"}],
        voice_style="惜字如金",
        novel_title="临渊",
    )

    assert verdict.valid is False
    text = captured["prompt"].user
    assert "沈砚" in text
    assert "轻信必死" in text
    assert "杀手无寸铁之人" in text
    assert "左肩被刺" in text
    assert "毫不犹豫地相信了陌生人" in text
    assert "临渊" in text


# ── 5. 路由层：失败不得报「符合设定」 ───────────────────────────────


@pytest.mark.asyncio
async def test_route_returns_unassessed_when_the_service_fails(monkeypatch):
    from interfaces.api.v1.engine import checkpoint_routes as routes

    monkeypatch.setattr(routes, "_novel_exists", lambda novel_id: True)
    monkeypatch.setattr(
        routes, "_resolve_character_mask",
        AsyncMock(return_value=SimpleNamespace(
            core_belief="只有力量能保护自己",
            moral_taboos=[],
            active_wounds=[],
            voice_style="惜字如金",
        )),
    )

    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    import interfaces.api.dependencies as deps

    monkeypatch.setattr(deps, "get_llm_service", lambda: _Boom())

    result = await routes.validate_character_behavior(
        "n1", "沈砚", routes.ValidateBehaviorRequest(action="沈砚毫不犹豫地相信了陌生人")
    )

    assert result.valid is None
    assert result.evaluated is False


@pytest.mark.asyncio
async def test_route_returns_unassessed_when_the_mask_is_missing(monkeypatch):
    from interfaces.api.v1.engine import checkpoint_routes as routes

    monkeypatch.setattr(routes, "_novel_exists", lambda novel_id: True)
    monkeypatch.setattr(routes, "_resolve_character_mask", AsyncMock(return_value=None))

    result = await routes.validate_character_behavior(
        "n1", "沈砚", routes.ValidateBehaviorRequest(action="沈砚轻信了陌生人")
    )

    assert result.valid is None
    assert result.evaluated is False


@pytest.mark.asyncio
async def test_route_returns_unassessed_when_something_breaks(monkeypatch):
    """路由自己的兜底分支也不得报「符合设定」。

    注意：LLM 异常是在 checker 内部兜住的，走不到路由的 except ——
    那条分支只会被「取面具就炸了」这类意外触发，但它是**活的**，
    旧实现在这里返回 valid=True（假通过）。
    """
    from interfaces.api.v1.engine import checkpoint_routes as routes

    monkeypatch.setattr(routes, "_novel_exists", lambda novel_id: True)
    monkeypatch.setattr(
        routes, "_resolve_character_mask", AsyncMock(side_effect=RuntimeError("boom"))
    )

    result = await routes.validate_character_behavior(
        "n1", "沈砚", routes.ValidateBehaviorRequest(action="沈砚轻信了陌生人")
    )

    assert result.valid is None
    assert result.evaluated is False


@pytest.mark.asyncio
async def test_route_reports_the_model_verdict(monkeypatch):
    from interfaces.api.v1.engine import checkpoint_routes as routes

    monkeypatch.setattr(routes, "_novel_exists", lambda novel_id: True)
    monkeypatch.setattr(
        routes, "_resolve_character_mask",
        AsyncMock(return_value=SimpleNamespace(
            core_belief="只有力量能保护自己",
            moral_taboos=[],
            active_wounds=[],
            voice_style="惜字如金",
        )),
    )

    class _LLM:
        async def generate(self, prompt, config=None):
            return SimpleNamespace(content=json.dumps({
                "consistent": False,
                "findings": [{
                    "type": "belief", "severity": "critical",
                    "message": "他从不轻信，却相信了陌生人",
                    "suggestion": "让他先试探",
                }],
            }))

    import interfaces.api.dependencies as deps

    monkeypatch.setattr(deps, "get_llm_service", lambda: _LLM())

    result = await routes.validate_character_behavior(
        "n1", "沈砚", routes.ValidateBehaviorRequest(action="沈砚毫不犹豫地相信了陌生人")
    )

    assert result.valid is False
    assert result.evaluated is True
    assert result.source == SOURCE_AI
    assert any("他从不轻信" in w for w in result.warnings)
    assert result.suggestions == ["让他先试探"]


def test_response_model_defaults_to_unassessed():
    """响应模型的默认值必须是「未评估」，不是「符合设定」。"""
    from interfaces.api.v1.engine.checkpoint_routes import ValidateBehaviorResponse

    response = ValidateBehaviorResponse()
    assert response.valid is None
    assert response.evaluated is False
