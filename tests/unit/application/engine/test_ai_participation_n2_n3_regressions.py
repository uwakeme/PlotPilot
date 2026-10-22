"""R5：N2 与 N3 两条修复的**回归保护**。

第 3 批三次复审（R5）实测：
- 把 N2 的 ``if ai_selection.is_usable:`` 改回 ``if True:``
  （= 恢复「空选择覆盖兜底」）⇒ **全套 3577 条测试照样绿**；
- 把 ``voice_drift_service`` 的准入型闸门改成 ``and (enabled(...) or True)``
  ⇒ 32 条照样绿；
- 让某个 ``voice_service`` 调用点**绕过**服务层分派 ⇒ 照样绿。

根因：``BEHAVIOUR_CASES`` 只覆盖了 8 个能力模块，
而 ``foreshadow_selection``（N2）与 ``voice_drift_service``（N3）**都不在其中** ——
偏偏这两个是上一轮最难、也最该被守住的修复。

本文件补上：
1. **N2**：在 ``base.py`` 的 ``_plan_foreshadows`` 层断言 ——
   AI 给不出可用结果（未评估）⇒ **代码兜底必须还在**。
2. **N3-a**：在 ``voice_drift_service`` 的分派点断言 ——
   总闸关闭 ⇒ 走**统计**分支（LLM 分支一次都不许进）；总闸开着 ⇒ 走 LLM 分支。
3. **N3-b**：**调用点守卫** —— 所有 ``voice_service.*`` 的 LLM 分派
   必须经由 ``score_chapter_async``（不许绕过服务层直接打 LLM）。

⚠ 本文件**打桩 planner 的返回值**而不是真的走 AI ——
真走 AI 会碰 CPMS 渲染（要 DB），在单测里既慢又会挂。
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def _set_master(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: SimpleNamespace(
            ai_participation={"master_enabled": enabled, "disabled_features": []}
        ),
    )


class _StubForeshadow:
    def __init__(self, fid: str, description: str):
        self.id = fid
        self.description = description
        self.planted_in_chapter = 1
        self.importance = SimpleNamespace(value=3)
        self.suggested_resolve_chapter = None

    def is_core(self) -> bool:
        # ⚠ 必须是**方法**：`collect_candidates` 写的是
        #   ``bool(f.is_core()) if hasattr(f, "is_core") else False``
        #   ⇒ 写成布尔属性会被当方法调用 ⇒ TypeError ⇒ 候选被静默跳过。
        return False


class _StubRegistry:
    def __init__(self, items):
        self._items = items

    def get_t0_eligible_foreshadowings(self, current_chapter=0, max_items=0):
        return list(self._items)


def _patch_planner(monkeypatch, *, usable: bool) -> None:
    """把 planner 的返回值打成「可用」或「未评估」。

    ⚠ 不真的走 AI（会碰 CPMS 渲染 ⇒ 要 DB ⇒ 单测里会挂）。
    """
    import application.world.services.foreshadow_selection as selection_mod
    from application.world.services.foreshadow_selection_schema import (
        SOURCE_AI,
        SOURCE_FALLBACK,
        ForeshadowSelection,
    )

    def _fake_select(self, **kwargs):
        async def _inner():
            candidates = tuple(kwargs.get("candidates") or ())
            if usable:
                return ForeshadowSelection(candidates, {}, SOURCE_AI, ())
            # 未评估：空选择 + 来源非 ai（= 总闸关闭 / AI 说不出时的形状）
            return ForeshadowSelection((), {}, SOURCE_FALLBACK, ())

        return _inner()

    monkeypatch.setattr(
        selection_mod.ForeshadowSelectionPlanner, "select", _fake_select, raising=True
    )


async def _plan_foreshadows(monkeypatch, *, usable: bool) -> tuple[str, str]:
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n-1", chapter_number=3)

    registry = _StubRegistry([_StubForeshadow("f1", "那把剑是谁的")])

    class _Repo:
        def get_by_novel_id(self, novel_id):
            return registry

    # ⚠ base.py 读的是**属性**（``getattr(ctx, "foreshadowing_repository", None)``）
    #   ⇒ 不能用 ``ctx.inject``（未知键会进 ``_dependencies``，getattr 读不到）。
    ctx.foreshadowing_repository = _Repo()
    ctx.llm_service = SimpleNamespace(generate=None)

    _patch_planner(monkeypatch, usable=usable)

    text = await pipeline._plan_foreshadows(ctx)
    return text, str(ctx.metadata.get("foreshadow_selection_source") or "")


# ───────────────────────── N2 · 伏笔兜底不许被空选择覆盖 ─────────────────────────


@pytest.mark.asyncio
async def test_the_foreshadow_fallback_survives_when_the_ai_cannot_help(monkeypatch):
    """AI 帮不上（未评估）⇒ **代码兜底必须还在**（N2 的回归保护）。

    ⚠ 第 3 批三次复审 R5：把 ``if ai_selection.is_usable:`` 改回 ``if True:``
    （= 恢复「空选择覆盖兜底」）此前**全套测试照样绿** ——
    因为没有任何用例在 **base.py 这一层**断言过兜底还在。
    """
    text, source = await _plan_foreshadows(monkeypatch, usable=False)

    assert "那把剑是谁的" in text, (
        "AI 帮不上时，代码兜底的伏笔**整块消失**了（N2 的修法被回退了）"
    )
    assert source != "ai", "来源不许标成 ai —— AI 这次根本没参与判断"


@pytest.mark.asyncio
async def test_the_foreshadow_ai_verdict_is_used_when_it_is_usable(monkeypatch):
    """反向：AI **真的**选过 ⇒ 用 AI 的结果（不许因为加了兜底判断就永远不用 AI）。"""
    text, source = await _plan_foreshadows(monkeypatch, usable=True)

    assert "那把剑是谁的" in text
    assert source == "ai", "AI 选过 ⇒ 来源必须是 ai"


# ───────────────────────── N3 · 文风漂移的闸门在分派点 ─────────────────────────


def _voice_service():
    from application.analyst.services.voice_drift_service import VoiceDriftService

    return VoiceDriftService(
        score_repo=object(),
        fingerprint_repo=object(),
        llm_voice_service=SimpleNamespace(analyze_chapter_style=None),
        use_llm_mode=True,
    )


@pytest.mark.asyncio
async def test_the_voice_drift_service_takes_the_statistics_path_when_the_master_is_off(
    monkeypatch,
):
    """总闸关闭 ⇒ ``score_chapter_async`` 必须走**统计**分支。

    ⚠ 第 3 批三次复审 R5：把准入型闸门改成 ``and (enabled(...) or True)``
    此前**测试照样绿** —— 因为没有任何用例断言过「关闸后 LLM 分支不进」。
    """
    _set_master(monkeypatch, False)

    calls = {"llm": 0, "stats": 0}
    service = _voice_service()

    async def _fake_llm(*args, **kwargs):
        calls["llm"] += 1
        return {"similarity_score": 0.9, "drift_alert": False}

    monkeypatch.setattr(service, "_score_chapter_llm", _fake_llm)
    monkeypatch.setattr(
        service,
        "score_chapter",
        lambda *a, **k: (calls.__setitem__("stats", calls["stats"] + 1)
                         or {"similarity_score": 0.5, "drift_alert": False}),
    )

    result = await service.score_chapter_async("n-1", 3, "正文")

    assert calls["llm"] == 0, "总闸关闭后仍然走了 LLM 分支（N3 的修法被回退了）"
    assert calls["stats"] == 1, "总闸关闭后应当回落统计分支"
    assert result["similarity_score"] == 0.5


@pytest.mark.asyncio
async def test_the_voice_drift_service_takes_the_llm_path_when_the_master_is_on(monkeypatch):
    """正对照：总闸**开着** ⇒ 走 LLM 分支（否则上面那条会恒真）。"""
    _set_master(monkeypatch, True)

    calls = {"llm": 0, "stats": 0}
    service = _voice_service()

    async def _fake_llm(*args, **kwargs):
        calls["llm"] += 1
        return {"similarity_score": 0.9, "drift_alert": False}

    monkeypatch.setattr(service, "_score_chapter_llm", _fake_llm)
    monkeypatch.setattr(
        service,
        "score_chapter",
        lambda *a, **k: (calls.__setitem__("stats", calls["stats"] + 1)
                         or {"similarity_score": 0.5, "drift_alert": False}),
    )

    await service.score_chapter_async("n-1", 3, "正文")

    assert calls["llm"] == 1, "正对照失败：总闸开着时也没走 LLM 分支"
    assert calls["stats"] == 0


# ───────────────────────── N3-b · 调用点必须经由服务层分派 ─────────────────────────


def test_no_voice_service_call_site_bypasses_the_service_layer():
    """所有 **voice 服务**的 LLM 分派必须经由 ``score_chapter_async``。

    ⚠ 第 3 批三次复审 R5：让某个调用点**绕过**服务层分派
    （直接调 ``_score_chapter_llm`` 或别的私有方法）⇒ 闸门就管不到它了
    ⇒ 而守卫此前**看不见**这类逃逸（探测器只认 ``.generate(...)``）。

    ⇒ 现在扫全仓：**名字里带 voice 的对象**上、带 ``llm`` 的方法调用，
    都必须走 ``score_chapter_async``（服务自己的私有实现除外）。

    ⚠ 判据只圈「voice 服务」—— 否则 ``close_llm_service`` /
    ``get_llm_provider_factory`` 这类无关调用会全部误报。
    """
    offenders: list[str] = []
    service_file = "application/analyst/services/voice_drift_service.py"
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "__pycache__" in rel or rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            attr = func.attr
            if "llm" not in attr.lower():
                continue
            if attr == "score_chapter_async":
                continue  # 正规入口
            base = func.value
            base_name = (
                getattr(base, "attr", None)
                or getattr(base, "id", None)
                or ""
            )
            if "voice" not in base_name.lower():
                continue  # 不是 voice 服务（避免 close_llm_service 之类误报）
            if rel == service_file:
                continue  # 服务内部的私有实现（由分派点把关）
            offenders.append(f"{rel}:{node.lineno} ({base_name}.{attr})")
    assert not offenders, (
        "这些地方绕过了 voice_drift_service 的分派点 ⇒ 总闸管不到它们：\n"
        + "\n".join(f"  - {item}" for item in offenders)
    )
