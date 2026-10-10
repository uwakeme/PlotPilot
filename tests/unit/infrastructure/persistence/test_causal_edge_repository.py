"""SqliteCausalEdgeRepository.resolve() 的承重契约测试。

resolve() 的返回值是因果边闭环能否工作的前提：调用方靠它区分「真的闭环了」
与「LLM 幻觉出的不存在的 id」。这里刻意打真实仓储而不是手写桩——若把仓储里
那段查存在性的逻辑写坏，业务层不会抛错，只会静默地不再闭环，而只验桩的测试
照样全绿。
"""
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_causal_edge_repository import (
    SqliteCausalEdgeRepository,
)


def _seed_one_edge(db):
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')")
    db.execute(
        """INSERT INTO causal_edges
           (id, novel_id, source_event_summary, source_chapter, causal_type, target_event_summary)
           VALUES ('edge-1', 'novel-1', '林渊在断魂崖拾得铜铃', 3, 'causes', '林渊习得归元剑法')"""
    )


def test_resolve_returns_true_for_existing_edge(tmp_path):
    db = DatabaseConnection(str(tmp_path / "causal.db"))
    _seed_one_edge(db)
    repo = SqliteCausalEdgeRepository(db)

    assert repo.resolve("edge-1", 7) is True

    row = db.fetch_one("SELECT * FROM causal_edges WHERE id = ?", ("edge-1",))
    assert row["is_resolved"] == 1
    assert row["resolved_chapter"] == 7


def test_resolve_returns_false_for_unknown_edge_id(tmp_path):
    """未知 id 必须让调用方知道「没闭环」，而不是静默成功。"""
    db = DatabaseConnection(str(tmp_path / "causal.db"))
    _seed_one_edge(db)
    repo = SqliteCausalEdgeRepository(db)

    assert repo.resolve("ghost-id-not-real", 7) is False


def test_resolve_stays_true_when_called_again_on_same_edge(tmp_path):
    """重复闭环同一 id 仍算命中：已闭环不等于不存在。"""
    db = DatabaseConnection(str(tmp_path / "causal.db"))
    _seed_one_edge(db)
    repo = SqliteCausalEdgeRepository(db)

    assert repo.resolve("edge-1", 7) is True
    assert repo.resolve("edge-1", 9) is True

    row = db.fetch_one("SELECT * FROM causal_edges WHERE id = ?", ("edge-1",))
    assert row["is_resolved"] == 1
    assert row["resolved_chapter"] == 9