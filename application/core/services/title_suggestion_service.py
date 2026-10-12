"""书名建议服务 - 建书时标题留空，由 LLM 根据梗概生成书名"""
import json
import logging
import re
from typing import List

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

_OPTIONS_SYSTEM_PROMPT = (
    "你是一位资深的中文网络小说书名策划，擅长根据故事的世界观、人物与主线剧情，"
    "起抓人眼球、贴合题材且风格各异的书名。"
)

_OPTIONS_USER_PROMPT_TEMPLATE = """请根据以下信息为这部小说起 {count} 个备选书名。

【核心梗概】
{premise}

{outline_section}{characters_section}{worldview_section}{style_section}【赛道/类型】{genre}
【世界观基调】{world_preset}
【剧情结构】{story_structure}

要求：
1. 只输出一个 JSON 数组，格式如 ["书名一", "书名二"]，不要输出其他解释
2. 每个书名 2-12 个汉字，符合中文网文命名习惯，不使用《》「」引号等包裹符号
3. {count} 个书名风格要有差异：有的突出核心冲突，有的突出主角特质，有的渲染世界观氛围
4. 书名要能体现故事的看点与题材氛围"""


def fallback_title_from_premise(premise: str) -> str:
    """书名生成的兜底：从梗概截取前 20 字（历史行为）"""
    cleaned = re.sub(r"\s+", "", premise or "")
    return cleaned[:20]


def _section(label: str, body: str, limit: int = 600) -> str:
    """拼接可选段落；空内容返回空串"""
    if not body or not body.strip():
        return ""
    text = body.strip()
    if len(text) > limit:
        text = text[:limit] + "……"
    return f"【{label}】\n{text}\n\n"


class TitleSuggestionService:
    """由梗概/总纲生成书名；只做生成与清洗，不做兜底决策（兜底在路由层）"""

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

    async def suggest_title_options(
        self,
        premise: str,
        *,
        outline_summary: str = "",
        characters_hint: List[str] | None = None,
        worldview_summary: str = "",
        style_hint: str = "",
        genre: str = "",
        world_preset: str = "",
        story_structure: str = "",
        count: int = 5,
    ) -> List[str]:
        """向导第 4 步（总纲已生成）的多候选书名。

        Args:
            premise: 核心梗概
            outline_summary: 剧情总纲摘要（主线概述/核心冲突/预期结局拼合）
            characters_hint: 主要角色提示，如 ["沈潮生（主角）", ...]
            worldview_summary: 世界观摘要
            style_hint: 文风提示
            count: 期望候选数；模型实际可用数可能更少

        Returns:
            清洗去重后的书名列表；不可用时返回空列表

        Raises:
            Exception: LLM 调用失败时向上抛出，由调用方决定兜底
        """
        if not premise or not premise.strip():
            return []
        count = max(1, min(int(count or 5), 8))

        prompt = Prompt(
            system=_OPTIONS_SYSTEM_PROMPT,
            user=_OPTIONS_USER_PROMPT_TEMPLATE.format(
                count=count,
                premise=premise.strip(),
                outline_section=_section("剧情总纲", outline_summary),
                characters_section=_section(
                    "主要角色", "、".join(characters_hint or []), limit=300
                ),
                worldview_section=_section("世界观", worldview_summary),
                style_section=_section("文风", style_hint, limit=200),
                genre=genre or "未指定",
                world_preset=world_preset or "未指定",
                story_structure=story_structure or "未指定",
            ),
        )
        config = GenerationConfig(temperature=1.0)
        result = await self.llm_service.generate(prompt, config)
        return self._parse_title_options(result.content, count)

    @staticmethod
    def _parse_title_options(raw: str, count: int) -> List[str]:
        """解析模型输出的多候选：优先 JSON 数组，失败退回逐行解析"""
        titles: List[str] = []
        raw = raw or ""
        # 优先按 JSON 数组解析（截取第一个 [ 到最后一个 ]）
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, str):
                            cleaned = TitleSuggestionService._clean_title(item)
                            if cleaned:
                                titles.append(cleaned)
            except json.JSONDecodeError:
                titles = []
        if not titles:
            # 退回逐行解析：去掉序号/破折号前缀
            for line in raw.splitlines():
                line = re.sub(r"^\s*(?:\d+[.、)]|[-*•])\s*", "", line)
                if not line.strip():
                    continue
                cleaned = TitleSuggestionService._clean_title(line)
                if cleaned:
                    titles.append(cleaned)

        # 去重（忽略大小写），保序，限量
        seen: set[str] = set()
        unique: List[str] = []
        for title in titles:
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(title)
        return unique[:count]

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
        # 含句末标点的行是说明文字而非书名（如拒答时的解释句）
        if re.search(r"[。！？；;…]", cleaned):
            return None
        # 中文标题去掉全部空白；纯外文标题保留词间空格
        if re.search(r"[\u4e00-\u9fff]", cleaned):
            cleaned = re.sub(r"\s+", "", cleaned)
        else:
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return None
        return cleaned[:MAX_TITLE_LENGTH]
