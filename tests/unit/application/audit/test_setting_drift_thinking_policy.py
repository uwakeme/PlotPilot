"""第 35 批：A1 设定漂移审计**按画像开思考**，不特判关掉（用户 2026-10-28 决定）。

⚠ 背景
------
第 33 批给 A1 加过「关思考」的单次覆盖（``thinking: {type: disabled}``）。
用户判断：「**大多数时候都是要开思考的**」⇒ 那条特判**没必要** ⇒ 撤掉。

⇒ 但**能力保留**：``GenerationConfig.extra_body``（第 32 批的**单次覆盖**）
仍是 provider 无关、默认零行为的通用口子
（第 34 批的账 A / 账 B 修复也靠它）⇒ **撤掉的是用法，不是能力**。

⚠ 守卫纪律：把「用户拍板的默认值」钉成守卫 ⇒ 以后谁想改 A1 的思考策略，
  会**当场红** ⇒ 逼他同步更新登记表（而不是悄悄改掉）。
"""
from __future__ import annotations

import pathlib

import yaml

from application.audit.services.setting_drift_audit import SettingDriftAuditService

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PERFORMANCE_YAML = REPO_ROOT / "config" / "performance.yaml"


class _RecordingHelper:
    """替身：只记录 A1 交给它的请求。"""

    def __init__(self) -> None:
        self.requests: list = []

    async def invoke_text(self, request):
        self.requests.append(request)
        return "{}"


async def _capture_audit_config(monkeypatch) -> dict:
    import application.ai_invocation.autopilot.factory as factory_module

    helper = _RecordingHelper()
    monkeypatch.setattr(
        factory_module, "get_or_create_autopilot_helper_invoker", lambda owner: helper
    )

    service = SettingDriftAuditService()
    await service._invoke({"novel_id": "n-1", "chapter_number": 1, "novel_title": "测试书"})

    assert helper.requests, "A1 没有发起调用"
    return dict(helper.requests[0].config)


async def test_the_audit_does_not_override_the_profile_thinking(monkeypatch):
    """⚠ A1 的调用**不带**单次 provider 覆盖 ⇒ 审计**按激活画像开思考**。

    ⚠ 用户 2026-10-28 决定：「大多数时候都是要开思考的」⇒ 那条特判没必要。
    ⚠ 要改的话请连同登记表一起改（别悄悄改掉）。
    """
    config = await _capture_audit_config(monkeypatch)

    assert "extra_body" not in config, (
        "A1 不该带单次 provider 覆盖（用户已决定审计按画像开思考）"
    )
    # ⚠ 旧的两个参数不许被顺手改掉
    assert config["max_tokens"] == 2048
    assert config["temperature"] == 0.2


def test_the_performance_yaml_has_no_leftover_setting_drift_section():
    """⚠ 撤掉开关时**不许留下僵尸配置**（仓库先例：``timeout_seconds``/``retries``
    就是这么删的 —— 留着只会让人以为「调小它就能限」）。

    ⚠ 仓库里已经吃过一次亏：``config/generation_profiles.yaml`` 里
    **22/38 个画像的 ``max_tokens`` 是死配置**（被 domain 层的下限顶成 120000）。
    """
    data = yaml.safe_load(PERFORMANCE_YAML.read_text(encoding="utf-8"))

    section = (data.get("audit") or {}).get("setting_drift")
    assert not section, "config/performance.yaml 还留着 audit.setting_drift 段（僵尸配置）"
