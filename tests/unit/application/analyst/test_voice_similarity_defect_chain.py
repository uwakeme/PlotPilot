# -*- coding: utf-8 -*-
"""第 86 批：文风相似度「不敏感」这条链上的**三条真缺陷**的守卫。

病根（生产实测 + 日志铁证）
--------------------------
用户报的现象：定向修文循环**分数纹丝不动**（连续 3 次都是 0.4167）⇒ 循环无法收敛
⇒ 后两次模型调用**纯烧钱**；而且**几乎每章**都被判成「严重偏离」。

第 85 批把它归因为「分数是离散分桶」——**那是错的**（截断只发生在**单维**上，
总分并不是 1/N 的整数倍）。第 86 批回到代码与生产数据，定位到**三条**独立缺陷：

┌─ N-16（**真凶**）：风格缓存键**不含内容身份**
│    缓存键是 ``f"{novel_id}:{chapter_number}"`` ⇒ 同一章**改写之后重评**，
│    仍然命中**改写前**那次分析的缓存 ⇒ 拿回来的风格向量一字未变
│    ⇒ 相似度当然一模一样。
│    日志铁证（logs/plotpilot.log:2993~3004）：第 8 章三次修文，
│    每次「修文后重评」都**没有新的模型请求**（只有修文本身那一次）。
│
├─ N-17：单维偏差被 ``min(1.0, rel_diff)`` **硬截断**
│    ⇒ 「差 1 个容差」与「差 5 个容差」在该维**完全同分**
│    ⇒ 已饱和的那一维**怎么改都没反应**。
│
└─ N-18：LLM 模式的「基准」与「当前值」**量纲错配**
     基准是从**统计口径**的指标推的（``adjective_density`` 当作 ``description_depth``、
     ``avg_sentence_length/50`` 当作 ``pacing``），而当前值是 LLM 的 0~1。
     ⇒ 生产实测：基准 description_depth = **0.0239** vs 当前值 ≈ 0.5
       ⇒ rel_diff = **4.761** ⇒ 该维**永久饱和**
     ⇒ 硬截断口径下相似度天花板恰好 **0.75**，而修文阈值是 **0.68**
     ⇒ **几乎每章都触发 3 轮定向修文** ⇒ 白烧钱。
     ⇒ 修法：基准必须用**同一把尺子**（本书自己的 LLM 风格向量）来建。

本文件钉住
----------
1. **N-16**：内容改了 ⇒ 必须**重算**；内容没改 ⇒ 必须**命中缓存**（不白花 token）。
2. **N-18**：基准只能来自**风格向量**；统计列的值**不许**进基准。
3. **N-18**：样本不够 ⇒ 相似度必须是 **None（未评估）**，不许拿错量纲的数字凑。
4. **N-18**：LLM 模式落库时，统计列必须保持**统计口径**（一列一义）。
5. **N-18**：风格向量必须落库（否则基准永远建不出来）。
6. **N-18**：基准回填**每本书只试一次**（否则每章重试 ⇒ 调用次数翻倍）。

⚠ N-17 的守卫在 ``test_drift_score_soft_saturation.py``（同一批新增）。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from application.analyst.services import llm_voice_analysis_service as llm_mod  # noqa: E402
from application.analyst.services.llm_voice_analysis_service import (  # noqa: E402
    STYLE_DIMENSIONS,
    LLMVoiceAnalysisService,
)
from application.analyst.services.voice_analysis_settings import (  # noqa: E402
    VoiceAnalysisRuntimeSettings,
)
from application.analyst.services.voice_drift_service import (  # noqa: E402
    LLM_BASELINE_MIN_SAMPLES,
    VoiceDriftService,
)


# ══════════════════════════════════════════════════════════════════════
# 1. N-16：缓存键必须含**内容身份**
# ══════════════════════════════════════════════════════════════════════


class _FakeGateway:
    """PromptGateway 替身（避免真连 DB）。"""

    def render(self, contract, variables):
        class _Rendered:
            prompt = "PROMPT"

        return _Rendered()


class _FakeLLM:
    """会记录调用次数的 LLM 替身。"""

    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    async def generate(self, prompt, config):
        self.calls += 1

        class _Result:
            pass

        result = _Result()
        result.content = self.content
        return result


def _style_json(description_depth: float = 0.4) -> str:
    payload = {dim: 0.5 for dim in STYLE_DIMENSIONS}
    payload["description_depth"] = description_depth
    return json.dumps(payload, ensure_ascii=False)


def _service_with_fake_llm(monkeypatch, llm) -> LLMVoiceAnalysisService:
    """把真服务里**依赖外部世界**的三处换掉，其余走真代码路径。"""
    monkeypatch.setattr(llm_mod, "get_prompt_gateway", lambda: _FakeGateway())
    monkeypatch.setattr(llm_mod, "generation_config_from_profile", lambda name: None)
    monkeypatch.setattr(llm_mod, "ensure_trace", lambda **kwargs: None)
    return LLMVoiceAnalysisService(
        llm_service=llm,
        runtime_settings=VoiceAnalysisRuntimeSettings(
            style_cache_ttl_seconds=300.0,
            style_cache_max_size=16,
        ),
    )


@pytest.mark.asyncio
async def test_a_rewritten_chapter_is_re_analyzed_not_served_from_cache(monkeypatch):
    """⚠ **N-16 核心**：内容改了 ⇒ 必须重新分析（否则分数永远不动）。

    ⚠ 这正是生产实测的那个洞：第 8 章连续 3 次定向修文，相似度**始终 0.4167**
    ⇒ 因为缓存键只有「书:章号」，重评拿回来的还是**改写前**的向量。
    """
    llm = _FakeLLM(_style_json())
    service = _service_with_fake_llm(monkeypatch, llm)

    await service.analyze_chapter_style("n1", 8, "第一版正文。")
    assert llm.calls == 1

    # 改写后的正文（内容变了）⇒ 必须**重新分析**
    await service.analyze_chapter_style("n1", 8, "改写之后的正文，完全不同的内容。")
    assert llm.calls == 2, (
        "内容都改了却没重新分析 ⇒ 命中了旧缓存 ⇒ 相似度不可能变"
    )


@pytest.mark.asyncio
async def test_an_unchanged_chapter_still_hits_the_cache(monkeypatch):
    """⚠ **反向**：内容没改 ⇒ 必须命中缓存（不许每章白花一次调用）。

    ⚠ 守卫的是「缓存」本身还在 ⇒ 只把键换成含内容的版本，
      不能把缓存整个废掉（那会让每章都多打一次模型）。
    """
    llm = _FakeLLM(_style_json())
    service = _service_with_fake_llm(monkeypatch, llm)

    await service.analyze_chapter_style("n1", 8, "同一份正文。")
    await service.analyze_chapter_style("n1", 8, "同一份正文。")

    assert llm.calls == 1, "同一份正文被分析了两次 ⇒ 缓存被废掉了"


@pytest.mark.asyncio
async def test_a_different_chapter_of_the_same_book_is_not_confused(monkeypatch):
    """⚠ 键里仍然要有**章号**：同一本书不同章不能互相命中。"""
    llm = _FakeLLM(_style_json())
    service = _service_with_fake_llm(monkeypatch, llm)

    await service.analyze_chapter_style("n1", 8, "正文。")
    await service.analyze_chapter_style("n1", 9, "正文。")

    assert llm.calls == 2, "不同章命中了同一份缓存"


def test_the_cache_key_mentions_the_content_fingerprint():
    """⚠ **结构级**：缓存键的**构造那一行**必须引用内容指纹。

    ⚠ 只查「文件里出现过这个词」是不够的（变异验证抓出来的洞）：
      把调用点改掉、函数留着 ⇒ 那种守卫照样绿。
    ⇒ 这里**定位到真正拼键的那一行**再断言。
    """
    source = (ROOT / "application" / "analyst" / "services" /
              "llm_voice_analysis_service.py").read_text(encoding="utf-8")
    key_lines = [ln.strip() for ln in source.splitlines() if "cache_key = " in ln]
    assert key_lines, "找不到缓存键的构造 ⇒ 守卫失效"
    assert any("content_fingerprint" in ln for ln in key_lines), (
        "拼缓存键的那一行没有引用内容指纹 ⇒ 改写后重评会命中旧缓存（N-16 回归）"
    )


# ══════════════════════════════════════════════════════════════════════
# 2. N-18：基准必须与当前值**同量纲**
# ══════════════════════════════════════════════════════════════════════


class _ScoreRepo:
    """记住每一行的风评仓储替身。"""

    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]
        self.upserts = []
        self.vector_upserts = []

    def upsert(self, **kw):
        self.upserts.append(dict(kw))
        for row in self.rows:
            if row.get("chapter_number") == kw.get("chapter_number"):
                row.update(kw)
                return "score-id"
        row = dict(kw)
        row.setdefault("style_vector_json", None)
        self.rows.append(row)
        return "score-id"

    def upsert_style_vector(self, novel_id, chapter_number, style_vector_json):
        self.vector_upserts.append((chapter_number, style_vector_json))
        for row in self.rows:
            if row.get("chapter_number") == chapter_number:
                row["style_vector_json"] = style_vector_json
                return
        self.rows.append(
            {
                "chapter_number": chapter_number,
                "style_vector_json": style_vector_json,
                "similarity_score": None,
            }
        )

    def list_by_novel(self, novel_id, limit=50):
        return [dict(r) for r in self.rows[:limit]]

    def list_recent_by_novel(self, novel_id, limit=50):
        return [dict(r) for r in self.rows[-limit:]]


class _FingerprintRepo:
    def get_by_novel(self, novel_id, pov_character_id=None):
        return None


class _FakeLLMVoiceService:
    """LLM 文风服务替身（含第 86 批新增的三个方法）。"""

    def __init__(self, description_depth: float = 0.9):
        self.description_depth = description_depth
        self.analyze_calls = 0

    async def analyze_chapter_style(self, novel_id, chapter_number, content):
        self.analyze_calls += 1
        vector = {dim: 0.5 for dim in STYLE_DIMENSIONS}
        vector["description_depth"] = self.description_depth
        return vector

    def compute_drift_score(self, style_vector, baseline):
        base = baseline.get("baseline", {})
        tol = baseline.get("tolerance", {})
        total = 0.0
        count = 0
        for dim in STYLE_DIMENSIONS:
            if dim not in base:
                continue
            rel = abs(style_vector.get(dim, 0.5) - base.get(dim, 0.5)) / max(
                tol.get(dim, 0.2), 1e-9
            )
            total += 1.0 - pow(2.718281828459045, -rel)
            count += 1
        if not count:
            return 1.0
        return round(max(0.0, 1.0 - total / count), 4)

    def build_baseline_from_vectors(self, style_vectors):
        baseline = {}
        tolerance = {}
        for dim in STYLE_DIMENSIONS:
            values = [v[dim] for v in style_vectors if dim in v]
            if values:
                avg = sum(values) / len(values)
                baseline[dim] = round(avg, 4)
                tolerance[dim] = 0.3
            else:
                baseline[dim] = 0.5
                tolerance[dim] = 0.3
        return {"baseline": baseline, "tolerance": tolerance}

    @staticmethod
    def vector_to_json(style_vector):
        clean = {k: v for k, v in style_vector.items() if not k.startswith("_")}
        return json.dumps(clean, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def vector_from_json(raw):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            k: float(v)
            for k, v in data.items()
            if not k.startswith("_") and isinstance(v, (int, float))
        }


def _vector_row(chapter_number: int, description_depth: float, **extra):
    row = {
        "chapter_number": chapter_number,
        "style_vector_json": json.dumps(
            {"description_depth": description_depth, "pacing": 0.5},
            ensure_ascii=False,
        ),
        "similarity_score": None,
    }
    row.update(extra)
    return row


def _drift_service(repo, llm, chapter_repo=None) -> VoiceDriftService:
    return VoiceDriftService(
        repo,
        _FingerprintRepo(),
        llm_voice_service=llm,
        use_llm_mode=True,
        runtime_settings=VoiceAnalysisRuntimeSettings(
            style_cache_ttl_seconds=300.0,
            style_cache_max_size=16,
            baseline_cache_ttl_seconds=600.0,
            baseline_cache_max_size=16,
        ),
        chapter_repo=chapter_repo,
    )


@pytest.mark.asyncio
async def test_the_baseline_comes_from_style_vectors_not_from_statistics():
    """⚠ **N-18 核心**：基准只能来自**风格向量**，统计列的值不许进基准。

    ⚠ 这里刻意把生产里的统计值（``adjective_density`` = 0.0239）放进统计列，
      同时给出风格向量（description_depth = 0.4）
      ⇒ 基准的 description_depth 必须是 **0.4**（向量口径），
        绝不能是 0.0239（统计口径）。
    """
    rows = [
        _vector_row(
            i,
            0.4,
            adjective_density=0.0239,       # ← 生产实测的统计口径值
            avg_sentence_length=15.13,
        )
        for i in range(1, LLM_BASELINE_MIN_SAMPLES + 1)
    ]
    repo = _ScoreRepo(rows)
    service = _drift_service(repo, _FakeLLMVoiceService())

    baseline = await service._get_or_init_baseline("n1")

    assert baseline is not None, "样本够却没建出基准"
    got = baseline["baseline"]["description_depth"]
    assert got == pytest.approx(0.4, abs=1e-6), (
        "基准的 description_depth = %s ⇒ 用的是**统计口径**（量纲错配，N-18 回归）"
        % got
    )
    assert got != pytest.approx(0.0239, abs=1e-3)


@pytest.mark.asyncio
async def test_without_enough_same_scale_samples_the_similarity_stays_unassessed():
    """⚠ 样本不够 ⇒ 相似度必须是 **None（未评估）**，不许拿错量纲的数字凑。

    ⚠ 「降级不许编」：没有同量纲的基准 ⇒ 这一章**没评过**。
    """
    rows = [
        {
            "chapter_number": i,
            "adjective_density": 0.0239,
            "avg_sentence_length": 15.13,
            "style_vector_json": None,        # ← 没有风格向量
            "similarity_score": None,
        }
        for i in range(1, LLM_BASELINE_MIN_SAMPLES + 1)
    ]
    repo = _ScoreRepo(rows)
    service = _drift_service(repo, _FakeLLMVoiceService())

    baseline = await service._get_or_init_baseline("n1")
    assert baseline is None, (
        "没有同量纲样本却建出了基准 ⇒ 那一定是拿统计口径凑的"
    )

    result = await service.score_chapter_async("n1", 8, "正文内容。")
    assert result["similarity_score"] is None, (
        "没有同量纲基准却给出了相似度 %s ⇒ 编数了"
        % result["similarity_score"]
    )


@pytest.mark.asyncio
async def test_llm_mode_keeps_the_statistics_columns_in_statistics_terms():
    """⚠ **一列一义**：LLM 模式落库时，统计列必须写**统计口径**的值。

    ⚠ 生产实测的病灶：ch1~7 的 ``adjective_density`` 是统计值 0.0245，
      ch8+ 却是 LLM 派生值 0.6 ⇒ 同一列**两套量纲** ⇒ 谁读都说不清。
    """
    repo = _ScoreRepo(
        [_vector_row(i, 0.4) for i in range(1, LLM_BASELINE_MIN_SAMPLES + 1)]
    )
    service = _drift_service(repo, _FakeLLMVoiceService(description_depth=0.9))

    await service.score_chapter_async("n1", 8, "他嘴角勾起一丝冷笑，心中满是警惕。")

    assert repo.upserts, "LLM 模式没有落库"
    stored = repo.upserts[-1]
    assert stored["adjective_density"] != pytest.approx(0.9, abs=1e-6), (
        "统计列里写的是 LLM 的 description_depth ⇒ 一列两义（N-18 回归）"
    )
    assert stored["adjective_density"] < 0.2, (
        "统计列的值 %s 不像形容词密度（0~0.1 量纲）"
        % stored["adjective_density"]
    )


@pytest.mark.asyncio
async def test_the_style_vector_is_persisted_so_the_baseline_can_be_built():
    """⚠ 风格向量必须落库 ⇒ 否则基准永远建不出来（永远「未评估」）。"""
    repo = _ScoreRepo(
        [_vector_row(i, 0.4) for i in range(1, LLM_BASELINE_MIN_SAMPLES + 1)]
    )
    service = _drift_service(repo, _FakeLLMVoiceService())

    await service.score_chapter_async("n1", 8, "正文内容。")

    stored = repo.upserts[-1].get("style_vector_json")
    assert stored, "风格向量没落库 ⇒ 基准永远建不出来"
    vector = _FakeLLMVoiceService.vector_from_json(stored)
    assert "description_depth" in vector, "落库的风格向量是空的"


class _FakeChapter:
    def __init__(self, number: int, content: str):
        self.number = number
        self.content = content


class _FakeChapterRepo:
    def __init__(self, chapters):
        self.chapters = chapters
        self.calls = 0

    def list_by_novel(self, novel_id, limit=None):
        self.calls += 1
        return self.chapters[:limit] if limit else list(self.chapters)


@pytest.mark.asyncio
async def test_the_baseline_backfill_is_attempted_only_once_per_novel():
    """⚠ **不许每章重试**：回填要调模型，每章重试 ⇒ 调用次数翻好几倍。

    ⚠ 这正是本批要治的「白烧钱」：回填失败时若不记，每章都会重试一次。

    ⚠ ⚠ 用例设计要点（变异验证抓出来的洞）：**必须造一个「回填拿不到东西」的场景**
      （章节没有正文 ⇒ 分析不出向量 ⇒ 基准建不出来 ⇒ **不会被缓存**）。
      否则基准一旦建成，第二次调用走**缓存**就短路了 ⇒
      「重试」这条路径根本走不到 ⇒ 守卫会**假绿**（变异 M06 改绿了才发现）。
    """
    # 章节**没有正文** ⇒ 回填分析不出东西 ⇒ 基准永远是 None（不缓存）
    chapters = [_FakeChapter(i, "") for i in range(1, 6)]
    chapter_repo = _FakeChapterRepo(chapters)
    repo = _ScoreRepo([])
    llm = _FakeLLMVoiceService()
    service = _drift_service(repo, llm, chapter_repo=chapter_repo)

    await service.score_chapter_async("n1", 8, "正文内容。")
    calls_after_first = chapter_repo.calls
    await service.score_chapter_async("n1", 9, "正文内容。")

    assert calls_after_first == 1, "第一次就该只回填一次"
    assert chapter_repo.calls == 1, (
        "回填被重试了 %d 次 ⇒ 每章都要调模型 ⇒ 白烧钱" % chapter_repo.calls
    )


@pytest.mark.asyncio
async def test_the_backfill_fills_the_baseline_when_the_chapters_have_content():
    """⚠ 回填**确实能补出基准**（不是空转）。

    ⚠ 场景：「早于 LLM 模式开启就写完」的书 ⇒ 那些章没有风格向量
    ⇒ 回填分析它们 ⇒ 基准建得出来 ⇒ 漂移检测重新有保护。
    """
    chapters = [_FakeChapter(i, "第%d章正文内容。" % i) for i in range(1, 6)]
    chapter_repo = _FakeChapterRepo(chapters)
    repo = _ScoreRepo([])
    llm = _FakeLLMVoiceService()
    service = _drift_service(repo, llm, chapter_repo=chapter_repo)

    result = await service.score_chapter_async("n1", 8, "正文内容。")

    assert llm.analyze_calls == LLM_BASELINE_MIN_SAMPLES + 1, (
        "回填没有逐章分析（调用 %d 次，期望 %d = 回填 %d + 当前章 1）⇒ 基准补不出来"
        % (llm.analyze_calls, LLM_BASELINE_MIN_SAMPLES + 1, LLM_BASELINE_MIN_SAMPLES)
    )
    assert len(repo.vector_upserts) == LLM_BASELINE_MIN_SAMPLES, (
        "回填分析出来的风格向量没有落库 ⇒ 下次还得重来"
    )
    assert result["similarity_score"] is not None, (
        "基准补齐了却仍然记「未评估」⇒ 回填白做了"
    )
