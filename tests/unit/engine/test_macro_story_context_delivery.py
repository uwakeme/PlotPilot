"""宏观规划：故事上下文必须真的进到提示词里。

背景 —— 这是**真实跑一本真书**时实测出来的，不是推演：
    自动驾驶的宏观规划走 invocation 管线，变量由**变量中枢**解析。
    而 delegate 只往中枢写了 ``planning_depth`` / ``rec_*`` 六个结构旋钮，
    模板声明的角色 / 世界观 / 地点三个 alias 一个都没给 ⇒

      * ``characters`` 是**必填** ⇒ 报「必填变量缺失」⇒ 会话直接 blocked，
        **模型根本没被调用**（实测 attempts 表为空，一条 attempt 都没有）；
      * ``worldbuilding.content`` / ``locations.list`` 不必填 ⇒ 静默渲染成空串，
        模型拿到的是「没有世界观、没有地点」的结构提案（更隐蔽的一种）。

本文件守的是**整条链**：delegate 装配 → 中枢解析 → CPMS 渲染，
断言最后送进模型的提示词里真的有角色、世界观、地点。

⚠ 守卫纪律（照仓库里已有的那几条来）：
    * 走**真实**的 delegate / 契约 / resolver / assembler / 模板引擎，
      不复刻任何一层的形状 —— 复制实现 = 零覆盖；
    * 中枢里的 ``novel.setup.*`` 按**真实流程**摆（建书时由 novel_service 写入）；
    * 载荷用本文件专属的名字 ⇒ 断言不会「碰巧」命中别处的字符串；
    * 另有两条断言检查「换输入 ⇒ 换输出」，防止守卫只在固定载荷下成立。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from application.ai_invocation.prompt_assembler import CPMSPromptAssembler
from application.ai_invocation.variable_hub import VariableResolver, VariableWrite
from domain.novel.entities.novel import Novel
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.ai.prompt_keys import PLANNING_QUICK_MACRO
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
    SqliteInvocationSpecRepository,
    SqliteVariableHubRepository,
)

# 载荷：本文件专属名字（与仓库里其它用例都不同 ⇒ 断言不会碰巧命中）
NOVEL_ID = "novel-macro-story-ctx"
CHARACTER_NAME = "沈砚青"
LOCATION_NAME = "青石巷放映厅"
WORLD_SETTING_NAME = "宵禁后的默片禁令"
PREMISE = "记者追查一场发生在战时城郊放映厅的连环昏厥案。"

CONTEXT_KEY = f"novel_id:{NOVEL_ID}"
TARGET_CHAPTERS = 3


# ======================================================================
#  夹具
# ======================================================================


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一个真 DB + 真 CPMS registry（与生产同构）。

    ⚠ 用完**必须还原**全局单例：``_manager_instance`` / ``_registry_instance``
    是模块级全局，指向临时库却不还原 ⇒ 临时库一关，
    后面任何一个用 ``get_prompt_registry()`` 的用例都会炸
    「Cannot operate on a closed database」。
    """
    conn = DatabaseConnection(str(tmp_path / "plotpilot-macro-story-ctx.db"))
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda db_path=None: conn,
    )

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prev_manager = prompt_manager_module._manager_instance
    prev_registry = prompt_registry_module._registry_instance
    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(conn)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    try:
        yield conn
    finally:
        prompt_manager_module._manager_instance = prev_manager
        prompt_registry_module._registry_instance = prev_registry
        conn.close_all(skip_checkpoint=True)


def _seed_setup_variables(conn) -> None:
    """按**真实流程**摆中枢：建书时 novel_service 会写这两条。"""
    repo = SqliteVariableHubRepository(conn)
    for key, value, value_type, display_name in (
        ("novel.setup.premise", PREMISE, "string", "设定"),
        ("novel.setup.target_chapters", TARGET_CHAPTERS, "integer", "章节数量"),
    ):
        repo.set_value(
            VariableWrite(
                key=key,
                value=value,
                context_key=CONTEXT_KEY,
                source_trace_id="novel_service_sync",
                source_node_key="novel_service",
                value_type=value_type,
                display_name=display_name,
                scope="global",
                stage="setup",
            )
        )


def _bible_service(character_name: str = CHARACTER_NAME):
    class _FakeBibleService:
        def get_bible_by_novel(self, novel_id):
            return SimpleNamespace(
                characters=[
                    SimpleNamespace(
                        id="char-1", name=character_name, description="跑社会新闻的记者"
                    )
                ],
                world_settings=[
                    SimpleNamespace(
                        id="w-1",
                        name=WORLD_SETTING_NAME,
                        description="战时宵禁，默片一律禁映",
                    )
                ],
                locations=[
                    SimpleNamespace(
                        id="loc-1", name=LOCATION_NAME, description="城郊的老放映厅"
                    )
                ],
                timeline_notes=[
                    SimpleNamespace(id="t-1", event="首场放映", description="三层观众同时昏厥")
                ],
            )

    return _FakeBibleService()


def _planning_service(character_name: str = CHARACTER_NAME):
    """**真实**的 ContinuousPlanningService（不 mock build_quick_macro_variables）。"""
    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    return ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
        bible_service=_bible_service(character_name),
    )


def _novel() -> Novel:
    return Novel(
        id=NovelId(NOVEL_ID),
        title="放映厅疑案",
        author="测试作者",
        target_chapters=TARGET_CHAPTERS,
        premise=PREMISE,
    )


class _CapturingOrchestrator:
    """截获 delegate 真正发出的 invocation intent。"""

    def __init__(self):
        self.intent = None

    async def request(self, intent):
        self.intent = intent
        return SimpleNamespace(
            session_id="session-1",
            status="completed",
            node_key=intent.node_key,
            operation=intent.operation,
            autopilot_pause_reason="",
            payload={},
        )


async def _request_macro_intent(monkeypatch, planning_service) -> _CapturingOrchestrator:
    """跑**真实**的 delegate 装配（只把「网络那一跳」替掉）。"""
    from engine.runtime import macro_planning_delegate

    orchestrator = _CapturingOrchestrator()
    monkeypatch.setattr(
        "application.ai_invocation.autopilot.factory.get_or_create_autopilot_orchestrator",
        lambda host: orchestrator,
    )

    host = SimpleNamespace(planning_service=planning_service, llm_service=None)
    await macro_planning_delegate._request_macro_invocation(
        host, _novel(), target_chapters=TARGET_CHAPTERS
    )
    assert orchestrator.intent is not None, "delegate 没有发出 invocation"
    return orchestrator


def _resolve_and_render(conn, explicit_variables):
    """走**真实**的契约 → resolver → assembler → 模板引擎。"""
    spec = SqliteInvocationSpecRepository(conn).get(
        "autopilot.macro.plan", PLANNING_QUICK_MACRO
    )
    assert spec is not None, "宏观规划契约没落库"

    repo = SqliteVariableHubRepository(conn)
    plan = VariableResolver(repo).resolve(
        spec=spec,
        explicit_variables=explicit_variables,
        context={"novel_id": NOVEL_ID, "target_chapters": TARGET_CHAPTERS},
    )
    snapshot = CPMSPromptAssembler().compile(spec=spec, variable_plan=plan)
    return plan, snapshot


# ======================================================================
#  A. 装配点：delegate 必须把故事上下文交出去
# ======================================================================


@pytest.mark.asyncio
async def test_the_delegate_hands_the_story_context_to_the_invocation(db, monkeypatch):
    """delegate 产出的 intent 必须带着角色 / 世界观 / 地点。

    改造前：``explicit_variables={}`` ⇒ 三个 alias 一个都取不到。
    """
    _seed_setup_variables(db)
    orchestrator = await _request_macro_intent(monkeypatch, _planning_service())

    explicit = orchestrator.intent.explicit_variables
    assert "characters" in explicit
    assert "worldbuilding.content" in explicit
    assert "locations.list" in explicit
    assert CHARACTER_NAME in explicit["characters"]
    assert WORLD_SETTING_NAME in explicit["worldbuilding.content"]
    assert LOCATION_NAME in explicit["locations.list"]


@pytest.mark.asyncio
async def test_the_delegate_uses_the_same_context_as_the_ui_path(db, monkeypatch):
    """自动驾驶路径与 UI 极速规划路径拿到的是**同一份**上下文。

    两边都走 ``build_quick_macro_variables`` ⇒ 同一逻辑只写一份。
    """
    _seed_setup_variables(db)
    planning_service = _planning_service()
    orchestrator = await _request_macro_intent(monkeypatch, planning_service)

    expected = planning_service.build_quick_macro_variables(
        planning_service._get_bible_context(NOVEL_ID), TARGET_CHAPTERS, ai_depth=None
    )
    explicit = orchestrator.intent.explicit_variables

    assert explicit["characters"] == expected["characters"]
    assert explicit["worldbuilding.content"] == expected["worldbuilding"]["content"]
    assert explicit["locations.list"] == expected["locations"]["list"]


# ======================================================================
#  B. 全链：渲染出来的提示词里真的有故事上下文
# ======================================================================


@pytest.mark.asyncio
async def test_the_rendered_prompt_contains_the_story_context(db, monkeypatch):
    """最后送进模型的提示词里必须有角色 / 世界观 / 地点。

    这一条守的是**整条链**：delegate → 中枢解析 → CPMS 渲染。
    只断言 intent 是不够的 —— 值可能在解析或渲染那一步被丢掉。
    """
    _seed_setup_variables(db)
    orchestrator = await _request_macro_intent(monkeypatch, _planning_service())

    plan, snapshot = _resolve_and_render(db, orchestrator.intent.explicit_variables)

    assert plan.required_missing == (), f"必填变量没解析出来：{plan.required_missing}"
    user = snapshot.prompt.user
    assert CHARACTER_NAME in user
    assert WORLD_SETTING_NAME in user
    assert LOCATION_NAME in user
    # 模板的三个区块标题都还在（说明是模板渲染出来的，不是别处塞进去的）
    assert "【角色与关系】" in user
    assert "【世界观】" in user
    assert "【地点】" in user


@pytest.mark.asyncio
async def test_a_different_bible_yields_a_different_prompt(db, monkeypatch):
    """换输入 ⇒ 换输出（防止守卫只在固定载荷下成立）。"""
    _seed_setup_variables(db)
    orchestrator = await _request_macro_intent(
        monkeypatch, _planning_service(character_name="陆听澜")
    )
    _, snapshot = _resolve_and_render(db, orchestrator.intent.explicit_variables)

    assert "陆听澜" in snapshot.prompt.user
    assert CHARACTER_NAME not in snapshot.prompt.user


# ======================================================================
#  C. 契约：故事上下文是「调用方提供」，characters 必填
# ======================================================================


def test_the_story_context_aliases_are_declared_as_caller_supplied(db):
    """契约要如实说清这三个 alias 由**调用方**提供，而不是从中枢取。

    中枢里根本没有 ``novel.characters.list``（只有手动回填接口会写），
    绑上去 ⇒ 必填变量解析不到 ⇒ 会话 blocked、模型没被调用。
    """
    from application.ai_invocation.contracts.autopilot_planning import (
        ensure_autopilot_macro_plan_contract,
    )

    ensure_autopilot_macro_plan_contract(db=db)
    bindings = {
        binding.alias: binding
        for binding in SqliteVariableHubRepository(db).get_bindings(
            f"{PLANNING_QUICK_MACRO}:input:autopilot:v1", PLANNING_QUICK_MACRO
        )
    }

    for alias in ("characters", "worldbuilding.content", "locations.list"):
        assert bindings[alias].source == "prompt_input", (
            f"{alias} 的来源标注不对：{bindings[alias].source}"
        )
        assert bindings[alias].variable_key == "", (
            f"{alias} 不该绑中枢键：{bindings[alias].variable_key}"
        )
    assert bindings["characters"].required is True


# ======================================================================
#  D. 中枢解析项：显式提供的值不许被报成「无效」
# ======================================================================


@pytest.mark.asyncio
async def test_the_resolution_items_report_explicit_values_as_resolved(db, monkeypatch):
    """调用方显式提供的值，解析项要报「已解析 / 来源 explicit」。

    此前这类 binding（没有中枢键）一律被记成 ``invalid`` —— 那是谎报：
    值明明就在，面板上却显示「无效」，排查时会被带偏。
    """
    _seed_setup_variables(db)
    orchestrator = await _request_macro_intent(monkeypatch, _planning_service())

    plan, _ = _resolve_and_render(db, orchestrator.intent.explicit_variables)
    items = {item["alias"]: item for item in plan.resolution_items}

    for alias in ("characters", "worldbuilding.content", "locations.list"):
        assert items[alias]["status"] == "resolved", items[alias]
        assert items[alias]["source"] == "explicit", items[alias]
