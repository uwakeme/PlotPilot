"""端到端：真正跑通一条此前只有「signature / 纯函数」测试的生产路径。

复审指出的缺口（§3 末尾）：「仍无端到端测试 —— `ensure_execution_plan`
成功路径、`run_chapter_audit` 调用方」。

`ensure_execution_plan` 是 P0 的事故现场：装饰器被吃导致实例调用 TypeError，
而旧测试用**类名**调用，恰好绕过了实例绑定。所以这里一律经实例、
经真实成功路径 —— 不短路。

`run_chapter_audit` 那边，真跑整个函数需要一份很大的 host 替身，
性价比不划算；改为「helper 行为断言 + 调用方 AST 判定」，
其中 AST 那半已由 `test_review_round1_regressions.py` 的
`test_no_unbound_names_in_rewritten_modules` 全模块覆盖。
"""
import ast
import json
import re
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "infrastructure" / "ai" / "prompt_packages").is_dir():
            return parent
    raise AssertionError("从测试文件向上找不到仓库根")


REPO_ROOT = _repo_root()


def _valid_seven_section_payload(target_words=2600):
    return {
        "target_words": target_words,
        "detail_title": "细纲标题",
        "key_plot_points": ["情节点A", "情节点B"],
        "chapter_characters": ["主角"],
        "chapter_plan": {
            "opening_entry": "从雨夜巷口的一场对峙切入",
            "scene_transitions": [
                {"scene": "巷口", "location": "城南", "cast": ["主角"],
                 "purpose": "正面对峙"},
            ],
            "key_dialogues": [
                {"speaker": "甲", "line": "你到底要什么", "reply": "要一个答案",
                 "purpose": "摊牌"},
            ],
            "event_chain": [
                {"phase": "触发", "content": "旧账被翻出"},
                {"phase": "升级", "content": "冲突外溢"},
            ],
            "character_decisions": [
                {"actor": "主角", "decision": "正面迎战", "purpose": "夺回主动"}
            ],
            "payoff_reversals": ["预期反转：陷阱反成机会"],
            "protagonist_state_change": {
                "位置": "城南→北营",
                "实力": "受损",
                "新获得": "一份账册",
                "身体状况": "轻伤",
                "重大变化": "被迫公开身份",
            },
        },
    }


class _FakeLLM:
    """按 ``_generate_text`` 的实际调用形态提供：先 stream_generate，再回退 generate。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.prompts: list = []

    def _body(self):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)

    def stream_generate(self, prompt, config=None):
        self.prompts.append(prompt)
        body = self._body()

        async def _gen():
            yield body

        return _gen()

    async def generate(self, prompt, config=None):
        self.prompts.append(prompt)
        return type("R", (), {"content": self._body()})()


@pytest.fixture()
def stub_gateway(monkeypatch):
    """只替 `render`（它要读 DB 里的提示词模板）；validate_output 走真实 schema。"""
    import application.blueprint.services.chapter_preplanning_service as mod
    from infrastructure.ai.prompt_gateway import get_prompt_gateway

    real = get_prompt_gateway()

    class _G:
        @staticmethod
        def render(*_a, **_k):
            return type("R", (), {"prompt": "p"})()

        @staticmethod
        def validate_output(contract, payload):
            return real.validate_output(contract, payload)

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _G())
    monkeypatch.setattr(mod, "generation_config_from_profile", lambda name: object())


def _make_service(payload):
    from application.blueprint.services.chapter_preplanning_service import (
        ChapterPreplanningService,
    )

    svc = ChapterPreplanningService(llm_service=_FakeLLM(payload))
    # 持久化与变量写入是协作副作用，这里换成无副作用替身；
    # 解析/校验/渲染这条主干保持真实。
    svc._persist_outline = _noop_persist
    svc._write_plan_variables = _noop_write_vars
    return svc


async def _noop_persist(*_a, **_k):
    return None


def _noop_write_vars(self, *args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_ensure_execution_plan_success_path_runs_on_an_instance(stub_gateway):
    """真跑成功路径（fake LLM 返回合法七段剧本）⇒ 返回 ChapterPreplanResult。

    P0 回归守卫：新方法若被插在 ``@staticmethod`` 与原方法之间吃掉装饰器，
    经**实例**调用必然 TypeError，而旧测试用类名调用恰好绕过了。
    """
    svc = _make_service(_valid_seven_section_payload())

    result = await svc.ensure_execution_plan(
        novel_id="n1",
        chapter_number=1,
        current_outline="本章大纲",
        target_words=2500,
    )
    assert result is not None
    # 七段执行剧本确实被渲染出来了（detail_title 是独立字段，不在正文里）
    for section in ("一、开篇切入点", "二、场景转换列表", "三、关键对话", "四、剧情事件链"):
        assert section in result.text, f"渲染结果缺少「{section}」段"
    # D-05：target_words 是**可选**输出通道
    assert result.proposed_target_words == 2600


@pytest.mark.asyncio
async def test_success_path_actually_reaches_normalize_string_list(
    stub_gateway, monkeypatch
):
    """证明成功路径**真的会**走到 ``self._normalize_string_list``。

    上一条只能证明「跑通」，这一条证明「跑通的路上经过那个方法」——
    否则把那两个调用点删掉，也不会有任何测试变红。
    """
    svc = _make_service(_valid_seven_section_payload())
    seen = {"n": 0}
    original = type(svc)._normalize_string_list

    def _counting(value):
        seen["n"] += 1
        return original(value)

    monkeypatch.setattr(type(svc), "_normalize_string_list", staticmethod(_counting))

    result = await svc.ensure_execution_plan(
        novel_id="n1", chapter_number=1, current_outline="本章大纲",
    )
    assert result is not None
    assert seen["n"] >= 2, (
        f"成功路径只调了 {seen['n']} 次 _normalize_string_list"
        "（key_plot_points + chapter_characters 各一次）—— 路径可能没走到那里"
    )


@pytest.mark.asyncio
async def test_success_path_carries_the_pacing_directive_into_the_prompt(stub_gateway):
    """D-01 端到端：指令必须真的出现在送给模型的 prompt 里。

    此前风险是「拼进 ctx.outline 却被步骤 3 覆盖、且 outline 不是 prompt 变量」。
    这里断言 prompt **内容**含指令；空串时不含。
    """
    from application.engine.services.chapter_mode_settings import (
        BUFFER_CHAPTER_DIRECTIVE,
    )
    from infrastructure.ai.prompt_template_engine import get_template_engine

    captured: dict = {}
    import application.blueprint.services.chapter_preplanning_service as mod
    from infrastructure.ai.prompt_seed.loader import NODES_DIR, load_node_dir
    from infrastructure.ai.prompt_template_engine import get_template_engine

    real_gateway = mod.get_prompt_gateway()
    node_dir = NODES_DIR / "planning-chapter-preplan"

    class _G:
        @staticmethod
        def render(contract, variables):
            # 走真实模板渲染，捕获最终 prompt 文本
            captured["vars"] = dict(variables)
            record = load_node_dir(node_dir)
            rendered = get_template_engine().render(
                record.get("system") or "", record.get("user_template") or "",
                dict(variables),
            )
            captured["user"] = rendered.user
            return type("R", (), {"prompt": type("P", (), {
                "system": rendered.system, "user": rendered.user,
            })()})()

        @staticmethod
        def validate_output(contract, payload):
            return real_gateway.validate_output(contract, payload)

    mod.get_prompt_gateway = lambda: _G()
    mod.generation_config_from_profile = lambda name: object()
    try:
        svc = _make_service(_valid_seven_section_payload())
        await svc.ensure_execution_plan(
            novel_id="n1", chapter_number=1, current_outline="本章大纲",
            pacing_directive=BUFFER_CHAPTER_DIRECTIVE,
        )
    finally:
        mod.get_prompt_gateway = real_gateway

    assert captured["vars"]["pacing_directive"] == BUFFER_CHAPTER_DIRECTIVE
    # 且**真的出现在 prompt 文本里** —— 只验变量字典的话，
    # 模板少渲染一段照样全绿。
    assert "本章节奏指令" in captured["user"], "节奏指令段没有进 prompt"
    assert BUFFER_CHAPTER_DIRECTIVE[:12] in captured["user"], (
        "指令原文没有进 prompt"
    )


# ── run_chapter_audit 的暂停闸门：措辞必须与真实触发原因一致 ────────────────


@pytest.mark.parametrize(
    "anti_evaluated,assessment,expect",
    [
        (True, "严重", "Anti-AI严重"),
        (True, "轻微", "Anti-AI未触发"),
        (False, "未评估", "Anti-AI审计未评估"),
    ],
)
def test_pause_gate_reason_matches_the_real_trigger(anti_evaluated, assessment, expect):
    """暂停原因的措辞必须与真实原因一致。

    「因未评估而停」不得写成「Anti-AI严重」—— 那等于把「没测出来」
    记成「测出严重」。这条同时钉住调用方复用 helper 的返回值
    （调用方曾自己重抄一遍，用的是只在 helper 内存在的变量）。
    """
    from engine.runtime.audit_delegate import ANTI_AI_UNASSESSED, _resolve_audit_pause_gate

    prefs = type("P", (), {
        "pause_after_each_chapter_audit": True,
        "audit_pause_on_hard_fail": True,
        "audit_pause_on_anti_ai_severe": True,
    })()

    gate, hard_fail, triggered, reason = _resolve_audit_pause_gate(
        auto=False,
        prefs=prefs,
        hard_narrative=False,
        hard_voice=False,
        hard_tension=False,
        anti_evaluated=anti_evaluated,
        anti_assessment=ANTI_AI_UNASSESSED if not anti_evaluated else assessment,
    )
    assert gate is True
    if expect == "Anti-AI审计未评估":
        assert reason.startswith(expect), (
            f"未评估时的措辞应说明「未评估」，实际 {reason!r}"
        )
        assert "严重" not in reason
    else:
        assert reason == expect


def test_callers_do_not_recompute_the_reason_themselves():
    """调用方必须复用 helper 的返回值，而不是自己重抄一遍。

    ⚠ 第三轮复审指出旧版是**恒真守卫**：它要求 `if pause_gate` 分支同时出现
    `buffer_reason` 与 `anti_reason` 赋值，而 `audit_delegate.py` 全文 0 处
    `buffer_reason` ⇒ 条件恒 False ⇒ 注入真实退化也全绿。

    这里只判 `anti_reason`（真正该断言的那个），并**显式确认**判定条件
    在当前代码里确实可满足 —— 避免再造一个恒真守卫。
    """
    src = (REPO_ROOT / "engine/runtime/audit_delegate.py").read_bytes().decode("utf-8-sig")
    assert "anti_reason" in src, "前置：audit_delegate 应有 anti_reason"
    assert "buffer_reason" not in src, (
        "audit_delegate 里出现了 buffer_reason（那是 StoryPipeline 的字段）—— "
        "若确实需要，请同步更新本守卫的判定条件，别让它退化成恒真"
    )

    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_chapter_audit"
    )
    branches = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "pause_gate"
    ]
    assert branches, "没找到 if pause_gate 分支"
    for b in branches:
        recomputes = any(
            isinstance(x, ast.Assign) and "anti_reason" in ast.unparse(x)
            for x in ast.walk(b)
        )
        assert not recomputes, (
            "if pause_gate 分支又自己重算了一遍 anti_reason —— "
            "那次重抄用的变量只存在于 helper 作用域内，正是 NameError 的来源"
        )


def test_unbound_probe_does_not_mask_outer_scope_via_nested_defs():
    """探针自身回归：嵌套 def 的形参**不得**算进外层作用域。

    旧版把 `def inner(x)` 的 `x` 加进 outer 的 local，于是
    `def outer(): def inner(x): return x; return x` 里的第二个 `x`
    （outer 里确实未绑定）会被掩蔽 —— 守卫看起来通过，实际漏报。
    """
    import ast as _ast
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_rt",
        REPO_ROOT / "tests" / "unit" / "test_review_round1_regressions.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    src = """
def outer():
    def inner(x):
        return x
    return x        # ← outer 里确实没有 x，应该被抓到
"""
    tree = _ast.parse(src)
    fn = tree.body[0]
    local = mod._scope_local_names(fn)
    assert "x" not in local, (
        "嵌套函数的形参被算进了外层作用域 —— 会掩蔽外层真实的未绑定名"
    )
    own = list(mod._own_scope_nodes(fn))
    assert any(
        isinstance(n, _ast.Name) and n.id == "x" and isinstance(n.ctx, _ast.Load)
        for n in own
    ), "own-scope 遍历应能看见外层里的 Load"


# ── logger 格式串与实参个数必须一致 ───────────────────────────────────────
#
# D-05 给 `ensure_execution_plan` 加了 `proposed_target_words` 实参却没同步格式串
# （4 个占位符 / 5 个实参）⇒ 每次成功预规划都打一条格式化错误。
#
# 为什么单测抓不到：`logger.info` 在**没有 handler** 时格式化错误被吞掉；
# 只有挂了 handler（生产环境、以及 pytest 的 logging 插件）才会冒出来。
# 「单跑全绿 + 全量跑红」正是它的表现形式 —— 也是本文件被端到端用例逼出来的原因。

_SPEC = re.compile(r"%(?:%|[-+ #0]*\d*(?:\.\d+)?[hlL]?([diouxXeEfFgGcrsa]))")

_LOGGER_FILES = [
    "application/blueprint/services/chapter_preplanning_service.py",
    "application/world/services/chapter_narrative_sync.py",
    "engine/pipeline/base.py",
    "engine/runtime/audit_delegate.py",
    "application/analyst/services/state_updater.py",
    "engine/runtime/writing_delegate.py",
    "engine/runtime/daemon_host.py",
]


@pytest.mark.parametrize("rel", _LOGGER_FILES)
def test_logger_format_placeholders_match_argument_count(rel):
    """`logger.*(fmt, *args)` 的占位符数必须与实参个数一致。

    多一个实参 ⇒ `TypeError: not all arguments converted`；
    少一个 ⇒ 渲染出错但不抛。两者都是日志内容在撒谎。
    """
    tree = ast.parse((REPO_ROOT / rel).read_bytes().decode("utf-8-sig"))
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in (
            "debug", "info", "warning", "error", "critical", "exception"
        )):
            continue
        if isinstance(f.value, ast.Name) and f.value.id != "logger":
            continue
        if not node.args:
            continue
        fmt_node = node.args[0]
        # f-string / 动态拼接判不了，静态跳过
        if isinstance(fmt_node, (ast.JoinedStr, ast.BinOp)):
            continue
        try:
            fmt = ast.literal_eval(fmt_node)
        except Exception:
            continue
        if not isinstance(fmt, str) or "%" not in fmt:
            continue
        n_slots = len(_SPEC.findall(fmt))
        n_args = len(node.args) - 1
        if n_slots != n_args:
            problems.append(
                f"L{node.lineno} logger.{f.attr}: {n_slots} 占位符 / {n_args} 实参"
            )
    assert not problems, (
        f"{rel} 日志格式串与实参个数不匹配：\n  " + "\n  ".join(problems)
    )


# ── 复审 §9 遗留项的行为守卫 ─────────────────────────────────────────────


def test_writing_delegate_passes_tension_through_without_inventing_a_value():
    """写作完成时把张力原样传给共享状态，**不做 `or 0` 之类的兜底**。

    `result.tension` 为 None（本章没产出张力）时必须原样传 None ——
    压成 0 就是把「没测过」写成「张力 0」。
    """
    tree = ast.parse(
        (REPO_ROOT / "engine/runtime/writing_delegate.py").read_bytes().decode("utf-8-sig")
    )
    found = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_update_shared_state"):
            continue
        for kw in node.keywords:
            if kw.arg == "last_chapter_tension":
                src = ast.unparse(kw.value)
                assert src == "result.tension", (
                    f"共享状态里的张力被改写成了 {src!r}，必须原样透传 result.tension"
                )
                found = True
    assert found, "writing_delegate 没有写 last_chapter_tension"


def test_main_plot_normalization_uses_the_ctx_target_chapters():
    """⚠ 变异 P4 不转红：主线候选的章数上界必须取 ctx，而不是硬编码 100。

    「merge_chapter <= 目标章数」这条不变量的上界取错，整条校验就是空转。
    """
    from application.blueprint.services.setup_main_plot_suggestion_service import (
        _ctx_target_chapters,
        normalize_main_plot_options_data,
    )

    assert _ctx_target_chapters({"target_chapters": 300}) == 300
    assert _ctx_target_chapters({"target_chapters": 40}) == 40
    # 没有 / 非法才回落到默认
    assert _ctx_target_chapters({}) == 100
    assert _ctx_target_chapters({"target_chapters": "不是数字"}) == 100
    assert _ctx_target_chapters({"target_chapters": 0}) == 100

    # 端到端：目标 20 章时，merge_chapter=60 必须被拒（硬编码 100 时会通过）
    def _opt(merge, i):
        # 注意 sublines 在**顶层**——`_normalize_options` 读的是
        # `item.get("sublines")`，不是 `item["structure"]["sublines"]`
        return {
            "id": f"opt{i}",
            "name": f"方案{i}",
            "type": "main",
            "title": f"主线方案 {i}",
            "logline": "一句话",
            "core_conflict": "矛盾",
            "description": "矛盾",
            "sublines": [
                {"name": f"线{i}", "description": "推进", "merge_chapter": merge},
            ],
        }

    payload = {"plot_options": [_opt(5, 1), _opt(6, 2), _opt(7, 3)]}
    ctx = {"target_chapters": 20}
    out = normalize_main_plot_options_data(payload, ctx)
    assert len(out) == 3, "5/6/7 都 <= 20，应当全部保留"

    # ⚠ 归一化**记错误**而不是抛异常（返回值里的 errors 列表），
    # 所以判据是「有没有记下越界」，不是 pytest.raises。
    from application.blueprint.services.subline_contract import normalize_sublines

    raw = [{"name": "线A", "description": "推进", "merge_chapter": 60}]
    _, _, errs_small = normalize_sublines(raw, target_chapters=20)
    assert errs_small, "目标 20 章时 merge=60 必须记下越界"

    _, _, errs_big = normalize_sublines(raw, target_chapters=100)
    assert not errs_big, "目标 100 章时 merge=60 合法，不该报错"

    # 而入口必须把 ctx 的 20 传下去（硬编码 100 时 errs 会是空）
    assert _ctx_target_chapters(ctx) == 20

    # ⚠ 变异 P4 改的是**调用点**，上面那些断言都绕过了它。
    # 这里直接判定 `normalize_main_plot_options_data` 的函数体里，
    # `_normalize_options` 的 target_chapters 实参取自 `_ctx_target_chapters(ctx)`
    # 而不是模块常量。
    tree = ast.parse(
        (REPO_ROOT / "application/blueprint/services/setup_main_plot_suggestion_service.py")
        .read_bytes().decode("utf-8-sig")
    )
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "normalize_main_plot_options_data"
    )
    targets = [
        kw.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_normalize_options"
        for kw in node.keywords
        if kw.arg == "target_chapters"
    ]
    assert targets, "normalize_main_plot_options_data 里没有 _normalize_options 调用"
    for v in targets:
        src = ast.unparse(v)
        assert "DEFAULT_TARGET_CHAPTERS" not in src, (
            f"入口把硬编码的 100 当目标章数传下去了（{src}）—— "
            f"「merge_chapter ≤ 目标章数」的上界取错，整条校验空转"
        )


def test_non_dict_hint_counts_into_the_fallback_tally():
    """⚠ 变异 P5 不转红：非结构化提示词同样要计入兜底汇总。

    那条路径意味着这一条**根本没经过模型判断**，与「模型没给
    suggested_resolve_offset」是同一性质的降级。
    """
    tree = ast.parse(
        (REPO_ROOT / "application/world/services/chapter_narrative_sync.py")
        .read_bytes().decode("utf-8-sig")
    )
    # 定位处理 hints 的那个循环体
    loops = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.For) and ast.unparse(n.iter) == "hints"
    ]
    assert loops, "没找到遍历 hints 的循环"

    non_dict_branch_has_tally = False
    for loop in loops:
        for node in ast.walk(loop):
            if not isinstance(node, ast.If):
                continue
            if "not isinstance" not in ast.unparse(node.test) or "dict" not in ast.unparse(node.test):
                continue
            body_src = "\n".join(ast.unparse(x) for x in node.body)
            if "offset_fallbacks += 1" in body_src:
                non_dict_branch_has_tally = True

    assert non_dict_branch_has_tally, (
        "非 dict hint 分支没有计入 offset_fallbacks —— 汇总数字会系统性偏小"
    )


def test_daemon_threshold_follows_the_configured_override(monkeypatch):
    """⚠ 变异 P8 不转红：F-04 的阈值覆盖必须贯通到 daemon 侧。

    用户改配置后，API 侧按新阈值判、daemon 侧仍按模块常量判 ⇒
    同一个数两边结论不同。
    """
    import engine.runtime.daemon_host as dh
    import application.analyst.services.voice_analysis_settings as vas

    host = object.__new__(dh.DaemonHostMixin)
    fn = dh.DaemonHostMixin._similarity_below_warning_threshold

    # 默认阈值 0.75
    assert fn(host, 0.8) is False
    assert fn(host, 0.7) is True

    # 覆盖成 0.95：0.8 现在应当**低于**阈值
    monkeypatch.setattr(
        vas, "get_voice_analysis_runtime_settings",
        lambda: vas.VoiceAnalysisRuntimeSettings(drift_alert_threshold=0.95),
    )
    assert fn(host, 0.8) is True, (
        "daemon 侧没采用配置里的阈值覆盖 —— 用户改配置后两边结论不同"
    )

    # None 仍返回 False（未评估不得当越阈）
    assert fn(host, None) is False


# ── legacy 写作路径的余韵章阈值 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expect_buffer",
    [
        (None, False),   # 从未评估 —— 不参与阈值比较
        (0, False),      # 真实张力 0 —— 合法值，但不触发
        # ⚠ 8 **不**触发。它是 0-100 刻度下的真实低张力。
        # 上一版这里是 (8, True)，那等于把「<=10 视为旧刻度 ×10」这个
        # 与生产写入刻度冲突的假设**固化成了期望**。第三轮复审指出后已改正：
        # 唯一两个生产写入者产出的都是 0-100 刻度，×10 分支只会把
        # 真实的低张力章凭空放大成余韵章。
        (8, False),
        (10, False),
        (79, False),
        (80, True),
        (85, True),
        (100, True),
    ],
)
def test_legacy_buffer_threshold_treats_none_as_no_signal(raw, expect_buffer):
    """⚠ `legacy_writing_delegate` 的余韵章阈值：None 不得按 0 参与比较。

    此前是 `novel.last_chapter_tension or 0`，None 被压成 0 再参与 `>= 80`
    比较 —— 结论恰好相同（不触发），但那是因为「0 本来也不够高」而不是
    「我们正确处理了没测过」。一旦阈值调低、或将来把 0 当作有效下界，
    这个 bug 就会显形。现在 None 是显式的一支。
    """
    from engine.runtime.legacy_writing_delegate import (
        _needs_buffer_chapter,
    )

    got, echoed = _needs_buffer_chapter(raw)
    assert got is expect_buffer, f"tension={raw!r} 期望 {expect_buffer}，实际 {got}"
    assert echoed == raw, "原始值必须原样回传（日志要用）"


def test_legacy_buffer_threshold_constant_is_eighty():
    """阈值是 80（0-100 刻度，对应旧 8/10）。改它要有人知道。"""
    from engine.runtime.legacy_writing_delegate import BUFFER_TENSION_THRESHOLD

    assert BUFFER_TENSION_THRESHOLD == 80


# ── 张力三态在 DB 层的往返 ───────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, 0, 87])
def test_tension_tri_state_round_trips_through_the_repository(value, tmp_path):
    """None / 0 / 87 三态都必须**原样**往返。

    `last_chapter_tension` 现在是 Optional，列在 SQLite 里也天然可存 NULL。
    这一条是「真的写进去再读出来」，不是静态判定 —— 静态能骗人：
    读取侧曾用 `or 0`，静态看起来没问题，只有真跑才会发现 0 和 None 同形。
    """
    from domain.novel.entities.novel import Novel
    from domain.novel.value_objects.novel_id import NovelId
    from infrastructure.persistence.database.connection import DatabaseConnection
    from infrastructure.persistence.database.sqlite_novel_repository import (
        SqliteNovelRepository,
    )

    db = DatabaseConnection(str(tmp_path / "novels.db"))
    try:
        repo = SqliteNovelRepository(db)
        novel = Novel(
            id=NovelId("n-tri"), title="t", author="a", target_chapters=10
        )
        novel.last_chapter_tension = value
        repo.save(novel)

        back = repo.get_by_id(NovelId("n-tri"))
        assert back is not None
        assert back.last_chapter_tension == value, (
            f"{value!r} 往返后变成 {back.last_chapter_tension!r} —— "
            f"None 与 0 在这一列上必须可分辨"
        )
    finally:
        db.close_all()
