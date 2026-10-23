"""G-04：情绪账本 —— **保留 LLM 的值**，截断放宽，账本规模上限，账本进生产上下文。

改造前
------
1. ``OpenLoop.urgency`` 被 clamp 到 [0,1]，读不出数字时**回落 0.5**
   （抽取路径 + 反序列化路径**各写了一份**）⇒ 等于**代码替模型说话**：
   模型说 0.9 被改成 1，说不出被说成 0.5；
2. 每类硬截断 3 条（``_normalize_deltas`` + ``merge_ledger`` **写了两遍**）
   ⇒ 模型抽出的第 4 条起被**静默丢掉**；
3. 账本**只写不读**：每章一次 LLM 抽取写进 ``checkpoints``，
   读回路径（``memory_orchestrator.assemble_context``）**零生产调用方**
   ⇒ 生产库实测 58 行 checkpoint 里 56 行有账本，却没人看。

改造后
------
- urgency **原样保留**；读不出数字 ⇒ **未评估**（``None``），不回落 0.5；
- 每类上限放宽到 8，且截断**只在一处**（``_normalize_deltas``）；
- **账本规模上限**（登记表的不变量）：每类最多留 20 条（留最新），
  丢弃的条数记在 ``trimmed_items``（**不许静默**）；
  ⚠ **未解悬念不裁剪** —— 还没回收的线不能因为上限而消失；
- 账本作为 **T1 槽**进生产上下文 ⇒ 写进去的东西终于有人看。
"""
from __future__ import annotations

import ast
import json
import pathlib
from unittest.mock import AsyncMock

import pytest

from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage
from engine.core.value_objects.emotion_ledger import (
    MAX_ITEMS_PER_CATEGORY,
    EmotionLedger,
    EmotionalWound,
    OpenLoop,
    coerce_urgency,
)
from engine.infrastructure.memory.emotion_ledger_extractor import (
    EmotionLedgerExtractionError,
    EmotionLedgerExtractor,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
EXTRACTOR_REL = "engine/infrastructure/memory/emotion_ledger_extractor.py"
ALLOCATOR_REL = "application/engine/services/context_budget_allocator.py"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


def _func_source(rel: str, func_name: str) -> str:
    """取某个函数的源码（AST 定位 ⇒ 不用文本匹配，避免命中注释）。"""
    tree = ast.parse(_read(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return ast.get_source_segment(_read(rel), node) or ""
    raise AssertionError(f"找不到函数 {func_name}")


# =====================================================================
# 一、LLM 的值原样保留
# =====================================================================


def test_the_urgency_is_not_clamped():
    """⚠ 模型说 1.5 就是 1.5 —— 此前被 clamp 成 1.0（代码替模型说话）。"""
    assert coerce_urgency(1.5) == 1.5
    assert coerce_urgency(0.9) == 0.9


def test_an_unreadable_urgency_is_not_assessed():
    """读不出数字 ⇒ **未评估**（None），不是回落 0.5。"""
    assert coerce_urgency("高") is None
    assert coerce_urgency(None) is None
    assert coerce_urgency(True) is None


def test_the_merge_keeps_the_model_urgency():
    """合并路径不许再 clamp。"""
    extractor = EmotionLedgerExtractor()
    updated = extractor.merge_ledger(
        EmotionLedger.create_empty(),
        {"open_loops": [{"description": "幕后黑手", "hint": "组织名", "urgency": 1.5}]},
        chapter_number=5,
    )

    assert updated.open_loops[0].urgency == 1.5


def test_the_merge_marks_an_unreadable_urgency_as_unassessed():
    extractor = EmotionLedgerExtractor()
    updated = extractor.merge_ledger(
        EmotionLedger.create_empty(),
        {"open_loops": [{"description": "幕后黑手", "hint": "组织名", "urgency": "很急"}]},
        chapter_number=5,
    )

    assert updated.open_loops[0].urgency is None


def test_the_roundtrip_keeps_the_urgency():
    """反序列化路径也不许 clamp（此前那里**又写了一份**）。"""
    ledger = EmotionLedger.create_empty().add_open_loop(
        OpenLoop(description="身世之谜", hint="血脉", planted_chapter=1, urgency=1.4)
    )
    restored = EmotionLedger.from_dict(ledger.to_dict())

    assert restored.open_loops[0].urgency == 1.4


def test_the_roundtrip_keeps_an_unassessed_urgency():
    restored = EmotionLedger.from_dict(
        {"open_loops": [{"description": "x", "hint": "y", "urgency": None}]}
    )

    assert restored.open_loops[0].urgency is None


# =====================================================================
# 二、截断放宽 + 只写一处
# =====================================================================


@pytest.mark.asyncio
async def test_more_than_three_items_per_chapter_are_kept():
    """模型抽出 5 条 ⇒ 5 条都留（此前每类硬截断 3 条）。"""
    llm = AsyncMock()
    llm.generate.return_value = GenerationResult(
        content=json.dumps(
            {
                "wounds": [],
                "boons": [],
                "power_shifts": [],
                "open_loops": [
                    {"description": f"悬念{i}", "hint": "线索", "urgency": 0.5}
                    for i in range(5)
                ],
                "resolved_loops": [],
            },
            ensure_ascii=False,
        ),
        token_usage=TokenUsage(input_tokens=10, output_tokens=20),
    )
    extractor = EmotionLedgerExtractor(llm_service=llm)

    deltas = await extractor.extract_deltas(
        chapter_content="林羽站在雨里，师父的背叛像刀一样扎进心里。" * 10,
        chapter_number=4,
        current_ledger=EmotionLedger.create_empty(),
    )

    assert len(deltas["open_loops"]) == 5


def test_the_truncation_lives_in_one_place_only():
    """截断只在 ``_normalize_deltas`` 一处（此前两处各写一遍）。"""
    merge_src = _func_source(EXTRACTOR_REL, "merge_ledger")
    normalize_src = _func_source(EXTRACTOR_REL, "_normalize_deltas")

    assert "_MAX_ITEMS_PER_CATEGORY" not in merge_src, (
        "merge_ledger 里又截断了一次 ⇒ 同一个上限写了两遍"
    )
    assert "_MAX_ITEMS_PER_CATEGORY" in normalize_src


def test_the_per_chapter_cap_is_wider_than_three():
    """每类上限放宽（登记表：截断放宽）。"""
    source = _read(EXTRACTOR_REL)
    assert "_MAX_ITEMS_PER_CATEGORY = 8" in source


# =====================================================================
# 三、账本规模上限（登记表的不变量）
# =====================================================================


def test_the_ledger_keeps_at_most_the_cap_per_category():
    ledger = EmotionLedger.create_empty()
    for i in range(MAX_ITEMS_PER_CATEGORY + 5):
        ledger = ledger.add_wound(
            EmotionalWound(description=f"损失{i}", impact="多疑", chapter_number=i)
        )

    assert len(ledger.wounds) == 20
    # 留最新
    assert ledger.wounds[-1].description == "损失24"


def test_the_trim_count_is_visible_in_the_payload():
    """丢弃的条数**不许静默** ⇒ 要能查出来丢了多少。"""
    ledger = EmotionLedger.create_empty()
    for i in range(MAX_ITEMS_PER_CATEGORY + 3):
        ledger = ledger.add_wound(
            EmotionalWound(description=f"损失{i}", impact="多疑", chapter_number=i)
        )

    assert ledger.trimmed_items == 3
    assert ledger.to_dict()["trimmed_items"] == 3


def test_open_loops_are_never_trimmed():
    """⚠ 未解悬念不能因为上限而消失（那会让「还没埋的线」静默消失）。"""
    ledger = EmotionLedger.create_empty()
    for i in range(MAX_ITEMS_PER_CATEGORY + 10):
        ledger = ledger.add_open_loop(
            OpenLoop(description=f"悬念{i}", hint="线索", planted_chapter=i)
        )

    assert len(ledger.open_loops) == 30
    assert ledger.trimmed_items == 0


# =====================================================================
# 四、接线：账本进生产上下文（此前只写不读）
# =====================================================================


def test_the_emotion_ledger_is_a_production_context_slot():
    """账本必须作为**上下文槽**进生产路径（否则写了没人看）。"""
    source = _read(ALLOCATOR_REL)

    assert "emotion_ledger" in source
    assert "EMOTION_LEDGER" in source
    assert "_get_emotion_ledger_section" in source
    # 槽位的内容必须**真的**来自那份账本（不能是个空壳槽）
    assert "content=emotion_ledger_content" in source


def test_the_ledger_is_read_from_the_checkpoints_table():
    """读的是 checkpoint 里那份账本（与写入路径同一张表）。"""
    src = _func_source(ALLOCATOR_REL, "_get_emotion_ledger_section")

    assert "FROM checkpoints" in src
    assert "emotion_ledger" in src
    # 列名也要对：``novel_checkpoints`` 用的是 ``novel_id``，``checkpoints`` 用的是 ``story_id``
    assert "WHERE story_id = ?" in src


def test_the_slot_is_compressible_not_critical():
    """账本是 T1（可压缩）—— 不能挤占 T0 的强制预算。"""
    src = _func_source(ALLOCATOR_REL, "_collect_all_slots")
    idx = src.find('slots["emotion_ledger"]')
    assert idx > 0
    # ⚠ 取**整个 ContextSlot(...) 块**，不用固定字符窗口 ——
    # 固定窗口会被注释长度顶掉（踩过一次：加了段说明注释后
    # ``max_tokens=`` 正好被挤出 400 字符窗口 ⇒ 守卫假红）。
    # ⚠ 也不能用 ``find(")")`` 找结尾 —— ``name="情绪账本(EMOTION_LEDGER)"``
    # 里就有括号（踩了第二次）⇒ 要**配平**地找。
    open_at = src.find("ContextSlot(", idx)
    assert open_at > idx, "没找到 emotion_ledger 的 ContextSlot("
    depth = 0
    end = -1
    for pos in range(open_at, len(src)):
        ch = src[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = pos
                break
    assert end > open_at, "ContextSlot(...) 的右括号没配平"
    block = src[idx : end + 1]
    assert "PriorityTier.T1_COMPRESSIBLE" in block
    assert "max_tokens=" in block


# =====================================================================
# 五、解析失败仍 raise（比同类健康，不动）
# =====================================================================


@pytest.mark.asyncio
async def test_a_parse_failure_still_raises():
    """解析失败**不许**用空增量伪装成功（这条比同类健康，保持不动）。"""
    llm = AsyncMock()
    llm.generate.return_value = GenerationResult(
        content="这不是 JSON",
        token_usage=TokenUsage(input_tokens=10, output_tokens=20),
    )
    extractor = EmotionLedgerExtractor(llm_service=llm)

    with pytest.raises(EmotionLedgerExtractionError):
        await extractor.extract_deltas(
            chapter_content="林羽站在雨里，师父的背叛像刀一样扎进心里。" * 10,
            chapter_number=4,
            current_ledger=EmotionLedger.create_empty(),
        )


def test_the_ledger_cap_is_pinned_by_value():
    """常量本身的值必须被钉住（每类 20 条是登记表写死的规模上限）。"""
    assert MAX_ITEMS_PER_CATEGORY == 20
