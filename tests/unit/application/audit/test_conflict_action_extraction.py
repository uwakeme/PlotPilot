"""H-02b：冲突检测的「实体-动作对」由 AI 抽，代码只校验不越界。

红线（本文件负责钉死）：

1. **不越界** ——
   ① 实体白名单：AI 不得凭空造人；
   ② ``kind`` 封闭枚举（``magic`` / ``weapon``）；
   ③ 属性值格式：魔法必须是「X系」（与设定库 ``magic_type`` 同口径）、
      武器值是武器名；
   ④ 证据必须**字面出现在大纲里**；
   ⑤ **归属**：证据的起点必须落在**这个实体**的小句里
      （H-02a 的误报不能从 AI 路径复活）。
2. **三态可分辨** ——
   ① AI 说「这段没有动作」≠ 未评估（**不回退**）；
   ② AI 不可用 / 输出不合契约 ⇒ 未评估 ⇒ 调用方兜底；
   ③ AI 给了动作但一条都不可用 ⇒ 判为未评估（这份回答不可信）。
3. **闭环** —— AI 抽到的动作**真的**被用于报冲突（含关键词表里没有的写法）。
4. **兜底逐位不变** —— AI 不可用时，关键词表路径的行为与改造前完全一致。
5. **来源可分辨** —— 「AI 抽的」与「兜底的」在程序层面可分辨。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from application.audit.services.conflict_action_extraction import ConflictActionExtractor
from application.audit.services.conflict_action_extraction_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    EntityAction,
    normalize_extraction,
)
from application.audit.services.conflict_detection_service import ConflictDetectionService


def _llm(payload):
    """假 LLM：返回给定 JSON 文本。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    class _LLM:
        async def generate(self, prompt, config=None):
            return SimpleNamespace(content=text)

    return _LLM()


def _action(entity_name, kind, value, evidence):
    return {
        "entity_name": entity_name,
        "kind": kind,
        "value": value,
        "evidence": evidence,
    }


# ── 1. 不越界 ────────────────────────────────────────────────────


def test_an_entity_outside_the_candidate_list_is_dropped():
    """AI 不得凭空造人。

    ⚠ 这里的证据是**真实存在**的（张三确实在大纲里出现），
    所以挡下它的只能是**白名单**这一条 —— 不能顺带被归属校验挡掉。
    """
    result = normalize_extraction(
        {"actions": [_action("张三", "magic", "水系", "张三放水球")]},
        valid_entity_names=["李四"],
        outline="李四放火球，张三放水球。",
    )

    assert result.actions == {}


def test_an_oversized_evidence_is_dropped():
    result = normalize_extraction(
        {
            "actions": [
                _action("李四", "magic", "火系", "李四" + "放火球" * 30)
            ]
        },
        valid_entity_names=["李四"],
        outline="李四" + "放火球" * 30 + "。",
    )

    assert result.actions == {}


def test_an_unknown_kind_is_dropped():
    """动作类别是封闭枚举。"""
    result = normalize_extraction(
        {"actions": [_action("李四", "teleport", "瞬移", "李四瞬移")]},
        valid_entity_names=["李四"],
        outline="李四瞬移。",
    )

    assert result.actions == {}


def test_a_magic_value_must_be_in_the_x_system_form():
    """魔法值必须「X系」——与设定库 magic_type 同口径（否则比较无从谈起）。"""
    ok = normalize_extraction(
        {"actions": [_action("李四", "magic", "火系", "李四放火球")]},
        valid_entity_names=["李四"],
        outline="李四放火球。",
    )
    bad = normalize_extraction(
        {"actions": [_action("李四", "magic", "火球术", "李四放火球")]},
        valid_entity_names=["李四"],
        outline="李四放火球。",
    )

    assert ok.actions["李四"][0].value == "火系"
    assert bad.actions == {}


def test_a_weapon_value_must_not_be_in_the_x_system_form():
    """武器值是武器名，不是「X系」那种魔法值。"""
    result = normalize_extraction(
        {"actions": [_action("李四", "weapon", "火系", "李四放火球")]},
        valid_entity_names=["李四"],
        outline="李四放火球。",
    )

    assert result.actions == {}


def test_an_oversized_value_is_dropped():
    result = normalize_extraction(
        {"actions": [_action("李四", "weapon", "极其沉重的玄铁重剑", "李四拔出极其沉重的玄铁重剑")]},
        valid_entity_names=["李四"],
        outline="李四拔出极其沉重的玄铁重剑。",
    )

    assert result.actions == {}


def test_an_evidence_not_in_the_outline_is_dropped():
    """AI 编出来的动作一律丢弃。"""
    result = normalize_extraction(
        {"actions": [_action("李四", "magic", "火系", "李四放火球")]},
        valid_entity_names=["李四"],
        outline="陈小拔剑。",
    )

    assert result.actions == {}


def test_the_evidence_must_belong_to_that_entity():
    """**归属**：AI 不能把别人的动作挂到某人头上（H-02a 的误报不许复活）。"""
    outline = "陈小拔剑，李四仓皇后退。"

    wrong = normalize_extraction(
        {"actions": [_action("李四", "weapon", "剑", "陈小拔剑")]},
        valid_entity_names=["陈小", "李四"],
        outline=outline,
    )
    right = normalize_extraction(
        {"actions": [_action("陈小", "weapon", "剑", "陈小拔剑")]},
        valid_entity_names=["陈小", "李四"],
        outline=outline,
    )

    assert wrong.actions == {}, "AI 把陈小的动作挂到李四头上，应当丢弃"
    assert right.actions["陈小"][0].value == "剑"


def test_a_connective_clause_is_still_credited_to_the_previous_subject():
    """「李明释放火球术，然后拔剑攻击」——后半句沿用主体，AI 抽的也要算数。"""
    outline = "李明释放火球术，然后拔剑攻击"

    result = normalize_extraction(
        {"actions": [_action("李明", "weapon", "剑", "然后拔剑攻击")]},
        valid_entity_names=["李明"],
        outline=outline,
    )

    assert result.actions["李明"][0].value == "剑"


# ── 2. 三态 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_ai_saying_no_actions_is_not_unassessed():
    """**模型说「没有」≠ 没评估** ⇒ 不回退（不回退才能让 AI 说了算）。"""
    result = await ConflictActionExtractor(_llm({"actions": []})).extract(
        outline="陈小独自沉思。", entity_names=["陈小"]
    )

    assert result.source == SOURCE_AI
    assert result.actions == {}


@pytest.mark.asyncio
async def test_the_ai_failing_is_unassessed():
    class _Boom:
        async def generate(self, prompt, config=None):
            raise RuntimeError("endpoint down")

    result = await ConflictActionExtractor(_Boom()).extract(
        outline="陈小拔剑。", entity_names=["陈小"]
    )

    assert result.source == SOURCE_NONE
    assert result.actions == {}


@pytest.mark.asyncio
async def test_a_non_json_answer_is_unassessed():
    result = await ConflictActionExtractor(_llm("这不是 JSON")).extract(
        outline="陈小拔剑。", entity_names=["陈小"]
    )

    assert result.source == SOURCE_NONE


@pytest.mark.asyncio
async def test_a_contract_violating_answer_is_unassessed():
    """缺 ``entity_name`` 的条目 ⇒ 不合契约 ⇒ 整份按未评估处理。"""
    result = await ConflictActionExtractor(
        _llm({"actions": [{"kind": "magic", "value": "火系", "evidence": "陈小放火球"}]})
    ).extract(outline="陈小放火球。", entity_names=["陈小"])

    assert result.source == SOURCE_NONE
    assert result.actions == {}


@pytest.mark.asyncio
async def test_a_contract_violating_answer_discards_the_whole_response():
    """输出不合契约 ⇒ **整份**按未评估处理（哪怕里面有一条是好的）。

    契约要求每条都有字符串形态的 ``entity_name``；一条不合规 ⇒ 整份作废。
    """
    result = await ConflictActionExtractor(
        _llm(
            {
                "actions": [
                    _action("陈小", "magic", "火系", "陈小放火球"),
                    {"entity_name": 123, "kind": "magic", "value": "水系",
                     "evidence": "陈小放水球"},
                ]
            }
        )
    ).extract(outline="陈小放火球，陈小放水球。", entity_names=["陈小"])

    assert result.source == SOURCE_NONE
    assert result.actions == {}


@pytest.mark.asyncio
async def test_an_answer_with_nothing_usable_is_unassessed():
    """AI 给了动作却一条都不可用 ⇒ 这份回答不可信 ⇒ 未评估（⇒ 调用方兜底）。"""
    result = await ConflictActionExtractor(
        _llm({"actions": [_action("陈小", "magic", "火球术", "陈小放火球")]})
    ).extract(outline="陈小放火球。", entity_names=["陈小"])

    assert result.source == SOURCE_NONE
    assert result.actions == {}


@pytest.mark.asyncio
async def test_an_entity_without_candidates_is_not_assessed():
    """没有候选实体 ⇒ 没什么可抽的（谈不上评估）。"""
    result = await ConflictActionExtractor(_llm({"actions": []})).extract(
        outline="陈小拔剑。", entity_names=[]
    )

    assert result.source == SOURCE_NONE


# ── 3. 闭环：AI 抽的动作真的被用上 ───────────────────────────────


@pytest.mark.asyncio
async def test_the_extracted_action_is_reported_as_a_conflict():
    """AI 抽到 ⇒ 真的报出设定冲突（含关键词表里没有的写法）。"""
    outline = "李四掌心凝出一团烈焰。"
    extraction = await ConflictActionExtractor(
        _llm({"actions": [_action("李四", "magic", "火系", "李四掌心凝出一团烈焰")]})
    ).extract(outline=outline, entity_names=["李四"])

    assert extraction.source == SOURCE_AI
    assert extraction.actions["李四"][0].value == "火系"

    annotations = ConflictDetectionService().detect(
        outline=outline,
        entity_states={"c2": {"magic_type": "水系", "weapon": ""}},
        name_to_entity_id={"李四": "c2"},
        entity_actions=extraction.actions,
    )

    assert len(annotations) == 1
    assert annotations[0].type == "setting_conflict"
    assert "水系" in annotations[0].message
    assert "火系" in annotations[0].message


@pytest.mark.asyncio
async def test_the_ai_saying_no_actions_does_not_fall_back_to_the_table():
    """AI 说「没有动作」⇒ **不回退**到关键词表（否则 AI 的话等于没说）。"""
    outline = "陈小拔剑。"
    extraction = await ConflictActionExtractor(_llm({"actions": []})).extract(
        outline=outline, entity_names=["陈小"]
    )

    annotations = ConflictDetectionService().detect(
        outline=outline,
        entity_states={"c1": {"magic_type": "", "weapon": "弓"}},
        name_to_entity_id={"陈小": "c1"},
        entity_actions=extraction.actions,
    )

    assert extraction.source == SOURCE_AI
    assert annotations == [], "AI 说没有动作，却还是按关键词表报了冲突"


# ── 4. 兜底逐位不变 ─────────────────────────────────────────────


def test_the_fallback_path_behaves_exactly_as_before():
    """AI 未评估 ⇒ 关键词表兜底：批注内容与改造前逐字一致。"""
    annotations = ConflictDetectionService().detect(
        outline="李明释放火球术，然后拔剑攻击",
        entity_states={"char-001": {"magic_type": "水系", "weapon": "弓"}},
        name_to_entity_id={"李明": "char-001"},
    )

    assert len(annotations) == 2
    magic = [a for a in annotations if "火系" in a.message]
    weapon = [a for a in annotations if "剑" in a.message]
    assert len(magic) == 1
    assert len(weapon) == 1
    assert magic[0].expected == "水系"
    assert magic[0].actual == "火系"
    assert weapon[0].expected == "弓"
    assert weapon[0].actual == "剑"


def test_the_fallback_does_not_fire_when_actions_are_given():
    """给了动作 ⇒ 关键词表**不参与**（否则同一份大纲被查两遍）。"""
    annotations = ConflictDetectionService().detect(
        outline="李明释放火球术，然后拔剑攻击",
        entity_states={"char-001": {"magic_type": "水系", "weapon": "弓"}},
        name_to_entity_id={"李明": "char-001"},
        entity_actions={"李明": [EntityAction(kind="magic", value="火系")]},
    )

    assert len(annotations) == 1
    assert "火系" in annotations[0].message


# ── 5. 来源可分辨（工作流层） ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_workflow_reports_the_extraction_source(monkeypatch):
    """「AI 抽的」与「兜底的」在程序层面必须可分辨。"""
    from application.workflows.auto_novel_generation_workflow import (
        AutoNovelGenerationWorkflow,
    )

    wf = AutoNovelGenerationWorkflow.__new__(AutoNovelGenerationWorkflow)
    wf.conflict_detection_service = ConflictDetectionService()
    wf._build_name_to_entity_id_mapping = lambda novel_id: {"李四": "c2"}
    wf._get_entity_states = lambda novel_id, chapter, mapping: {
        "c2": {"magic_type": "水系", "weapon": ""}
    }

    class _Extractor:
        def __init__(self, usable):
            self._usable = usable

        async def extract(self, **kwargs):
            if self._usable:
                return SimpleNamespace(
                    actions={"李四": [EntityAction(kind="magic", value="火系")]},
                    source=SOURCE_AI,
                    is_usable=True,
                )
            return SimpleNamespace(actions={}, source=SOURCE_NONE, is_usable=False)

    outline = "李四掌心凝出一团烈焰。"

    wf._conflict_action_extractor = _Extractor(True)
    annotations, source = await wf._detect_conflicts("n1", 3, outline)
    assert source == SOURCE_AI
    assert len(annotations) == 1

    wf._conflict_action_extractor = _Extractor(False)
    annotations, source = await wf._detect_conflicts("n1", 3, outline)
    assert source != SOURCE_AI, "AI 不可用时必须标成兜底，不能读成「AI 判过」"
