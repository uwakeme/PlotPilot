"""第 41 批：抬**引擎侧总预算**与**事实锁额度**（用户 2026-10-29 拍板「真正吃 1M 窗口」）。

背景（实测，别靠推断）
----------------------
生产实测（``prodrealtest-001`` / 529 章那本）：

* 上下文包**从来没满过**：当前书 12889/35000 = 37%、长书 9073/35000 = 26%
  ⇒ ⇒ 35000 **不是**当时的瓶颈 ⇒ 抬它是**给将来留天花板**，不是现在就变贵；
* 真正的卡点是**事实锁槽位**（4000 tokens = 6000 字）：
  - 529 章那本事实锁原始 38911 字 ⇒ 只送到 6000 字（15%）；
  - 而被切掉的 87% **全是「核心事件时间线」**（2074 条流水账）
    ⇒ 硬约束节（白名单 / 身份锁 / 角色设定 / 关系）合计仅 2273 字 ⇒ **本来就 100% 送达**。

本批改了三处（**必须一起改**，少一处就白改）
------------------------------------------
1. ``DEFAULT_CONTEXT_MAX_TOKENS``：35000 → **60000**（唯一常量点）
   ⇒ T0 保护线 14000 → **24000** ⇒ 事实锁抬到 10000 tokens 后 T0 仍不顶破；
2. ``MAX_FACT_LOCK_TOKENS``：4000 → **10000**（= 15000 字）
   ⇒ 夹缝 = (正文每块上限 12000, 15000] ⇒ **宽 3000 字**；
3. ``prose_context.max_chars_per_slot``：5000 → **12000**（+ 默认值 2000 → 12000）

⚠ 守卫纪律：期望值**写死字面量**（不许用被测模块导出的常量算期望值
  ⇒ 改常量必红）；走**真实链路**（分配器那一刀 → 真实 builder）；
  样本形状按**生产实测**造（长书 = 硬约束在前、流水账在后）。
"""
from __future__ import annotations

from types import SimpleNamespace

from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.context_budget_models import (
    BudgetAllocation,
    ContextSlot,
    PriorityTier,
)
from application.engine.services.context_budget_policy import cap_slots_by_own_limit
from application.engine.services.context_builder import ContextBuilder
from application.engine.services.memory_engine import (
    FACT_LOCK_SECTION_HEADERS,
    FactLockBuilder,
)
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
)
from engine.core.services.context_budget_limits import DEFAULT_CONTEXT_MAX_TOKENS

# ── 口径值（写死 ⇒ 改常量必红）────────────────────────────────────────
#: 引擎侧上下文包总预算（第 41 批：35000 → 60000）
TOTAL_BUDGET_TOKENS = 60000
#: 事实锁槽位上限（第 41 批：4000 → 10000）
FACT_LOCK_TOKEN_CAP = 10000
#: 正文每块字符上限（第 41 批：5000 → 12000）
PROSE_SLOT_CHAR_CAP = 12000
#: 夹缝最小宽度（字）—— 夹缝太窄 ⇒ 第 24 批那条守卫的样本会「贴边」⇒ 空过
MIN_GAP_CHARS = 1000

#: 生产实测：529 章那本的事实锁原始规模（38911 字 / 21842 tokens）
LONG_BOOK_FACT_LOCK_CHARS = 38911


class _Char:
    def __init__(self, name, **kw):
        self.name = name
        self.description = kw.get("description", "")
        self.relationships = kw.get("relationships", [])
        self.public_profile = kw.get("public_profile", "")
        self.hidden_profile = kw.get("hidden_profile", "")
        self.reveal_chapter = kw.get("reveal_chapter", None)
        self.mental_state = kw.get("mental_state", "")
        self.gender = kw.get("gender", "")
        self.age = kw.get("age", "")


class _Bible:
    def __init__(self, characters, timeline_notes=None):
        self.characters = characters
        self.timeline_notes = timeline_notes or []
        self.locations = []


def _builder():
    return FactLockBuilder.__new__(FactLockBuilder)


def _long_book_shaped_bible(timeline_count: int = 700) -> _Bible:
    """长书形状：少量角色（硬约束）+ **大量**时间线条目（流水账）。

    ⚠ 形状来自**生产实测**（529 章那本）：5 个角色、2074 条时间线
    ⇒ 事实锁 38911 字 ⇒ **94% 是时间线** ⇒ 硬约束只占 2273 字。
    """
    characters = [
        _Char(
            f"角色{index}",
            description="定位" + "描述" * 20,
            public_profile="房里挂着名目的探员，同僚眼里的冷面笔杆子。" * 2,
            hidden_profile=(
                "第四张戏票此刻就在她大衣内袋，她尚未声张；她天生耳聪。"
                f"{index}。（POV防火墙：她的视角在后台认人之前，不得确知送票人身份{index}。）"
            ),
            mental_state="绷紧的孤勇",
            gender="女" if index % 2 == 0 else "男",
            age="25",
        )
        for index in range(8)
    ]
    timeline = [
        SimpleNamespace(timestamp=f"第 {index} 章", event="某件事发生了。" * 3)
        for index in range(timeline_count)
    ]
    return _Bible(characters, timeline_notes=timeline)


# ── ① 三个数字：口径钉住 ───────────────────────────────────────────────


def test_the_total_budget_is_pinned():
    """⚠ 总预算是**唯一定义点** ⇒ 改它必须改这里。"""
    assert DEFAULT_CONTEXT_MAX_TOKENS == TOTAL_BUDGET_TOKENS


def test_the_fact_lock_slot_cap_is_pinned():
    """⚠ 事实锁槽位上限**不许**退回 4000（那会让长书再丢 85%）。"""
    assert ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS == FACT_LOCK_TOKEN_CAP


def test_the_prose_slot_cap_is_pinned():
    """⚠ 正文每块上限**不许**退回 5000。"""
    settings = get_prose_context_runtime_settings()
    assert settings.max_chars_per_slot == PROSE_SLOT_CHAR_CAP


# ── ② 两级上限的关系（夹缝必须存在且够宽）──────────────────────────────


def test_the_two_caps_keep_a_wide_enough_gap():
    """⚠ 「分配器保住、builder 切掉」那个**夹缝**必须存在**且够宽**。

    ⚠ 相等 ⇒ 夹缝消失 ⇒ 第 24 批「被截掉的小节必须被点名」那条守卫会空过；
    ⚠ 太窄 ⇒ 样本贴边 ⇒ 任一侧口径一变就失效。
    """
    builder_cap = get_prose_context_runtime_settings().max_chars_per_slot
    allocator_cap_chars = int(ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS * 1.5)

    assert builder_cap < allocator_cap_chars, "两级上限相等 ⇒ 夹缝消失"
    assert allocator_cap_chars - builder_cap >= MIN_GAP_CHARS, (
        f"夹缝只有 {allocator_cap_chars - builder_cap} 字 ⇒ 样本贴边 ⇒ 会空过"
    )


def test_the_fact_lock_cap_stays_inside_the_t0_protection_line():
    """⚠ 抬事实锁槽位后，T0 需求仍要低于总预算的 40% 保护线。"""
    t0_line = int(DEFAULT_CONTEXT_MAX_TOKENS * 0.40)

    assert ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS < t0_line, (
        f"事实锁槽位 {ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS} tokens "
        f"≥ T0 保护线 {t0_line} ⇒ 会被 _truncate_t0_slots 砍回去"
    )


# ── ③ 端到端：长书形状 ⇒ 硬约束整段送达，流水账尾巴被切**并点名**──────


def _delivered_fact_lock(text: str) -> str:
    """走**真实链路**：分配器那一刀 → 真实 builder。

    ⚠ 槽位的 ``tokens`` 必须填**真实**估值（不是假的大数）⇒
      分配器才只在「真的超限」时才切 ⇒ 否则小书也会被贴上截断标记。
    """
    from domain.shared.token_estimation import estimate_tokens

    slot = ContextSlot(
        name="绝对事实边界(FACT_LOCK)",
        tier=PriorityTier.T0_CRITICAL,
        content=text,
        tokens=estimate_tokens(text),
        max_tokens=ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )
    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)
    return ContextBuilder._build_hard_facts(BudgetAllocation(slots={"fact_lock": slot}))


def test_the_long_book_keeps_its_hard_constraints_whole():
    """⚠ **本批的根因守卫**：长书里硬约束**一条都不许丢**。

    ⇒ 事实锁原始 30000+ 字 ⇒ 分配器切到 15000 字 ⇒ builder 切到 12000 字
    ⇒ 硬约束节（白名单 / 地点 / 身份锁）**全部在**（它们在最前面）
    ⇒ 被切掉的只有**时间线尾巴**（低价值流水账）。
    """
    builder = _builder()
    text = builder._build_from_bible(_long_book_shaped_bible(), current_chapter=1)
    assert len(text) > PROSE_SLOT_CHAR_CAP, "样本前提变了（没超过正文上限）"

    prose = _delivered_fact_lock(text)

    assert "角色白名单（只可使用以下有名字的角色）：" in prose, "白名单被切掉了"
    assert "身份锁死：" in prose, "身份锁死被切掉了 ⇒ POV 防火墙会跟着丢"
    assert prose.count("POV防火墙") == 8, "POV 防火墙不是 8/8"
    # ⇒ 被切掉的**只有**时间线，而且必须**被点名**（模型得知道）
    assert "被截掉的小节" in prose, "时间线被切了却没点名"
    assert "核心事件时间线" in prose.split("被截掉的小节", 1)[1][:200], "点名里没有时间线"


def test_the_long_book_delivers_far_more_than_the_old_cap():
    """⚠ 第 41 批的**收益**可量化：送达量从 5000 字 → 12000+ 字。"""
    builder = _builder()
    text = builder._build_from_bible(_long_book_shaped_bible(), current_chapter=1)

    prose = _delivered_fact_lock(text)
    fact_block = prose.split("===", 2)[-1]

    assert len(fact_block) > 10000, (
        f"送达量只有 {len(fact_block)} 字 ⇒ 抬上限没生效？"
    )


def test_the_small_book_is_still_delivered_whole():
    """⚠ 小书（当前在写的那本）**不受影响**：它的内容本来就 < 上限 ⇒ 整条送达。"""
    builder = _builder()
    bible = _Bible([_Char("沈砚青", gender="女", age="25")])

    text = builder._build_from_bible(bible, current_chapter=1)
    prose = _delivered_fact_lock(text)

    assert "已按上限截断" not in prose, "小书的事实锁被截断了（不该发生）"
    assert "沈砚青 = 女 / 25" in prose


# ── ④ 生产实测规模对得上（样本不许漂）─────────────────────────────────


def test_the_measured_long_book_size_is_still_the_reference():
    """⚠ 长书的事实锁原始规模是**实测值** ⇒ 变了就重新量（别让守卫跟着漂）。"""
    assert LONG_BOOK_FACT_LOCK_CHARS > 30000
    assert LONG_BOOK_FACT_LOCK_CHARS > PROSE_SLOT_CHAR_CAP


# ── ⑤ 生产调用点必须**引用常量**（第 32 批同族：常量对了但链上没用它）────


def test_the_fact_lock_slot_call_site_uses_the_constant():
    """⚠ **端到端的一环**：分配器给 fact_lock 槽位设上限时，必须**引用常量**。

    ⚠ 第 32 批的教训：provider 层全对、中间层也没错，但生产入口**重建**配置对象
    ⇒ 字段当场消失 ⇒ 能力等于不存在。
    ⇒ 同理：常量抬到 10000，但调用点写死 4000 ⇒ **抬了也没用**。

    ⇒ 用 **AST** 判定（不是文本 contains）：定位 ``slots["fact_lock"] = ContextSlot(...)``
    那个赋值，检查 ``max_tokens`` 的实参是 ``self.MAX_FACT_LOCK_TOKENS``（属性访问）
    ⇒ 写死字面量（4000 / 1500 / …）一律红。
    """
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[4]
    src = (path / "application/engine/services/context_budget_allocator.py").read_bytes()
    tree = ast.parse(src.decode("utf-8"))

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # 目标是 slots["fact_lock"]
        target_ok = False
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name)
                and tgt.value.id == "slots"
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "fact_lock"
            ):
                target_ok = True
        if not target_ok or not isinstance(node.value, ast.Call):
            continue
        for kw in node.value.keywords:
            if kw.arg != "max_tokens":
                continue
            value = kw.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "MAX_FACT_LOCK_TOKENS"
            ):
                found.append("constant")
            else:
                found.append(ast.dump(value)[:80])

    assert found, "没有找到 fact_lock 槽位的 max_tokens ⇒ 守卫本身失效了"
    assert found == ["constant"], (
        f"fact_lock 槽位的 max_tokens 没有引用常量（实际：{found}）"
        "⇒ 抬了常量也不会生效"
    )
