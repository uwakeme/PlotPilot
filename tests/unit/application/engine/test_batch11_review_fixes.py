"""第 11 批守卫：第二轮复审（2026-10-24）的 3 条阻塞 + 同族建议项。

复审报告：``review-briefs/2026-10-24-batch6-9-fix-review.md``（结论「**不通过**」，3 条阻塞）

三条阻塞
--------
**B0** J-02 端点在生产上 **HTTP 500** —— ``dependencies.py`` 里
    ``StylePresetMatcher(llm_service=...)``，而构造器的形参叫 ``llm``
    ⇒ TypeError ⇒ 端点直接 500 ⇒ **整个「AI 推荐文风预设」是死的**。
    ⇒ 为什么没人发现：**所有测试都用手搓 stub 直接构造**（位置参数传对了）
    ⇒ ``get_style_preset_matcher`` **全仓零测试覆盖**。
    ⇒ 这就是「集成缝」的**第三层**：实现内部 → 路由函数 → **DI 工厂/装配点**。

**B-1** 正文提示词链路把「点名被截掉的小节」**抹掉了** ——
    分配器给 fact_lock 的上限是 1500 tokens × 1.5 = **2250 字**，
    而正文槽的上限是 **2000 字** ⇒ 分配器留在**结尾**的点名正好被切没
    ⇒ 模型在正文提示词里**仍然**看不见「哪一节没了」（= 上轮 §4 的同一个病灶）。

**B-4** 点名机制**可被静默关掉**，而没有一条守卫会红：
    (d) 生产调用点把 ``section_headers=`` 改成 ``()`` ⇒ 全量绿；
    (c) 构建器把标题写死（不再用 tuple）⇒ 相关 65 条全绿。
    ⇒ 「单一来源」当时只是**约定**，不是**被守住的**不变量。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.engine.services.context_budget_models import ContextSlot, PriorityTier
from application.engine.services.context_budget_policy import (
    TRUNCATION_MARKER,
    cap_slots_by_own_limit,
)
from application.engine.services.memory_engine import FACT_LOCK_SECTION_HEADERS
from application.world.services.style_preset_match_schema import SOURCE_NONE

ROOT = Path(__file__).resolve().parents[4]
ALLOCATOR = ROOT / "application/engine/services/context_budget_allocator.py"
PERFORMANCE_YAML = ROOT / "config/performance.yaml"


def _strip_comments(source: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', "", source)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return text


# ═══════════════════════════════════════════════════════════════════
# B0 · DI 工厂那一层（第三层缝）
# ═══════════════════════════════════════════════════════════════════


def test_the_di_factory_builds_the_matcher():
    """⚠ **B0**：**DI 工厂**必须能构造出匹配器（否则端点直接 500）。

    上一轮写成了 ``StylePresetMatcher(llm_service=...)``，而构造器的形参叫
    ``llm`` ⇒ TypeError ⇒ 端点 500 ⇒ 功能整个是死的；
    而**所有测试都用手搓 stub 直接构造**（位置参数传对了）⇒ 没人走过这个工厂。
    """
    from application.world.services.style_preset_matcher import StylePresetMatcher
    from interfaces.api.dependencies import get_style_preset_matcher

    matcher = get_style_preset_matcher()

    assert isinstance(matcher, StylePresetMatcher), "DI 工厂返回的不是匹配器"


@pytest.mark.asyncio
async def test_the_route_works_with_the_real_di_factory():
    """⚠ **B0**（端到端）：路由 + **真实 DI 工厂** ⇒ 不许抛异常。

    ⚠ 这是「缝的第三层」的守卫：路由级守卫（上一轮）注入了 **stub matcher**
    ⇒ 覆盖的是「路由函数」；本条覆盖的是「**装配点**」。
    """
    from interfaces.api.dependencies import get_style_preset_matcher
    from interfaces.api.v1.world.bible import suggest_style_preset

    class _Novel:
        title = "书名"
        genre = "悬疑"
        premise = "梗概"

    class _NovelService:
        def get_novel(self, novel_id):
            return _Novel()

    class _BibleService:
        def get_bible_by_novel(self, novel_id):
            return None

    resp = await suggest_style_preset(
        "n-1",
        novel_service=_NovelService(),
        bible_service=_BibleService(),
        matcher=get_style_preset_matcher(),
    )

    assert resp.novel_id == "n-1"
    # 没有 LLM key ⇒ 走 mock ⇒ 可能匹配上、也可能「AI 判都不匹配」或未评估；
    # ⚠ 断言的是「**不许抛异常**」+ 独立的形状约束。
    # ⚠ 复审 O4：不要写 ``assessed is (source == "ai")`` 那种**从 source 推**的断言
    #   （那是自洽性，不是语义）⇒ 这里只断言**独立**的事实；
    #   三态语义由 batch10 的路由级守卫（用**已知**判定）覆盖。
    assert resp.source in ("ai", ""), f"source 不是合法取值：{resp.source!r}"
    assert resp.value is None or isinstance(resp.value, str)
    assert resp.body == "" or resp.value is not None, (
        "响应带了正文却没带命中的 value ⇒ 形状不自洽"
    )


# ═══════════════════════════════════════════════════════════════════
# B-1 · 点名必须在**开头** ⇒ 才不会被下游的二次截断切掉
# ═══════════════════════════════════════════════════════════════════


def _fake_fact_lock_text() -> str:
    """一份**五节齐全**、且**足够长**的 FACT_LOCK 文本。

    ⚠ 必须**长过**分配器的上限（1500 tokens × 1.5 = 2250 字），
    否则根本不会发生截断 ⇒ 下面两条守卫会「因样本前提不成立而空过」。
    """
    names = "、".join(f"角色{i}" for i in range(400))
    return (
        "【绝对事实边界（一旦违背即为废稿）】\n\n"
        f"角色白名单（只可使用以下有名字的角色）：\n"
        f"   允许: {names}\n"
        "   禁止: 创造任何其他有名字的角色！路人可以无名但不许命名！\n\n"
        "已死亡角色（绝对不可复活、不可在当下时间线中出现）：\n"
        "   禁止: 丁(配角) - 死于刀伤（死于第 3 章）\n\n"
        "核心关系（不可更改）：\n"
        "   甲 ——师父→ 乙\n\n"
        "身份锁死：\n"
        "   乙 = 表面是散修 | 真实身份是丙 | [已揭露] 丙才是真身\n\n"
        "核心事件时间线（不可矛盾）：\n"
        "   [第 3 章] 丁已死\n"
    )


def test_the_named_sections_are_at_the_front():
    """⚠ **B-1**：「被截掉的小节」这段必须在**开头**。

    放在结尾 ⇒ 会被**下游的二次截断**（正文槽 2000 字 < 分配器 2250 字）切没
    ⇒ 模型在正文提示词里**又**看不见「哪一节没了」（= 上轮 §4 的同一个病灶）。
    """
    slot = ContextSlot(
        name="fact_lock",
        tier=PriorityTier.T0_CRITICAL,
        content=_fake_fact_lock_text(),
        tokens=9999,
        max_tokens=1500,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )

    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)

    text = slot.content
    # 前提：白名单**保住**、身份锁死/时间线**被截掉**（复审量到的那条链）
    assert "角色白名单（只可使用以下有名字的角色）：" in text, "样本前提变了（白名单没保住）"
    assert "身份锁死：" not in text, "样本前提变了（身份锁死没被截掉）"
    assert "被截掉的小节" in text, "截断没有点名被截掉的小节"
    assert text.index("被截掉的小节") < 60, (
        "点名不在开头 ⇒ 下游任何一次截断都可能把它切掉"
    )


def _fact_lock_text_with_identity_in_window() -> str:
    """构造一份 fact_lock 文本，使「身份锁死：」落在 **2000–2250 字**窗口内。

    ⚠ 这个窗口是**硬常量**决定的：
      分配器给 fact_lock 1500 tokens × 1.5 = **2250 字**；
      正文 builder 每块上限 **2000 字** ⇒ 中间永远有 250 字的差。
    ⇒ 落在这个窗口里的小节：**分配器保住、builder 切掉** ⇒
    它是「模型看不见、也没被告知」的那一节。
    """
    names = "、".join(f"角色{i}" for i in range(360))
    head = (
        "【绝对事实边界（一旦违背即为废稿）】\n\n"
        "角色白名单（只可使用以下有名字的角色）：\n"
        f"   允许: {names}\n"
        "   禁止: 创造任何其他有名字的角色！\n\n"
    )
    identity = "身份锁死：\n   乙 = 真实身份是丙\n\n"
    timeline = "核心事件时间线（不可矛盾）：\n   [第 3 章] 丁已死\n"
    return head + identity + timeline


def test_the_real_prose_builder_names_the_sections_it_drops():
    """⚠ **B-1′**：**真的调**真实 builder ⇒ 它自己切掉的小节必须被点名。

    ⚠ 复审 B-1′：上一轮这条守卫**自己写了一遍** builder 的截断形状
    ⇒ 真实 builder 路径上的命名完整性**零覆盖**
    （变异：真实 builder 不再把 fact_lock 送进正文提示词 ⇒ 那条守卫照样绿）。

    ⇒ 现在走**完整链路**：分配器的裁剪 → **真实的** ``ContextBuilder._build_hard_facts``。
    ⚠ 纪律：**守卫要调用被测的那一层，而不是重写一遍它的形状。**
    """
    from application.engine.services.context_budget_models import BudgetAllocation
    from application.engine.services.context_builder import ContextBuilder

    original = _fact_lock_text_with_identity_in_window()
    identity_at = original.index("身份锁死：")
    assert 2000 < identity_at < 2250, (
        f"样本前提变了（身份锁死落在 {identity_at} 字，不在 2000–2250 窗口里）"
    )

    # ── 分配器那一刀（真实函数） ────────────────────────────────
    slot = ContextSlot(
        name="绝对事实边界(FACT_LOCK)",
        tier=PriorityTier.T0_CRITICAL,
        content=original,
        tokens=9999,
        max_tokens=1500,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )
    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)
    assert "身份锁死：" in slot.content, "样本前提变了（分配器没保住身份锁死）"

    # ── 正文 builder 那一刀（**真实函数**） ──────────────────────
    allocation = BudgetAllocation(slots={"fact_lock": slot})
    prose = ContextBuilder._build_hard_facts(allocation)

    assert "身份锁死：" not in prose, (
        "样本前提变了（builder 没切掉身份锁死 ⇒ 这条验不到东西）"
    )
    assert "被截掉的小节" in prose, (
        "正文提示词里**看不到**哪一节被截了 ⇒ 模型不知道「身份锁」这一节不存在"
    )
    match = re.search(r"【被截掉的小节：(.+?)】", prose, re.S)
    assert match, "点名块格式不对（外层应为【】）"
    named = match.group(1)
    assert "身份锁死" in named, "builder 自己切掉的那一节没有进点名"
    # ⚠ 点名块**只能有一块**（复审 B-2′：二次截断不该叠加两块）
    assert prose.count("【被截掉的小节：") == 1, "点名块叠成了多块"


def test_the_naming_block_stays_one_after_a_second_truncation():
    """⚠ **B-2′**：二次截断后点名块**只能有一块**（重新截断要**合并**，不是叠加）。

    ⚠ 复审 B-2′：``_mark_truncated`` 此前每次都在最前面**再加**一个点名块
    ⇒ T0 的第 2 步（按总预算再裁一次）会叠成两块
    ⇒ 守卫自己的解析约定只认第一个 ⇒ 第二个名字被吞。

    ⚠ 样本要**两层都裁**：第 1 步（按槽上限）先产出点名块，
    第 2 步（按总预算）再裁一次 ⇒ 才会走到「重新标记」那条路径。
    """
    from application.engine.services.context_budget_policy import truncate_t0_slots

    slot = ContextSlot(
        name="fact_lock",
        tier=PriorityTier.T0_CRITICAL,
        content=_fake_fact_lock_text(),
        tokens=9999,
        max_tokens=1500,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )
    # 第 1 步：按槽自己的上限裁（1500 tokens × 1.5 = 2250 字）
    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)
    assert slot.content.count("【被截掉的小节：") == 1, (
        "样本前提变了（第 1 步没产出点名块 ⇒ 这条验不到东西）"
    )

    # 第 2 步：按**总预算**再裁一次（比槽上限更小 ⇒ 必然触发）
    truncate_t0_slots({"fact_lock": slot}, budget=1000, chars_per_token_zh=1.5)

    assert slot.content.count("【被截掉的小节：") == 1, (
        "点名块叠成了多块 ⇒ 解析约定只认第一个 ⇒ 后面的名字被吞"
    )
    match = re.search(r"【被截掉的小节：(.+?)】", slot.content, re.S)
    assert match, "点名块格式不对（外层应为【】）"
    assert "身份锁死" in match.group(1), "合并后丢了已点名的名字"


def test_the_unset_word_count_is_written_as_zero_not_as_the_default():
    """⚠ **O2**：小说**没设**每章字数时，变量回填写 **0**（不是 2500）。

    ⚠ 复审 O2：S8 把两处 ``or 0`` 改走唯一入口后，**没设**也变成了 2500
    ⇒ 提示词里「每章字数」从「未设置（0）」变成「像作者设的 2500」⇒ 不诚实。
    """
    from application.core.chapter_target_limits import resolve_chapter_words_for_display

    assert resolve_chapter_words_for_display(None) == 0
    assert resolve_chapter_words_for_display(0) == 0
    assert resolve_chapter_words_for_display("") == 0
    # 设了 ⇒ 夹逼后的值
    assert resolve_chapter_words_for_display(3000) == 3000
    assert resolve_chapter_words_for_display(99999) == 20_000
    assert resolve_chapter_words_for_display(10) == 500


def test_the_named_sections_survive_the_prose_side_cut():
    """⚠ **B-1**（端到端）：走**真实的两级上限** ⇒ 点名必须仍在。

    分配器：fact_lock 1500 tokens × 1.5 = **2250 字**
    正文槽：``DEFAULT_MAX_CHARS_PER_SLOT`` = **2000 字**
    ⇒ 第二个常量比第一个小 ⇒ 结尾的点名必被切掉（这正是复审量到的那条链）。
    """
    from application.engine.services.prose_context_settings import (
        DEFAULT_MAX_CHARS_PER_SLOT,
    )

    slot = ContextSlot(
        name="fact_lock",
        tier=PriorityTier.T0_CRITICAL,
        content=_fake_fact_lock_text(),
        tokens=9999,
        max_tokens=1500,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )
    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)
    assert len(slot.content) > DEFAULT_MAX_CHARS_PER_SLOT, (
        "样本前提变了（分配器后的长度没超过正文槽上限 ⇒ 这条验不到东西）"
    )
    assert "身份锁死：" not in slot.content, "样本前提变了（身份锁死没被截掉）"

    # 正文链路的二次截断（**真实**的 builder，不是自己复刻一遍）
    from application.engine.services.context_budget_models import BudgetAllocation
    from application.engine.services.context_builder import ContextBuilder

    prose = ContextBuilder._build_hard_facts(
        BudgetAllocation(slots={"fact_lock": slot})
    )

    assert "被截掉的小节" in prose, (
        "正文提示词里**看不到**哪一节被截了 ⇒ 模型不知道「身份锁」这一节不存在"
    )
    match = re.search(r"【被截掉的小节：(.+?)】", prose, re.S)
    assert match, "标记格式不对（外层应为【】）"
    named = match.group(1)
    assert "身份锁死" in named, "点名里没有「身份锁死」"
    assert "核心事件时间线" in named, "点名里没有「核心事件时间线」"


# ═══════════════════════════════════════════════════════════════════
# B-4 · 点名机制的**两处接线**都要被守住
# ═══════════════════════════════════════════════════════════════════


def test_the_fact_lock_slot_receives_the_section_headers():
    """⚠ **B-4(d)**：生产调用点必须**真的**把 tuple 传下去。

    ⚠ 上一轮的守卫只查 ``"section_headers=" in code`` ⇒ 把值改成 ``()``
    **键名还在** ⇒ 断言照样成立 ⇒ 机制被静默关掉而全绿。
    ⇒ 现在查**完整调用**。
    """
    code = _strip_comments(ALLOCATOR.read_text(encoding="utf-8"))

    assert "section_headers=_fact_lock_section_headers()" in code, (
        "FACT_LOCK 槽位没有把分节标题传下去 ⇒ 点名机制被静默关掉"
    )


def test_the_section_headers_helper_returns_the_canonical_tuple():
    """⚠ **B-4**：辅助函数必须返回**那一份** tuple（且非空）。

    ⚠ 复审 S12：此前导入失败**静默** ``return ()`` ⇒ 点名机制无声失效
    ⇒ 现在导入失败会**告警**，且这条守卫会红。
    """
    from application.engine.services.context_budget_allocator import (
        _fact_lock_section_headers,
    )

    headers = _fact_lock_section_headers()

    assert headers, "分节标题为空 ⇒ 点名机制被静默关掉"
    assert tuple(headers) == tuple(FACT_LOCK_SECTION_HEADERS)


def test_the_builder_uses_the_same_titles_as_the_truncation():
    """⚠ **B-4(c)**：构建器产出的标题必须**就是** tuple 里那几条。

    ⚠ 上一轮没有任何守卫钉住这一点 ⇒ 构建器一旦把标题写死，
    ``_missing_sections`` 就再也认不出那一节（它按 tuple 找）
    ⇒ 点名机制静默失效，而全绿。
    """
    from application.engine.services.memory_engine import FactLockBuilder

    class _Char:
        def __init__(self, name, **kw):
            self.name = name
            self.description = kw.get("description", "")
            self.relationships = kw.get("relationships", [])
            self.public_profile = kw.get("public_profile", "")
            self.hidden_profile = kw.get("hidden_profile", "")
            self.reveal_chapter = kw.get("reveal_chapter", None)
            self.mental_state = kw.get("mental_state", "")

    class _Note:
        def __init__(self, event, timestamp=""):
            self.event = event
            self.timestamp = timestamp
            self.description = event

    class _Bible:
        characters = [
            _Char("甲"),
            _Char("乙", description="已故", public_profile="配角"),
            _Char("丙", relationships=[{"target": "甲", "type": "仇敌"}],
                  hidden_profile="真身", reveal_chapter=10),
        ]
        timeline_notes = [_Note("丁已死", "第 3 章")]

    builder = FactLockBuilder.__new__(FactLockBuilder)
    text = builder._build_from_bible(_Bible(), current_chapter=1)

    produced = [line for line in text.splitlines() if line.endswith("：")]
    for header in FACT_LOCK_SECTION_HEADERS:
        assert header in text, f"构建器的输出里没有 tuple 里的标题：{header}"
    for line in produced:
        assert line in FACT_LOCK_SECTION_HEADERS, (
            f"构建器产出了 tuple 之外的标题：{line!r} ⇒ 点名认不出这一节"
        )


# ═══════════════════════════════════════════════════════════════════
# S13 / S14 · 超时的值有人守 + 守卫不许「因错误的原因而绿」
# ═══════════════════════════════════════════════════════════════════


def test_the_style_preset_timeout_is_thirty_seconds():
    """⚠ **S13**：超时的值必须**有人守**（改成 300 或 5 都不该放行）。

    ⚠ 复审 S2：这个「30」此前是服务里的**硬编码**，前端那个是 env 可配
    ⇒ 现在后端也从 ``config/performance.yaml`` 读。
    """
    import yaml

    from application.world.services.style_preset_matcher import (
        DEFAULT_TIMEOUT_SECONDS,
        resolve_match_timeout_seconds,
    )

    config = yaml.safe_load(PERFORMANCE_YAML.read_text(encoding="utf-8")) or {}
    section = config.get("style_preset_match") or {}

    assert int(section.get("timeout_seconds", 0)) == 30, (
        "config/performance.yaml 里的 style_preset_match.timeout_seconds 不是 30"
    )
    assert DEFAULT_TIMEOUT_SECONDS == 30
    assert resolve_match_timeout_seconds() == 30, "解析出来的超时不是 30"


def test_the_style_preset_timeout_really_comes_from_the_config():
    """⚠ **B-3′**：「值是 30」与「值**来自配置**」是两件事。

    ⚠ 复审 B-3′：上一条断言的是「YAML 里是 30」+「解析出来是 30」，
    而**默认值恰好也是 30** ⇒ 两者**无法区分** ⇒ 复审的变异
    （把配置读取路径打断 ⇒ ``section = None``）**全绿**。

    ⇒ 这条**改配置、让解析值跟着变** ⇒ 「来自配置」这条承诺才有人守。
    """
    from application.core.config.config_loader import reload_config
    from application.world.services.style_preset_matcher import (
        resolve_match_timeout_seconds,
    )

    original = PERFORMANCE_YAML.read_text(encoding="utf-8")
    tmp = PERFORMANCE_YAML.with_name("performance.__j02_test__.yaml")
    try:
        tmp.write_text(
            original.replace(
                "style_preset_match:\n  timeout_seconds: 30",
                "style_preset_match:\n  timeout_seconds: 45",
            ),
            encoding="utf-8",
        )
        reload_config(str(tmp))
        assert resolve_match_timeout_seconds() == 45, (
            "改了配置但解析值没跟着变 ⇒ 超时**不是**从配置读的"
        )
    finally:
        tmp.unlink(missing_ok=True)
        reload_config()  # 恢复默认配置（别把测试环境留在临时文件上）


def test_the_real_config_disables_no_features():
    """⚠ **S14**：真实配置里 ``disabled_features`` 必须是空的。

    ⚠ 否则服务级守卫会因为「能力被关掉」而**因错误的原因而绿**
    （它压根不会调模型）。
    """
    import yaml

    config = yaml.safe_load(PERFORMANCE_YAML.read_text(encoding="utf-8")) or {}
    section = config.get("ai_participation") or {}

    assert section.get("master_enabled") is True, "总闸在真实配置里是关的"
    assert not (section.get("disabled_features") or []), (
        "真实配置里关掉了 AI 参与能力 ⇒ 服务级守卫会因错误的原因而绿"
    )


@pytest.mark.asyncio
async def test_the_service_calls_the_model(monkeypatch):
    """⚠ **S14**：服务级守卫必须断言「模型确实被调过」。

    ⚠ 上一轮那条守卫**从不断言模型被调用过** ⇒ 若能力被列进
    ``disabled_features``，它会**因错误的原因而绿**。
    """
    import application.world.services.style_preset_matcher as mod
    from application.world.services.style_preset_match_schema import SOURCE_AI

    class _FakeGateway:
        def render(self, contract, variables):
            return SimpleNamespace(
                prompt=SimpleNamespace(system="sys", user="usr"),
                node_key=contract.node_key,
            )

        def validate_output(self, contract, payload):
            return contract.output_schema.model_validate(payload)

    class _StubLLM:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, config):
            self.calls += 1
            return SimpleNamespace(content='{"value": "mystery", "reason": "线索回收"}')

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway())
    llm = _StubLLM()
    svc = mod.StylePresetMatcher(llm)

    suggestion = await svc.suggest(novel_id="n-1")

    assert llm.calls == 1, "模型压根没被调用（守卫会因错误的原因而绿）"
    assert suggestion.source == SOURCE_AI
    assert suggestion.value == "mystery"
