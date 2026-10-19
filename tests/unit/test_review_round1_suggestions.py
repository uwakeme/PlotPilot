"""独立审查 §2 建议级的回归守卫。

与 ``test_review_round1_regressions.py``（守阻塞项）分开 —— 那一批守的是
「默认写作路径会不会崩」，这一批守的是「AI 参与的语义有没有被悄悄压平」。

本批修的问题
------------
1. voice-drift 前端自定 3.0/6.0 阈值（后端是 0.3/0.5，drift ∈ [0.5,0.6) 时
   两边结论不同），且把「前端拉取时刻」当「检测时刻」展示 —— 未评估时
   显示「最近检测=现在」，是伪造时间戳。
2. D-01 把「未评估时写入的 0」当真实张力喂给模型。
3. `_decide_chapter_mode` 异常分支不留原因（与自身 docstring 矛盾）。
4. 四个开关的 docstring 指向 `config/performance.yaml` 的某个段，而那个段
   从不存在 —— 指向了一个不存在的地方。
5. `context_weight_settings` 的 `cache_ttl_seconds` / `failure_backoff_seconds`
   被声明、被 docstring 承诺，但全仓无消费；而两个 planner 的 DI 工厂没有
   `@lru_cache`（docstring 却写「长生命周期」），就算实现了缓存也必然失效。
6. G-01/G-03 的来源标记只进日志，落库后「AI 说的」与「代码编的」同形。
7. 两个 planner 手写解析，契约里声明的 `output_schema` 形同虚设；
   `ChapterModeLlmPayload.needs_buffer` 在 pydantic lax 模式下会把字符串
   `"true"` 静默强转成 `True` —— 含糊被读成斩钉截铁的「是」。
8. 墙钟复杂度守卫在 5000 规模下抓不住 O(n²)（实测 0.728s < 1s 阈值）。
"""
import ast
import inspect
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "infrastructure" / "ai" / "prompt_packages").is_dir():
            return parent
    raise AssertionError("找不到仓库根")


REPO_ROOT = _repo_root()


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8-sig")


def _ast(rel: str) -> ast.Module:
    return ast.parse(_read(rel))


# ── 1. voice-drift：后端是唯一事实源 ─────────────────────────────────────


def test_voice_drift_response_carries_the_real_threshold_and_time():
    """前端不再自定阈值、不再伪造时间戳 ⇒ 后端必须把这两样发出来。"""
    from interfaces.api.v1.workbench.monitor import VoiceDriftResponse

    fields = VoiceDriftResponse.model_fields
    for name in ("computed_at", "warning_threshold", "critical_threshold", "status"):
        assert name in fields, f"响应里缺 {name}"

    r = VoiceDriftResponse(
        novel_id="n1",
        drift_score=0.42,
        status="warning",
        evaluated=True,
        sample_count=20,
        last_scored_chapter=7,
        computed_at="2026-10-18T00:00:00",
    )
    assert r.computed_at == "2026-10-18T00:00:00"
    assert r.warning_threshold == 0.3
    assert r.critical_threshold == 0.5

    unevaluated = VoiceDriftResponse(
        novel_id="n1",
        drift_score=None,
        status="unevaluated",
        evaluated=False,
        sample_count=0,
        last_scored_chapter=None,
        reason="基准样本不足",
    )
    # 未评估 ⇒ 真实时刻必须是 None，不能退化成「现在」
    assert unevaluated.computed_at is None


def test_voice_drift_unevaluated_helper_never_fabricates_a_timestamp():
    """`_unevaluated()` 是未评估的唯一出口，它填的时间戳必须是 None。"""
    from interfaces.api.v1.workbench.monitor import _unevaluated

    r = _unevaluated("n1", 3, "基准样本不足（3/10）")
    assert r.evaluated is False
    assert r.drift_score is None
    assert r.computed_at is None
    assert r.last_scored_chapter is None


def test_frontend_no_longer_defines_its_own_drift_thresholds():
    """前端一旦自定阈值，后端就退化成「只提供数值」—— 两边会给出不同结论。"""
    src = _read("frontend/src/components/autopilot/VoiceDriftIndicator.vue")
    assert "safeThreshold" not in src, "前端仍在自定 safeThreshold"
    assert "dangerThreshold" not in src, "前端仍在自定 dangerThreshold"
    # 且不得用拉取时刻冒充检测时刻
    assert "last_check_time: new Date()" not in src, (
        "前端把拉取时刻当检测时刻 —— 未评估时会显示「最近检测=现在」"
    )


def test_frontend_uses_backend_status_as_single_source_of_truth():
    src = _read("frontend/src/components/autopilot/VoiceDriftIndicator.vue")
    assert "driftData.value?.status" in src, (
        "driftStatus 没有采用后端下发的 status"
    )
    # 不得再用前端刻度自行比较
    assert ">= dangerThreshold" not in src and ">= safeThreshold" not in src


# ── 2. D-01 输入：未评估 ≠ 0 ──────────────────────────────────────────────


def test_audit_writes_none_not_zero_when_tension_was_not_evaluated():
    """未评估时必须写 ``None``，**不是 0**。

    `last_chapter_tension` 现为 `Optional[int]`，None = 从未评估。
    此前它是 int 且默认 0，未评估时也写 0 —— 两者在 0 上同形，
    只能另立一个 `_evaluated` 标记区分；那等于制造了第二个事实源（值 + 标记），
    两者可以漂移。现在单一事实源就是值本身。
    """
    tree = _ast("engine/runtime/audit_delegate.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_chapter_audit"
    )

    def assignments_to(name):
        # 目标可能是 `novel.last_chapter_tension`（Attribute），
        # 不只是裸 Name —— 只认 Name 会一个都找不到
        def hits(target):
            if isinstance(target, ast.Name):
                return target.id == name
            return isinstance(target, ast.Attribute) and target.attr == name

        return [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Assign) and any(hits(x) for x in n.targets)
        ]

    tension_asg = assignments_to("last_chapter_tension")
    assert len(tension_asg) >= 2, (
        f"只找到 {len(tension_asg)} 处 last_chapter_tension 赋值 —— 两个分支都该有"
    )
    zero_writes = [
        a.lineno for a in tension_asg if ast.unparse(a.value).strip() == "0"
    ]
    assert not zero_writes, (
        f"L{zero_writes} 把未评估写成了 0 —— 「真是 0」与「没测过」就此同形"
    )
    none_writes = [
        a.lineno for a in tension_asg if ast.unparse(a.value).strip() == "None"
    ]
    assert none_writes, "未评估分支没有写 None"

    # 共享状态也要跟着写 None
    shared = [
        kw for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_update_shared_state"
        for kw in node.keywords
        if kw.arg == "last_chapter_tension"
    ]
    assert shared, "没找到共享状态里的 last_chapter_tension 写入"
    assert not [
        kw for kw in shared if ast.unparse(kw.value).strip() == "0"
    ], "共享状态仍在把未评估压成 0"

    # 冗余标记必须已被移除。
    # ⚠ 用 AST 判关键字参数名，**不要**用 `"..." not in src` ——
    # 注释里提到这个名字就会让断言误判（这轮已经吃过一次）。
    flag_kwargs = [
        (n.lineno, kw.arg)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if (kw.arg or "").startswith("last_chapter_tension_evaluated")
    ]
    assert not flag_kwargs, (
        f"评估标记仍作为参数存在：{flag_kwargs} —— 值本身就是三态，"
        f"留着它等于留了第二个可以漂移的事实源"
    )


def test_novel_entity_declares_tension_as_optional():
    from domain.novel.entities.novel import Novel
    import inspect

    ann = str(Novel.__init__.__annotations__.get("last_chapter_tension", ""))
    assert "None" in ann and ("Optional" in ann or "| None" in ann), (
        f"last_chapter_tension 应为 Optional[int]，实际 {ann!r}"
    )
    sig = inspect.signature(Novel.__init__)
    assert sig.parameters["last_chapter_tension"].default is None, (
        "默认值必须是 None（= 从未评估），0 会与「真实张力 0」同形"
    )
    fresh = Novel(
        id=__import__("domain.novel.value_objects.novel_id", fromlist=["NovelId"]).NovelId("n1"),
        title="t", author="a", target_chapters=10,
    )
    assert fresh.last_chapter_tension is None


def test_decide_chapter_mode_treats_none_as_no_signal():
    """D-01 的取值直接来自 Optional 值：None ⇒ 不给信号。

    不再经过任何中间判定函数 —— 值本身就是三态，没有第二个事实源。
    """
    tree = _ast("engine/pipeline/base.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_decide_chapter_mode"
    )
    assigns = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "last_tension" for t in n.targets)
    ]
    assert assigns, "没找到 last_tension 的赋值"
    # 赋值必须在 `raw is not None` 的守卫之内
    src = ast.unparse(fn)
    assert "if raw is not None" in src, (
        "last_tension 的取值没有被 None 守卫 —— 未评估的 None 会直接喂给模型"
    )
    assert "last_chapter_tension_evaluated" not in src, (
        "仍在走已删除的评估标记 —— 值本身就是三态，不需要第二个事实源"
    )


def test_decide_failure_branch_records_the_reason():
    """docstring 承诺「未判定必须留原因」，异常分支也必须兑现。"""
    src = _read("engine/pipeline/base.py")
    tree = _ast("engine/pipeline/base.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_decide_chapter_mode"
    )
    # 找到 except 分支体，确认里面有对 buffer_reason 的赋值
    except_bodies = [
        h.body for h in ast.walk(fn)
        if isinstance(h, ast.ExceptHandler)
    ]
    assert except_bodies, "找不到 except 分支"
    assigns_reason = any(
        any(isinstance(x, ast.Assign) and "buffer_reason" in ast.unparse(x)
            for x in body)
        for body in except_bodies
    )
    assert assigns_reason, "异常分支没有写 buffer_reason"


# ── 4. 开关的 docstring 必须指向真实存在的地方 ────────────────────────────


def test_all_four_switch_sections_exist_in_performance_yaml():
    """settings 的 docstring 都写着「配置位置：config/performance.yaml 的 xxx 段」，
    那个段此前根本不存在 —— 照着做的人找不到任何东西。"""
    import yaml

    data = yaml.safe_load(_read("config/performance.yaml"))
    for section in ("context_weights", "chapter_mode", "chapter_target"):
        assert section in data, f"config/performance.yaml 缺 {section} 段"
        assert isinstance(data[section], dict)
    va = data.get("analyst", {}).get("voice_analysis", {})
    assert "use_llm_mode" in va
    assert "drift_alert_threshold" in va


def test_switch_defaults_are_off_and_match_the_dataclass_defaults():
    """配置文件里的默认值必须与 dataclass 默认值一致 ——
    不一致的话「默认关闭」这句话对其中一边就不成立。"""
    import yaml

    from application.engine.services.chapter_mode_settings import (
        ChapterModeRuntimeSettings,
    )
    from application.engine.services.chapter_target_settings import (
        ChapterTargetRuntimeSettings,
    )
    from application.engine.services.context_weight_settings import (
        ContextWeightRuntimeSettings,
    )

    data = yaml.safe_load(_read("config/performance.yaml"))
    cw = ContextWeightRuntimeSettings()
    assert data["context_weights"]["enabled"] is cw.enabled is False
    assert data["context_weights"]["shadow"] is cw.shadow is True
    assert data["context_weights"]["cache_ttl_seconds"] == cw.cache_ttl_seconds
    assert data["context_weights"]["failure_backoff_seconds"] == cw.failure_backoff_seconds

    cm = ChapterModeRuntimeSettings()
    assert data["chapter_mode"]["enabled"] is cm.enabled is False
    assert data["chapter_mode"]["apply"] is cm.apply is False

    ct = ChapterTargetRuntimeSettings()
    assert data["chapter_target"]["enabled"] is ct.enabled is False
    assert data["chapter_target"]["max_deviation_ratio"] == ct.max_deviation_ratio


# ── 5. 缓存与退避必须真的生效 ────────────────────────────────────────────


def test_planner_factories_are_cached():
    """docstring 写「长生命周期」。没有 @lru_cache 的话每次新建实例，
    实例上的缓存永远 miss、退避永远不触发。"""
    tree = _ast("interfaces/api/dependencies.py")
    targets = {"get_context_weight_planner", "get_chapter_mode_planner"}
    cached = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in targets:
            names = {ast.unparse(d) for d in node.decorator_list}
            if any("cache" in n for n in names):
                cached.add(node.name)
    assert cached == targets, f"缺 @lru_cache 的工厂：{targets - cached}"


def _stub_gateway(monkeypatch, module: str, contract=None):
    """把目标模块的 `get_prompt_gateway` 换成不碰 DB 的替身。

    ⚠ **必须自己 stub**。这些用例原先直接用真实 `PromptGateway`，
    单跑时恰好通过、全量跑时被前面测试留下的 DB 状态影响 ⇒ 顺序依赖。
    「单跑绿、全量红」通常就是这个。

    只替掉 ``render``（它要读 DB 里的提示词模板）；
    ``validate_output`` **仍走真实的 pydantic schema** ——
    那正是这些用例要验的东西，替掉等于把断言对象也一起抹了。
    """
    from infrastructure.ai.prompt_gateway import PromptGateway

    real = PromptGateway()

    class _G:
        @staticmethod
        def render(*_a, **_k):
            return type("R", (), {"prompt": "p"})()

        @staticmethod
        def validate_output(contract_, payload):
            if contract is not None:
                return real.validate_output(contract, payload)
            return payload

    monkeypatch.setattr(
        f"{module}.get_prompt_gateway", lambda: _G(), raising=True,
    )


@pytest.mark.asyncio
async def test_planner_caches_and_backs_off(monkeypatch):
    """第二次同章调用必须命中退避（不再打上游）；
    失败后必须进入退避期（不重复打失败的上游）。"""
    from application.engine.services.context_weight_planner import ContextWeightPlanner
    from infrastructure.ai.prompt_contracts.context_weights import CONTEXT_WEIGHTS_CONTRACT

    _stub_gateway(monkeypatch, "application.engine.services.context_weight_planner",
                 contract=CONTEXT_WEIGHTS_CONTRACT)
    monkeypatch.setattr(
        "application.engine.services.context_weight_planner."
        "generation_config_from_profile", lambda name: object(),
    )

    calls = {"n": 0}

    class _LLM:
        async def generate(self, prompt, config):
            calls["n"] += 1
            raise RuntimeError("上游炸了")

    planner = ContextWeightPlanner(_LLM())
    got = await planner.plan_weights(novel_id="n1", chapter_number=1, chapter_outline="x")
    assert got is None, f"上游失败必须返回 None，实际 {got!r}"
    assert calls["n"] == 1
    await planner.plan_weights(novel_id="n1", chapter_number=1, chapter_outline="x")
    assert calls["n"] == 1, "失败后第二次调用没有进入退避期"


@pytest.mark.asyncio
async def test_planner_returns_cached_weights_without_calling_llm(monkeypatch):
    """成功结果进缓存 ⇒ 同章第二次直接返回，LLM 调用次数不增加。"""
    import json

    from application.engine.services.context_weight_planner import ContextWeightPlanner
    from infrastructure.ai.prompt_contracts.context_weights import CONTEXT_WEIGHTS_CONTRACT

    payload = {
        "evaluated": True,
        "weights": {"recent_chapters": 0.9},
        "reason": "",
    }
    calls = {"n": 0}

    class _LLM:
        async def generate(self, prompt, config):
            calls["n"] += 1
            return type("R", (), {"content": json.dumps(payload, ensure_ascii=False)})()

    _stub_gateway(monkeypatch, "application.engine.services.context_weight_planner",
                 contract=CONTEXT_WEIGHTS_CONTRACT)
    monkeypatch.setattr(
        "application.engine.services.context_weight_planner."
        "generation_config_from_profile", lambda name: object(),
    )

    planner = ContextWeightPlanner(_LLM())
    first = await planner.plan_weights(novel_id="n1", chapter_number=1, chapter_outline="x")
    second = await planner.plan_weights(novel_id="n1", chapter_number=1, chapter_outline="x")
    assert first == second == {"recent_chapters": 0.9}
    assert calls["n"] == 1, f"缓存未生效，LLM 被调用了 {calls['n']} 次"


# ── 6. 来源标记必须落产物 ────────────────────────────────────────────────


def test_foreshadowing_carries_the_resolve_offset_source():
    """落库后仍要能分辨「AI 判的回收节奏」与「代码兜底的 +5」。"""
    from domain.novel.value_objects.foreshadowing import (
        Foreshadowing,
        ForeshadowingStatus,
        ImportanceLevel,
    )

    f = Foreshadowing(
        id="f1", planted_in_chapter=3, description="x",
        importance=ImportanceLevel.MEDIUM, status=ForeshadowingStatus.PLANTED,
        resolve_offset_source="fallback", resolve_offset_note="模型未给出预估回收节奏",
    )
    assert f.resolve_offset_source == "fallback"
    # 默认必须是空串，**不是** "ai" —— 否则旧数据会被读成「AI 的判断」
    bare = Foreshadowing(
        id="f2", planted_in_chapter=3, description="y",
        importance=ImportanceLevel.MEDIUM, status=ForeshadowingStatus.PLANTED,
    )
    assert bare.resolve_offset_source == ""


def test_source_survives_the_registry_payload_round_trip():
    from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
    from domain.novel.value_objects.novel_id import NovelId
    from domain.novel.value_objects.foreshadowing import (
        Foreshadowing,
        ForeshadowingStatus,
        ImportanceLevel,
    )
    from infrastructure.persistence.mappers.foreshadowing_mapper import (
        ForeshadowingMapper,
    )

    registry = ForeshadowingRegistry(id="r1", novel_id=NovelId("n1"))
    registry.register(Foreshadowing(
        id="f1", planted_in_chapter=3, description="x",
        importance=ImportanceLevel.MEDIUM, status=ForeshadowingStatus.PLANTED,
        resolve_offset_source="fallback", resolve_offset_note="兜底 +5",
    ))
    blob = ForeshadowingMapper.to_dict(registry)
    assert blob["foreshadowings"][0]["resolve_offset_source"] == "fallback"
    assert blob["foreshadowings"][0]["resolve_offset_note"] == "兜底 +5"

    back = ForeshadowingMapper.from_dict(blob)
    assert back.foreshadowings[0].resolve_offset_source == "fallback"
    assert back.foreshadowings[0].resolve_offset_note == "兜底 +5"


def test_legacy_payload_without_source_stays_empty_not_ai():
    """旧 payload 没有这个字段 ⇒ 落空串。默认填 "ai" 会把未标注读成 AI 判断。"""
    from infrastructure.persistence.mappers.foreshadowing_mapper import (
        ForeshadowingMapper,
    )

    legacy = {
        "id": "r1", "novel_id": "n1",
        "foreshadowings": [{
            "id": "f1", "planted_in_chapter": 3, "description": "x",
            "importance": 2,          # ImportanceLevel 是 int enum
            "status": "planted",
            "suggested_resolve_chapter": 8, "resolved_in_chapter": None,
            "emotional_weight": 0.5, "binding_level": 1,
        }],
        "subtext_entries": [],
    }
    got = ForeshadowingMapper.from_dict(legacy)
    assert got.foreshadowings[0].resolve_offset_source == ""


def test_storyline_end_returns_source_and_is_logged():
    """G-03：来源必须可观察。此前 `_storyline_end` 把 source 直接丢掉。"""
    from application.world.services import chapter_narrative_sync as cns

    end, source, note = cns._storyline_end(10, 40)
    assert end == 40 and source == "ai"
    end_fb, source_fb, note_fb = cns._storyline_end(10, None)
    assert end_fb == 22 and source_fb == "fallback" and note_fb
    assert hasattr(cns, "_log_storyline_end_source")


def test_storyline_end_call_sites_actually_pass_the_ai_value():
    """⚠ 复审实证缺口④：把调用点的实参换成 `None`、原表达式留在**注释**里，
    3 条 G-03 测试全绿 —— `inspect.getsource` 的子串断言被注释骗过。

    这里改成**AST 判定**：注释不在 AST 里。
    两跳都要查 ——
      ① 读取侧：`_make_storyline_from_progress_item` 的调用方必须从
         `progress_item` 里取 `estimated_chapter_end`；
      ② 转发侧：该函数内部必须把 `ai_end` 传给 `_storyline_end`。
    只查 ② 会漏掉 ① 的断链（上一版就栽在这）。
    """
    tree = _ast("application/world/services/chapter_narrative_sync.py")

    # ① 读取侧
    maker = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "_make_storyline_from_progress_item"
    )
    maker_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Attribute)
             and n.func.attr == "_make_storyline_from_progress_item")
            or (isinstance(n.func, ast.Name)
                and n.func.id == "_make_storyline_from_progress_item")
        )
    ]
    assert maker_calls, "没找到 _make_storyline_from_progress_item 的调用"
    reads_ai_end = any(
        any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and sub.args[0].value == "estimated_chapter_end"
            for sub in ast.walk(call)
        )
        for call in maker_calls
    )
    assert reads_ai_end, (
        "没有任何调用方从 progress_item 里读 estimated_chapter_end —— "
        "AI 给的收尾章到不了 G-03"
    )

    # ② 转发侧
    end_calls = [
        n for n in ast.walk(maker)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_storyline_end"
    ]
    assert end_calls, "没找到 _storyline_end 调用"
    for call in end_calls:
        args = [ast.unparse(a) for a in call.args]
        assert len(args) >= 2, f"_storyline_end 只传了 {args} —— AI 入参不可达"
        assert args[1] == "ai_end", (
            f"第二个实参是 {args[1]!r}，不是形参 ai_end"
        )


def test_pacing_directive_block_is_absent_when_the_directive_is_empty():
    """⚠ 复审实证缺口⑤：删掉模板里的 `{% if pacing_directive %}`（保留占位符）
    后 3 条 pacing 测试全绿；真实渲染却会让**非余韵章**也出现一个空的
    「本章节奏指令」头 —— 那是个虚假信号，读者/模型会以为本章有节奏约束。

    这里直接渲染，断言空串时**整段不出现**。
    """
    from infrastructure.ai.prompt_template_engine import get_template_engine

    user_tpl = _read(
        "infrastructure/ai/prompt_packages/nodes/planning-chapter-preplan/user.md"
    )
    system_tpl = ""
    engine = get_template_engine()
    base_vars = {
        "chapter_number": 5,
        "chapter_title": "第五章",
        "act_chapter_plan": "主线推进",
        "continuity_context": "承接摘要",
        "pacing_directive": "",
    }

    out_empty = engine.render(system_tpl, user_tpl, base_vars).user
    assert "本章节奏指令" not in out_empty, (
        "空指令时仍渲染出「本章节奏指令」标题 —— 虚假信号"
    )
    assert "余韵" not in out_empty

    out_full = engine.render(
        system_tpl, user_tpl,
        {**base_vars, "pacing_directive": "【余韵章：高潮余波】节奏放缓"},
    ).user
    assert "本章节奏指令" in out_full
    assert "【余韵章：高潮余波】节奏放缓" in out_full


def test_offset_fallbacks_counter_is_actually_read():
    """`offset_fallbacks` 此前只累加从不读取 —— 是个死变量。"""
    src = _read("application/world/services/chapter_narrative_sync.py")
    uses = [
        line for line in src.splitlines()
        if "offset_fallbacks" in line
        and "= 0" not in line and "+= 1" not in line
    ]
    assert uses, "offset_fallbacks 只被赋值/自增，从未被读取"


# ── 7. 契约必须真的被用 ──────────────────────────────────────────────────


def test_both_planners_route_through_validate_output():
    """契约里声明了 output_schema，手写解析等于让它形同虚设。

    ⚠ 这条只是**补充**：它验的是源码里出现了 `validate_output`，
    属于弱信号（注释里写一遍就���通过）。真正的行为保证在上面两条
    「跑一个契约外输出看它是否被拒」的用例里 —— 上一轮变异验证正是
    用这条源码断言当主要防线，结果打不动。
    """
    for rel in (
        "application/engine/services/context_weight_planner.py",
        "application/engine/services/chapter_mode_planner.py",
    ):
        src = _read(rel)
        assert "validate_output" in src, f"{rel} 没有走 PromptGateway.validate_output"


def test_chapter_mode_schema_refuses_coerced_booleans():
    """pydantic lax 模式会把 ``"true"`` 静默强转成 ``True`` ——
    含糊输出被读成斩钉截铁的「是」，正是本轮红线。"""
    from application.engine.services.chapter_mode_schema import ChapterModeLlmPayload

    assert ChapterModeLlmPayload(needs_buffer=True).needs_buffer is True
    assert ChapterModeLlmPayload(needs_buffer=False).needs_buffer is False
    assert ChapterModeLlmPayload(needs_buffer=None).needs_buffer is None
    for bad in ("true", "false", 1, 0, "yes", ""):
        with pytest.raises(Exception):
            ChapterModeLlmPayload(needs_buffer=bad)


def test_chapter_mode_schema_normalizes_signals_instead_of_rejecting():
    """单条脏数据不该让整次判定作废，但脏项也不能原样流进产物。"""
    from application.engine.services.chapter_mode_schema import ChapterModeLlmPayload

    p = ChapterModeLlmPayload(
        needs_buffer=True, reason="ok",
        signals=["  张力 87  ", "", None, 123, "a", "a", "  张力 87  "],
    )
    got = p.signals
    assert "  张力 87  " not in got
    assert "" not in got
    assert None not in got
    assert len(got) == len(set(got)), "未去重"
    assert all(isinstance(s, str) for s in got)
    assert ChapterModeLlmPayload(signals=None).signals == []
    assert ChapterModeLlmPayload(signals="不是列表").signals == []


def test_evaluated_voice_drift_carries_the_real_computed_at(monkeypatch):
    """**已评估**路径必须把 DB 里的 `computed_at` 真值带出来。

    只测响应模型不够 —— 模型上有字段不等于赋值处真的填了。
    变异验证发现：把 `computed_at=latest.get(...)` 改成 `None` 时，
    响应模型层的测试全绿。
    """
    import interfaces.api.v1.workbench.monitor as mon

    class _Novel:
        novel_id = type("N", (), {"value": "n1"})()

    monkeypatch.setattr(
        "interfaces.api.v1.workbench.monitor.get_novel_repository",
        lambda: type("R", (), {"get_by_id": staticmethod(lambda _i: _Novel())})(),
    )
    monkeypatch.setattr(
        "interfaces.api.v1.workbench.monitor.get_voice_fingerprint_repository",
        lambda: type("R", (), {
            "get_by_novel": staticmethod(lambda _n, _p: {"sample_count": 20}),
        })(),
    )
    monkeypatch.setattr(
        "interfaces.api.v1.workbench.monitor.SqliteChapterStyleScoreRepository",
        lambda _db: type("R", (), {
            "get_latest_by_novel": staticmethod(lambda _n: {
                "chapter_number": 7,
                "similarity_score": 0.55,       # drift = 0.45 → warning
                "computed_at": "2026-10-18T09:30:00",
            }),
        })(),
    )
    monkeypatch.setattr(
        "interfaces.api.v1.workbench.monitor.get_database", lambda: object(),
    )

    got = mon._get_voice_drift_sync("n1")
    assert got is not None and got.evaluated is True
    assert got.computed_at == "2026-10-18T09:30:00", (
        f"已评估路径没有把真实评分时刻带出来：{got.computed_at!r}"
    )
    assert got.status == "warning"
    assert got.last_scored_chapter == 7


@pytest.mark.asyncio
async def test_chapter_mode_planner_rejects_coerced_boolean_at_runtime(monkeypatch):
    """**行为**证明解析走的是契约，而不是「源码里出现了 validate_output」。

    模型回 `{"needs_buffer": "true"}`：契约判定「不是明确结论」⇒ 未判定。
    若有人把 `_parse` 改回宽松解析，这个字符串会变成 truthy 的 True，
    于是含糊输出被当成「AI 明确说是余韵章」。
    """
    from application.engine.services.chapter_mode_planner import ChapterModePlanner
    from infrastructure.ai.prompt_contracts.chapter_mode import CHAPTER_MODE_CONTRACT

    _stub_gateway(monkeypatch, "application.engine.services.chapter_mode_planner",
                 contract=CHAPTER_MODE_CONTRACT)
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_planner.generation_config_from_profile",
        lambda name: object(),
    )

    class _LLM:
        async def generate(self, prompt, config):
            return type("R", (), {"content": '{"needs_buffer": "true", "reason": "?"}'})()
    got = await ChapterModePlanner(_LLM()).decide(
        novel_id="n1", chapter_number=1, outline="本章大纲",
    )
    assert got is None, (
        f"字符串 \"true\" 被当成了明确结论：{got!r}"
    )


@pytest.mark.asyncio
async def test_chapter_mode_planner_rejects_junk_signals_via_contract(monkeypatch):
    """契约归一化脏 signals，而不是把整次判定作废。

    反例：把 `_parse` 换成「原样返回 dict」，脏 signals 会直接流进产物。
    """
    from application.engine.services.chapter_mode_planner import ChapterModePlanner
    from infrastructure.ai.prompt_contracts.chapter_mode import CHAPTER_MODE_CONTRACT

    _stub_gateway(monkeypatch, "application.engine.services.chapter_mode_planner",
                 contract=CHAPTER_MODE_CONTRACT)
    monkeypatch.setattr(
        "application.engine.services.chapter_mode_planner.generation_config_from_profile",
        lambda name: object(),
    )

    class _LLM:
        async def generate(self, prompt, config):
            return type("R", (), {"content": (
                '{"needs_buffer": true, "reason": "ok", '
                '"signals": ["  张力 87  ", "", null, 123, "a", "a"]}'
            )})()
    got = await ChapterModePlanner(_LLM()).decide(
        novel_id="n1", chapter_number=1, outline="本章大纲",
    )
    assert got is not None and got["needs_buffer"] is True
    assert got["signals"], "脏 signals 把整次判定作废了"
    assert all(isinstance(s, str) and s.strip() for s in got["signals"])
    assert len(got["signals"]) == len(set(got["signals"])), "signals 未去重"


@pytest.mark.asyncio
async def test_context_weight_planner_rejects_out_of_contract_payload(monkeypatch):
    """契约外结构 ⇒ 未判定（返回 None），不得照单全收。"""
    from application.engine.services.context_weight_planner import ContextWeightPlanner
    from infrastructure.ai.prompt_contracts.context_weights import CONTEXT_WEIGHTS_CONTRACT

    _stub_gateway(monkeypatch, "application.engine.services.context_weight_planner",
                 contract=CONTEXT_WEIGHTS_CONTRACT)
    monkeypatch.setattr(
        "application.engine.services.context_weight_planner."
        "generation_config_from_profile", lambda name: object(),
    )

    class _LLM:
        async def generate(self, prompt, config):
            # weights 不是 dict —— 契约不认
            return type("R", (), {"content": '{"evaluated": true, "weights": "不是字典"}'})()
    got = await ContextWeightPlanner(_LLM()).plan_weights(
        novel_id="n1", chapter_number=1, chapter_outline="x",
    )
    assert got is None, f"不合契约的输出被当成了有效权重：{got!r}"



# ── 7. D-02 的 T0 层必须拒绝权重（复审建议 7：结构正确但无测试钉住）────────


def test_t0_truncation_has_no_slot_weights_parameter():
    """T0 是「不可压缩」层，**结构上**就不接受权重。

    `truncate_t0_slots` 没有 `slot_weights` 形参 ⇒ 权重无法施加到 T0。
    这比「运行时验证它被拒绝」更强：连传都传不进去。
    """
    import inspect

    from application.engine.services.context_budget_policy import truncate_t0_slots

    params = list(inspect.signature(truncate_t0_slots).parameters)
    assert "slot_weights" not in params, (
        f"truncate_t0_slots 出现了 slot_weights：{params} —— "
        f"T0 是不可压缩层，权重不应影响它"
    )
    assert "weights" not in " ".join(params)


def test_t0_never_routes_through_allocate_tier():
    """分配器对 T0 只能走截断路径，不能调带权重的 `allocate_tier`。

    用 AST 判定调用形态，而不是数 `_allocate_tier` 的调用点 ——
    形态判定能直接回答「T0 有没有可能拿到权重」。
    注意 T0 走的是包装方法 `self._truncate_t0_slots`，不是模块函数直调。
    """
    tree = _ast("application/engine/services/context_budget_allocator.py")

    allocate_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "allocate"
    )
    truncate_calls = [
        n for n in ast.walk(allocate_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and "truncate_t0" in n.func.attr
    ]
    assert truncate_calls, "allocate() 里没有对 T0 的截断调用 —— T0 语义可能已变"

    # T1/T2/T3 才允许走带权重的 _allocate_tier
    weighted_calls = [
        n for n in ast.walk(allocate_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_allocate_tier"
    ]
    assert weighted_calls, "没找到 T1/T2/T3 的分配调用"

    # 包装层不得偷偷传权重
    wrapper = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_truncate_t0_slots"
    )
    for arg in wrapper.args.args:
        assert "weight" not in ast.unparse(arg).lower(), (
            f"T0 截断包装层传了权重：{ast.unparse(arg)}"
        )
    inner = [
        n for n in ast.walk(wrapper)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "truncate_t0_slots"
    ]
    assert inner, "_truncate_t0_slots 没有转调模块函数 truncate_t0_slots"
    for call in inner:
        for kw in call.keywords:
            assert "weight" not in (kw.arg or ""), (
                f"T0 截断调用传了 {kw.arg}"
            )
        for arg in call.args[1:]:
            assert "weight" not in ast.unparse(arg).lower(), (
                f"T0 截断实参含权重：{ast.unparse(arg)}"
            )


def test_allocate_tier_is_only_reached_from_t1_and_below():
    """`allocate_tier` 带 `slot_weights` 形参 ⇒ 只有 T1/T2/T3 能调它。"""
    import inspect

    from application.engine.services.context_budget_policy import allocate_tier

    assert "slot_weights" in inspect.signature(allocate_tier).parameters

# ── 8. 墙钟守卫必须真的抓得住 ────────────────────────────────────────────


def test_perf_guard_uses_a_size_where_quadratic_actually_exceeds_threshold():
    """5000 规模下规范 O(n²) 只要 0.728s < 1s 阈值 —— 抓不住。
    20000 规模下 O(n²) ≈ 11s，真实实现 15ms。

    这条钉的是**规模**，不是阈值：阈值收紧到 0.2s 会把当初那次的门禁抖动带回来。
    """
    import re

    src = _read("tests/unit/domain/bible/services/test_relationship_engine.py")
    fn = re.search(
        r"def test_large_graph_common_connections\(self\):(.*?)(?=\n    def |\Z)",
        src, re.S,
    )
    assert fn, "找不到 test_large_graph_common_connections"
    body = fn.group(1)
    m = re.search(r"for i in range\((\d+)\):", body)
    assert m, "找不到规模循环"
    size = int(m.group(1))
    assert size >= 20000, (
        f"共同连接测试规模仍是 {size}；该规模下 O(n²) 只要 0.728s，"
        f"1s 阈值抓不住（实测 20000 时 O(n²)≈11s / 真实 15ms）"
    )
    assert f"assert len(common) == {size}" in body, (
        "断言里的期望值与构造规模不一致 —— 那条断言实际没在承重"
    )


# ── 恢复：091cded5 静默删掉了 819aac33 的这两条守卫 ───────────────────────
#
# 第三轮复审发现：我在 091cded5 改写「flag → None 语义」时，把 819aac33
# 刚补好的两条守卫**整段删掉**了，而它们当时正挡着两类回归。独立变异证实
# 两类回退后仍全绿：
#   · audit 两分支的标记/取值方向对调
#   · D-02 主路径 slot_weights=slot_weights 改成 =None
# 「改测试时顺手删掉旧守卫」本身就是回归 —— 记在这里以防第三次。


def test_audit_branch_direction_is_not_swapped():
    """⚠ 本条曾被静默删除。audit 的两个分支方向**不能对调**。

    方向语义：评估过的分支写真值；未评估的分支写 None。
    对调之后「没测过」会被报成真实张力（红线），或反过来把真实值丢掉。
    这里按**分支归属**判定，不是「两个值都出现过就算过」。
    """
    tree = _ast("engine/runtime/audit_delegate.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_chapter_audit"
    )

    def hits(target, name):
        if isinstance(target, ast.Name):
            return target.id == name
        return isinstance(target, ast.Attribute) and target.attr == name

    def values_in(body, name):
        return [
            ast.unparse(node.value)
            for node in body
            for sub in ast.walk(node)
            if isinstance(sub, ast.Assign) and any(hits(t, name) for t in sub.targets)
        ]

    branches = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and "tension_evaluated" in ast.unparse(n.test)
        and (values_in(n.body, "last_chapter_tension") or n.orelse)
    ]
    assert branches, "没找到按 tension_evaluated 分支的张力写入"

    for node in branches:
        if_values = values_in(node.body, "last_chapter_tension")
        else_values = values_in(node.orelse, "last_chapter_tension") if node.orelse else []
        if if_values:
            assert "None" not in if_values, (
                f"评估过的分支写了 None：{if_values} —— 真实值会被丢掉"
            )
        if else_values:
            assert "0" not in else_values, (
                f"未评估分支写了 0：{else_values} —— 「没测过」又变回「张力 0」"
            )

    # 至少存在一对：评估分支写真值 + 未评估分支写 None
    assert any(
        values_in(n.body, "last_chapter_tension")
        and values_in(n.orelse, "last_chapter_tension") if n.orelse else False
        for n in branches
    ), f"没有任何一对分支同时写两侧：{[n.lineno for n in branches]}"


def test_workflow_main_path_forwards_the_actual_slot_weights_value():
    """⚠ 本条曾被静默删除。主路径必须转发**原值**，不是 None。

    `prepare_chapter_generation` 的主路径是生产 daemon 恒走的那一条
    （`start_daemon` -> `StoryPipelineRunner` -> `ctx.chapter_workflow`）。
    改成 `=None` 权重算完即丢，而旧守卫只查 `"slot_weights" in body`，
    根本抓不住 —— token 在、值没了。
    """
    tree = _ast("application/workflows/auto_novel_generation_workflow.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "prepare_chapter_generation"
    )
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "build_structured_context"
    ]
    assert calls, "没找到 build_structured_context 调用"
    for call in calls:
        forwarded = [kw for kw in call.keywords if kw.arg == "slot_weights"]
        assert forwarded, "存在不传 slot_weights 的 build_structured_context 调用"
        for kw in forwarded:
            assert ast.unparse(kw.value) == "slot_weights", (
                f"slot_weights 被改成了 {ast.unparse(kw.value)!r} —— "
                f"生产 daemon 恒走主路径，权重会算完即丢"
            )


def test_signals_validator_drops_non_strings_instead_of_stringifying():
    """⚠ 复审指出：signals 的「真 drop」修复当时**没有守卫**。

    把实现改回 `str(item)`（None -> "None"、123 -> "123"）后相关测试仍全绿 ——
    那等于把「把模型没给伪装成模型说了 None」的缺陷又放回去。
    """
    from application.engine.services.chapter_mode_schema import ChapterModeLlmPayload

    got = ChapterModeLlmPayload(
        needs_buffer=True, reason="ok",
        signals=["  张力 87  ", "", None, 123, True, "a", "a"],
    ).signals
    assert "None" not in got, f"None 被 str() 伪装成了 'None'：{got}"
    assert "123" not in got, f"数字被 str() 伪装成了信号：{got}"
    assert "True" not in got, f"布尔被 str() 伪装成了信号：{got}"
    assert "" not in got
    assert got == ["张力 87", "a"], f"归一化结果不对：{got}"


# ── 第三轮复审项 ─────────────────────────────────────────────────────────


def test_code_adjusted_value_is_not_labelled_as_ai():
    """⚠ 代码抬高的取值**不得**标 'ai'。

    第三轮复审指出：上一轮把 source 落库之后，`resolve_storyline_end(10, 8)`
    会把代码抬到 11、却在产物里写 source='ai' —— 模型给的是 8，落库声称
    「AI 判断第 11 章」。那是**落库后的假来源**，比落日志时的不精确严重：
    下游会把它当可信 provenance 消费。
    """
    from application.world.services.foreshadow_pacing import (
        SOURCE_AI,
        SOURCE_AI_ADJUSTED,
        resolve_storyline_end,
    )

    end, source, note = resolve_storyline_end(10, 8)
    assert end == 11, "不变量：收尾章必须晚于当前章"
    assert source == SOURCE_AI_ADJUSTED, (
        f"代码抬高的取值被标成了 {source!r} —— 落库后会伪造 provenance"
    )
    assert source != SOURCE_AI
    assert "模型" in note and "抬" in note, "说明里应写明是代码抬升"

    # 原值确实来自模型时才是 ai
    end2, source2, _ = resolve_storyline_end(10, 40)
    assert (end2, source2) == (40, SOURCE_AI)


def test_state_updater_reads_the_aligned_offset_key():
    """G-01 的 AI 优先通道必须**真的可达**。

    第三轮复审：state_updater 读 `resolve_offset`，而 prompt 与
    narrative_sync 都用 `suggested_resolve_offset` ⇒ 字段名对不上，
    模型给什么都不影响结果，「AI 优先」是条死通道。

    这里判定读的是**对齐后的键名**，且旧键作为兼容保留。
    """
    tree = _ast("application/analyst/services/state_updater.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "update_chapter_state"
    ) if any(
        isinstance(n, ast.FunctionDef) and n.name == "update_chapter_state"
        for n in ast.walk(tree)
    ) else None
    if fn is None:
        # 兜底：函数名可能不同，直接扫模块里的字符串字面量
        fn = tree
    src = ast.unparse(fn)
    assert "suggested_resolve_offset" in src, (
        "没有读 suggested_resolve_offset —— 与 prompt / narrative_sync 键名不一致，"
        "模型给的偏移根本走不到"
    )
    assert "resolve_offset_from_ai" in src or "resolve_offset(" in src, (
        "没有用共享裁决器 —— 手写 `if x and isinstance(...)` 会把模型明说的 0 "
        "和字符串当成缺失，再写下「模型未给出」这句假说明"
    )


def test_state_updater_does_not_treat_zero_as_missing():
    """模型明说的 0 不算「未给出」——由共享裁决器钳制，而不是走兜底分支。"""
    from application.world.services.foreshadow_pacing import resolve_offset

    from application.world.services.foreshadow_pacing import SOURCE_AI_ADJUSTED

    offset_zero, source_zero, note_zero = resolve_offset(0)
    assert source_zero != "fallback", "0 是合法的（会被钳到 2），不该走兜底"
    assert source_zero == SOURCE_AI_ADJUSTED, (
        f"被钳过的取值标成 {source_zero!r} —— 落库后下游会以为「第 2 章是模型说的」，"
        f"而模型给的是 0"
    )
    assert offset_zero == 2
    assert "钳" in note_zero

    # 上界同样
    _, source_high, _ = resolve_offset(40)
    assert source_high == SOURCE_AI_ADJUSTED

    _, source_none, _ = resolve_offset(None)
    assert source_none == "fallback"

    # 字符串数字也能解析，不该判成缺失
    _, source_str, _ = resolve_offset("7")
    assert source_str != "fallback", "'7' 是可解析的，不该当成模型没给"


# ── 第四轮复审 §3.4 的守卫盲区 ───────────────────────────────────────────


def test_audit_branch_condition_polarity_is_positive():
    """⚠ 第四轮复审发现的盲区：只判「值落在哪个分支」时，
    把 `if tension_evaluated:` 改成 `if not tension_evaluated:`（等价破坏）
    仍然全绿 —— 因为两个分支体还在，只是判断反了。

    这里断言条件**必须正向表达**「已评估」。

    ⚠ 第八轮复审：上一版用「精确等于 `tension_evaluated`」判极性，
    会把**合法的**等价写法 `if tension_evaluated is True:` / `bool(...)`
    判红 —— 而同一文件里另一条守卫恰好允许这些形态，两条自相矛盾：
    合法重构会让旧用例 RED、新用例 GREEN。
    本版改为**语义判定**：把条件归一化成「是否等价于 bool(tension_evaluated)」。
    """
    tree = _ast("engine/runtime/audit_delegate.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_chapter_audit"
    )
    conds = [
        n.test for n in ast.walk(fn)
        if isinstance(n, ast.If) and "tension_evaluated" in ast.unparse(n.test)
    ]
    assert conds, "没找到按 tension_evaluated 分支的 if"
    for test in conds:
        # 归一化：bool(tension_evaluated) 与 tension_evaluated 视为同义；
        # `is True` / `is 1` 视为真值侧；任何 `not` 视为极性反转。
        src = ast.unparse(test)
        norm = src.replace(" ", "").replace("(", "").replace(")", "")
        negated = norm.split("tension_evaluated")[0].endswith("not")
        assert not negated, f"条件带否定（极性反转）：{src!r}"
        tail = norm.split("tension_evaluated", 1)[1]
        # 尾部只能是「是 True / 是 1 / 空」——即真值侧；出现 ==False 等即语义反转
        assert tail in ("", "isTrue", "is1", "andTrue", "orTrue"), (
            f"条件不是「已评估为真」的语义（{src!r}）—— 未评估分支可能被当成评估过"
        )


def test_state_updater_delegates_the_offset_decision():
    """`update_from_chapter` 必须走共享裁决器/纯函数，不能自己手写判定。

    ⚠ 第六轮复审指出上一版这条是**源码文本断言**，可在「把调用挪进嵌套 def」
    时绕过（`ast.walk` 会下降进嵌套 def）。本版已改为：
      · 结构部分只判「函数体内出现该调用名」（不做可达性推断）
      · 真正的行为断言在
        `tests/unit/test_review_round1_state_updater_e2e.py` ——
        真跑 update_from_chapter 验产物上的 source/note。
    """
    tree = _ast("application/analyst/services/state_updater.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "update_from_chapter"
    )
    src = ast.unparse(fn)
    assert "_resolve_plant_offset" in src, "该函数内没有调用 G-01 的裁决纯函数"
    assert "suggested_resolve_offset" in src, "该函数内没有读对齐后的键名"
    assert "resolve_offset" in src, "该函数内没有读兼容旧键"


def test_adjusted_source_survives_the_repository_round_trip():
    """⚠ 第四轮复审：`ai_adjusted` 只覆盖了纯函数，没有落库往返测试。

    而「代码改写了取值却标成 ai」这个缺陷恰恰是在**落库后**才致命的 ——
    必须在真实仓储上验证 source 与 note 都原样保留。
    """
    import importlib.util

    from domain.novel.entities.storyline import Storyline
    from domain.novel.value_objects.novel_id import NovelId
    from domain.novel.value_objects.storyline_status import StorylineStatus
    from domain.novel.value_objects.storyline_type import StorylineType
    from infrastructure.persistence.database.sqlite_storyline_repository import (
        SqliteStorylineRepository,
    )

    # 复用既有的裸 sqlite3 夹具（真实 schema 派生 DDL + 轻量 db 壳）
    spec = importlib.util.spec_from_file_location(
        "_sfn",
        REPO_ROOT / "tests" / "unit" / "infrastructure" / "persistence"
        / "test_storyline_new_fields.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repo = SqliteStorylineRepository(mod._make_db())

    repo.save(Storyline(
        id="sl-adj", novel_id=NovelId("n1"),
        storyline_type=StorylineType.MAIN_PLOT, status=StorylineStatus.ACTIVE,
        estimated_chapter_start=1, estimated_chapter_end=11,
        estimated_chapter_end_source="ai_adjusted",
        estimated_chapter_end_note="模型给的 8 不晚于当前章 10，已抬到 11",
    ))
    back = repo.get_by_id("sl-adj")
    assert back is not None
    assert back.estimated_chapter_end == 11
    assert back.estimated_chapter_end_source == "ai_adjusted", (
        f"落库往返丢了 adjusted 标注：{back.estimated_chapter_end_source!r}"
    )
    assert "抬到 11" in back.estimated_chapter_end_note

    # 缺省必须是空串 —— 默认填 ai 会把未标注数据读成「模型判断」
    repo.save(Storyline(
        id="sl-none", novel_id=NovelId("n1"),
        storyline_type=StorylineType.MAIN_PLOT, status=StorylineStatus.ACTIVE,
        estimated_chapter_start=1, estimated_chapter_end=50,
    ))
    bare = repo.get_by_id("sl-none")
    assert bare.estimated_chapter_end_source == ""
    assert bare.estimated_chapter_end_source != "ai"


def test_g03_log_covers_all_three_sources(caplog):
    """G-03 建档日志必须让三种来源互相可分辨。

    ⚠ 第六轮复审指出上一版是**源码 marker 断言** ——
    在注释里写 `SOURCE_AI_ADJUSTED:` 就能假绿。这里改用 caplog 实跑。

    ⚠ 第三版（本次）修正了第一版的两个空洞：
      1. 没传 ``storyline_repository`` ⇒ ``has_storyline_activity`` 整块被跳过，
         三次都没发日志，断言却只查了「缺 frag」而没查「日志是否真的发过」。
      2. 只断言目标 frag 存在 ⇒ 把三分支合并成一条「采用模型判断」也能过。
         现在额外断言**另外两个 frag 都不出现**。
    """
    import logging

    import application.analyst.services.state_updater as su

    def build(end_value):
        class _ForeshadowRepo:
            def __init__(self):
                from domain.novel.entities.foreshadowing_registry import (
                    ForeshadowingRegistry,
                )
                from domain.novel.value_objects.novel_id import NovelId
                self.registry = ForeshadowingRegistry(
                    id="reg", novel_id=NovelId("n1"))

            def get_by_novel_id(self, _n):
                return self.registry

            def save(self, _r):
                pass

        class _StorylineRepo:
            def __init__(self):
                self.saved = []

            def save(self, storyline):
                self.saved.append(storyline)

        repo = _StorylineRepo()
        updater = su.StateUpdater(
            bible_repository=None,
            foreshadowing_repository=_ForeshadowRepo(),
            storyline_repository=repo,
        )
        from domain.novel.value_objects.chapter_state import ChapterState

        cs = ChapterState(
            new_characters=[], character_actions=[], relationship_changes=[],
            foreshadowing_planted=[], foreshadowing_resolved=[], events=[],
            new_storylines=[{"name": "线", "type": "main",
                             "description": "d",
                             "estimated_chapter_end": end_value}],
        )
        updater.update_from_chapter(
            novel_id="n1", chapter_number=3, chapter_state=cs)
        assert repo.saved, "故事线没被建档 —— 日志断言会在空日志上假红"
        return repo.saved[0]

    logger_name = "application.analyst.services.state_updater"
    frags = {"ai": "采用模型判断",
             "ai_adjusted": "被代码改写",
             "fallback": "兜底"}

    # chapter_number=3：40 合法直用；2 违反「收尾章 > 当前章」被抬升 ⇒ adjusted；None 兜底
    for value, expect in [(40, "ai"), (2, "ai_adjusted"), (None, "fallback")]:
        with caplog.at_level(logging.INFO, logger=logger_name):
            caplog.clear()
            sl = build(value)
        text = "\n".join(r.getMessage() for r in caplog.records)
        # 先断言日志确实发过 —— 否则「缺 frag」和「压根没跑」分不开
        assert any("故事线收尾章" in m for m in
                   (r.getMessage() for r in caplog.records)), (
            f"end={value!r} 未产生任何故事线日志：\n{text}")
        assert frags[expect] in text, f"end={value!r} 的建档日志缺 {frags[expect]!r}：\n{text}"
        for other, frag in frags.items():
            if other == expect:
                continue
            assert frag not in text, (
                f"end={value!r} 判为 {expect}，日志里却同时出现 {other} 的措辞 "
                f"{frag!r} —— 三态不可分辨：\n{text}"
            )
        # 日志措辞必须与落库的 source 同向，不许日志说「模型判断」而产物标 ai_adjusted
        assert sl.estimated_chapter_end_source == expect, (
            f"日志说 {expect}、落库 source 是 "
            f"{sl.estimated_chapter_end_source!r}"
        )


def test_audit_polarity_check_allows_is_true_form():
    """正向样本：`is True` 写法必须被上面那条判绿。

    ⚠ 第八轮复审指出本条与 `test_audit_branch_condition_polarity_is_positive`
    自相矛盾（旧版要求精确等于 `tension_evaluated`，本条却允许 `is True`）。
    已把两者合并为一条语义判定，本条改为它的**正向样本** ——
    用真实文件的 AST 喂进同一段归一化逻辑，确认合法写法不会被误判。

    这不是重复断言：上面那条判的是**生产代码当前写法**，本条判的是
    「若将来有人改写成 `is True`，守卫会不会假红」。
    """
    tree = _ast("engine/runtime/audit_delegate.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_chapter_audit"
    )

    def polarity_ok(test_node) -> bool:
        """与 test_audit_branch_condition_polarity_is_positive 同一套判定。"""
        norm = ast.unparse(test_node).replace(" ", "").replace("(", "").replace(")", "")
        if norm.split("tension_evaluated")[0].endswith("not"):
            return False
        return norm.split("tension_evaluated", 1)[1] in (
            "", "isTrue", "is1", "andTrue", "orTrue")

    # 正向样本：三种合法写法都必须判绿
    for src in ("tension_evaluated", "tension_evaluated is True",
                "bool(tension_evaluated)"):
        assert polarity_ok(ast.parse(src, mode="eval").body), (
            f"合法写法被误判为极性反转：{src!r}"
        )
    # 反向样本：否定与 `is False` 必须判红（否则上面那条守卫形同虚设）
    for src in ("not tension_evaluated", "tension_evaluated is False",
                "tension_evaluated == False"):
        assert not polarity_ok(ast.parse(src, mode="eval").body), (
            f"极性反转写法被放过了：{src!r}"
        )

