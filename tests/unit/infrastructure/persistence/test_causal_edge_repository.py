"""SqliteCausalEdgeRepository 的承重契约测试。

resolve() 的返回值是因果边闭环能否工作的前提：调用方靠它区分「真的闭环了」
与「LLM 幻觉出的不存在的 id」。这里刻意打真实仓储而不是手写桩——若把仓储里
那段查存在性的逻辑写坏，业务层不会抛错，只会静默地不再闭环，而只验桩的测试
照样全绿。

get_unresolved 的排序/limit 同理：它决定每章哪几条边能进入 LLM 评估窗口，
策略写坏时没有任何异常，只表现为"某些边永远闭环不了"。
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


def _seed_edges(db, chapters):
    """按 source_chapter 升序建若干条未闭环边。"""
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')")
    for i, chapter in enumerate(chapters):
        db.execute(
            """INSERT INTO causal_edges
               (id, novel_id, source_event_summary, source_chapter, causal_type, target_event_summary)
               VALUES (?, 'novel-1', ?, ?, 'causes', ?)""",
            (f"edge-{i:03d}", f"第{i}桩因由", chapter, f"第{i}个后果"),
        )


class TestGetUnresolvedSelectionPolicy:
    """选择策略的承重契约。

    调用方每章只把 MAX_PENDING_CAUSAL_EDGES_FOR_LLM 条边交给 LLM 判定，
    所以"哪些边进入窗口"完全由这里的排序决定。此前按 source_chapter ASC
    且无 LIMIT：最老的 15 条边永久霸占窗口，第 15 名之后的边永远得不到评估。
    """

    def test_orders_newest_source_chapter_first(self, tmp_path):
        db = DatabaseConnection(str(tmp_path / "causal.db"))
        _seed_edges(db, [3, 10, 25])
        repo = SqliteCausalEdgeRepository(db)

        edges = repo.get_unresolved("novel-1")

        assert [e.source_chapter for e in edges] == [25, 10, 3]

    def test_limit_selects_the_newest_window(self, tmp_path):
        """窗口落在最近的边上，而不是最老的一批。"""
        db = DatabaseConnection(str(tmp_path / "causal.db"))
        _seed_edges(db, list(range(1, 31)))  # 30 条未闭环边
        repo = SqliteCausalEdgeRepository(db)

        edges = repo.get_unresolved("novel-1", limit=15)

        assert len(edges) == 15
        assert [e.source_chapter for e in edges] == list(range(30, 15, -1))

    def test_limit_is_pushed_into_sql(self, tmp_path, monkeypatch):
        """LIMIT 必须落在 SQL 层：Python 侧拿回全集再切片等于没限。"""
        db = DatabaseConnection(str(tmp_path / "causal.db"))
        _seed_edges(db, list(range(1, 31)))
        repo = SqliteCausalEdgeRepository(db)

        seen = {}
        real_fetch_all = db.fetch_all

        def _spy(sql, params=None):
            seen["sql"] = " ".join(str(sql).split()).upper()
            seen["params"] = params
            return real_fetch_all(sql, params)

        monkeypatch.setattr(db, "fetch_all", _spy)
        repo.get_unresolved("novel-1", limit=15)

        assert "LIMIT ?" in seen["sql"], seen["sql"]
        assert seen["params"] == ("novel-1", 15)

    def test_no_limit_returns_every_unresolved_edge(self, tmp_path):
        db = DatabaseConnection(str(tmp_path / "causal.db"))
        _seed_edges(db, list(range(1, 31)))
        repo = SqliteCausalEdgeRepository(db)

        assert len(repo.get_unresolved("novel-1")) == 30

    def test_resolved_edges_still_excluded(self, tmp_path):
        """换排序不能把已闭环的边塞回候选窗口。"""
        db = DatabaseConnection(str(tmp_path / "causal.db"))
        _seed_edges(db, [3, 10, 25])
        repo = SqliteCausalEdgeRepository(db)
        repo.resolve("edge-001", 9)

        edges = repo.get_unresolved("novel-1", limit=15)

        assert [e.source_chapter for e in edges] == [25, 3]