"""书名建议服务 - 建书时标题留空，由 LLM 根据梗概生成书名"""
import logging
import re

from domain.ai.services.llm_service import GenerationConfig, LLMService
from domain.ai.value_objects.prompt import Prompt

logger = logging.getLogger(__name__)

# 书名合理长度上限；超过视为模型输出异常，做硬截断
MAX_TITLE_LENGTH = 30

_SYSTEM_PROMPT = "你是一位资深的中文网络小说书名策划，擅长根据故事梗概起抓人眼球、贴合题材的书名。"

_USER_PROMPT_TEMPLATE = """请根据以下信息为这部小说起一个书名。

核心梗概：{premise}
赛道/类型：{genre}
世界观基调：{world_preset}
剧情结构：{story_structure}

要求：
1. 只输出一个书名，不要输出任何解释、序号或标点符号
2. 书名长度 2-12 个汉字，符合中文网文命名习惯（如《诡秘之主》输出为：诡秘之主）
3. 书名要能体现核心梗概的看点与题材氛围
4. 不要使用《》、「」、引号等任何包裹符号"""


def fallback_title_from_premise(premise: str) -> str:
    """书名生成的兜底：从梗概截取前 20 字（历史行为）"""
    cleaned = re.sub(r"\s+", "", premise or "")
    return cleaned[:20]


class TitleSuggestionService:
    """由梗概生成书名；只做生成与清洗，不做兜底决策（兜底在路由层）"""

    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service

    async def suggest_title(
        self,
        premise: str,
        genre: str = "",
        world_preset: str = "",
        story_structure: str = "",
    ) -> str | None:
        """生成书名。

        Returns:
            清洗后的书名；模型输出不可用时返回 None

        Raises:
            Exception: LLM 调用失败时向上抛出，由调用方决定兜底
        """
        if not premise or not premise.strip():
            return None

        prompt = Prompt(
            system=_SYSTEM_PROMPT,
            user=_USER_PROMPT_TEMPLATE.format(
                premise=premise.strip(),
                genre=genre or "未指定",
                world_preset=world_preset or "未指定",
                story_structure=story_structure or "未指定",
            ),
        )
        config = GenerationConfig(temperature=0.9)
        result = await self.llm_service.generate(prompt, config)
        return self._clean_title(result.content)

    @staticmethod
    def _clean_title(raw: str) -> str | None:
        """清洗模型输出：取首行、去包裹符号与引号、限长"""
        if not raw or not raw.strip():
            return None
        # 只取第一行非空内容，防止模型多行输出解释
        first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        if not first_line:
            return None
        # 去掉书名号、引号等包裹符及前后缀说明（如 "书名：xxx"）
        cleaned = re.sub(r"[《》「」『』【】\"“”‘’']", "", first_line)
        cleaned = re.sub(r"^(?:书名|标题)\s*[:：]\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if not cleaned:
            return None
        return cleaned[:MAX_TITLE_LENGTH]
