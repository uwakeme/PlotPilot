"""幕级生成不许用代码占位冒充创作内容（C-06 / B-11）。

C-06 —— `_generate_next_act_info` 有两条编造路径：
  1. LLM 调用抛异常 → 返回 `{"title": "第N幕", "description": "描述"}`
  2. LLM 调用成功但缺字段 → `setdefault` 补上 `"第N幕"` / `"继续推进剧情"`

两条都会被 `create_next_act_auto` 原样喂给 `_create_node_from_data`，而那个方法
盖的是 `planning_source=AI_MACRO` / `planning_status=CONFIRMED` —— 也就是说数据库
里会留下一条「模型确认过的」记录，内容其实是代码写的。读端无从分辨，这是本项目
反复强调的三态红线里最刺眼的一种：不是「没说话被当成说了」，是「代码说的话被
标成模型说的」。

B-11 —— LLM 没给出有效宏观结构时，代码曾经会拼一整棵部–卷–幕占位骨架落库
（10 个硬编码幕标题 + 占位简介 + 公式章数）。该分支在 `c2c15330` 之后已不可达
（两个调用方都显式传 False），但它仍读起来像一道生效中的保险。本文件锁定
「现在没有任何路径能造出占位结构」。

全部是行为断言：不使用 `inspect.getsource` 之类的源码文本断言。
"""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services import continuous_planning_service as cps
from application.blueprint.services.continuous_planning_service import (
    ActPlanningGenerationError,
    ContinuousPlanningService,
)


# ======================================================================
#  替身
# ======================================================================


def _svc() -> ContinuousPlanningService:
    return ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )


def _current_act(number: int = 7):
    return SimpleNamespace(
        id="act-7",
        novel_id="n-1",
        number=number,
        order_index=41,
        parent_id="vol-1",
    )


def _arm_llm(svc, *, raise_exc=None, raw='{"title": "第八幕", "description": "真相浮出"}'):
    """把 `_generate_next_act_info` 的三个外部依赖换成可控替身。"""
    svc._act_budget_context = Mock(return_value={"chapters_per_act": 12})
    svc._collect_dual_track_context = AsyncMock(return_value={})
    svc._build_next_act_prompt_with_dual_track = Mock(return_value=object())
    if raise_exc is not None:
        svc._collect_llm_stream_text = AsyncMock(side_effect=raise_exc)
    else:
        svc._collect_llm_stream_text = AsyncMock(return_value=raw)


# ======================================================================
#  C-06：LLM 失败 → 抛错，绝不返回占位文案
# ======================================================================


@pytest.mark.asyncio
async def test_next_act_llm_exception_does_not_fabricate_content():
    """C-06-1：LLM 调用抛异常时必须失败，不能返回「第N幕 / 描述」。"""
    svc = _svc()
    _arm_llm(svc, raise_exc=RuntimeError("upstream 502"))

    with pytest.raises(ActPlanningGenerationError) as exc:
        await svc._generate_next_act_info("n-1", _current_act(), {})

    # 异常文本里带上幕号，运维才知道是哪一幕失败
    assert "8" in str(exc.value)


@pytest.mark.asyncio
async def test_next_act_unparsable_response_does_not_fabricate_content():
    """C-06-2：响应解析不出来也必须失败。"""
    svc = _svc()
    _arm_llm(svc, raw="这不是 JSON")

    with pytest.raises(ActPlanningGenerationError):
        await svc._generate_next_act_info("n-1", _current_act(), {})


@pytest.mark.asyncio
async def test_next_act_non_dict_response_does_not_fabricate_content():
    """C-06-3：解析成功但不是对象（如数组/标量）时不能被压成 {} 再补占位。

    断言消息里点名「不是 JSON 对象」，而不是只断言抛错 —— 否则「压成 {}」
    这个变异会被后面那条「缺 title/description」的重叠守卫顺手接住，
    两种失真塌成同一条错误，排查时看不出模型到底返回了什么形状。
    """
    svc = _svc()
    _arm_llm(svc, raw='["第八幕"]')

    with pytest.raises(ActPlanningGenerationError) as exc:
        await svc._generate_next_act_info("n-1", _current_act(), {})

    assert "JSON" in str(exc.value)
    assert "title" not in str(exc.value)


@pytest.mark.asyncio
async def test_next_act_missing_title_is_failure_not_placeholder():
    """C-06-4：LLM 漏填 title → 旧代码 setdefault 成「第8幕」，读端会当成模型的决策。"""
    svc = _svc()
    _arm_llm(svc, raw='{"description": "真相浮出水面"}')

    with pytest.raises(ActPlanningGenerationError) as exc:
        await svc._generate_next_act_info("n-1", _current_act(), {})

    assert "title" in str(exc.value)


@pytest.mark.asyncio
async def test_next_act_blank_description_is_failure_not_placeholder():
    """C-06-5：description 是空白串等同没给 —— 不能回填「继续推进剧情」。"""
    svc = _svc()
    _arm_llm(svc, raw='{"title": "第八幕", "description": "   "}')

    with pytest.raises(ActPlanningGenerationError) as exc:
        await svc._generate_next_act_info("n-1", _current_act(), {})

    assert "description" in str(exc.value)


@pytest.mark.asyncio
async def test_next_act_generated_content_never_contains_code_placeholders():
    """C-06-6：无论成功与否，返回值/异常里都不该出现代码写死的占位文案。"""
    svc = _svc()
    _arm_llm(svc, raise_exc=RuntimeError("boom"))

    try:
        produced = await svc._generate_next_act_info("n-1", _current_act(), {})
    except ActPlanningGenerationError as e:
        produced = str(e)

    assert "继续推进剧情" not in produced
    assert "第8幕" not in produced


# ======================================================================
#  C-06：章数是预算，必须标来源
# ======================================================================


@pytest.mark.asyncio
async def test_next_act_chapter_count_from_llm_is_marked_ai():
    """C-06-7：模型给了章数 → 原值保留（B-03 口径），且标 ai。"""
    svc = _svc()
    _arm_llm(
        svc,
        raw='{"title": "第八幕", "description": "真相浮出", "suggested_chapter_count": 7}',
    )

    info = await svc._generate_next_act_info("n-1", _current_act(), {})

    assert info["suggested_chapter_count"] == 7
    assert info["chapter_count_source"] == "ai"
    assert info["source"] == "ai"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_count",
    [None, 0, -3, "十二", True],
    ids=["absent", "zero", "negative", "non_numeric", "bool"],
)
async def test_next_act_chapter_count_fallback_is_marked_not_faked_as_ai(raw_count):
    """C-06-8：模型没说（或说了个非正数）→ 退到本书预算，但必须标 fallback。

    这是本条与 C-06-1~6 的区别：章数是预算而不是创作内容，代码有权算；
    但「代码算的」和「模型说的」必须可分辨。
    """
    svc = _svc()
    payload = {"title": "第八幕", "description": "真相浮出"}
    if raw_count is not None:
        payload["suggested_chapter_count"] = raw_count
    _arm_llm(svc, raw="{}".format(payload).replace("'", '"'))

    info = await svc._generate_next_act_info("n-1", _current_act(), {})

    assert info["suggested_chapter_count"] == 12  # _act_budget_context 的值
    assert info["chapter_count_source"] == "fallback"


# ======================================================================
#  C-06：create_next_act_auto 不得把 None 章数补成常数 5
# ======================================================================


@pytest.mark.asyncio
async def test_create_next_act_auto_does_not_invent_chapter_count():
    """C-06-9：`create_next_act_auto` 里的 `.get(..., 5)` 是静默常数兜底。

    「模型没说」与「模型说的就是 5」在下游不可分辨，而 5 又是个看起来完全正常的
    幕长。缺就是 None。
    """
    svc = _svc()
    svc.story_node_repo.get_by_id = AsyncMock(return_value=_current_act())
    svc._get_bible_context = Mock(return_value={})
    svc._generate_next_act_info = AsyncMock(
        return_value={"title": "第八幕", "description": "推进"}
    )
    svc.story_node_repo.save = AsyncMock()

    await svc.create_next_act_auto("n-1", "act-7")

    saved = svc.story_node_repo.save.await_args.args[0]
    assert saved.suggested_chapter_count is None


@pytest.mark.asyncio
async def test_create_next_act_auto_persists_chapter_count_source():
    """C-06-10：章数来源要跟着节点落库，否则「标了来源」等于没标。"""
    svc = _svc()
    svc.story_node_repo.get_by_id = AsyncMock(return_value=_current_act())
    svc._get_bible_context = Mock(return_value={})
    svc._generate_next_act_info = AsyncMock(
        return_value={
            "title": "第八幕",
            "description": "推进",
            "suggested_chapter_count": 12,
            "chapter_count_source": "fallback",
        }
    )
    svc.story_node_repo.save = AsyncMock()

    await svc.create_next_act_auto("n-1", "act-7")

    saved = svc.story_node_repo.save.await_args.args[0]
    assert saved.metadata["chapter_count_source"] == "fallback"


# ======================================================================
#  B-11：无效宏观结构不得被替换成代码占位骨架
# ======================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm_result",
    [
        {"success": True, "structure": []},
        {"success": True, "structure": [{"title": "第一部", "volumes": []}]},
        {"success": False, "structure": [{"title": "第一部", "volumes": [{"acts": []}]}]},
        {},
    ],
    ids=["empty_list", "no_volumes", "not_success", "no_keys"],
)
async def test_apply_macro_plan_rejects_invalid_structure_without_persisting(llm_result):
    """B-11-1：无有效结构时必须抛错，且**一个节点都不许落库**。"""
    svc = _svc()
    svc.persist_macro_structure_with_fallback = AsyncMock()

    with pytest.raises(ValueError):
        await svc.apply_macro_plan_from_llm_result(
            llm_result, novel_id="n-1", target_chapters=100
        )

    svc.persist_macro_structure_with_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_apply_macro_plan_persists_valid_structure():
    """B-11-2（反向）：有效结构仍要正常落库 —— 守卫不是把功能焊死。"""
    svc = _svc()
    struct = [{"title": "第一部", "volumes": [{"title": "卷一", "acts": []}]}]
    svc.persist_macro_structure_with_fallback = AsyncMock(
        return_value={"created_nodes": 2, "message": "已同步 2 个结构节点"}
    )

    result = await svc.apply_macro_plan_from_llm_result(
        {"success": True, "structure": struct}, novel_id="n-1", target_chapters=100
    )

    assert result["success"] is True
    assert result["created_nodes"] == 2
    svc.persist_macro_structure_with_fallback.assert_awaited_once()


def test_placeholder_structure_generator_is_gone():
    """B-11-3：造占位骨架的那个方法必须不存在。

    留着它就等于留着一个「看起来能兜底」的入口；任何人日后想把它接回去，
    都会重新引入静默转正。
    """
    assert not hasattr(ContinuousPlanningService, "build_minimal_macro_structure")
    assert not hasattr(cps, "build_minimal_macro_structure")


def test_no_macro_plan_caller_can_opt_into_placeholder_structure():
    """B-11-4：开关本身也不该存在 —— 没有「重新允许占位结构」这种调用方式。

    守卫用的是签名精确匹配（`inspect.signature(...).parameters`），
    不是 `contains` 子串匹配：`allow_minimal_placeholder_on_empty` 与别的参数名
    没有包含关系时也要能报出来，所以这里直接断言参数集合。
    """
    params = inspect.signature(
        ContinuousPlanningService.apply_macro_plan_from_llm_result
    ).parameters
    assert set(params) == {"self", "llm_result", "novel_id", "target_chapters"}


@pytest.mark.asyncio
async def test_act_planning_delegate_does_not_pass_placeholder_flag():
    """B-11-5：调用方不得再传那个已被签名移除的开关。

    用 **AST 判定实际调用**，不用 `inspect.getsource` + `not in src`：
    后者会被注释里的关键字骗红，也会被「换个调用写法」绕过 ——
    复审把这条列为弱信号。注释不进 AST。
    """
    import ast

    import engine.runtime.macro_planning_delegate as mpd

    src = inspect.getsource(mpd)
    tree = ast.parse(textwrap_dedent(src))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "apply_macro_plan_from_llm_result":
            continue
        for kw in node.keywords:
            if (kw.arg or "").startswith("allow_minimal_placeholder"):
                offenders.append((kw.lineno, kw.arg))

    assert not offenders, (
        f"调用方仍在传 {offenders} —— 该开关已从方法签名移除"
    )


def textwrap_dedent(s: str) -> str:
    import textwrap

    return textwrap.dedent(s)
