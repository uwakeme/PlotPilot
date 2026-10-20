"""F-13 + F-14：审计的基准与评分不许编造。

F-13 —— 设定漂移审计的 FactLock 基准恒空，且与生成侧不同源
-----------------------------------------------------------
旧实现 ``SettingDriftAuditService._load_fact_locks`` 用
``MemoryEngine.__new__`` 绕过 ``__init__`` 去翻会话缓存：``_cache`` 是在
``__init__`` 里建的，``__new__`` 绕过后 ``getattr(engine, "_cache", None)``
得 None ⇒ **恒返回 []**；而且 ``MemoryState`` 压根没有 ``fact_locks`` 属性
（类定义里只有 completed_beats / revealed_clues / fact_violations_history）
⇒ 两处都落空。

更要紧的是口径：生成侧锁进 T0 的 FACT_LOCK 是 ``FactLockBuilder`` 从 Bible
推出的**角色白名单 / 死亡锁 / 关系 / 身份锁 / 时间线**；审计侧却拿
world_settings / locations 比对 ⇒ **两侧基准不同源**，
「正文是否违反生成时被锁住的事实」这个问题就问错了。

现在审计侧直接用同一个 ``FactLockBuilder`` 出清单。

F-14 —— 审稿的分数与总结不许编造
---------------------------------
活的路径是 ``ChapterAIReviewService``（章节页的 AI 审阅）：

- 模型没给 score ⇒ 旧实现塞 ``70``（一个看起来正常的中位分，会写进 memo
  「综合评分：70」，随 memo 落进 chapter_reviews）；
- 模型没给 summary ⇒ 旧实现在代码里编一句「本章整体可通过，综合评分 85。」。

两处都是「代码说的话被标成模型说的」。prompt 的 JSON 契约本来就要求 score
与 summary，缺它是模型违约；同一方法里「缺 suggestions 就 raise」已是这个范式。

（登记表 F-14 点名的「代码公式」在 ``ChapterReviewService`` 那条**前端零调用**
的路径上；活路径的分数本来就是 LLM 给的。）

全部是行为断言：不碰源码文本。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from application.audit.services.chapter_ai_review_service import (
    ChapterAIReviewContractError,
    ChapterAIReviewService,
)
from application.audit.services.setting_drift_audit import SettingDriftAuditService
from application.engine.services.memory_engine import FactLockBuilder
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage


# ── 夹具 ──────────────────────────────────────────────────────────


def _character(
    name,
    *,
    description="",
    relationships=None,
    public_profile="",
    hidden_profile="",
    reveal_chapter=None,
    mental_state="",
):
    return SimpleNamespace(
        name=name,
        description=description,
        relationships=list(relationships or []),
        public_profile=public_profile,
        hidden_profile=hidden_profile,
        reveal_chapter=reveal_chapter,
        mental_state=mental_state,
    )


def _bible():
    return SimpleNamespace(
        characters=[
            _character("陆深", description="被灰引书覆写的宿主", public_profile="档案馆职员"),
            _character("周队", description="已于雨夜牺牲", public_profile="刑侦支队队长"),
            _character(
                "宋扬",
                description="刑侦画像师",
                relationships=[{"target": "陆深", "type": "对手"}],
            ),
        ],
        world_settings=[SimpleNamespace(category="力量体系", content="修为分九境")],
        locations=[SimpleNamespace(name="临渊城", description="北境边城", location_type="city")],
        timeline_notes=[SimpleNamespace(timestamp="第1章", event="档案馆失窃")],
    )


class _BibleRepo:
    def __init__(self, bible=None, raises=False):
        self._bible = bible
        self._raises = raises

    def get_by_novel_id(self, novel_id):
        if self._raises:
            raise RuntimeError("db down")
        return self._bible


def _audit_service(bible=None, raises=False):
    return SettingDriftAuditService(
        llm_service=MagicMock(),
        bible_repository=_BibleRepo(bible, raises),
    )


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def generate(self, prompt, config):
        self.calls.append((prompt, config))
        return GenerationResult(
            json.dumps(self.payload, ensure_ascii=False),
            TokenUsage(input_tokens=1, output_tokens=1),
        )


# ── F-13：审计基准与生成侧同源 ─────────────────────────────────────


def test_fact_lock_baseline_is_no_longer_empty():
    """旧实现恒返回 [] —— 审计的 FactLock 基准一直是空的。"""
    svc = _audit_service(_bible())

    locks = svc._load_fact_locks("n-1", 3)

    assert locks, "FactLock 基准恒空 —— 审计比的不是生成时被锁住的那份事实"


def test_audit_baseline_contains_the_character_whitelist():
    """生成侧锁住「只可使用这些角色」，审计侧必须也拿得到这份白名单。"""
    svc = _audit_service(_bible())

    facts = svc._collect_setting_facts("n-1", 3)

    assert any("陆深" in f and "白名单" in f for f in facts), facts


def test_audit_baseline_contains_the_dead_character_lock():
    """死亡锁（不可复活）必须进审计基准。"""
    svc = _audit_service(_bible())

    facts = svc._collect_setting_facts("n-1", 3)

    assert any("周队" in f and "死亡锁" in f for f in facts), facts


def test_audit_baseline_comes_from_the_same_source_as_the_t0_fact_lock():
    """审计基准与生成侧 T0 FACT_LOCK **同源**。

    旧实现审计侧比 world_settings/locations，而生成侧锁的是
    characters/timeline/relationships —— 口径对不上，「是否违反被锁住的事实」
    这个问题就问错了。
    """
    repo = _BibleRepo(_bible())
    svc = SettingDriftAuditService(llm_service=MagicMock(), bible_repository=repo)

    audit_facts = svc._collect_setting_facts("n-1", 3)
    t0_text = FactLockBuilder(repo).build("n-1", 3)

    # 审计基准里出现的每个角色名，都必须出现在生成侧锁给模型的那份文本里
    for name in ("陆深", "周队", "宋扬"):
        assert any(name in f for f in audit_facts), f"{name} 不在审计基准里"
        assert name in t0_text, f"{name} 不在生成侧 FACT_LOCK 里"


def test_audit_baseline_survives_a_bible_repository_that_raises():
    """仓库读失败 ⇒ 基准为空且不炸（不能把整章审稿带崩）。"""
    svc = _audit_service(None, raises=True)

    assert svc._load_fact_locks("n-1", 3) == []


def test_chapter_number_reaches_the_fact_lock_baseline():
    """章节号要传进 FactLock —— 身份锁里 hidden_profile 的可见性依赖它。"""
    repo = _BibleRepo(_bible())
    svc = SettingDriftAuditService(llm_service=MagicMock(), bible_repository=repo)

    seen = {}
    original = FactLockBuilder.build_facts

    def _spy(self, novel_id, current_chapter=0):
        seen["chapter"] = current_chapter
        return original(self, novel_id, current_chapter)

    FactLockBuilder.build_facts = _spy
    try:
        svc._collect_setting_facts("n-1", 7)
    finally:
        FactLockBuilder.build_facts = original

    assert seen.get("chapter") == 7


# ── F-14：分数与总结不许编造 ───────────────────────────────────────


async def _review(payload):
    service = ChapterAIReviewService(_FakeLLM(payload))
    return await service.review(
        chapter_number=3,
        chapter_title="雨夜追问",
        chapter_content="雨声压在窗沿。沈岚把证据推到灯下。",
    )


async def test_missing_score_fails_instead_of_fabricating_seventy():
    """模型没给 score ⇒ 契约失败。旧实现塞 70（编造的中位分）。"""
    with pytest.raises(ChapterAIReviewContractError, match="score"):
        await _review(
            {
                "status": "approved",
                "summary": "推进完整。",
                "issues": [],
                "suggestions": ["微调个别长句。"],
            }
        )


async def test_non_numeric_score_fails_instead_of_fabricating_seventy():
    """score 给了但不是数值 ⇒ 同样是违约，不塞 70。"""
    with pytest.raises(ChapterAIReviewContractError, match="score"):
        await _review(
            {
                "status": "approved",
                "score": "还不错",
                "summary": "推进完整。",
                "issues": [],
                "suggestions": ["微调个别长句。"],
            }
        )


async def test_missing_summary_fails_instead_of_code_written_summary():
    """模型没给总结 ⇒ 契约失败。

    旧实现在代码里编一句「本章整体可通过，综合评分 85。」——
    那是**代码替编辑下的判断**，且会随 memo 落库。
    """
    with pytest.raises(ChapterAIReviewContractError, match="summary"):
        await _review(
            {
                "status": "approved",
                "score": 85,
                "issues": [],
                "suggestions": ["微调个别长句。"],
            }
        )


async def test_model_score_and_summary_are_preserved():
    """对照组：模型给了就原样保留（别把修复做成「一律不给分」）。"""
    result = await _review(
        {
            "status": "approved",
            "score": 91,
            "summary": "推进完整，人物行动可信。",
            "issues": [],
            "suggestions": ["保留当前冲突链，只微调个别长句。"],
        }
    )

    assert result.score == 91
    assert result.summary == "推进完整，人物行动可信。"
    assert "综合评分：91" in result.memo


async def test_score_is_clamped_into_the_zero_to_hundred_range():
    """区间仍是 0-100（登记表 F-14 的不变量），只是来源必须是模型。"""
    result = await _review(
        {
            "status": "reviewed",
            "score": 140,
            "summary": "略超，需要收一收。",
            "issues": [],
            "suggestions": ["收紧结尾。"],
        }
    )

    assert result.score == 100