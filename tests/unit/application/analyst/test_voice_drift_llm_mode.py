"""F-04：文风漂移的 LLM 模式 + 可覆盖阈值。

登记表 F-04 的指控核实成立
--------------------------
`VoiceDriftService.__init__` 的 ``use_llm_mode`` 默认 ``False``
（voice_drift_service.py:51），而生产唯一装配点
（``interfaces/api/dependencies.py::get_voice_drift_service``）**既不传
``use_llm_mode`` 也不传 ``llm_voice_service``** ⇒ LLM 模式从未启用过，
文风漂移一直只跑统计模式。

同时 ``DRIFT_ALERT_THRESHOLD = 0.75`` 是模块级常量，无任何运行时覆盖入口。

分派机制其实早就就位
--------------------
三处调用点（``daemon_host.py:1346``、``chapter_aftermath_pipeline.py:314``、
``engine/pipeline/base.py:1193``）都已经在检查 ``use_llm_mode`` 并分派到
``score_chapter_async`` / ``score_chapter``。缺的只是装配。

默认仍必须是统计模式
--------------------
LLM 模式每章多一次 LLM 调用（约 500 token），是**产品成本决策**不是缺陷修复，
所以默认关。守卫会钉住这一点 —— 防止「顺手改成 True」。
"""
import pytest

from application.analyst.services.llm_voice_analysis_service import (
    LLMVoiceAnalysisService,
)
from application.analyst.services.voice_analysis_settings import (
    VoiceAnalysisRuntimeSettings,
    get_voice_analysis_runtime_settings,
)
from application.analyst.services.voice_drift_service import (
    DRIFT_ALERT_THRESHOLD,
    VoiceDriftService,
)


class _ScoreRepo:
    def __init__(self):
        self.rows = []

    def upsert(self, **kw):
        self.rows.append(kw)

    def list_by_novel(self, novel_id, limit=50):
        return list(self.rows)[-limit:]


class _FingerprintRepo:
    def __init__(self, sample_count=0):
        self._n = sample_count

    def get_by_novel(self, novel_id, pov_character_id=None):
        if self._n <= 0:
            return None
        return {"sample_count": self._n,
                "adjective_density": 0.1, "avg_sentence_length": 20.0,
                "sentence_count": 100}


def _svc(**kw):
    kw.setdefault("runtime_settings", VoiceAnalysisRuntimeSettings())
    return VoiceDriftService(_ScoreRepo(), _FingerprintRepo(sample_count=50), **kw)


# ── 默认仍为统计模式 ────────────────────────────────────────────────────

def test_settings_default_to_statistics_mode():
    """默认必须关 —— 这是 token 成本决策，不能被顺手改成 True。"""
    s = VoiceAnalysisRuntimeSettings()
    assert s.use_llm_mode is False
    assert get_voice_analysis_runtime_settings().use_llm_mode is False


def test_default_constructed_service_is_statistics_mode():
    assert _svc().use_llm_mode is False


def test_module_default_threshold_unchanged():
    """模块常量保留 0.75 —— daemon_host 还直接 import 它。"""
    assert DRIFT_ALERT_THRESHOLD == 0.75


def test_default_threshold_matches_module_constant():
    """settings 的默认值必须与模块常量一致，不能各写一份。"""
    assert VoiceAnalysisRuntimeSettings().drift_alert_threshold == DRIFT_ALERT_THRESHOLD


# ── 开启后确实走 LLM 路径 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enabled_with_llm_service_dispatches_to_llm_path(monkeypatch):
    svc = _svc(llm_voice_service=object(), use_llm_mode=True)
    called = {"n": 0}

    async def _fake_llm(novel_id, chapter_number, content):
        called["n"] += 1
        return {"chapter_number": chapter_number, "similarity_score": 0.9,
                "drift_alert": False, "mode": "llm"}

    monkeypatch.setattr(svc, "_score_chapter_llm", _fake_llm)
    got = await svc.score_chapter_async("n1", 3, "正文")

    assert called["n"] == 1, "开启后没有走 LLM 路径"
    assert got["mode"] == "llm"


@pytest.mark.asyncio
async def test_use_llm_mode_without_service_stays_on_statistics(monkeypatch):
    """只开开关但没给 LLM 服务 ⇒ 必须退回统计模式，不得崩。"""
    svc = _svc(use_llm_mode=True)   # 没传 llm_voice_service
    monkeypatch.setattr(svc, "_score_chapter_llm",
                        lambda *a: pytest.fail("不应进入 LLM 路径"))
    got = await svc.score_chapter_async("n1", 3, "正文")
    assert got["mode"] == "statistics"


# ── 阈值可覆盖 ──────────────────────────────────────────────────────────

def test_threshold_can_be_overridden_via_settings():
    """F-04 的另一半：0.75 此前是死常量，现在有运行时入口。"""
    svc = _svc(runtime_settings=VoiceAnalysisRuntimeSettings(drift_alert_threshold=0.95))
    assert svc._runtime_settings.drift_alert_threshold == 0.95


@pytest.mark.asyncio
async def test_overridden_threshold_actually_changes_alert(monkeypatch):
    """阈值必须真的影响判定，不只是被读进来。"""
    svc = _svc(runtime_settings=VoiceAnalysisRuntimeSettings(drift_alert_threshold=0.95))
    svc.score_repo.rows = [
        {"chapter_number": i, "similarity_score": 0.8} for i in range(1, 7)
    ]
    # 相似度 0.8：低于 0.95 → 告警；高于 0.75 → 不告警
    assert svc._check_drift_alert("n1") is True

    svc2 = _svc(runtime_settings=VoiceAnalysisRuntimeSettings(drift_alert_threshold=0.70))
    svc2.score_repo.rows = [
        {"chapter_number": i, "similarity_score": 0.8} for i in range(1, 7)
    ]
    assert svc2._check_drift_alert("n1") is False


def test_reported_alert_threshold_reflects_the_setting():
    svc = _svc(runtime_settings=VoiceAnalysisRuntimeSettings(drift_alert_threshold=0.6))
    report = svc.get_drift_report("n1")
    assert report["alert_threshold"] == 0.6, "上报的阈值没有跟随设置"
    assert report["mode"] == "statistics"


# ── 装配点 ──────────────────────────────────────────────────────────────

def test_production_factory_wires_the_setting(monkeypatch):
    """生产装配必须把开关传下去，而不是又造一个默认实例。

    这条对着 ``interfaces/api/dependencies.py::get_voice_drift_service``。
    """
    import interfaces.api.dependencies as deps
    from infrastructure.persistence.database.sqlite_chapter_style_score_repository import (
        SqliteChapterStyleScoreRepository,
    )

    monkeypatch.setattr(deps, "get_database", lambda *a, **k: object())
    monkeypatch.setattr(
        SqliteChapterStyleScoreRepository, "__init__", lambda self, db: None)

    made = {}

    class _Recorder:
        def __init__(self, *args, **kwargs):
            made["args"] = args
            made["kwargs"] = kwargs

    monkeypatch.setattr(deps, "VoiceDriftService", _Recorder)

    # 关闭：只传 repos + settings
    monkeypatch.setattr(
        "application.analyst.services.voice_analysis_settings.get_voice_analysis_runtime_settings",
        lambda: VoiceAnalysisRuntimeSettings(use_llm_mode=False))
    deps.get_voice_drift_service()
    assert made["kwargs"].get("use_llm_mode") is None, "关闭时不应传 use_llm_mode"
    assert "llm_voice_service" not in made["kwargs"], "关闭时不应构造 LLM 服务"

    # 开启：必须传 llm_voice_service + use_llm_mode=True
    made.clear()
    monkeypatch.setattr(
        "application.analyst.services.voice_analysis_settings.get_voice_analysis_runtime_settings",
        lambda: VoiceAnalysisRuntimeSettings(use_llm_mode=True))
    monkeypatch.setattr(deps, "get_llm_service", lambda: object())
    deps.get_voice_drift_service()
    assert made["kwargs"].get("use_llm_mode") is True, (
        "开关打开却没传 use_llm_mode —— LLM 模式仍然不会生效"
    )
    assert made["kwargs"].get("llm_voice_service") is not None, (
        "开关打开却没注入 llm_voice_service —— 分派条件仍然不成立"
    )


def test_llm_voice_service_is_constructible_from_the_factory():
    """开启路径依赖的 LLMVoiceAnalysisService 必须能被真的构造出来。"""
    s = LLMVoiceAnalysisService(
        llm_service=object(),
        runtime_settings=VoiceAnalysisRuntimeSettings(use_llm_mode=True),
    )
    assert s is not None
