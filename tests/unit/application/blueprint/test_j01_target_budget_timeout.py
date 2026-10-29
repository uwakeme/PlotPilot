"""J-01：目标预算提案的**硬超时**（第 49 批）。

背景（读码核实）
----------------
``config/generation_profiles.yaml`` 里原先给 ``target_budget_proposal`` 写着
``timeout_seconds: 30`` / ``retries: 0``，看起来「超时会短」——
但 ``generation_config_from_profile`` **只读** model / max_tokens / temperature /
response_format ⇒ **那两个键全仓无人读**（装饰品）。
（键已删，但**闸**一直没补上。）

真实上限在 **provider 层**（``LLM_MAX_TOTAL_ATTEMPTS`` × ``Settings.timeout_seconds``
⇒ 最坏 ~900s）⇒ 用户点「继续写」可能被一个慢模型卡住十几分钟。

⇒ 本批照 J-02（``style_preset_match``）的同款做法，在**服务自己**里加一道
``asyncio.wait_for`` 硬闸：超时 ⇒ 回落**引擎默认公式**（``source=fallback``），
**不编造**提案。

本文件钉住
----------
1. **超时真被闸住**（不是「值配了就算」）：打桩模型睡 5s、服务闸 0.05s ⇒
   服务必须**很快**返回（没有闸 ⇒ 外层 wait_for 先炸 ⇒ 守卫红）。
2. 超时后**回落公式**（不编造、不透传异常）。
3. 超时的值**有人守**（YAML / 常量 / 解析值三处一致）。
4. 超时的值**真的来自配置**（改配置 ⇒ 解析值跟着变）——
   ⚠ 这一条不能省：默认值恰好也是 30 ⇒ 不改配置就**分不出**
   「来自配置」与「来自默认值」。
5. 真实配置**没关掉**这个能力（否则上面的守卫会**因错误的原因而绿**）。
6. **同族守卫**：画像里**不许**再出现装饰性的 ``timeout_seconds`` / ``retries`` 键
   （留着只会让人以为「调小它就能限」——上一份审查简报就信过同族的键）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from application.blueprint.services.target_budget import (
    propose_continue_block,
)
from application.blueprint.services.target_budget_proposal import (
    DEFAULT_TIMEOUT_SECONDS,
    TargetBudgetProposalService,
    resolve_proposal_timeout_seconds,
)
from application.blueprint.services.target_budget_schema import SOURCE_FALLBACK

ROOT = Path(__file__).resolve().parents[4]
PERFORMANCE_YAML = ROOT / "config" / "performance.yaml"
PROFILES_YAML = ROOT / "config" / "generation_profiles.yaml"


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

    def __init__(self, captured: dict):
        self._captured = captured

    def render(self, contract, variables):
        self._captured["variables"] = dict(variables)
        self._captured["node_key"] = contract.node_key
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"),
            node_key=contract.node_key,
        )

    def validate_output(self, contract, payload):
        return contract.output_schema.model_validate(payload)


class _SlowLLM:
    """睡很久的模型 —— 用来证明「服务自己」有闸。"""

    def __init__(self, delay: float):
        self._delay = delay
        self.calls = 0

    async def generate(self, prompt, config):
        self.calls += 1
        await asyncio.sleep(self._delay)
        return SimpleNamespace(content=json.dumps({}))


def _service(monkeypatch, llm) -> TargetBudgetProposalService:
    import application.blueprint.services.target_budget_proposal as mod

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway({}))
    return TargetBudgetProposalService(llm)


# ─────────────────────── 1) 超时真被闸住 ───────────────────────


@pytest.mark.asyncio
async def test_a_slow_model_is_cut_off_and_falls_back(monkeypatch):
    """模型睡 5s、服务闸 0.05s ⇒ 服务必须**很快**回落公式。

    ⚠ 外层再套一个 ``wait_for(2s)``：**没有服务自己的闸**时，
    这里会先炸 TimeoutError ⇒ 守卫红（而不是「慢一点也过」）。
    """
    import application.blueprint.services.target_budget_proposal as mod

    monkeypatch.setattr(mod, "resolve_proposal_timeout_seconds", lambda: 0.05)
    llm = _SlowLLM(delay=5.0)
    svc = _service(monkeypatch, llm)

    result = await asyncio.wait_for(
        svc.propose(
            novel_id="n-1",
            target_chapters=100,
            completed_chapters=100,
            current_finale_budget=12,
        ),
        timeout=2.0,
    )

    assert llm.calls == 1, "模型压根没被调用 ⇒ 这条守卫是因错误的原因而绿"
    assert result["source"] == SOURCE_FALLBACK, "超时后不许冒充 AI 提案"
    assert result["continue_block"] == propose_continue_block(
        target_chapters=100, completed_chapters=100
    ), "超时后必须回落引擎默认公式（不编造）"
    assert result["finale_budget"] == 12


@pytest.mark.asyncio
async def test_the_timeout_gate_is_really_in_the_service(monkeypatch):
    """形状守卫：服务里必须真有 ``asyncio.wait_for`` 包住模型调用。

    ⚠ 「配置里写了 30」**不等于**「服务会超时放弃」——
    键曾经就是那样摆设的（``generation_profiles.yaml`` 的装饰品）。
    """
    import ast

    import application.blueprint.services.target_budget_proposal as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    wait_for_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_for"
    ]
    assert wait_for_calls, "服务里没有 asyncio.wait_for ⇒ 超时闸不存在"


# ─────────────────────── 2) 超时的值有人守 ───────────────────────


def test_the_proposal_timeout_is_thirty_seconds():
    """超时的值必须**有人守**（改成 300 或 5 都不该放行）。"""
    config = yaml.safe_load(PERFORMANCE_YAML.read_text(encoding="utf-8")) or {}
    section = config.get("target_budget_proposal") or {}

    assert int(section.get("timeout_seconds", 0)) == 30, (
        "config/performance.yaml 里的 target_budget_proposal.timeout_seconds 不是 30"
    )
    assert DEFAULT_TIMEOUT_SECONDS == 30
    assert resolve_proposal_timeout_seconds() == 30, "解析出来的超时不是 30"


def test_the_proposal_timeout_really_comes_from_the_config():
    """⚠ 「值是 30」与「值**来自配置**」是两件事。

    ⚠ 默认值恰好也是 30 ⇒ 不改配置就**分不出**这两者
    ⇒ 这条必须**改配置**、让解析值跟着变。
    """
    from application.core.config.config_loader import reload_config

    original = PERFORMANCE_YAML.read_text(encoding="utf-8")
    tmp_dir = tempfile.mkdtemp(prefix="plotpilot_j01_")
    tmp = Path(tmp_dir) / "performance.yaml"
    # ⚠ 临时 YAML **不许写进仓库的 `config/` 目录**（不在 .gitignore 里）
    assert (ROOT / "config") not in tmp.parents, "临时 YAML 写进了仓库的 config/ 目录"
    try:
        tmp.write_text(
            original.replace(
                "target_budget_proposal:\n  timeout_seconds: 30",
                "target_budget_proposal:\n  timeout_seconds: 45",
            ),
            encoding="utf-8",
        )
        reload_config(str(tmp))
        assert resolve_proposal_timeout_seconds() == 45, (
            "改了配置但解析值没跟着变 ⇒ 超时**不是**从配置读的"
        )
    finally:
        reload_config()  # 恢复默认配置（别把测试环境留在临时文件上）
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─────────────────────── 3) 别因错误的原因而绿 ───────────────────────


def test_the_real_config_does_not_disable_this_feature():
    """真实配置里总闸必须开着、这个能力不许被关掉。

    否则上面的守卫会**因错误的原因而绿**（压根不调模型）。
    """
    config = yaml.safe_load(PERFORMANCE_YAML.read_text(encoding="utf-8")) or {}
    section = config.get("ai_participation") or {}

    assert section.get("master_enabled") is True, "总闸在真实配置里是关的"
    assert "target_budget_proposal" not in (section.get("disabled_features") or []), (
        "真实配置里关掉了目标预算提案 ⇒ 服务级守卫会因错误的原因而绿"
    )


def test_generation_profiles_have_no_decorative_timeout_keys():
    """同族守卫：画像里**不许**再有 ``timeout_seconds`` / ``retries`` 键。

    ⚠ 它们**全仓无人读**（``generation_config_from_profile`` 不读）
    ⇒ 留着只会让人以为「调小它就能限超时」——上一份审查简报就信过同族的键。
    ⚠ 注释里提到这两个词不算（那是在解释它们已被删）。
    """
    lines = []
    for raw in PROFILES_YAML.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)

    offenders = [
        line
        for line in lines
        if line.startswith("timeout_seconds:") or line.startswith("retries:")
    ]
    assert not offenders, (
        f"画像里又出现了装饰性的超时/重试键（无人读）: {offenders}"
    )
