"""ThemedStoryPipeline — 将 ThemeAgent 注入 BaseStoryPipeline 的桥接基类

老 ThemeAgent 体系（context/audit/beat 指令）通过此类接入新管线，
无需重写 10 步流程。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.steps import StepResult

logger = logging.getLogger(__name__)


class ThemedStoryPipeline(BaseStoryPipeline):
    """题材感知管线 — 自动注入 ThemeAgent 的上下文与审计规则"""

    _default_genre_key: str = ""

    def __init__(self, genre_key: str = ""):
        super().__init__()
        self._genre_key = genre_key or self._default_genre_key

    @property
    def genre_key(self) -> str:
        return self._genre_key

    def _get_theme_agent(self):
        if not self.genre_key:
            return None
        try:
            from application.engine.theme.theme_registry import ThemeAgentRegistry

            registry = ThemeAgentRegistry()
            if not registry.registered_keys:
                registry.auto_discover()
            return registry.get(self.genre_key)
        except Exception as e:
            logger.debug("加载 ThemeAgent 失败 genre=%s: %s", self.genre_key, e)
            return None

    async def _step_build_context(self, ctx: PipelineContext) -> StepResult:
        result = await super()._step_build_context(ctx)
        if not result.passed:
            return result

        agent = self._get_theme_agent()
        if agent is None:
            return result

        try:
            directives = agent.get_effective_context_directives(
                ctx.novel_id, ctx.chapter_number, ctx.outline or ""
            )
            directive_text = directives.to_context_text()
            if directive_text:
                ctx.context_text += f"\n\n{directive_text}"

            persona = agent.get_effective_system_persona()
            if persona:
                ctx.context_text += f"\n\n【题材人设】\n{persona}"

            rules = agent.get_effective_writing_rules()
            if rules:
                numbered = "\n".join(f"- {rule}" for rule in rules)
                ctx.context_text += f"\n\n【题材写作规则】\n{numbered}"

            skills_text = agent.invoke_skills_context(
                ctx.novel_id,
                ctx.chapter_number,
                ctx.outline or "",
                ctx.context_text,
            )
            if skills_text:
                ctx.context_text += f"\n\n{skills_text}"
        except Exception as e:
            logger.warning("ThemeAgent 上下文注入失败 genre=%s: %s", self.genre_key, e)

        return result

    async def _step_validate_content(self, ctx: PipelineContext) -> StepResult:
        result = await super()._step_validate_content(ctx)
        agent = self._get_theme_agent()
        if agent is None or not ctx.chapter_content:
            return result

        try:
            criteria = agent.get_audit_criteria(ctx.chapter_number, ctx.outline or "")
            for check in criteria.quality_checks:
                ctx.validation_violations.append({
                    "dimension": self.genre_key or "theme",
                    "type": "theme_audit",
                    "severity": 0.5,
                    "description": check,
                    "suggestion": "对照题材审计标准修订本章",
                })

            skill_checks = agent.invoke_skills_audit(
                ctx.chapter_number, ctx.chapter_content, ctx.outline or ""
            )
            for check in skill_checks:
                ctx.validation_violations.append({
                    "dimension": self.genre_key or "theme",
                    "type": "theme_skill_audit",
                    "severity": 0.5,
                    "description": check,
                    "suggestion": "对照题材 Skill 审计项修订本章",
                })
        except Exception as e:
            logger.warning("ThemeAgent 审计注入失败 genre=%s: %s", self.genre_key, e)

        return result

    def _enrich_script_context(self, ctx: PipelineContext) -> None:
        """若大纲命中题材节拍模板，将模板指令注入剧本生成上下文。

        登记表 E-02：改造前这里按 priority 降序遍历，**命中第一条就
        `break`** —— 一章最多只会注入一个节拍模板。

        这不是「优先级」而是「丢数据」。题材模板的关键词本来就重叠：
        实测 11 个题材共 48 套模板，武侠是**唯一**有关键词被两个模板
        同时认领的 —— `灭门` 既属于「门派危机」(prio 80) 也属于
        「血债复仇」(prio 85)。一章大纲同时写「灭门 + 寻宝 + 报仇」会命中
        三个模板，改造前只有优先级最高的那个进来，另外两套节拍结构
        被静默丢弃，模型根本不知道它们存在。

        改为注入**全部命中的模板**（按 priority 降序），并如实报出
        命中了几个、各自因为哪个关键词命中 —— 怎么在场景里取舍与融合，
        交给模型判断。代码只负责「把命中的都摆出来」，不负责「替模型选一套」。
        """
        agent = self._get_theme_agent()
        if agent is None or not ctx.outline:
            return

        templates = agent.get_beat_templates()
        if not templates:
            return

        outline = ctx.outline
        matched = sorted(templates, key=lambda t: t.priority, reverse=True)

        hits: List[Tuple[Any, str]] = []
        for template in matched:
            hit_keywords = [kw for kw in template.keywords if kw and kw in outline]
            if hit_keywords:
                hits.append((template, "、".join(hit_keywords)))

        if not hits:
            return

        sections: List[str] = []
        for template, hit_keywords in hits:
            beat_instructions = "\n".join(
                f"- {desc}（约{words}字，聚焦{focus}）"
                for desc, words, focus in template.beats
            )
            sections.append(
                f"### 命中关键词：{hit_keywords}\n{beat_instructions}"
            )

        names = "；".join(
            f"优先级 {t.priority}（命中：{kws}）" for t, kws in hits
        )
        # ⚠️ 不要把 `"\n".join(sections)` 直接接在 f-string 串尾：
        # 隐式字符串拼接的优先级**低于**对字符串字面量的属性访问，
        # `f"头" f"\n".join(x)` 会被解析成 `"头\n\n".join(x)` ——
        # 整段抬头变成 join 的分隔符，只命中一套时 sep.join([x]) == x，
        # 抬头直接消失，而且有两套时看起来还「基本正确」。
        # 这里显式先拼 header 再相加。
        header = (
            "\n\n【题材节拍指令（融入导演剧本）】\n"
            f"本章大纲命中 {len(hits)} 套题材节拍模板：{names}。\n"
            "请在撰写导演剧本时确保这些节拍都在场景中体现；"
            "若其中若干套在本章并不需要全部展开，由你判断取舍与融合顺序。\n"
        )
        ctx.context_text += header + "\n".join(sections)
