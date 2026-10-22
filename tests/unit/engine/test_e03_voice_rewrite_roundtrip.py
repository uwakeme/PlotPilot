"""E-03：声线定向改写要返回「下一轮换什么手法」，并**回传**给下一轮。

改造前
------
``voice-rewrite`` 节点**只输出改写后的正文**（纯文本）⇒
第 2 轮改写时模型**不知道上一轮为什么没达标**，只能换个说法再改一遍
⇒ 最多 3 轮 = 最多 3 次**盲改**。

而且是**两条平行链**：

- StoryPipeline：``engine/pipeline/base.py::_apply_voice_rewrite_loop``
- daemon：``engine/runtime/daemon_host.py::_apply_voice_rewrite_loop``

改造后
------
· 节点输出 JSON：``rewritten`` / ``diagnosis`` / ``next_technique``；
· 两条链都把上一轮的 ``diagnosis`` 与 ``next_technique`` 回传
  （新增提示词变量 ``previous_diagnosis`` / ``previous_next_technique``）；
· 模型没给 JSON ⇒ **原样当正文**（改造前的行为），结构化字段为空
  ⇒ 下一轮没有可回传的内容，**如实为空，不编**。
"""
from __future__ import annotations

import ast
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from application.engine.services.voice_rewrite_result import (
    MAX_DIAGNOSIS_CHARS,
    VoiceRewriteOutcome,
    parse_voice_rewrite,
)
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROMPT_DIR = "infrastructure/ai/prompt_packages/nodes/voice-rewrite"
BASE_REL = "engine/pipeline/base.py"
DAEMON_REL = "engine/runtime/daemon_host.py"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


# ======================================================================
#  A. 解析层
# ======================================================================


def test_json_output_is_parsed_into_three_parts():
    """JSON 输出 ⇒ 正文 + 本轮诊断 + 下一轮手法。"""
    outcome = parse_voice_rewrite(
        json.dumps(
            {
                "rewritten": "改写后的正文",
                "diagnosis": "心理描写太多，仍在直接命名情绪",
                "next_technique": "把心理描写改成动作外化",
            },
            ensure_ascii=False,
        )
    )

    assert outcome.rewritten == "改写后的正文"
    assert outcome.diagnosis == "心理描写太多，仍在直接命名情绪"
    assert outcome.next_technique == "把心理描写改成动作外化"
    assert outcome.structured is True


def test_non_json_output_is_treated_as_the_whole_body():
    """模型没给 JSON ⇒ **原样当正文**（改造前的行为），结构化字段为空。"""
    outcome = parse_voice_rewrite("这是一整章改写后的正文，没有 JSON。")

    assert outcome.rewritten == "这是一整章改写后的正文，没有 JSON。"
    assert outcome.diagnosis == ""
    assert outcome.next_technique == ""
    assert outcome.structured is False


def test_json_without_a_usable_body_yields_no_content():
    """给了 JSON 但没有可用正文 ⇒ 交由调用方按「未产生有效变化」处理。"""
    outcome = parse_voice_rewrite(
        json.dumps({"rewritten": "   ", "diagnosis": "d", "next_technique": "t"})
    )

    assert outcome.rewritten == ""
    assert outcome.structured is True
    assert outcome.diagnosis == "d"


def test_diagnosis_and_technique_are_clipped():
    """诊断与手法有长度上限（防模型写长篇）。"""
    outcome = parse_voice_rewrite(
        json.dumps(
            {
                "rewritten": "正文",
                "diagnosis": "D" * (MAX_DIAGNOSIS_CHARS + 100),
                "next_technique": "T" * (MAX_DIAGNOSIS_CHARS + 100),
            },
            ensure_ascii=False,
        )
    )

    assert len(outcome.diagnosis) == MAX_DIAGNOSIS_CHARS
    assert len(outcome.next_technique) == MAX_DIAGNOSIS_CHARS


def test_empty_output_is_empty():
    """空输出 ⇒ 空 outcome（调用方按「未产生有效变化」处理）。"""
    outcome = parse_voice_rewrite("   ")

    assert outcome.rewritten == ""
    assert outcome.structured is False


# ======================================================================
#  B. 提示词
# ======================================================================


def test_the_prompt_asks_for_the_json_output():
    """提示词必须要求 JSON + 那三个字段。

    ⚠ 判据要落在**输出指令**上，不能只判「文件里出现过 JSON 这个词」——
    字段清单里本来就写着 JSON，那样的话把输出指令改回纯文本也照样绿。
    """
    system = _read(f"{PROMPT_DIR}/system.md")

    assert "只输出一个 JSON 对象" in system, "提示词没有要求「只输出一个 JSON 对象」"
    assert "输出只能是修订后的完整章节正文" not in system, (
        "提示词还留着「输出只能是正文」那句旧指令 ⇒ 模型会只给纯文本"
    )
    assert "rewritten" in system
    assert "diagnosis" in system
    assert "next_technique" in system


def test_the_prompt_package_declares_the_two_previous_variables():
    """提示词包必须声明那两个回传变量。"""
    package = _read(f"{PROMPT_DIR}/package.yaml")

    assert "previous_diagnosis" in package
    assert "previous_next_technique" in package


def test_the_prompt_shows_the_previous_round_to_the_model():
    """用户模板要把上一轮的情况真的**送进提示词**（否则回传是假的）。"""
    user = _read(f"{PROMPT_DIR}/user.md")

    assert "{previous_diagnosis}" in user
    assert "{previous_next_technique}" in user


# ======================================================================
#  C. 接线：两条链都回传
# ======================================================================


def _ctx_with_drift():
    ctx = PipelineContext(
        novel_id="n-1",
        chapter_number=7,
        chapter_content="原始正文" * 20,
        outline="大纲",
    )
    ctx.similarity_score = 0.4
    ctx.drift_alert = True
    fingerprint_repo = MagicMock()
    fingerprint_repo.get_by_novel.return_value = None
    voice = MagicMock()
    voice.fingerprint_repo = fingerprint_repo
    voice.score_chapter.return_value = {"similarity_score": 0.4, "drift_alert": True}
    ctx.voice_drift_service = voice
    ctx.llm_service = MagicMock()
    return ctx


@pytest.mark.asyncio
async def test_the_pipeline_passes_the_previous_round_back(monkeypatch):
    """第二轮的变量里必须带着第一轮的诊断与手法。"""
    seen: list[dict] = []

    class _Pipeline(BaseStoryPipeline):
        pass

    async def _fake_invoke(p, c, variables):
        seen.append(dict(variables))
        return VoiceRewriteOutcome(
            rewritten=f"第{len(seen)}次改写",
            diagnosis="上一轮诊断",
            next_technique="换一种手法",
        )

    monkeypatch.setattr(_Pipeline, "_invoke_voice_rewrite", _fake_invoke)
    pipeline = _Pipeline.__new__(_Pipeline)

    await pipeline._apply_voice_rewrite_loop(_ctx_with_drift())

    assert len(seen) >= 2, "至少要跑两轮才谈得上「回传」"
    assert seen[0]["previous_diagnosis"] == ""
    assert seen[0]["previous_next_technique"] == ""
    assert seen[1]["previous_diagnosis"] == "上一轮诊断"
    assert seen[1]["previous_next_technique"] == "换一种手法"


def test_the_daemon_path_passes_the_previous_round_back():
    """daemon 那条链也要把上一轮回传（AST 判实参里的键）。"""
    tree = ast.parse(_read(DAEMON_REL))

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "_rewrite_chapter_for_voice":
            continue
        kwargs = {kw.arg for kw in node.keywords}
        if {"previous_diagnosis", "previous_next_technique"} <= kwargs:
            found = True
    assert found, "daemon 链没把上一轮的诊断/手法传下去"


def test_both_paths_use_the_shared_parser():
    """两条链共用**同一个**解析实现（不写两份）。"""
    for rel in (BASE_REL, DAEMON_REL):
        tree = ast.parse(_read(rel))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name:
                    called.add(name)
        assert "parse_voice_rewrite" in called, f"{rel} 没用共享解析器"


def test_the_unread_config_section_is_gone():
    """``autopilot.voice_rewrite:`` 那段配置**全仓无人读取** ⇒ 已删。

    它的三个值在代码里有真来源（类属性，支持子类覆盖）⇒ 留着就是假来源。
    """
    import yaml

    data = yaml.safe_load(_read("config/performance.yaml"))
    autopilot = data.get("autopilot") or {}

    assert "voice_rewrite" not in autopilot, (
        "config/performance.yaml 里的 voice_rewrite 段仍然无人读取（假来源）"
    )
