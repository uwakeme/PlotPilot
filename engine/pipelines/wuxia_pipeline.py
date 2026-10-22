"""WuxiaPipeline — 武侠引擎（正式版）

继承 ThemedStoryPipeline，自动注入 WuxiaThemeAgent 的上下文/审计规则，
并追加武侠专属的整章写作约束与内容验证逻辑。

E-04
----
· **删掉 4 个死常量**（``DEFAULT_TARGET_WORDS`` / ``MIN_PASS_SCORE`` /
  ``COMBAT_SCENE_MAX_WORDS`` / ``CULTIVATION_DETAIL_LEVEL``）——
  它们**从未生效**：
  - 生产的章节目标字数取自 ``StoryPipelineRunner``（基类的 2500），
    **不是**本管线的 3000；
  - 质量门槛的真值在 ``policy_validator.MIN_PASS_SCORE`` 与
    ``quality_guardrail.MIN_PASS_SCORE``（都是 0.6）——
    本管线声明的 0.55 **从未被读** ⇒「武侠门槛更低」是假的；
  - 后两个零引用。
  （``BATCH_WRITE_INTERVAL = 3`` 与基类同值 ⇒ 冗余覆写，一并删。）
· **两条代码校验器 ⇒ AI 审**（``wuxia-style-audit``）：
  「一招秒杀」的两条正则 + 现代口语的 7 项黑名单都是**代码代做的语义判断**
  ⇒ 词表外的写法一律漏报，而真正违反战斗逻辑的写法反而抓不到。
  AI 不可用 ⇒ 回落**代码表**（改造前逐位一致），来源可辨。
"""
from __future__ import annotations

import logging

from engine.pipelines.themed_pipeline import ThemedStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.steps import StepResult

logger = logging.getLogger(__name__)


class WuxiaPipeline(ThemedStoryPipeline):
    """武侠引擎 — 修炼体系 + 战斗编排 + 江湖规矩"""

    _default_genre_key = "wuxia"

    # E-04：原先这里声明的 DEFAULT_TARGET_WORDS / MIN_PASS_SCORE /
    # COMBAT_SCENE_MAX_WORDS / CULTIVATION_DETAIL_LEVEL / BATCH_WRITE_INTERVAL
    # **全部是死的**（见模块 docstring）⇒ 已删。
    # 真值：目标字数 2500（StoryPipelineRunner / 基类）、
    #       质量门槛 0.6（policy_validator / quality_guardrail）。

    async def _step_build_context(self, ctx: PipelineContext) -> StepResult:
        """注入 ThemeAgent 规则 + 武侠补充约束"""
        result = await super()._step_build_context(ctx)

        wuxia_rules = (
            "\n\n【武侠补充规则】\n"
            "1. 同境界看功法，跨境界需有合理铺垫，禁止无铺垫一招秒杀\n"
            "2. 对白半文半白，禁止现代网络用语\n"
            "3. 打斗一招一式有名字，简洁有力，不堆砌形容词\n"
        )
        ctx.context_text += wuxia_rules

        if ctx.knowledge_service is not None:
            try:
                knowledge = ctx.knowledge_service.get_knowledge(ctx.novel_id)
                if knowledge and hasattr(knowledge, "cultivation_system"):
                    ctx.context_text += f"\n\n【修炼体系】\n{knowledge.cultivation_system}"
            except Exception:
                pass

        return result

    async def _step_validate_content(self, ctx: PipelineContext) -> StepResult:
        result = await super()._step_validate_content(ctx)

        content = ctx.chapter_content
        if not content:
            return result

        # E-04：战斗逻辑 / 现代口语改由 **AI** 审（代码只校验不越界）。
        violations = await self._audit_wuxia_style(ctx, content)
        for violation in violations:
            ctx.validation_violations.append(violation)

        return result

    async def _audit_wuxia_style(self, ctx: PipelineContext, content: str):
        """AI 审武侠战斗逻辑 / 现代口语。

        AI 不可用 / 输出不合契约 ⇒ 回落**代码表**（改造前逐位一致），
        来源可辨（``ai`` / ``fallback``）。
        """
        from application.audit.services.wuxia_style_audit import (
            WuxiaStyleAuditor,
            fallback_audit,
        )

        llm_service = getattr(ctx, "llm_service", None)
        audit = await WuxiaStyleAuditor(llm_service).audit(content=content)
        if audit.is_usable:
            return [v.to_dict() for v in audit.violations]
        fallback = fallback_audit(content)
        return [v.to_dict() for v in fallback.violations]
