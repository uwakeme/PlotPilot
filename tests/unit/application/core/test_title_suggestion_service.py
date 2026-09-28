"""TitleSuggestionService 单测：书名生成与清洗"""
from unittest.mock import AsyncMock, Mock

import pytest

from application.core.services.title_suggestion_service import (
    MAX_TITLE_LENGTH,
    TitleSuggestionService,
    fallback_title_from_premise,
)
from domain.ai.services.llm_service import GenerationConfig, GenerationResult, LLMService
from domain.ai.value_objects.token_usage import TokenUsage


def _make_llm(content: str) -> LLMService:
    llm = Mock(spec=LLMService)
    llm.generate = AsyncMock(return_value=GenerationResult(
        content=content,
        token_usage=TokenUsage(input_tokens=100, output_tokens=10),
    ))
    return llm


class TestSuggestTitle:
    """书名生成"""

    @pytest.mark.asyncio
    async def test_returns_cleaned_title(self):
        """模型输出带书名号时应被剥掉"""
        service = TitleSuggestionService(llm_service=_make_llm("《星尘之上》"))
        assert await service.suggest_title(premise="少年踏上星路") == "星尘之上"

    @pytest.mark.asyncio
    async def test_takes_first_line_of_multiline_output(self):
        """模型多行输出（书名 + 解释）时只取第一行"""
        service = TitleSuggestionService(llm_service=_make_llm(
            "深空回响\n\n解析：这个书名体现了……"
        ))
        assert await service.suggest_title(premise="深空电站的守夜人") == "深空回响"

    @pytest.mark.asyncio
    async def test_strips_title_prefix(self):
        """「书名：」前缀应被剥离"""
        service = TitleSuggestionService(llm_service=_make_llm("书名：雾都疑云"))
        assert await service.suggest_title(premise="侦探在雾都追凶") == "雾都疑云"

    @pytest.mark.asyncio
    async def test_empty_premise_returns_none(self):
        service = TitleSuggestionService(llm_service=_make_llm("任意"))
        assert await service.suggest_title(premise="   ") is None

    @pytest.mark.asyncio
    async def test_unusable_output_returns_none(self):
        service = TitleSuggestionService(llm_service=_make_llm("《》"))
        assert await service.suggest_title(premise="少年踏上星路") is None

    @pytest.mark.asyncio
    async def test_overlong_output_hard_truncated(self):
        service = TitleSuggestionService(llm_service=_make_llm("超" * 50))
        assert await service.suggest_title(premise="少年踏上星路") == "超" * MAX_TITLE_LENGTH

    @pytest.mark.asyncio
    async def test_llm_error_propagates(self):
        """LLM 调用失败应向上抛出，由路由层兜底"""
        llm = Mock(spec=LLMService)
        llm.generate = AsyncMock(side_effect=RuntimeError("provider down"))
        service = TitleSuggestionService(llm_service=llm)
        with pytest.raises(RuntimeError):
            await service.suggest_title(premise="少年踏上星路")

    @pytest.mark.asyncio
    async def test_context_passed_in_prompt(self):
        """题材/世界观应拼进提示词"""
        llm = _make_llm("书名")
        service = TitleSuggestionService(llm_service=llm)
        await service.suggest_title(
            premise="少年踏上星路",
            genre="科幻",
            world_preset="星际废土",
            story_structure="三幕式",
        )
        user_prompt = llm.generate.call_args[0][0].user
        assert "少年踏上星路" in user_prompt
        assert "科幻" in user_prompt
        assert "星际废土" in user_prompt
        assert "三幕式" in user_prompt


class TestFallbackTitle:
    """梗概截取兜底"""

    def test_strips_whitespace_and_truncates(self):
        premise = "男主是一个青年男性，平时沉默寡言。\n他有一只猫。" * 3
        title = fallback_title_from_premise(premise)
        assert len(title) == 20
        assert "\n" not in title

    def test_empty_premise(self):
        assert fallback_title_from_premise("") == ""
