"""张力解析失败不得返回中性 50（登记表 F-07）。

背景：`application/analyst/services/tension_scoring_service.py` 在
`parsed.failure_stage == "parse_or_schema"` 分支上返回
`TensionDimensions.neutral()`（全维度 50.0）。而 neutral() 的 docstring
自己已标注「⚠ 已废弃：请使用 unevaluated() 代替」——全仓只有这一处仍在调用它。

50.0 是合法张力值，下游读到它会把「解析失败」当成「张力正好 50」。
unevaluated() 返回 -1.0 哨兵，下游（chapter_narrative_sync:1578、
monitor.py:64-74）已按该约定正确跳过写入。

注意：TensionDimensions **没有** `.state` 或 `.overall_score` 属性，
三态由 `is_evaluated` 属性与 UNEVALUATED(-1.0) 哨兵表达。
"""
from types import SimpleNamespace

import pytest

from application.analyst.services import tension_scoring_service as mod
from application.analyst.services.tension_scoring_service import TensionScoringService
from domain.ai.value_objects.prompt import Prompt
from domain.novel.value_objects.tension_dimensions import UNEVALUATED, TensionDimensions


class _Gateway:
    """PromptGateway 替身：只提供被测代码实际用到的 .render(...).prompt。"""

    def render(self, contract, variables):
        return SimpleNamespace(prompt=Prompt(system="score tension", user=variables["content"]))


async def _parse_failure(*args, **kwargs):
    """structured_json_generate_with_status 的替身：解析/schema 失败。

    真实签名是 `async def (llm, prompt, config, schema_model, *, max_retries)`，
    返回值 `StructuredJSONGenerateResult` 带 .payload / .failure_stage / .errors
    （errors 是 tuple）。这里必须用 async def，否则 await 会抛 TypeError。
    """
    return SimpleNamespace(payload=None, failure_stage="parse_or_schema", errors=("不是 JSON",))


@pytest.mark.asyncio
async def test_parse_schema_failure_is_unevaluated_not_neutral_50(monkeypatch):
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _Gateway())
    monkeypatch.setattr(mod, "structured_json_generate_with_status", _parse_failure)

    dims = await TensionScoringService(SimpleNamespace()).score_chapter(
        chapter_content="这一章有足够正文用于评分。",
        chapter_number=3,
    )

    assert dims.is_evaluated is False, "解析失败不得声称已评估"
    assert dims.composite_score == UNEVALUATED, "解析失败必须返回 -1 哨兵，不是中性 50"
    assert dims.composite_score != 50.0
    # 全维度都要是哨兵：只把 composite 置 -1 而三个维度留 50 同样是假分
    assert dims.plot_tension == UNEVALUATED
    assert dims.emotional_tension == UNEVALUATED
    assert dims.pacing_tension == UNEVALUATED


@pytest.mark.asyncio
async def test_parse_failure_does_not_log_a_fake_score_as_the_result(monkeypatch, caplog):
    """降级日志文案不得再宣称「使用中性 50」——改完后那句话就是假的。"""
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _Gateway())
    monkeypatch.setattr(mod, "structured_json_generate_with_status", _parse_failure)

    with caplog.at_level("WARNING"):
        await TensionScoringService(SimpleNamespace()).score_chapter(
            chapter_content="这一章有足够正文用于评分。",
            chapter_number=3,
        )

    assert "中性 50" not in caplog.text, f"降级日志仍在宣称中性 50：{caplog.text}"


def test_declared_deprecated_neutral_is_gone():
    """neutral() 必须从值对象上消失——否则下一个人还会用。"""
    assert not hasattr(TensionDimensions, "neutral"), (
        "已废弃的 neutral() 仍在，留着就会被再次误用"
    )
