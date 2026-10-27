"""第 19 批：``last_audit_narrative_ok`` 的**三态**要贯穿 DB 全链路（第 18 批只做了共享内存那一层）。

背景
----
第 18 批把「未评估 ≠ 失败」做进了**判定层**与**共享内存**，但 DB 列仍是布尔
⇒ 落库那一层会把 ``None``（未评估）**压成 0（失败）** ⇒ 面板上又变回谎报。

⇒ 这一列的语义必须是**三态**：``1``=已同步 / ``0``=明确失败 / ``NULL``=**未评估**。
（``NULL`` 与「从未审计过」可分辨：后者 ``last_audit_chapter_number`` 也是 NULL。）

守卫清单
--------
- 实体字段是 ``Optional[bool]``
- 三态 → DB 列值的转换：**None 保持 None**
- 仓储读、缓存读：**NULL 读成 None**，不压成 True/False
- 守护进程的落盘字段走**同一个**转换函数（单一来源）
- API 不再把它包成 ``bool(...)``（否则又压成 False）
"""
from __future__ import annotations

import inspect
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_the_entity_field_is_optional():
    """实体字段必须能承载 None（未评估）。

    ⚠ ``Novel`` 是**普通类**（不是 dataclass）⇒ 注解在 ``__init__`` 的签名里，
    ``typing.get_type_hints(Novel)`` 看不到它 ⇒ 要看签名。
    """
    from domain.novel.entities.novel import Novel

    import typing

    param = inspect.signature(Novel.__init__).parameters["last_audit_narrative_ok"]
    assert param.annotation == typing.Optional[bool], (
        f"实体的 last_audit_narrative_ok 注解是 {param.annotation!r}，"
        "承载不了 None ⇒ 未评估会退化成 False"
    )


def test_the_db_conversion_keeps_none():
    """三态 → DB 列值：None 必须**原样**（写 NULL）。"""
    from infrastructure.persistence.database.sqlite_novel_repository import (
        narrative_ok_to_db,
    )

    assert narrative_ok_to_db(None) is None, "未评估被压成了 0/1"
    assert narrative_ok_to_db(True) == 1
    assert narrative_ok_to_db(False) == 0


def test_the_repository_read_keeps_none():
    """仓储读：列是 NULL ⇒ 实体字段是 None（不是 True/False）。"""
    from infrastructure.persistence.database.sqlite_novel_repository import (
        SqliteNovelRepository,
    )
    from domain.novel.value_objects.novel_id import NovelId

    repo = SqliteNovelRepository.__new__(SqliteNovelRepository)  # 不碰 DB
    row = {
        "id": "n-1",
        "title": "t",
        "author": "a",
        "target_chapters": 3,
        "current_stage": "writing",
        "autopilot_status": "stopped",
        "last_audit_narrative_ok": None,
        "last_audit_chapter_number": 2,
    }
    novel = repo._row_to_novel(NovelId("n-1"), row)
    assert novel.last_audit_narrative_ok is None, (
        f"仓储把 NULL 读成了 {novel.last_audit_narrative_ok!r} ⇒ 未评估变成了谎报"
    )

    row_ok = dict(row, last_audit_narrative_ok=1)
    assert repo._row_to_novel(NovelId("n-1"), row_ok).last_audit_narrative_ok is True
    row_bad = dict(row, last_audit_narrative_ok=0)
    assert repo._row_to_novel(NovelId("n-1"), row_bad).last_audit_narrative_ok is False


def test_the_cached_read_keeps_none():
    """缓存/优化路径的读法也必须保持 None（两处口径不能分裂）。"""
    from infrastructure.persistence.database import query_optimizations as qo

    src = inspect.getsource(qo)
    assert "last_audit_narrative_ok=(None if _lano is None else bool(_lano))" in src, (
        "query_optimizations 的读法没有保持三态 ⇒ 与仓储口径分裂"
    )


def test_the_daemon_uses_the_shared_conversion():
    """守护进程落盘字段必须走**同一个**转换函数（单一来源）。"""
    from engine.runtime import daemon_host

    src = inspect.getsource(daemon_host)
    assert "narrative_ok_to_db(" in src, "守护进程没有用共享的三态转换函数"
    assert "1 if getattr(novel, 'last_audit_narrative_ok', True) else 0" not in src, (
        "守护进程里仍内联了一份转换 ⇒ 两份会漂（且把 None 压成 0）"
    )


def test_the_api_does_not_collapse_none_into_false():
    """API 不许把它包成 bool（那会把 None 变成 False）。"""
    path = ROOT / "interfaces/api/v1/engine/autopilot_routes.py"
    src = path.read_text(encoding="utf-8")
    assert 'bool(getattr(novel, "last_audit_narrative_ok"' not in src, (
        "API 仍把三态压成布尔 ⇒ 面板上「未评估」又变「失败」"
    )
    assert 'bool(shared.get("last_audit_narrative_ok"' not in src, (
        "API 的共享内存路径仍把三态压成布尔"
    )
