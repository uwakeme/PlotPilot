"""收掉复审遗留的最后两条（第 8 / 9 条）。

## 第 8 条 —— SSE 端点的契约描述

向导 Step 4 早迁到 invocation 审阅流：端点建会话 → 发 `approval_required`
→ 候选内容在会话里由用户审阅后接受。docstring 仍写着「解析到一条推送一条」，
那是 invocation 之前的架构。

`plot_options: []` 对新架构**是对的**，但对消费者是**歧义** —— 空数组
既可能读成「生成了 0 条候选」，也可能读成「候选在别处」。所以补一个
`plot_options_location` 字段把位置讲明。

同时删掉前端的 `consumeMainPlotOptionsStream`：它解析后端**早就不发**的
`option` / `chunk` 事件，而且是文件里唯一零调用的 `consume*Stream`。

## 第 9 条 —— `suggest_options` 零生产调用方

整条「直接调 LLM 生成主线候选」的旧链路已死：
`suggest_options` → `parse_suggested_options` → `_build_prompt_and_config`
（+ 只服务于它的 `_build_context`）。

⚠ **A-01 的支线合同覆盖必须保住** —— 它的受测对象存活，只是入口从死包装
换成了活函数 `normalize_main_plot_options`（invocation 续写处理器在用）。
所以那批测试是**重指**而不是删除；本文件钉住「活路径仍在用那个函数」。
"""
import ast
import pathlib
import re
import sys

import pytest

from application.blueprint.services.setup_main_plot_suggestion_service import (
    MainPlotSuggestionContractError,
    normalize_main_plot_options,
)


def _repo_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() or (parent / "pytest.ini").exists():
            return parent
    raise RuntimeError("找不到仓库根")


ROOT = _repo_root()
SVC_REL = "application/blueprint/services/setup_main_plot_suggestion_service.py"
GEN_REL = "interfaces/api/v1/engine/generation.py"
WF_REL = "frontend/src/api/workflow.ts"


def _read(rel: str) -> str:
    return (ROOT / rel).read_bytes().decode("utf-8-sig")


# ======================================================================
#  第 9 条：死链路已清空，活路径仍在
# ======================================================================


@pytest.mark.parametrize(
    "method",
    ["suggest_options", "parse_suggested_options", "_build_prompt_and_config", "_build_context"],
)
def test_dead_direct_llm_methods_are_gone(method: str):
    """旧架构的四个方法必须不在。

    它们的整条链路零生产调用方：向导两条路由都迁到了 `create_invocation`。
    """
    tree = ast.parse(_read(SVC_REL))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "SetupMainPlotSuggestionService")
    names = {b.name for b in cls.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert method not in names, f"{method} 还在 —— 旧架构没清干净"


def test_live_service_methods_are_kept():
    """反过来：活着的必须还在，别把整类删掉。"""
    tree = ast.parse(_read(SVC_REL))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "SetupMainPlotSuggestionService")
    names = {b.name for b in cls.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for keep in ("build_context", "_parse_plot_json", "_normalize_options",
                 "_complete_option_architecture"):
        assert keep in names, f"{keep} 被误删了"

    module_funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for keep in ("normalize_main_plot_options", "normalize_main_plot_options_data"):
        assert keep in module_funcs, f"{keep} 被误删了"


def test_live_caller_still_uses_the_normalizer():
    """invocation 续写处理器必须仍走活函数。

    这条是「A-01 覆盖没丢」的最后一环：守卫测试重指到了
    `normalize_main_plot_options`，而**生产路径**也必须调它 ——
    否则测试守着一个没人用的函数。
    """
    src = _read("application/blueprint/services/setup_main_plot_continuation.py")
    tree = ast.parse(src)
    called = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = n.func
            called.add(getattr(fn, "attr", None) or getattr(fn, "id", None))
    assert "normalize_main_plot_options" in called
    assert "normalize_main_plot_options_data" in called


def test_normalizer_still_refuses_to_fabricate_sublines():
    """A-01 合同在活函数上原样生效：模型没给支线就不编造。"""
    payload = {
        "plot_options": [
            {"id": "a", "type": "t", "title": "t", "logline": "l",
             "core_conflict": "c", "starting_hook": "h"},
            {"id": "b", "type": "t", "title": "t", "logline": "l",
             "core_conflict": "c", "starting_hook": "h"},
            {"id": "c", "type": "t", "title": "t", "logline": "l",
             "core_conflict": "c", "starting_hook": "h"},
        ]
    }
    import json

    parsed = normalize_main_plot_options(
        json.dumps(payload, ensure_ascii=False),
        {"target_chapters": 60},
    )
    assert parsed[0]["sublines"] == []
    assert parsed[0]["sublines_source"] == "none"


def test_normalizer_still_reports_out_of_range_merge_chapter():
    """越界要报出来，不默默改 —— 与 A-01 同一条不变量。"""
    import json

    payload = {
        "plot_options": [
            {"id": "a", "type": "t", "title": "t", "logline": "l",
             "core_conflict": "c", "starting_hook": "h",
             "sublines": [{"id": "x", "name": "越界线", "merge_chapter": 999}]},
            {"id": "b", "type": "t", "title": "t", "logline": "l",
             "core_conflict": "c", "starting_hook": "h"},
            {"id": "c", "type": "t", "title": "t", "logline": "l",
             "core_conflict": "c", "starting_hook": "h"},
        ]
    }
    parsed = normalize_main_plot_options(
        json.dumps(payload, ensure_ascii=False), {"target_chapters": 60}
    )
    assert parsed[0]["subline_issues"], "越界未被报告"
    assert parsed[0]["sublines"][0]["merge_chapter"] == 999, "值被改写了"


def test_normalizer_still_rejects_a_broken_payload():
    """合同错误仍然抛 —— 活路径的失败语义没被削弱。"""
    with pytest.raises(MainPlotSuggestionContractError):
        normalize_main_plot_options("不是 JSON", {"target_chapters": 60})


# ======================================================================
#  第 8 条：契约描述与事件形状
# ======================================================================


def test_response_model_declares_where_the_options_live():
    """空 `plot_options` 必须能被读成「在会话里」而不是「没生成」。"""
    from interfaces.api.v1.engine.generation import SuggestMainPlotOptionsResponse

    fields = SuggestMainPlotOptionsResponse.model_fields
    assert "plot_options_location" in fields
    assert fields["plot_options_location"].default == "invocation_session"


def test_both_endpoints_report_the_options_location():
    """非流式与流式两个端点都要说清候选在哪。

    ⚠ 断言必须落在**精确形态**上，不能用 `count(...) >= 3` ——
    docstring 里也会提到 `plot_options_location` 这个词名，于是
    「总数 ≥ 3」在真正删掉 SSE 那处之后依然成立（实测 5 → 4）。
    下面三个形态互不重叠：模型字段声明、响应构造的 kwarg、SSE 字典的 key。
    """
    src = _read(GEN_REL)
    assert '    plot_options_location: str = "invocation_session"' in src, (
        "响应模型没有声明 plot_options_location"
    )
    assert '            plot_options_location="invocation_session",' in src, (
        "非流式端点的响应没有带上位置字段"
    )
    assert "'plot_options_location': 'invocation_session'" in src, (
        "SSE done 事件没有带上位置字段"
    )


def test_stale_streaming_claims_are_gone():
    """旧 docstring 声称「解析到一条推送一条」—— 后端早就不这么发了。"""
    src = _read(GEN_REL)
    for stale in ("解析到一条推送一条", "流式推演主线候选"):
        assert stale not in src, f"docstring 还在声称「{stale}」"


def test_backend_never_emits_option_or_chunk_events():
    """后端不发 `option` / `chunk` 事件 —— 前端不该再有对应解析分支。"""
    src = _read(GEN_REL)
    # SSE done 事件里只有 plot_options / location / session id
    for bogus in ("'type': 'option'", "'type': 'chunk'"):
        assert bogus not in src, f"后端不该发 {bogus} 事件"


def test_frontend_has_no_client_for_the_retired_protocol():
    """前端的流客户端已删 —— 它解析的是后端不再发的事件。"""
    src = _read(WF_REL)
    assert "consumeMainPlotOptionsStream" not in src
    assert "MainPlotOptionsStreamEvent" not in src


def test_frontend_keeps_the_live_api_surface():
    """反过来：活端点的 DTO 与 SDK 方法必须还在。"""
    src = _read(WF_REL)
    assert "MainPlotOptionDTO" in src
    assert "SuggestMainPlotOptionsResponse" in src
    assert "suggestMainPlotOptions" in src


def test_other_stream_clients_are_untouched():
    """别把别的向导流客户端一起删了。"""
    src = _read(WF_REL)
    for keep in ("consumePlotOutlineStream", "consumeGenerateChapterStream"):
        assert keep in src, f"{keep} 被误删了"
