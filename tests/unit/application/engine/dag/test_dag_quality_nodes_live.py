"""DAG 质量节点必须真的跑起来，不能把失败伪装成「一切正常」。

背景（登记表 F-10 / F-15）：

- F-10 `val_style`（class StyleNode）：用 `VoiceDriftService()` 无参构造。真实签名
  （voice_drift_service.py:46-53）要求 `score_repo` / `fingerprint_repo` 两个必选位置参数，
  必然 TypeError；随后调用的 `.analyze()` 根本不存在（真实方法是 `score_chapter_async`，
  :74）。异常被裸 `except Exception` 吞成一条 warning，`drift_score` 停在初值 0.0、
  `drift_alert` 停在 False，节点恒返回 SUCCESS —— 这个「文风警报器」从未响过一次。

- F-15 `anti_ai_audit`（class ChapterAuditNode）：用 `LLMService()` 无参构造。真实是
  `class LLMService(ABC)`（llm_service.py:47-65），带 @abstractmethod generate /
  stream_generate，无法实例化。异常同样被吞；且 JSON 解析失败时 `severity_score`
  停在 0.0，与「真干净」不可区分。

这组用例断言的是**三态**（domain/ai/value_objects/assessment_state.py）：
跑成 → DECIDED；跑不成 → UNASSESSED 且不谎报通过。绝不允许「失败」长得像「干净」。

两条方向都要钉死，缺一不可：
- 降级路径不能返回 0.0/SUCCESS（否则失败伪装成干净）；
- 合法返回 0.0 时仍然必须是 DECIDED（否则「修成永远不报 0」，把一个 bug 换成另一个）。

替身形状逐个按真实定义写（async 与否、参数名与顺序都核对过），否则用例会挂在
TypeError 上而不是断言失败：
- `VoiceDriftService.score_chapter_async(self, novel_id, chapter_number, content,
  pov_character_id=None) -> dict`（voice_drift_service.py:74-80）
- `DynamicLLMService.generate(self, prompt: Prompt, config: GenerationConfig)
  -> GenerationResult`（provider_factory.py:263）—— 用真的 GenerationResult，
  它**只有 .content**（llm_service.py:34-44），没有 .text。

放 tests/unit/application/engine/dag/ 而不是 tests/dag/：CI 只跑 `pytest tests/unit`
（见 CLAUDE.md），放 tests/dag/ 这些用例永远不会被门禁跑到。
"""

import json

import pytest

from application.engine.dag.models import NodeConfig, NodeStatus
from application.engine.dag.nodes.anti_ai_nodes import ChapterAuditNode
from application.engine.dag.nodes.validation_nodes import StyleNode
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.assessment_state import AssessmentState
from domain.ai.value_objects.token_usage import TokenUsage

CONTENT = "他推开门，屋里没有人。风把窗纸吹得响了一声。"


# ─── 替身（形状按真实定义） ───


class StubVoiceDriftService:
    """VoiceDriftService 的替身，只暴露节点真正该调用的那个方法。"""

    def __init__(self, payload=None, raises=None):
        self._payload = payload
        self._raises = raises
        self.calls = []

    async def score_chapter_async(self, novel_id, chapter_number, content, pov_character_id=None):
        # 记录实参：用来证明节点没有伪造 chapter_number
        self.calls.append(
            {"novel_id": novel_id, "chapter_number": chapter_number, "content": content}
        )
        if self._raises is not None:
            raise self._raises
        return self._payload


class StubLLMService:
    """DynamicLLMService 的替身。

    `generate` 的真实签名是 (self, prompt, config) 且是 async；返回 GenerationResult。
    """

    def __init__(self, text=None, raises=None):
        self._text = text
        self._raises = raises
        self.calls = []

    async def generate(self, prompt, config):
        self.calls.append({"prompt": prompt, "config": config})
        if self._raises is not None:
            raise self._raises
        return GenerationResult(
            content=self._text,
            token_usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


def _patch_voice_drift(monkeypatch, factory):
    """替换 DI 装配点 get_voice_drift_service（节点在 execute 内部惰性 import 它）。"""
    import interfaces.api.dependencies as deps

    monkeypatch.setattr(deps, "get_voice_drift_service", factory)


def _patch_llm(monkeypatch, factory):
    import interfaces.api.dependencies as deps

    monkeypatch.setattr(deps, "get_llm_service", factory)


def _audit_node(monkeypatch) -> ChapterAuditNode:
    """构造审计节点并掐掉 CPMS 查询，返回固定的 system/user 模板。"""
    node = ChapterAuditNode()
    monkeypatch.setattr(
        node,
        "resolve_prompt",
        lambda variables=None: {
            "system": "你是反AI味审计员。",
            "user": "章节正文：\n{{content}}",
            "source": "test",
        },
    )
    return node


# ─── F-10：val_style / StyleNode ───


class TestStyleNodeNeverFakesACleanScore:
    """F-10：跑不成必须显式未评估，不能返回 0.0 + SUCCESS。"""

    async def test_di_factory_unavailable_yields_unassessed(self, monkeypatch):
        """DI 装配点炸了 → UNASSESSED + score=None，状态不得是 SUCCESS。"""
        def _broken_factory():
            raise RuntimeError("文风服务装配失败")

        _patch_voice_drift(monkeypatch, _broken_factory)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute(
            {"content": CONTENT}, {"novel_id": "n1", "chapter_number": 3}
        )

        assert result.outputs.get("state") is AssessmentState.UNASSESSED, (
            "DI 装配点不可用时必须显式 UNASSESSED；修复前这里恒为 SUCCESS（警报器从未响过）"
        )
        assert result.outputs.get("drift_score") is None, (
            "未评估不得回落到 0.0——0.0 与「文风完美」不可区分"
        )
        assert result.outputs.get("drift_alert") is None, (
            "未评估时不得谎报 drift_alert=False（那等于宣称「没有漂移」）"
        )
        assert result.status is not NodeStatus.SUCCESS, "未评估不得声称成功"
        assert result.status is NodeStatus.ERROR
        assert result.error, "必须留下失败原因"

    async def test_service_exception_yields_unassessed(self, monkeypatch):
        """服务抛异常 → UNASSESSED，而不是把异常吞成 0.0。"""
        service = StubVoiceDriftService(raises=RuntimeError("指纹仓库炸了"))
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute(
            {"content": CONTENT}, {"novel_id": "n1", "chapter_number": 3}
        )

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert result.outputs.get("drift_score") is None
        assert result.status is NodeStatus.ERROR

    async def test_missing_similarity_key_is_unassessed(self, monkeypatch):
        """返回 dict 缺 similarity_score 键 → 未评估，不得静默补 0.0。"""
        service = StubVoiceDriftService(payload={"drift_alert": False})
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute(
            {"content": CONTENT}, {"novel_id": "n1", "chapter_number": 3}
        )

        assert result.outputs.get("state") is AssessmentState.UNASSESSED, (
            "缺少 similarity_score 键表示服务没判，不能当成 0 分"
        )
        assert result.outputs.get("drift_score") is None

    async def test_service_returned_none_similarity_is_unassessed(self, monkeypatch):
        """真实服务在无指纹基准时返回 similarity_score=None（voice_drift_service.py:111）。

        那是「没得比」，不是「文风完美」，因此记未评估 —— 但绝不回填 0.0。
        """
        service = StubVoiceDriftService(
            payload={"similarity_score": None, "drift_alert": False}
        )
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute(
            {"content": CONTENT}, {"novel_id": "n1", "chapter_number": 3}
        )

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert result.outputs.get("drift_score") is None, (
            "服务说 None 就是「没结论」，节点不得翻译成 0.0"
        )


class TestStyleNodeDoesNotFabricateChapterNumber:
    """chapter_number 必须是真实章节号；拿不到就未评估，不能塞 0/1 蒙混过关。"""

    async def test_missing_chapter_number_yields_unassessed_and_skips_service(self, monkeypatch):
        service = StubVoiceDriftService(payload={"similarity_score": 0.42, "drift_alert": False})
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        # registry.py:309 把缺失的 chapter_number 兜成 0，而领域不变量是 >= 1
        # （domain/novel/value_objects/timeline_event.py:19），所以 0 == 没有章节上下文。
        result = await node.execute({"content": CONTENT}, {"novel_id": "n1", "chapter_number": 0})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert result.outputs.get("drift_score") is None
        assert service.calls == [], (
            "没有真实章节号时不得调用服务——score_chapter 会写 chapter_style_scores 行，"
            "伪造 chapter_number=0 会往库里灌垃圾基准"
        )

    async def test_absent_chapter_number_key_yields_unassessed(self, monkeypatch):
        service = StubVoiceDriftService(payload={"similarity_score": 0.42, "drift_alert": False})
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert service.calls == []

    async def test_real_chapter_number_from_context_is_forwarded(self, monkeypatch):
        """DAG 真实来源：registry.py:307-311 把 state.chapter_number 放进 context。"""
        service = StubVoiceDriftService(payload={"similarity_score": 0.42, "drift_alert": False})
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        await node.execute({"content": CONTENT}, {"novel_id": "n1", "chapter_number": 7})

        assert service.calls, "有真实章节号时必须真的调服务"
        assert service.calls[0]["chapter_number"] == 7, (
            "必须把 context 里的真实章节号原样传给 score_chapter_async"
        )
        assert service.calls[0]["novel_id"] == "n1"

    async def test_inputs_chapter_number_takes_precedence(self, monkeypatch):
        """inputs 里有章节号时优先取 inputs（与 KGInferNode 同款取值顺序）。"""
        service = StubVoiceDriftService(payload={"similarity_score": 0.42, "drift_alert": False})
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        await node.execute(
            {"content": CONTENT, "chapter_number": 12},
            {"novel_id": "n1", "chapter_number": 7},
        )

        assert service.calls, "有真实章节号时必须真的调服务"
        assert service.calls[0]["chapter_number"] == 12

    async def test_missing_novel_id_yields_unassessed(self, monkeypatch):
        """没有 novel_id 时同样不评估：score_repo.upsert 会写出无主数据。"""
        service = StubVoiceDriftService(payload={"similarity_score": 0.42, "drift_alert": False})
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute({"content": CONTENT}, {"chapter_number": 3})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert service.calls == []


class TestStyleNodeReportsRealScoresWhenItRuns:
    """真跑成时，结论必须来自服务返回值，阈值判定必须真的生效。"""

    async def test_score_comes_from_service_and_is_decided(self, monkeypatch):
        service = StubVoiceDriftService(
            payload={"similarity_score": 0.42, "drift_alert": False}
        )
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.9}))

        result = await node.execute(
            {"content": CONTENT}, {"novel_id": "n1", "chapter_number": 3}
        )

        assert result.outputs.get("state") is AssessmentState.DECIDED
        assert result.outputs.get("drift_score") == pytest.approx(0.42), (
            "分数必须来自 score_chapter_async 的返回值"
        )
        assert result.outputs.get("drift_alert") is False
        assert result.status is NodeStatus.SUCCESS

    async def test_threshold_breach_raises_alert(self, monkeypatch):
        """造一个超过 drift_warning 的分数，断言警报真的响了。"""
        service = StubVoiceDriftService(
            payload={"similarity_score": 0.95, "drift_alert": False}
        )
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute(
            {"content": CONTENT}, {"novel_id": "n1", "chapter_number": 3}
        )

        assert result.outputs.get("drift_score") == pytest.approx(0.95)
        assert result.outputs.get("drift_alert") is True, (
            "0.95 > drift_warning(0.5) 必须触发警报——这个节点以前从未响过"
        )
        assert result.status is NodeStatus.WARNING
        assert result.outputs.get("state") is AssessmentState.DECIDED

    async def test_zero_similarity_is_decided_not_unassessed(self, monkeypatch):
        """反向钉死：真值 0 必须如实表达为 0 + DECIDED，不得被当成「没评估」。"""
        service = StubVoiceDriftService(
            payload={"similarity_score": 0.0, "drift_alert": False}
        )
        _patch_voice_drift(monkeypatch, lambda: service)
        node = StyleNode(NodeConfig(thresholds={"drift_warning": 0.5}))

        result = await node.execute(
            {"content": CONTENT}, {"novel_id": "n1", "chapter_number": 3}
        )

        assert result.outputs.get("drift_score") == 0.0, (
            "真实相似度为 0 必须原样表达为 0.0，不能被改成 None"
        )
        assert result.outputs.get("state") is AssessmentState.DECIDED, (
            "0 与「没评估」必须被区分：跑成了就是 DECIDED"
        )
        assert result.outputs.get("drift_alert") is False
        assert result.status is NodeStatus.SUCCESS


# ─── F-15：anti_ai_audit / ChapterAuditNode ───


class TestAntiAiAuditNeverFakesZeroSeverity:
    """F-15：LLM 跑不成时 severity_score 必须是 None + UNASSESSED。"""

    async def test_non_json_llm_output_yields_unassessed(self, monkeypatch):
        llm = StubLLMService(text="我觉得这章还行，没什么 AI 味。")
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED, (
            "LLM 返回非 JSON 时必须未评估；修复前 severity_score 停在 0.0 = 假干净"
        )
        assert result.outputs.get("severity_score") is None, (
            "解析失败必须给 None，不能给 0.0——0.0 在这里读作「没有 AI 味」"
        )
        assert result.status is not NodeStatus.SUCCESS
        assert result.status is NodeStatus.ERROR

    async def test_json_list_instead_of_object_yields_unassessed(self, monkeypatch):
        llm = StubLLMService(text=json.dumps([1, 2, 3]))
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert result.outputs.get("severity_score") is None

    async def test_json_missing_severity_key_yields_unassessed(self, monkeypatch):
        """LLM 漏给 severity_score → 未评估，不得 .get(key, 0) 静默补 0。"""
        llm = StubLLMService(text=json.dumps({"total_hits": 0, "hits": []}))
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert result.outputs.get("severity_score") is None, (
            "字段缺失是「没判」，.get(key, 0) 会把它伪装成「判了 0 分」"
        )

    async def test_llm_exception_yields_unassessed(self, monkeypatch):
        llm = StubLLMService(raises=RuntimeError("LLM 超时"))
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert result.outputs.get("severity_score") is None
        assert result.status is NodeStatus.ERROR

    async def test_abstract_llm_service_is_never_constructed(self, monkeypatch):
        """真实 get_llm_service() 返回 DynamicLLMService；ABC 只能被实现类满足。

        这条直接钉死「不再无参构造 LLMService()」这个根因。
        """
        import domain.ai.services.llm_service as llm_mod

        with pytest.raises(TypeError):
            llm_mod.LLMService()

        llm = StubLLMService(
            text=json.dumps(
                {
                    "total_hits": 1,
                    "severity_score": 12.0,
                    "hits": [{"pattern": "环境描写堆砌"}],
                    "overall_assessment": "轻度",
                }
            )
        )
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.DECIDED


class TestAntiAiAuditReportsRealSeverity:
    """真跑成时结论必须来自 LLM 的真实返回值。"""

    async def test_severity_is_read_from_generation_result_content(self, monkeypatch):
        """GenerationResult 只有 .content（llm_service.py:34-44），没有 .text。

        修复前 `result.text if hasattr(result, 'text') else str(result)` 会退化成
        str(result) 的对象 repr，然后当 JSON 解析失败处理。
        """
        llm = StubLLMService(
            text=json.dumps(
                {
                    "total_hits": 4,
                    "severity_score": 72.5,
                    "hits": [{"pattern": "排比堆砌"}, {"pattern": "空泛收尾"}],
                    "overall_assessment": "AI 味明显",
                }
            )
        )
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.DECIDED
        assert result.outputs.get("severity_score") == pytest.approx(72.5)
        assert result.outputs.get("total_hits") == 4
        assert result.outputs.get("overall_assessment") == "AI 味明显"
        assert len(result.outputs.get("hits") or []) == 2
        assert result.status is NodeStatus.WARNING, "72.5 > 50 应告警"

    async def test_zero_severity_is_decided_not_unassessed(self, monkeypatch):
        """反向用例（最关键的一条）：LLM 合法返回 0.0 时仍是 DECIDED。

        光把 0.0 改成 None 是不够的——那只是把「永远报 0」换成「永远不报」。
        这里证明 0 本身仍能被如实表达，且与「没评估」走不同分支。
        """
        llm = StubLLMService(
            text=json.dumps(
                {
                    "total_hits": 0,
                    "severity_score": 0.0,
                    "hits": [],
                    "overall_assessment": "干净",
                }
            )
        )
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("severity_score") == 0.0, (
            "真实 severity 为 0 必须原样表达为 0.0，不能被改成 None"
        )
        assert result.outputs.get("state") is AssessmentState.DECIDED, (
            "0 与「没评估」必须被区分：解析成功就是 DECIDED"
        )
        assert result.outputs.get("total_hits") == 0
        assert result.outputs.get("overall_assessment") == "干净"
        assert result.status is NodeStatus.SUCCESS

    async def test_zero_and_unassessed_are_different_states(self, monkeypatch):
        """把两条路径并排跑一次，钉死「0 ≠ 没评估」不是巧合而是结构差异。"""
        _patch_llm(
            monkeypatch,
            lambda: StubLLMService(text=json.dumps({"severity_score": 0.0})),
        )
        decided = await _audit_node(monkeypatch).execute(
            {"content": CONTENT}, {"novel_id": "n1"}
        )

        _patch_llm(monkeypatch, lambda: StubLLMService(text="完全不是 JSON"))
        unassessed = await _audit_node(monkeypatch).execute(
            {"content": CONTENT}, {"novel_id": "n1"}
        )

        assert decided.outputs.get("severity_score") == 0.0
        assert unassessed.outputs.get("severity_score") is None
        assert decided.outputs.get("state") is AssessmentState.DECIDED
        assert unassessed.outputs.get("state") is AssessmentState.UNASSESSED
        assert decided.outputs.get("state") is not unassessed.outputs.get("state")

    async def test_non_numeric_severity_is_unassessed(self, monkeypatch):
        llm = StubLLMService(text=json.dumps({"severity_score": "非常严重"}))
        _patch_llm(monkeypatch, lambda: llm)
        node = _audit_node(monkeypatch)

        result = await node.execute({"content": CONTENT}, {"novel_id": "n1"})

        assert result.outputs.get("state") is AssessmentState.UNASSESSED
        assert result.outputs.get("severity_score") is None
