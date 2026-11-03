# -*- coding: utf-8 -*-
"""第 86 批：文风漂移**报告接口**的契约守卫（此前这个接口是坏的，且没有测试）。

病根
----
`chapter_style_scores.similarity_score` 自第 82 批（N-05）起就是**可空列**
（NULL = 这一章**没评过**），但响应模型 `StyleScoreItem.similarity_score`
仍声明成**必填** ``float`` ⇒ 只要书里有「没评过」的章
（前 5 章建基准、或还没跑到评分的章）
⇒ pydantic 校验失败 ⇒ **整个接口 500**。

⚠ 生产实测：两本书的 ``GET /api/v1/novels/{id}/voice/drift`` **都打不开**
   （`Input should be a valid number ... input_value=None`）。
   ⇒ 这个接口**从第 82 批起就是坏的**，而且**没有任何测试**覆盖它。

本文件钉住
----------
1. DTO 必须接受 ``similarity_score=None``（未评估 ≠ 0.0）；
2. DB 的列可空 ⇒ DTO **必须**可空（契约一致，不许一边可空一边必填）；
3. 路由函数遇到未评分的章必须**照常返回**（不许 500），且 None **原样保留**
   （不许被压成 0.0 —— 那会把「没评过」说成「完全不像」）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from interfaces.api.v1.analyst.voice import (  # noqa: E402
    DriftReportResponse,
    StyleScoreItem,
    get_drift_report,
)


def _row(chapter_number: int, similarity_score):
    return {
        "chapter_number": chapter_number,
        "similarity_score": similarity_score,
        "adjective_density": 0.0239,
        "avg_sentence_length": 15.13,
        "sentence_count": 111,
        "computed_at": "2026-11-03 09:43:55",
    }


# ── 1. DTO 必须能表达「没评过」 ────────────────────────────────────────


def test_the_dto_accepts_an_unevaluated_score():
    item = StyleScoreItem(**_row(1, None))
    assert item.similarity_score is None, "未评估被压成了别的值"


def test_the_dto_still_distinguishes_zero_from_none():
    """⚠ 「相似度 0.0」与「没评过」必须**可分辨**（前者是完全不像，后者是没比）。"""
    zero = StyleScoreItem(**_row(1, 0.0))
    none = StyleScoreItem(**_row(1, None))
    assert zero.similarity_score == 0.0
    assert none.similarity_score is None


# ── 2. 契约一致：DB 可空 ⇒ DTO 必须可空 ────────────────────────────────


def test_the_database_column_is_nullable_so_the_dto_must_be_too(tmp_path):
    """⚠ DB 列可空、DTO 必填 ⇒ 接口在「有未评估章节」时必然 500。

    ⚠ 这条是**契约一致性**检查：两边必须同时可空（或同时必填）。
    """
    from infrastructure.persistence.database.connection import DatabaseConnection

    db = DatabaseConnection(str(tmp_path / "voice_drift.db"))
    try:
        cur = db.get_connection().execute("PRAGMA table_info(chapter_style_scores)")
        columns = {row[1]: row for row in cur.fetchall()}
    finally:
        db.close()

    assert "similarity_score" in columns, "表结构变了"
    nullable = not columns["similarity_score"][3]   # notnull == 0 ⇒ 可空
    assert nullable, (
        "DB 的 similarity_score 变成 NOT NULL 了 ⇒ DTO 必填反而对了；"
        "若确实是契约变更，请同步改这条守卫与 DTO 的注释"
    )
    # DTO 必须能表达 None（否则就是上面那个 500）
    assert StyleScoreItem(**_row(1, None)).similarity_score is None


# ── 3. 路由函数遇到未评分的章必须照常返回 ──────────────────────────────


def test_the_report_endpoint_survives_unevaluated_chapters():
    """⚠ 回归：报告里混着「没评过」的章时，接口**不许**炸。"""
    class _Service:
        def get_drift_report(self, novel_id):
            return {
                "novel_id": novel_id,
                "scores": [
                    _row(1, None),        # 建基准的前几章：没评过
                    _row(6, 0.5531),
                    _row(7, 0.5103),
                ],
                "drift_alert": False,
                "alert_threshold": 0.50,
                "alert_consecutive": 5,
            }

    response = get_drift_report("n1", service=_Service())

    assert isinstance(response, DriftReportResponse)
    assert len(response.scores) == 3
    assert response.scores[0].similarity_score is None, "未评估被压成了 0.0"
    assert response.scores[1].similarity_score == pytest.approx(0.5531)
    assert response.alert_threshold == pytest.approx(0.50), (
        "报告里回显的告警阈值不是标定值 ⇒ 前端与后台口径不一致"
    )


def test_the_report_endpoint_reports_the_calibrated_threshold():
    """⚠ 报告回显的阈值必须与配置一致（前端直接用它显示）。"""
    from application.analyst.services.voice_drift_service import DRIFT_ALERT_THRESHOLD

    class _Service:
        def get_drift_report(self, novel_id):
            return {
                "novel_id": novel_id,
                "scores": [],
                "drift_alert": False,
                "alert_threshold": DRIFT_ALERT_THRESHOLD,
                "alert_consecutive": 5,
            }

    response = get_drift_report("n1", service=_Service())
    assert response.alert_threshold == pytest.approx(0.50)
