"""第 33 批：A1 设定漂移审计的「关思考」单次覆盖。

背景（生产实测，第 31/32 批登记）
--------------------------------
审计是**核对型**任务，不需要长思考；实测（同一份 prompt、生产同口径）：

* 思考照开：``394 秒`` / ``out=15709`` / 报出 **4** 条 issues
* 关思考：  ``36 秒``  / ``out=1206``  / 报出 **5** 条 issues（**检出没减**）

⇒ 值放 ``config/performance.yaml`` 的 ``audit.setting_drift.extra_body``
（**provider 特定 ⇒ 归配置**；换 provider 改配置即可）。

⚠ 守卫纪律：
* 配置**必须真被读到**（防「假来源」：配置里写了、代码没读）；
* **中间层不许剥掉**（orchestrator 的 ``_build_config`` 会重建 config）；
* 段缺失 ⇒ **零行为变化**。
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest
import yaml

from application.core.config.config_loader import reload_config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PERFORMANCE_YAML = REPO_ROOT / "config" / "performance.yaml"


# ── ① 配置段必须存在（且非空） ─────────────────────────────────────────


def test_the_performance_yaml_declares_the_setting_drift_extra_body():
    """⚠ 配置里必须有这一段（否则「关思考」根本不会生效）。"""
    data = yaml.safe_load(PERFORMANCE_YAML.read_text(encoding="utf-8"))

    section = (data.get("audit") or {}).get("setting_drift") or {}
    extra_body = section.get("extra_body")

    assert isinstance(extra_body, dict), "config/performance.yaml 缺 audit.setting_drift.extra_body 段"
    assert extra_body, "audit.setting_drift.extra_body 不能为空（空 ⇒ 等于没配）"


# ── ② 读取模块必须真读到配置 ───────────────────────────────────────────


def test_the_runtime_settings_read_the_configured_extra_body(tmp_path):
    """⚠ 读出来的值必须与配置**逐字相同**。"""
    from application.audit.services.setting_drift_settings import (
        get_setting_drift_runtime_settings,
    )

    config_path = tmp_path / "performance.yaml"
    config_path.write_text(
        """
audit:
  setting_drift:
    extra_body:
      thinking:
        type: disabled
      reasoning_effort: none
""",
        encoding="utf-8",
    )

    try:
        reload_config(str(config_path))
        settings = get_setting_drift_runtime_settings()

        assert settings.extra_body == {
            "thinking": {"type": "disabled"},
            "reasoning_effort": "none",
        }
    finally:
        reload_config()


def test_a_missing_section_means_no_override(tmp_path):
    """⚠ 段缺失 ⇒ ``None`` ⇒ **行为与改造前完全一致**（零行为变化）。"""
    from application.audit.services.setting_drift_settings import (
        get_setting_drift_runtime_settings,
    )

    config_path = tmp_path / "performance.yaml"
    config_path.write_text("autopilot:\n  poll_interval: 5\n", encoding="utf-8")

    try:
        reload_config(str(config_path))
        settings = get_setting_drift_runtime_settings()

        assert settings.extra_body is None
    finally:
        reload_config()


def test_a_broken_extra_body_means_no_override(tmp_path):
    """⚠ 配置写错（不是 dict / 是空的）⇒ 宁可**不覆盖**，也不能把审计改坏。"""
    from application.audit.services.setting_drift_settings import (
        get_setting_drift_runtime_settings,
    )

    config_path = tmp_path / "performance.yaml"
    config_path.write_text(
        "audit:\n  setting_drift:\n    extra_body: oops\n", encoding="utf-8"
    )

    try:
        reload_config(str(config_path))
        settings = get_setting_drift_runtime_settings()

        assert settings.extra_body is None
    finally:
        reload_config()


# ── ③ A1 的调用必须把覆盖带出去 ───────────────────────────────────────


class _RecordingHelper:
    """替身：只记录 A1 交给它的请求。"""

    def __init__(self) -> None:
        self.requests: list = []

    async def invoke_text(self, request):
        self.requests.append(request)
        return "{}"


@pytest.mark.asyncio
async def test_the_audit_sends_the_extra_body_with_the_call(monkeypatch):
    """⚠ A1 的调用 config 里必须带 ``extra_body``。"""
    import application.ai_invocation.autopilot.factory as factory_module
    from application.audit.services.setting_drift_audit import SettingDriftAuditService

    helper = _RecordingHelper()
    monkeypatch.setattr(
        factory_module, "get_or_create_autopilot_helper_invoker", lambda owner: helper
    )

    service = SettingDriftAuditService()
    await service._invoke({"novel_id": "n-1", "chapter_number": 1, "novel_title": "测试书"})

    assert helper.requests, "A1 没有发起调用"
    config = helper.requests[0].config
    assert config["extra_body"] == {"thinking": {"type": "disabled"}}
    # ⚠ 旧的两个参数不许被顺手改掉
    assert config["max_tokens"] == 2048
    assert config["temperature"] == 0.2


def test_the_extra_body_survives_the_orchestrator_config_build():
    """⚠ **中间层不许剥掉**（orchestrator 会重建 ``GenerationConfig``）。

    ⚠ 第 32 批实测就栽在这：重建时漏了 ``extra_body`` ⇒ 能力等于不存在。
    """
    from application.ai_invocation.autopilot.orchestrator import (
        AutopilotInvocationOrchestrator,
    )
    from application.audit.services.setting_drift_settings import (
        get_setting_drift_runtime_settings,
    )

    extra_body = get_setting_drift_runtime_settings().extra_body
    intent = SimpleNamespace(
        config={"max_tokens": 2048, "temperature": 0.2, "extra_body": extra_body}
    )

    built = AutopilotInvocationOrchestrator._build_config(intent)

    assert built is not None
    assert built.extra_body == {"thinking": {"type": "disabled"}}
