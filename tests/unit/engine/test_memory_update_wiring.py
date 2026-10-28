"""第 29 批：**记忆状态回写接线** + **空槽位显式化**。

背景（第 28 批核实）
--------------------
1. `MemoryEngine.update_from_chapter` **早就实现了**（LLM 提取 → 去重合并 → 落库），
   但 `engine/` 全目录**没有任何调用点** ⇒
   ⇒ 「已完成节拍锁（防剧情鬼打墙 / 重复写）」与「已揭露线索清单（防前后矛盾）」
   这两个**跨章一致性机制从未生效**（实测 `memory_engine_state` **全库 0 行**）。
2. 空槽位在槽位表里**照样占着优先级**（有的还是 T0）⇒ 看着像「有内容」⇒ **假信号**；
   而「正常为空」与「功能没开」语义完全不同。

⚠ 守卫纪律：走**真实**的 MemoryEngine 落库 / 读回；接线用 **AST** 判定。
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from application.engine.services.memory_engine import (
    CompletedBeatItem,
    MemoryEngine,
    RevealedClueItem,
)
from engine.runtime.audit_delegate import (
    MEMORY_UPDATE_ISSUE_TYPE,
    _merge_memory_update_issue,
)
from infrastructure.persistence.database.connection import get_database

ROOT = Path(__file__).resolve().parents[3]


class _BibleRepo:
    """只要能过 `FactLockBuilder.build` 的最小 Bible 仓储。"""

    def get_by_novel_id(self, novel_id):
        return SimpleNamespace(
            id="b1",
            novel_id=novel_id,
            characters=[],
            timeline_notes=[],
            locations=[],
            world_settings=[],
            style_notes=[],
        )


def _engine(db):
    return MemoryEngine(
        llm_service=None,
        bible_repository=_BibleRepo(),
        db_connection=db,
    )


# ── ① 写侧落库 ⇒ 下一章读得到 ─────────────────────────────────────────


def test_the_memory_state_is_persisted_and_readable_next_chapter(tmp_path):
    """⚠ 「写侧 ⇒ 读侧」必须通：落库的节拍/线索，下一章的槽位读得到。

    ⚠ 这是「防剧情鬼打墙 / 防前后矛盾」两个机制的**地基**：
    写不进去 ⇒ 读出来永远是空 ⇒ 机制从未生效。
    """
    db = get_database(str(tmp_path / "memory.db"))
    engine = _engine(db)

    state = engine._get_or_load_state("n-1")
    engine._merge_beats(
        state,
        [
            CompletedBeatItem(
                beat_id="ch3-first-trace",
                summary="她第一次听见了声痕",
                chapter=3,
            )
        ],
        3,
    )
    engine._merge_clues(
        state,
        [
            RevealedClueItem(
                clue_id="ch3-ticket-is-invitation",
                content="第四张戏票是请柬，不是催命",
                revealed_at_chapter=3,
            )
        ],
        3,
    )
    state.last_updated_chapter = 3
    engine._persist_state("n-1", state)

    # 下一章（新实例 ⇒ 只能从 DB 读）读得到
    fresh = _engine(db)
    beats = fresh.get_completed_beats_section("n-1")
    clues = fresh.get_revealed_clues_section("n-1")

    assert "她第一次听见了声痕" in beats, "已完成节拍没落库 ⇒ 下一章读不到"
    assert "第四张戏票是请柬，不是催命" in clues, "已揭露线索没落库 ⇒ 下一章读不到"


# ── ② 接线：章后审计必须真的调它 ───────────────────────────────────────


def test_the_chapter_audit_actually_calls_the_memory_update():
    """⚠ 接线必须**真的存在**（此前 `update_from_chapter` 从没被调用过）。

    ⚠ 用 AST 判定（函数体内的真实调用）⇒ 不用文本 contains（会匹配到注释/别的函数）。
    """
    path = ROOT / "engine/runtime/audit_delegate.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_chapter_audit"
    )
    called = {
        call.func.attr
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "host"
    }
    assert "_run_memory_update" in called, (
        "章后审计没有**调用**记忆状态回写 ⇒ 「已完成节拍锁 / 已揭露线索锁」永远为空"
    )


async def test_the_memory_update_degrades_instead_of_crashing_the_chapter():
    """⚠ LLM 挂掉 / 缺依赖 ⇒ **降级**（返回 updated=False + 原因），不许把章末带崩。"""
    from engine.runtime.daemon_host import DaemonHostMixin

    result = await DaemonHostMixin._run_memory_update(
        SimpleNamespace(llm_service=None), "n-1", 1, "正文内容", "大纲"
    )

    assert result["updated"] is False, "LLM 不可用却报成功 ⇒ 静默谎报"
    assert result.get("reason"), "降级时必须给出原因"


async def test_the_memory_update_degrades_when_the_engine_raises(monkeypatch):
    """⚠ **抛异常**的那条降级路径也要降级（不许报成功）。

    ⚠ 上一条走的是「引擎自己吞掉异常、返回 errors」那条路径；
    这里补「引擎直接把异常抛出来」那条 ⇒ 两条降级路径都得守住。
    """
    from application.engine.services import memory_engine as memory_engine_module
    from engine.runtime.daemon_host import DaemonHostMixin

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("引擎炸了")

    monkeypatch.setattr(
        memory_engine_module.MemoryEngine, "update_from_chapter", _boom, raising=True
    )

    result = await DaemonHostMixin._run_memory_update(
        SimpleNamespace(llm_service=None), "n-1", 1, "正文内容", "大纲"
    )

    assert result["updated"] is False, "引擎抛异常却报成功 ⇒ 静默谎报"
    assert "引擎炸了" in str(result.get("reason") or ""), result


# ── ③ 降级**可见**（不许静默） ──────────────────────────────────────────


def test_a_failed_memory_update_leaves_a_visible_entry():
    """⚠ 没更新成功 ⇒ 写一条**可见**的「未更新」条目（同 A1 的降级纪律）。"""
    items = _merge_memory_update_issue(None, {"updated": False, "reason": "LLM 超时"})

    assert len(items) == 1, items
    assert items[0]["type"].startswith(MEMORY_UPDATE_ISSUE_TYPE)
    assert "未更新" in items[0]["description"]
    assert items[0]["severity"] == "info"


def test_a_successful_memory_update_leaves_no_entry():
    """⚠ 成功 ⇒ **一条都不加**（此时 issues 为空才真的意味着「没问题」）。"""
    items = _merge_memory_update_issue(
        [{"type": "setting_drift", "description": "别的漂移"}],
        {"updated": True, "reason": "", "new_beats": 2, "new_clues": 1},
    )

    assert [i for i in items if i["type"].startswith(MEMORY_UPDATE_ISSUE_TYPE)] == []
    assert len(items) == 1, "别的类型的条目被误删了"


def test_the_memory_entry_type_does_not_collide_with_the_drift_one():
    """⚠ 两类条目**必须**用不同 type 前缀 ⇒ 面板按 type 过滤时不会混为一谈。"""
    from engine.runtime.audit_delegate import SETTING_DRIFT_ISSUE_TYPE

    assert not MEMORY_UPDATE_ISSUE_TYPE.startswith(SETTING_DRIFT_ISSUE_TYPE)
    assert not SETTING_DRIFT_ISSUE_TYPE.startswith(MEMORY_UPDATE_ISSUE_TYPE)


# ── ④ 空槽位**显式化**（不再是假信号） ──────────────────────────────────


def test_the_empty_slots_are_collected_with_their_reason():
    """⚠ 空槽位必须**登记**出来 + 带原因 ⇒ 「没有」不再静默。

    ⚠ 也要区分「正常为空」与「功能没开」：
    「已延期伏笔」为空是**正常**，「记忆引擎写侧没跑」是**没开**。
    """
    from application.engine.services.context_budget_allocator import (
        EMPTY_SLOT_REASONS,
        collect_empty_slots,
    )
    from application.engine.services.context_budget_models import ContextSlot, PriorityTier

    assert "deferred_foreshadowings" in EMPTY_SLOT_REASONS
    assert "正常" in EMPTY_SLOT_REASONS["deferred_foreshadowings"]
    for name in ("completed_beats", "revealed_clues"):
        assert name in EMPTY_SLOT_REASONS, f"{name} 没有登记空槽位的原因"
        assert "记忆引擎" in EMPTY_SLOT_REASONS[name]

    slots = {
        "有内容的": ContextSlot(
            name="有内容的", tier=PriorityTier.T0_CRITICAL, content="内容"
        ),
        "空的": ContextSlot(name="空的", tier=PriorityTier.T0_CRITICAL, content=""),
    }
    slots["空的"].empty_reason = "没有数据"

    result = collect_empty_slots(slots)

    assert "空的" in result, "空槽位没有被登记 ⇒ 「没有」看不见"
    assert result["空的"] == "没有数据"
    assert "有内容的" not in result, "有内容的槽位被误报成空"


def test_a_slot_with_content_has_no_empty_reason():
    """⚠ 有内容的槽位**不许**带「空」的原因（否则就是自相矛盾）。"""
    from application.engine.services.context_budget_models import ContextSlot, PriorityTier

    slot = ContextSlot(name="x", tier=PriorityTier.T0_CRITICAL, content="有内容")
    assert slot.empty_reason is None

    empty = ContextSlot(name="y", tier=PriorityTier.T0_CRITICAL, content="")
    assert empty.empty_reason is None, "空原因要由分配器按数据源填，不许构造时臆测"
