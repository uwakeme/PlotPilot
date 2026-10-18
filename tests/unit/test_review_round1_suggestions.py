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


def test_audit_writes_an_explicit_evaluated_flag_for_both_branches():
    """`last_chapter_tension=0` 同时表达「真是 0」与「没测过」，必须另带标记。"""
    src = _read("engine/runtime/audit_delegate.py")
    assert src.count("last_chapter_tension_evaluated") >= 2, (
        "评估标记必须两个分支都写：True（评估过）与 False（未评估）"
    )
    assert "last_chapter_tension_evaluated=True" in src
    assert "last_chapter_tension_evaluated=False" in src


def test_audit_flag_is_not_swapped_between_the_two_branches():
    """⚠ 复审实证：把两个分支的标记**对调**（语义反转）时，上一条全绿。

    上一条只验「两个字符串都出现过」，注释里写一遍就能过。
    这里改成**结构判定**：标记必须落在对应的 `if/else` 分支里。

    `tension_evaluated=True` 的分支 ⇒ 标记必须是 True；
    `else`（未评估）分支 ⇒ 标记必须是 False。
    对调即红线语义反转 —— 未评估会被报成「评估过，张力 0」。
    """
    tree = _ast("engine/runtime/audit_delegate.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_chapter_audit"
    )

    def flag_of(nodes) -> str | None:
        for node in nodes:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "_update_shared_state"):
                    for kw in sub.keywords:
                        if kw.arg == "last_chapter_tension_evaluated":
                            if isinstance(kw.value, ast.Constant):
                                return repr(kw.value.value)
                            return ast.unparse(kw.value)
        return None

    pairs = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if "tension_evaluated" not in ast.unparse(node.test):
            continue
        body_flag = flag_of(node.body)
        else_flag = flag_of(node.orelse) if node.orelse else None
        if body_flag is not None or else_flag is not None:
            pairs.append((node.lineno, body_flag, else_flag))

    assert pairs, "没找到写评估标记的 tension_evaluated 分支"
    # 逐对检查语义方向，而不是只看「两个值都出现过」
    for lineno, body_flag, else_flag in pairs:
        assert body_flag != "False", (
            f"L{lineno}：评估过的分支标记写成了 False —— 未评估会被报成「张力 0」，"
            f"或评估过的被当成没测过"
        )
        assert else_flag != "True", (
            f"L{lineno}：未评估分支标记写成了 True —— 「没测过」会被当成真实张力"
        )
    # 且必须至少有一对是「True + False」的标准形态
    assert any(b == "True" and e == "False" for _, b, e in pairs), (
        f"没有任何分支呈现「评估过=True / 未评估=False」的标准形态：{pairs}"
    )


def test_workflow_main_path_forwards_the_actual_slot_weights_value():
    """⚠ 复审实证：把主路径的 `slot_weights=slot_weights` 改成 `=None`，
    相关 20+ 条测试全绿 —— 因为守卫只查 AST token 存在，不验转发值。

    这里改成**判定转发的是不是原值**。`prepare_chapter_generation` 的
    协作依赖太重，不适合在单测里真跑；AST 判 kwarg 的值足够，
    且比源码子串匹配强（注释里写一遍骗不过去）。
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
                f"权重会算完即丢（生产 daemon 恒走这条主路径）"
            )


def test_decide_chapter_mode_only_trusts_flagged_tension():
    src = _read("engine/pipeline/base.py")
    assert "_last_chapter_tension_evaluated" in src, (
        "D-01 没有校验张力是否真的评估过"
    )
    # 取值必须被标记门控
    assert "if self._last_chapter_tension_evaluated(ctx.novel_id):" in src


def test_flag_reader_defaults_to_false_on_any_problem():
    """读不到 / 抛错一律 False —— 「不知道」必须与「是 0」区分开。"""
    from engine.pipeline.base import BaseStoryPipeline

    src = inspect.getsource(BaseStoryPipeline._last_chapter_tension_evaluated)
    assert "except Exception" in src
    assert src.rstrip().endswith("return False")


# ── 3. 异常分支必须留原因 ────────────────────────────────────────────────


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
