"""AIText Engine — Opinionated Plot Engine for Long-Form Fiction

A batteries-included narrative engine with built-in database schema,
top-tier generation pipeline, and all advanced capabilities out of the box.

Architecture:
- core/       : Pure domain layer — entities, value objects, ports
- pipeline/   : Generation pipeline base class (inherit to extend)
- runtime/    : Pipeline runner, policy validator, quality guardrails,
                plot state machine, checkpoint manager, writing orchestrator
- infrastructure/: Infrastructure implementations — persistence, events, memory
- examples/   : Official pipeline subclasses (ShortDrama, Wuxia)

Extension Model:
- Inherit BaseStoryPipeline, override _step_xxx() methods
- See engine.pipeline.base.BaseStoryPipeline for details

Naming Convention:
- CharacterPsycheEngine (not CharacterSoulEngine) — 心理画像 > 灵魂
- engine.runtime.* (not engine.application.*) — 运行时统一

Quick Start:
    from engine.pipeline import BaseStoryPipeline, PipelineContext, StepResult

    class MyPipeline(BaseStoryPipeline):
        DEFAULT_TARGET_WORDS = 1500

        async def _step_build_context(self, ctx):
            result = await super()._step_build_context(ctx)
            # 所有 _step_* 必须返回 StepResult（不是 ctx）；
            # ctx 是可变引用，步骤内的修改直接生效
            if result.passed:
                ctx.context_text += "\\n【强制规则】每3分钟必须有一个反转。"
            return result

    pipeline = MyPipeline()
    result = await pipeline.run_chapter(ctx)
"""
__version__ = "3.0.0-alpha"
