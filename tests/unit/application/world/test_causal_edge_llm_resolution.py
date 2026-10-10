"""因果边闭环改由 LLM 判定。

背景：原实现用 str.split() 提关键词做整串包含匹配，中文无空格导致
keywords 恒为整串，真实库 1511 条因果边仅闭环 1 条（0.07%）。
控制器已验证 bigram 覆盖率与滑窗相似度在真实数据上同样失效
（前者 P25 即 0.571、后者中位 0.092），字符串路线整体不可行。
"""
from application.world.services.chapter_narrative_sync import (
    _build_causal_edge_context,
    _resolve_causal_edges_from_ids,
)


class _Edge:
    def __init__(self, eid, summary):
        self.id = eid
        self.target_event_summary = summary
        self.causal_type = "causes"
        self.source_event_summary = "某个前因"


class _Repo:
    def __init__(self, known=("edge-a", "edge-b")):
        self.resolved = []
        self._known = set(known)

    def resolve(self, edge_id, chapter_number):
        """模拟真实仓储语义：命中返回 True，未知 id 返回 False 且不记录。"""
        if edge_id not in self._known:
            return False
        self.resolved.append((edge_id, chapter_number))
        return True


class TestBuildCausalEdgeContext:
    def test_empty_input_returns_empty_string(self):
        assert _build_causal_edge_context([]) == ""

    def test_contains_id_and_summary(self):
        text = _build_causal_edge_context([
            {"id": "edge-abc", "target_event_summary": "林渊习得归元剑法"},
        ])
        assert "edge-abc" in text, "必须带上 id，否则回程无法精确定位"
        assert "林渊习得归元剑法" in text

    def test_includes_instructive_instruction(self):
        text = _build_causal_edge_context([
            {"id": "e1", "target_event_summary": "某事"},
        ])
        assert "closed_causal_edge_ids" in text, "必须告知 LLM 输出字段名"


class TestResolveCausalEdgesFromIds:
    def test_resolves_ids_returned_by_llm(self):
        repo = _Repo()
        n = _resolve_causal_edges_from_ids(
            "novel-1", 7,
            {"closed_causal_edge_ids": ["edge-a", "edge-b"]},
            repo,
        )
        assert n == 2
        assert repo.resolved == [("edge-a", 7), ("edge-b", 7)]

    def test_hallucinated_id_is_not_counted(self):
        """LLM 可能返回不存在的 id —— resolve 返回 False，不得计入闭环数。"""
        repo = _Repo()
        n = _resolve_causal_edges_from_ids(
            "novel-1", 7,
            {"closed_causal_edge_ids": ["edge-a", "ghost-id-not-real"]},
            repo,
        )
        assert n == 1, "未知 id 不得计入闭环数"
        assert repo.resolved == [("edge-a", 7)]

    def test_missing_key_is_noop(self):
        repo = _Repo()
        assert _resolve_causal_edges_from_ids("novel-1", 7, {}, repo) == 0
        assert repo.resolved == []

    def test_non_list_value_is_noop(self):
        repo = _Repo()
        assert _resolve_causal_edges_from_ids(
            "novel-1", 7, {"closed_causal_edge_ids": "not-a-list"}, repo
        ) == 0

    def test_repository_none_is_noop(self):
        assert _resolve_causal_edges_from_ids(
            "novel-1", 7, {"closed_causal_edge_ids": ["x"]}, None
        ) == 0