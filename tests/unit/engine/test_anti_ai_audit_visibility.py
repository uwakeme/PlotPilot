"""反 AI 审计结论可见性（登记表 F-11）。

缺陷背景：`DaemonHostMixin._run_anti_ai_audit` 在 ``overall_assessment`` 为
「中等/严重」时**只打一条 warning**，无状态变更、无可观测标记、无任何动作。
结论除了翻日志之外无处可见 —— 「AI味过重」既不会被运维看见，也不会出现在
/status，等于默认无动作。

本组用例钉死两条红线：

1. 结论必须经既有共享状态通道送出，且**每个键都必须进
   ``query_service._RUNTIME_STATUS_KEYS`` 白名单**（本批已踩两次的坑）；
2. 反 AI 评估**不得**获得任何自动干预权：不改写正文、不重试、不阻断。

全部为行为测试：不读源码，不断言「某段源码字符串不存在」。
"""
from __future__ import annotations

import logging

import pytest

from application.audit.services.anti_ai_audit import AntiAIAuditReport, AntiAIMetrics
from application.engine.services import query_service
from application.engine.services.query_service import _merge_runtime_fields_from_raw
from engine.runtime.daemon_host import DaemonHostMixin


def _report(
    assessment: str = "严重",
    *,
    score: float = 42.0,
    total: int = 9,
    critical: int = 3,
    recs=("紧急：发现 3 处严重AI味模式", "少用排比句式"),
) -> AntiAIAuditReport:
    """按真实 dataclass 形状构造报告（AntiAIAuditReport / AntiAIMetrics）。"""
    return AntiAIAuditReport(
        chapter_id="ch-7",
        metrics=AntiAIMetrics(
            chapter_id="ch-7",
            total_hits=total,
            critical_hits=critical,
            severity_score=score,
            overall_assessment=assessment,
        ),
        recommendations=list(recs),
    )


class _ForbiddenIntervention(BaseException):
    """反 AI 评估触碰了未预期的宿主方法。

    刻意继承 ``BaseException`` 而非 ``Exception``：``_run_anti_ai_audit``
    自带一个宽泛的 ``except Exception``，用 ``Exception`` 子类会被它当场吞掉
    —— 变异验证 F 实测确认，用 AssertionError 的初版守卫就是这样形同虚设：
    违规调用被静默吞下，调用序列断言根本没看见。所以这里既记录又抛出，
    并且抛的东西必须能穿过那个 except。
    """


class _Host(DaemonHostMixin):
    """只实现 ``_run_anti_ai_audit`` 真正需要的两个宿主方法。

    ``__getattr__`` 是本组用例的核心守卫：任何**新增**的宿主方法调用
    （改写正文、重试、阻断之类）都会先被记进 ``calls``、再抛出
    ``_ForbiddenIntervention``。魔术替身不会守这条。

    ``llm_service`` 显式声明为 ``None``：真实 daemon 由
    ``init_daemon_dependencies``（daemon_host.py:79）挂上它，
    供 F-06 的 AI 模式归纳使用。本组用例不关心归纳，
    声明「本替身没有 LLM 服务」即可让归纳短路 ——
    顺带避免 ``__getattr__`` 把这个**合法**属性当成「未预期的干预」。
    """

    llm_service = None

    def __init__(self, report: AntiAIAuditReport):
        self.calls: list = []
        self._report = report

    def _sync_anti_ai_audit(self, novel_id, chapter_number, content):
        self.calls.append(("sync", novel_id, chapter_number))
        return self._report

    def _update_shared_state(self, novel_id, **fields):
        # 与真实实现一致（daemon_host.py:1139）注入 novel_id
        fields["novel_id"] = novel_id
        self.calls.append(("shared", novel_id, fields))

    def __getattr__(self, name):
        # dunder 交回 AttributeError，避免干扰 pytest / copy / repr 的协议探测
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        self.calls.append(("forbidden", name, {}))
        raise _ForbiddenIntervention(
            f"_run_anti_ai_audit 触碰了未预期的宿主方法 {name!r}："
            "反 AI 评估只允许被看见，不允许自动干预"
        )

    @property
    def shared_fields(self) -> dict:
        for kind, _novel_id, fields in self.calls:
            if kind == "shared":
                return fields
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("assessment", ["中等", "严重"])
async def test_heavy_assessment_is_pushed_to_shared_state(assessment):
    """「中等/严重」必须经共享状态通道送出结论，不能只留在日志里。"""
    host = _Host(_report(assessment))

    returned = await host._run_anti_ai_audit("n1", 7, "正文内容")

    assert returned is host._report, "报告必须原样返回给章末闸门"
    fields = host.shared_fields
    assert fields, "评估结论必须送出，否则它在 /status 上不可见"
    assert fields["anti_ai_assessment"] == assessment
    assert fields["anti_ai_severity_score"] == 42.0
    assert fields["anti_ai_total_hits"] == 9
    assert fields["anti_ai_critical_hits"] == 3
    assert fields["anti_ai_chapter_number"] == 7
    assert fields["anti_ai_recommendations"], "建议摘要必须一起送出，否则结论不可行动"


@pytest.mark.asyncio
async def test_clean_assessment_writes_nothing():
    """健康章节不刷状态 —— 否则状态位失去了「有问题」的含义。"""
    host = _Host(_report("纯净", score=0.2, total=0, critical=0, recs=()))

    await host._run_anti_ai_audit("n1", 8, "正文内容")

    assert host.shared_fields == {}


@pytest.mark.asyncio
async def test_audit_grants_no_automatic_intervention():
    """反向守卫：严重结论不得触发任何正文改写 / 重试 / 阻断。

    宿主只允许被触碰 ``_sync_anti_ai_audit`` 与 ``_update_shared_state``；
    任何新增调用会在 ``_Host.__getattr__`` 里直接变成 AssertionError。
    依据 Task 12 的 F-01 裁定：护栏分数只负责被看见，不决定生死。
    """
    host = _Host(_report("严重"))

    await host._run_anti_ai_audit("n1", 7, "正文内容")

    assert [kind for kind, *_ in host.calls] == ["sync", "shared"], (
        "「严重」只允许可见性动作，不得新增任何宿主调用"
    )


@pytest.mark.asyncio
async def test_every_surfaced_key_is_whitelisted_for_status():
    """F-11 的老坑：新增键不补白名单 = 键被 /status 滤掉 = 等于没做。

    这里动态取**实际送出**的键，而不是硬编码一份清单：日后有人再加一个
    anti_ai_* 键却忘了补白名单，本用例会立刻转红。
    """
    host = _Host(_report("严重"))
    await host._run_anti_ai_audit("n1", 7, "正文内容")

    keys = set(host.shared_fields) - {"novel_id"}
    assert keys, "一条键都没送出时本用例会空转，先钉住「确实送出了键」"
    missing = keys - set(query_service._RUNTIME_STATUS_KEYS)
    assert not missing, (
        f"未进 _RUNTIME_STATUS_KEYS 白名单，/status 读不到: {sorted(missing)}"
    )


def test_status_payload_actually_exposes_anti_ai_keys():
    """白名单补了还不够 —— 走真实合并逻辑确认 /status 真能透出这些键。"""
    raw = {
        "anti_ai_assessment": "严重",
        "anti_ai_severity_score": 42.0,
        "anti_ai_total_hits": 9,
        "anti_ai_critical_hits": 3,
        "anti_ai_recommendations": ["紧急：发现 3 处严重AI味模式"],
        "anti_ai_chapter_number": 7,
    }

    payload = _merge_runtime_fields_from_raw({"novel_id": "n1"}, raw)

    for key, value in raw.items():
        assert payload.get(key) == value, f"{key} 没被 /status 透出"


@pytest.mark.asyncio
async def test_audit_failure_still_returns_none(caplog):
    """审计本身失败仍是「失败信号」，不得被本次可见性改造改掉。

    docstring 已把 None 定义为失败信号，返回值语义本次不动（见报告的
    范围外发现一节）。本用例是把它钉住，防止顺手改成返回假报告。
    """
    host = _Host(_report("严重"))
    host._sync_anti_ai_audit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scanner down"))

    with caplog.at_level(logging.WARNING, logger="engine.runtime.daemon_host"):
        result = await host._run_anti_ai_audit("n1", 7, "正文内容")

    assert result is None, "审计失败必须返回 None（失败信号），不得伪造报告"
    assert host.shared_fields == {}, "审计失败时不得送出任何评估结论"
    warns = [
        r for r in caplog.records
        if r.name == "engine.runtime.daemon_host" and r.levelno == logging.WARNING
    ]
    assert warns, "审计失败必须留 warning 痕迹"
