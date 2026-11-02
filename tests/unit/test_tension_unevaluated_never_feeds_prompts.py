# -*- coding: utf-8 -*-
"""第 84 批：「没评过」的张力**不许**进提示词。

病根
----
第 82 批给张力加了「有没有真的评过」的标记（``tension_evaluated``），
但**读**的一侧还有 6 处把**占位的 50.0** 当成真实分值喂给模型：

1. ``TensionScoringService.score_chapter(prev_chapter_tension=50.0)``
   ⇒ **默认值本身就是假值** ⇒ 没有前章时也在说「前章综合张力为 50/100」；
2. 提示词模板里的**条件指令**（「若前章≥70 可以回落 / 若前章≤30 需要拉升」）
   ⇒ 光把数字换成「未评估」不够 ⇒ 模型仍会自己假设一个数；
3. ``chapter_narrative_sync`` 的**轻量 SQL** 路径只查 ``tension_score``
   ⇒ **完全绕过了标记**（性能优化路径反而更不可信）；
4. 同上的降级路径（``prev_ch.tension_score``）；
5. ``tension_analyzer`` 把四个 50.0 写成「库内章节张力字段（仅作参考）」；
6. ``reader_simulation_service`` 把 50.0 写成 ``tension_score=50``。

⇒ 本批统一口径：**没评过 ⇒ 给「未评估」**（而不是一个看起来正常的数字）。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TENSION_NODE = ROOT / "infrastructure/ai/prompt_packages/nodes/tension-scoring"

sys.path.insert(0, str(ROOT))


# ── 1. 实体的合法出口 ───────────────────────────────────────────────────


def _chapter(evaluated: bool, score: float = 50.0):
    from domain.novel.entities.chapter import Chapter
    from domain.novel.value_objects.novel_id import NovelId

    return Chapter(
        id="ch-1",
        novel_id=NovelId("n1"),
        number=1,
        title="第一章",
        content="正文",
        tension_score=score,
        tension_evaluated=evaluated,
    )


def test_an_unevaluated_chapter_exposes_no_tension():
    """⚠ 没评过 ⇒ ``evaluated_tension_score`` 是 ``None``（不是 50.0）。"""
    assert _chapter(evaluated=False).evaluated_tension_score is None


def test_an_evaluated_chapter_exposes_its_tension():
    """⚠ 评过 ⇒ 给真值。"""
    assert _chapter(evaluated=True, score=71.0).evaluated_tension_score == 71.0


# ── 2. 提示词变量：未评估 ⇒ 明确说「未评估」并撤掉条件指令 ──────────────


def test_the_unknown_prev_tension_text_says_unevaluated():
    from application.analyst.services.tension_scoring_service import (
        PREV_TENSION_UNKNOWN_TEXT,
        build_prev_tension_text,
    )

    text = build_prev_tension_text(None)
    assert text == PREV_TENSION_UNKNOWN_TEXT
    assert "未评估" in text
    # ⚠ 条件指令必须在「未评估」时**撤掉**（否则模型会自己假设一个数）
    assert "若前章≥70" not in text
    assert "若前章≤30" not in text
    # ⚠ 也不许暗示「前章是 50」
    assert "50/100" not in text


def test_the_known_prev_tension_text_carries_the_number():
    from application.analyst.services.tension_scoring_service import (
        build_prev_tension_text,
    )

    text = build_prev_tension_text(70.0)
    assert "70/100" in text
    assert "若前章≥70" in text


def test_the_scoring_service_default_is_not_a_fake_fifty():
    """⚠ ``score_chapter`` 的默认值必须是 ``None``（此前是 ``50.0``）。"""
    import inspect

    from application.analyst.services.tension_scoring_service import (
        TensionScoringService,
    )

    sig = inspect.signature(TensionScoringService.score_chapter)
    default = sig.parameters["prev_chapter_tension"].default
    assert default is None, "默认值还是 50.0 ⇒ 没有前章时也在骗模型"


@pytest.mark.asyncio
async def test_the_rendered_prompt_says_unevaluated(monkeypatch):
    """⚠ **行为级**：没给前章张力 ⇒ 送进提示词的就是「未评估」那句。"""
    from application.analyst.services import tension_scoring_service as mod

    captured: dict = {}

    class _FakeGateway:
        def render(self, _contract, variables):
            captured.update(variables)
            return SimpleNamespace(prompt="P")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("到此为止（测试只需要渲染后的变量）")

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway())
    monkeypatch.setattr(mod, "structured_json_generate_with_status", _boom)

    result = await mod.TensionScoringService(object()).score_chapter("正文内容", 1)

    assert "未评估" in captured["prev_tension"]
    assert "50/100" not in captured["prev_tension"], (
        "提示词里仍然出现了「50/100」⇒ 占位值还在污染判断依据"
    )
    assert result.is_evaluated is False


# ── 3. 提示词包：模板与变量声明都不能再自带假值 ──────────────────────────


def test_the_prompt_template_has_no_hardcoded_prev_tension_rule():
    """⚠ 模板里的条件指令已挪进变量（未评估时整句撤掉）。"""
    text = (TENSION_NODE / "system.md").read_text(encoding="utf-8-sig")
    assert "前章综合张力为 {prev_tension}/100" not in text, (
        "模板里仍写死了「前章综合张力为 {prev_tension}/100」"
        "⇒ 未评估时会被渲染成「未评估/100」"
    )
    assert "{prev_tension}" in text, "模板没引用 prev_tension 变量"


def test_the_variable_declaration_has_no_fake_default():
    """⚠ ``prev_tension`` 不能再有 ``default: 50``（那个默认就是假值）。

    ⚠ 按 **YAML 解析**看声明（不是搜原文）⇒ 注释里提到这串字不会被误判。
    """
    import yaml

    data = yaml.safe_load((TENSION_NODE / "package.yaml").read_text(encoding="utf-8-sig")) or {}
    variables = {v["name"]: v for v in (data.get("variables") or [])}
    assert "prev_tension" in variables, "prev_tension 变量不见了"
    spec = variables["prev_tension"]
    assert "default" not in spec, (
        "prev_tension 还有默认值 %r ⇒ 缺值时又变成假 50" % spec.get("default")
    )
    assert spec.get("type") == "string", "prev_tension 现在是一整句 ⇒ 类型应是 string"


# ── 4. 轻量 SQL 路径也必须看标记 ───────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    from infrastructure.persistence.database.connection import DatabaseConnection

    database = DatabaseConnection(str(tmp_path / "tension.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES ('n1', 'N', 'n1', 10)"
    )
    database.get_connection().commit()
    yield database
    database.close()


def _seed_chapter(db, number: int, evaluated: int, tension: float) -> None:
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status, "
        "tension_score, tension_evaluated) VALUES (?, 'n1', ?, ?, '', 'completed', ?, ?)",
        ("ch-%d" % number, number, "第%d章" % number, tension, evaluated),
    )
    db.get_connection().commit()


def test_the_lightweight_sql_path_respects_the_flag(db):
    """⚠ 轻量 SQL（性能优化路径）**也**必须读 ``tension_evaluated``。"""
    from application.world.services.chapter_narrative_sync import (
        _fetch_evaluated_tension,
    )

    _seed_chapter(db, 1, 0, 50.0)     # 未评估 ⇒ 值是占位
    _seed_chapter(db, 2, 1, 71.0)     # 评过

    # ⚠ 传的是 **DatabaseConnection**（生产路径就是这样：``chapter_repository.db``）
    assert _fetch_evaluated_tension(db, "n1", 1) is None, (
        "未评估章节的占位 50.0 被当成真实分值了"
    )
    assert _fetch_evaluated_tension(db, "n1", 2) == 71.0


def test_the_previous_tension_reader_respects_the_flag(db):
    """⚠ 剧情点判定的那条「证据」路径同样不许拿占位值。"""
    from application.world.services.chapter_narrative_sync import (
        _fetch_previous_tension,
    )

    _seed_chapter(db, 1, 0, 50.0)
    _seed_chapter(db, 2, 1, 64.0)

    class _Repo:
        def __init__(self, conn):
            self.db = conn

    repo = _Repo(db)
    assert _fetch_previous_tension("n1", 2, repo) is None, "前章未评估 ⇒ 证据应为 None"
    _seed_chapter(db, 3, 1, 64.0)
    assert _fetch_previous_tension("n1", 3, repo) == 64.0


# ── 5. 另外两个提示词消费方 ────────────────────────────────────────────


def test_the_tension_analyzer_says_unevaluated():
    """⚠ 文风/张力分析那段「仅作参考」的字段：没评过 ⇒ 说未评估。"""
    import inspect

    from application.analyst.services import tension_analyzer as mod

    src = inspect.getsource(mod)
    assert "tension_evaluated" in src, "分析器没看标记"
    assert "未评估" in src, "分析器没说「未评估」"


def test_the_reader_simulation_says_unevaluated():
    """⚠ 读者模拟的 ``tension_score`` 槽位：没评过 ⇒ 说未评估。"""
    import inspect

    from application.reader.services import reader_simulation_service as mod

    src = inspect.getsource(mod)
    assert "tension_evaluated" in src, "读者模拟没看标记"
    assert "未评估" in src, "读者模拟没说「未评估」"


def test_the_reader_simulation_context_formats_none_as_unevaluated():
    """⚠ 行为级：``tension_score=None`` ⇒ 槽位里是「未评估」，不是「0」。"""
    from application.reader.services.reader_simulation_service import (
        ReaderSimulationService,
    )

    parts = ReaderSimulationService._build_context(
        ReaderSimulationService,
        current_content="正文",
        current_outline="大纲",
        current_title="第一章",
        chapter_number=1,
        prev_summary="",
        prev_content="",
        next_exists=False,
        tension_score=None,
    )
    assert "未评估" in parts["tension_score"]
    assert "0" not in parts["tension_score"]
