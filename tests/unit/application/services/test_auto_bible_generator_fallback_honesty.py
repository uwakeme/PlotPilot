"""A-05：Bible 生成拿不到内容时，代码不得代写一整本兜底 Bible。

改造前
------
``AutoBibleGenerator._generate_bible_data`` 在 LLM 返回空内容时代写整本兜底：

    characters=[{name: 主角, role: 主角, description: 待补充}]
    locations=[{name: 主要场景, type: 城市, description: 待补充}]
    style=第三人称有限视角，轻松幽默

这三样会被当成**用户设定**落库：

- 文风 → ``bible_style_notes``（category="文风公约"）——**该表没有来源列**
- 角色 → ``unified_characters``
- 地点 → ``bible_locations``

而文风那一路会经 ``format_style_notes_for_prompt`` 进 ``narrative_contract``
槽（T0_CRITICAL / priority=122）—— 每章 prompt 的**最高优先级强制层**。
落库之后，库里的「代码代写的文风」与「AI 写的文风」完全同形，用户无从分辨。

核实（与登记表的描述有出入）
--------------------------------
登记表 A-05 写的是「LLM 失败时代码代写整本兜底 Bible」。实测**不准确**：
``_call_llm_and_parse_with_retry`` 在所有重试失败时**抛 ValueError**
（其 docstring 明说「避免调用方将空结果当作有效数据继续处理」），
所以「调用失败 / JSON 解析失败」**走不到**兜底分支。

兜底真正的触发口只有一个：**模型返回了合法但为空的对象 ``{}``**
（``if bible_data:`` 的 falsy 判定）。窄，但真实存在 ——
而且后果是上面那三样假设定进 T0 强制层。

生产库实测（data/plotpilot.db，3 本书）：``bible_style_notes`` 2 条、
``bible_locations`` 20 条、``unified_characters`` 13 条，
**没有一条**是兜底代写的内容 ⇒ 它至今没被触发过。
本文件的作用是让它**永远不再触发**，且真发生时**显式失败**而不是静默污染。

全部是行为断言：不碰源码文本。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock

from application.world.services.auto_bible_generator import (
    AutoBibleGenerator,
    BibleGenerationIncomplete,
)
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage


# 代写内容里的特征字样 —— 只要它们出现在产物/异常里就是回归
_FABRICATED_MARKERS = ("轻松幽默", "待补充", "主要场景")


def _svc(content=None, *, raises=None):
    llm = Mock()
    if raises is not None:
        llm.generate = AsyncMock(side_effect=raises)
    else:
        llm.generate = AsyncMock(
            return_value=GenerationResult(
                content=content,
                token_usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        )
    bible_service = Mock()
    svc = AutoBibleGenerator(llm_service=llm, bible_service=bible_service)
    return svc, bible_service


async def test_empty_llm_payload_fails_instead_of_fabricating_a_bible():
    """模型返回空对象 ⇒ 显式失败（未评估），不是一本假 Bible。"""
    svc, _ = _svc(content="{}")

    with pytest.raises(BibleGenerationIncomplete):
        await svc._generate_bible_data("premise", 10)


async def test_incomplete_error_carries_no_fabricated_content():
    """异常消息里不得出现代写内容 —— 报错本身也是用户可见文案。"""
    svc, _ = _svc(content="{}")

    with pytest.raises(BibleGenerationIncomplete) as exc:
        await svc._generate_bible_data("premise", 10)

    for marker in _FABRICATED_MARKERS:
        assert marker not in str(exc.value), f"异常文案里带上了代写内容：{marker!r}"


async def test_incomplete_generation_writes_nothing_to_the_bible():
    """失败时不得留下半个 Bible（角色/地点/文风一个都不许落库）。"""
    svc, bible_service = _svc(content="{}")

    with pytest.raises(BibleGenerationIncomplete):
        await svc._generate_bible_data("premise", 10)

    bible_service.add_character.assert_not_called()
    bible_service.add_location.assert_not_called()
    bible_service.add_style_note.assert_not_called()


async def test_llm_exception_does_not_produce_a_fallback_bible():
    """调用异常/解析失败由重试层抛错，**不**落回代写兜底。"""
    svc, bible_service = _svc(raises=RuntimeError("upstream 502"))

    with pytest.raises(ValueError):
        await svc._generate_bible_data("premise", 10)

    bible_service.add_style_note.assert_not_called()


async def test_valid_payload_is_returned_unchanged():
    """正常路径不受影响：模型给的内容原样返回。"""
    svc, _ = _svc(
        content='{"characters":[],"locations":[],"style":"冷白描实","worldbuilding":{}}'
    )

    result = await svc._generate_bible_data("premise", 10)

    assert result["style"] == "冷白描实"


async def test_empty_style_does_not_create_a_style_note():
    """模型没给文风 ⇒ 不落 style note ⇒ T0 创作契约里没有文风段。

    这条守住「没有文风」与「有一段看起来正常的文风」的区别 ——
    后者正是 A-05 要消灭的东西。
    """
    svc, bible_service = _svc(
        content='{"characters":[],"locations":[],"style":"","worldbuilding":{}}'
    )

    data = await svc._generate_bible_data("premise", 10)
    await svc._save_to_bible("n-1", data)

    bible_service.add_style_note.assert_not_called()


async def test_ai_written_style_is_still_saved():
    """对照组：AI 写的文风照常落库（别把修复做成「一律不存」）。"""
    svc, bible_service = _svc(
        content='{"characters":[],"locations":[],"style":"冷白描实","worldbuilding":{}}'
    )

    data = await svc._generate_bible_data("premise", 10)
    await svc._save_to_bible("n-1", data)

    bible_service.add_style_note.assert_called_once()
    kwargs = bible_service.add_style_note.call_args.kwargs
    assert kwargs["content"] == "冷白描实"
    assert kwargs["category"] == "文风公约"