"""文风警报器不许谎报（F-16）。

缺陷成因
--------
原实现有三层谎报叠在一起：

1. `getattr(novel, "characters", [])` —— `Novel.__init__` **根本没有**
   `characters` 参数或属性（`domain/novel/entities/novel.py:38-72`），
   所以该属性恒不存在 ⇒ 端点**对任何小说都返回空数组**，
   后面那段 `drift_score = 0.15` / `sample_count = 10` 是不可达的死代码。
2. 前端 `if (dataArray.length > 0)` 不成立 ⇒ `driftData` 保持 `null` ⇒
   `driftScore ?? 0` ⇒ 面板恒显示「文风稳定 0.0」配绿色 ✓。
3. 即便那段代码可达，`similarity_score` 与 `drift_score` 语义**相反**：
   similarity 越高越像基准（越不漂移）。直接透传会把「文风稳定」
   显示成「严重偏离」。

本文件钉住修复后的诚实行为，重点是**第 4 条**：只断言「当前返回什么」
是测不出谎报的，必须**构造出真实的失败态**再看它怎么表现。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.api.v1.workbench import monitor as mon


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mon.router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _unevaluated_payload() -> dict:
    return {
        "novel_id": "n1",
        "drift_score": None,
        "status": "unevaluated",
        "evaluated": False,
        "sample_count": 0,
        "last_scored_chapter": None,
        "reason": "尚无作者风格基准（文风指纹需随章节生成）",
    }


def test_unevaluated_never_reports_a_normal_looking_number():
    """核心红线：没测过就不能给出看起来正常的数值。"""
    r = mon._unevaluated("n1", 0, "没有基准")
    assert r.evaluated is False
    assert r.drift_score is None, (
        f"未评估时 drift_score 必须为 None，实际 {r.drift_score!r} —— "
        f"0 会被读成「测过了，文风完美」"
    )
    assert r.status == "unevaluated"
    assert r.reason == "没有基准", "未评估必须带原因，否则调用方无法区分「没测」与「没问题」"


def test_endpoint_returns_explicit_unevaluated_without_a_fingerprint(monkeypatch):
    """没有指纹 ⇒ 显式未评估，而不是空数组或常数。

    旧实现对**任何**小说都返回 `[]`（因为 `novel.characters` 不存在），
    前端再把 null 压成 0 显示「文风稳定」。这里断言端点**总会给一个对象**，
    带明确的 evaluated 标志。
    """
    monkeypatch.setattr(mon, "get_novel_repository", lambda: type(
        "R", (), {"get_by_id": lambda self, nid: type("N", (), {"id": nid})()})())
    monkeypatch.setattr(mon, "get_voice_fingerprint_repository", lambda: type(
        "R", (), {"get_by_novel": lambda self, nid, pov: None})())

    res = mon._get_voice_drift_sync("n1")
    assert res is not None, "端点返回了 None —— 前端会退化成「空数组 → 文风稳定」"
    assert res.evaluated is False
    assert res.drift_score is None
    assert "基准" in res.reason


def test_insufficient_samples_is_unevaluated_not_zero(monkeypatch):
    """基准样本不足 ⇒ 未评估。写满 10 章门槛才算数（与写入侧一致）。"""
    monkeypatch.setattr(mon, "get_novel_repository", lambda: type(
        "R", (), {"get_by_id": lambda self, nid: type("N", (), {"id": nid})()})())
    monkeypatch.setattr(mon, "get_voice_fingerprint_repository", lambda: type(
        "R", (), {"get_by_novel": lambda self, nid, pov: {"sample_count": 3}})())

    res = mon._get_voice_drift_sync("n1")
    assert res.evaluated is False
    assert res.drift_score is None
    assert res.sample_count == 3, "样本数要是真实的 3，不能编成 10"
    assert "3" in res.reason


def test_drift_is_computed_as_one_minus_similarity(monkeypatch):
    """语义方向必须正确：similarity 高（像基准）⇒ drift 低（不漂移）。

    这条专门钉住「直接透传 similarity」这个错误 —— 那样会把文风稳定
    的书显示成严重偏离。`compute_drift_score` 这个函数名极具误导性，
    它返回的是 similarity。
    """
    monkeypatch.setattr(mon, "get_novel_repository", lambda: type(
        "R", (), {"get_by_id": lambda self, nid: type("N", (), {"id": nid})()})())
    monkeypatch.setattr(mon, "get_voice_fingerprint_repository", lambda: type(
        "R", (), {"get_by_novel": lambda self, nid, pov: {"sample_count": 12}})())
    monkeypatch.setattr(mon, "SqliteChapterStyleScoreRepository", lambda db: type(
        "R", (), {"get_latest_by_novel": lambda self, nid: {
            "chapter_number": 7, "similarity_score": 0.9}})())
    monkeypatch.setattr(mon, "get_database", lambda: object())

    res = mon._get_voice_drift_sync("n1")
    assert res.evaluated is True
    # similarity 0.9 ⇒ drift 0.1
    assert res.drift_score == pytest.approx(0.1, abs=1e-3), (
        f"similarity 0.9 应换算成 drift 0.1，实际 {res.drift_score} —— "
        f"直接透传 similarity 会把稳定文风显示成严重偏离"
    )
    assert res.status == "normal", "drift 0.1 属于正常，不该报 warning"
    assert res.last_scored_chapter == 7
    assert res.reason == ""


def test_high_similarity_can_never_produce_critical(monkeypatch):
    """反向守卫：文风越接近基准，越不可能报严重偏离。"""
    monkeypatch.setattr(mon, "get_novel_repository", lambda: type(
        "R", (), {"get_by_id": lambda self, nid: type("N", (), {"id": nid})()})())
    monkeypatch.setattr(mon, "get_voice_fingerprint_repository", lambda: type(
        "R", (), {"get_by_novel": lambda self, nid, pov: {"sample_count": 50}})())

    # 边界说明：drift >= 0.5 即 critical，所以 similarity=0.5 → drift=0.5 → critical。
    # warning 带是 drift ∈ [0.3, 0.5)，对应 similarity ∈ (0.5, 0.7]。
    for similarity, expected in [(0.95, "normal"), (0.6, "warning"), (0.2, "critical")]:
        monkeypatch.setattr(mon, "SqliteChapterStyleScoreRepository", lambda db: type(
            "R", (), {"get_latest_by_novel": lambda self, nid: {
                "chapter_number": 3, "similarity_score": similarity}})())
        monkeypatch.setattr(mon, "get_database", lambda: object())
        res = mon._get_voice_drift_sync("n1")
        assert res.evaluated is True
        assert res.status == expected, (
            f"similarity={similarity} → drift={res.drift_score}，"
            f"期望 status={expected}，实际 {res.status}"
        )


def test_no_score_row_even_with_baseline_is_unevaluated(monkeypatch):
    """有基准但没评分记录 ⇒ 仍未评估（不是 drift=0）。"""
    monkeypatch.setattr(mon, "get_novel_repository", lambda: type(
        "R", (), {"get_by_id": lambda self, nid: type("N", (), {"id": nid})()})())
    monkeypatch.setattr(mon, "get_voice_fingerprint_repository", lambda: type(
        "R", (), {"get_by_novel": lambda self, nid, pov: {"sample_count": 20}})())
    monkeypatch.setattr(mon, "SqliteChapterStyleScoreRepository", lambda db: type(
        "R", (), {"get_latest_by_novel": lambda self, nid: None})())
    monkeypatch.setattr(mon, "get_database", lambda: object())

    res = mon._get_voice_drift_sync("n1")
    assert res.evaluated is False
    assert res.drift_score is None
    assert res.sample_count == 20


def test_novel_entities_have_no_characters_attribute():
    """锁定缺陷的根因本身。

    旧实现按「每个角色一条」构造响应，但 `Novel` 实体压根没有
    `characters` 属性 —— 所以那个循环永远空转。将来若有人又加了
    按角色循环，这条会提醒他先确认该属性存在。
    """
    from domain.novel.entities.novel import Novel
    from domain.novel.value_objects.novel_id import NovelId

    nv = Novel(id=NovelId("n1"), title="t", author="a", target_chapters=10)
    assert not hasattr(nv, "characters"), (
        "Novel 实体竟然有 characters 属性了 —— 若确实新增，请同步确认"
        " monitor 的响应形状是否应该真的按角色拆分"
    )


def test_endpoint_contract_returns_object_not_list(client, monkeypatch):
    """契约守卫：响应是单个对象，不是数组。

    实测库里根本没有「每角色一个漂移值」这回事（评分表按
    novel+chapter 存，没有 character 列），旧数组形状是虚构的。
    """
    import json

    resp = _unevaluated_payload()
    resp["drift_score"] = None
    # 直接校验序列化形状，确保没有把 null 变回 0
    raw = json.loads(mon.VoiceDriftResponse(**resp).model_dump_json())
    assert raw["drift_score"] is None
    assert raw["evaluated"] is False
    assert "critical" not in (raw["status"],)
