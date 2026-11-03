# -*- coding: utf-8 -*-
"""第 91 批（N-25 残余）：DAG 展示层节点的 JSON 解析 —— **行为级**守卫。

背景
----
第 89 批把 22 处自造 ``json.loads`` 收敛到共享抽取器
（``application/ai/llm_json_extract.parse_llm_json_to_dict``）；
第 90 批补齐 D-02 / 叙事校准 / 道具抽取；
本批收掉 **DAG 展示层**剩下的 6 处（planning 3 个 + ext 3 个）。

为什么结构守卫不够
------------------
``tests/unit/application/ai/test_shared_json_extractor_usage.py`` 只保证
「不再出现裸 ``json.loads(raw_text)``」⇒ 它**抓不住**两件事：

1. 换了抽取器，但**退化形态写错了** —— 例如把失败时的
   ``{"raw_text": ...}`` 兜底删掉 ⇒ 面板上直接变成空对象（用户看不到原文，
   也没法判断是模型没听话还是解析炸了）；
2. 抽取器用错（比如拿到 list 却当 dict 用）⇒ 面板拿到的类型不对。

⇒ 本文件**真的调节点**：假 LLM 返回**围栏包裹**的 JSON ⇒ 断言输出是
**解析后的 dict**；再补一条**退化形态**（模型吐散文 ⇒ 必须落回
``{"raw_text": ...}``）。

⚠ 刻意**不**碰 CPMS：``resolve_prompt`` 在实例上打桩 ⇒ 测试不连生产库
（``tests/unit`` 不隔离生产数据目录 ⇒ 这里必须自己保证不碰）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from application.engine.dag.nodes.ext_supplement_nodes import (
    NarrativeSyncExtractionNode,
    StateExtractionNode,
    StyleAnalysisNode,
)
from application.engine.dag.nodes.planning_nodes import (
    ActPlanningNode,
    BeatSheetNode,
    QuickMacroNode,
)

#: 模型把 JSON 包在 ```json 围栏里（**最常见的失败形状**）
FENCED = '```json\n{"beats": [{"id": 1, "kind": "scene"}], "total": 1}\n```'

#: 期望值**写死字面量**（不从被测模块 import 常量 ⇒ 不自指）
EXPECTED = {"beats": [{"id": 1, "kind": "scene"}], "total": 1}

#: 模型吐散文（根本不是 JSON）⇒ 必须落回退化形态
GARBAGE = "好的，我来分析一下这一章的节拍表结构。"

#: (节点类, 输出键) —— 6 个本批迁移的节点
MIGRATED_NODES = [
    (BeatSheetNode, "beat_sheet_json"),
    (QuickMacroNode, "macro_plan_json"),
    (ActPlanningNode, "act_chapters_json"),
    (StateExtractionNode, "state_json"),
    (StyleAnalysisNode, "style_fingerprint"),
    (NarrativeSyncExtractionNode, "sync_json"),
]


class _FakeLLM:
    """只回一个固定文本的假 LLM。"""

    def __init__(self, text: str):
        self._text = text

    async def generate(self, prompt, config):  # noqa: ANN001 - 假实现
        return SimpleNamespace(text=self._text)


def _run_node(monkeypatch, node_cls, output_key, text, inputs=None):
    """真跑一个 DAG 节点的 ``execute()``，返回 ``outputs[output_key]``。"""
    import domain.ai.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "LLMService", lambda: _FakeLLM(text))

    node = node_cls()
    # 打桩 resolve_prompt：CPMS_FIRST 会去查 CPMS 提示词库 ⇒ 不连库
    node.resolve_prompt = lambda variables=None: {
        "system": "s",
        "user": "u",
        "source": "none",
    }
    result = asyncio.run(node.execute(inputs or {}, {}))
    return result.outputs.get(output_key)


@pytest.mark.parametrize("node_cls,output_key", MIGRATED_NODES)
def test_a_fenced_json_response_is_parsed_into_a_dict(monkeypatch, node_cls, output_key):
    """⚠ 围栏包裹的 JSON **必须**解析成 dict（而不是退化成 ``raw_text``）。"""
    value = _run_node(monkeypatch, node_cls, output_key, FENCED)

    assert value == EXPECTED, (
        "%s 没剥掉 ```json 围栏 ⇒ 面板拿到的不是结构化结果：%r" % (output_key, value)
    )
    assert "raw_text" not in value, (
        "%s 走了退化形态（模型明明给了合法 JSON）⇒ 面板显示原始文本" % output_key
    )


@pytest.mark.parametrize("node_cls,output_key", MIGRATED_NODES)
def test_a_non_json_response_falls_back_to_the_raw_text(monkeypatch, node_cls, output_key):
    """⚠ 模型吐散文时**必须**落回 ``{"raw_text": ...}``（不许静默变空）。

    ⇒ 面板上用户能看到原文 ⇒ 能自己判断「是模型没听话」还是「解析炸了」。
    """
    value = _run_node(monkeypatch, node_cls, output_key, GARBAGE)

    assert value == {"raw_text": GARBAGE}, (
        "%s 解析失败时的退化形态不对（原始文本丢了 ⇒ 面板变成空对象）：%r"
        % (output_key, value)
    )
