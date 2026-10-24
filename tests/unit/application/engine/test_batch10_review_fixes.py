"""第 10 批守卫：复审（2026-10-23）的四条阻塞 + 同族建议项。

复审报告：``review-briefs/2026-10-23-batch6-9-review.md``

四条阻塞
--------
**B1** J-02 的头号不变量「清单外的 value ⇒ 未评估，**不回落到第一个预设**」
    **无守卫**：复审把服务里的 ``preset_lookup`` 换成「取不到就回落
    ``get_style_presets()[0]``」⇒ **全套 3664 条照样绿**。
    ⇒ 此前只有**文件内容级**守卫，没有**穿过集成缝**的行为级守卫。
    路由 handler 的函数体更是**从未被任何测试执行过**。

**B2** 画像里的 ``timeout_seconds`` / ``retries`` **全仓无人读**
    ⇒ 简报建议的修法是**空操作**；真实上限在 provider 层
    （``LLM_MAX_TOTAL_ATTEMPTS`` × ``Settings.timeout_seconds``）⇒ 最坏 ~900s，
    而前端 30s 就放弃 ⇒ 用户看到失败、后端还在烧 token。

**B3** G-05 的「含有已知名即丢弃」把**它自己的补救**（explicit AI 名单）
    **一起废掉了** ⇒ AI 明确点名的「林风华」被丢。

**B4** I-05 的「唯一入口」不成立 —— **默认生产路径**
    （``writing_delegate.py``）仍绕过 500..20000 钳制。

⚠ 本文件的核心纪律（复审 §10 提的）：
**每个「已做」条目的头号不变量，必须有一条穿过集成缝的行为级守卫**
（从入口一路走到出口，断言输出），而不是只测那一层的内部函数。
⇒ B1 的两条（服务级 + 路由级）就是按这条写的。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.core.chapter_target_limits import resolve_chapter_words
from application.engine.services.context_budget_models import ContextSlot, PriorityTier
from application.engine.services.context_budget_policy import (
    TRUNCATION_MARKER,
    cap_slots_by_own_limit,
    truncate_t0_slots,
)
from application.world.services.style_preset_match_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    StylePresetMatchLlmPayload,
    StylePresetSuggestion,
    normalize_suggestion,
)
from application.world.style_preset_catalog import (
    get_style_preset,
    get_style_presets,
    style_preset_values,
)

ROOT = Path(__file__).resolve().parents[4]
PROFILES = ROOT / "config/generation_profiles.yaml"
WRITING_DELEGATE = ROOT / "engine/runtime/writing_delegate.py"
ENGINE_DAEMON = ROOT / "engine/runtime/engine_daemon.py"


def _strip_comments(source: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', "", source)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return text


# ═══════════════════════════════════════════════════════════════════
# B1 · J-02 头号不变量的**集成缝**守卫
# ═══════════════════════════════════════════════════════════════════


class _FakeGateway:
    """打桩网关：校验仍走真实契约。"""

    def __init__(self):
        self.variables: dict = {}

    def render(self, contract, variables):
        self.variables = dict(variables)
        return SimpleNamespace(
            prompt=SimpleNamespace(system="sys", user="usr"), node_key=contract.node_key
        )

    def validate_output(self, contract, payload):
        return contract.output_schema.model_validate(payload)


class _StubLLM:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    async def generate(self, prompt, config):
        self.calls += 1
        return SimpleNamespace(content=self._content)


def _service(monkeypatch, content: str):
    import application.world.services.style_preset_matcher as mod

    gateway = _FakeGateway()
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: gateway)
    return mod.StylePresetMatcher(_StubLLM(content)), gateway


@pytest.mark.asyncio
async def test_the_service_never_falls_back_to_the_first_preset(monkeypatch):
    """⚠ **B1**：服务收到清单外的 value ⇒ 必须是「未评估」，**不许**回落第一个预设。

    复审的变异（``style_preset_matcher.py`` 里把 ``preset_lookup`` 换成
    「取不到就回落 ``get_style_presets()[0]``」）⇒ **全套 3664 条照样绿**。
    ⇒ 这条是**穿过集成缝**（模型输出 → 服务 → 结果）的行为级守卫。
    """
    svc, _ = _service(
        monkeypatch,
        json.dumps({"value": "xianxia_hot_v2", "reason": "听起来很像"}, ensure_ascii=False),
    )

    suggestion = await svc.suggest(novel_id="n-1")

    assert suggestion.source == SOURCE_NONE, (
        "清单外的 value 被当成了判定 ⇒ 回落到了预设（或别的）"
    )
    assert suggestion.value is None, "清单外的 value 被塞进了结果"
    assert suggestion.body == "", "清单外的 value 带出了正文"
    assert suggestion.label == ""


@pytest.mark.asyncio
async def test_the_route_passes_the_verdict_through():
    """⚠ **B1**：路由 handler **不许自己编** ``source`` / ``assessed`` / ``value``。

    复审的 M2（路由里硬写 ``source='ai'``）、M3（硬写 ``value=None``）都漏
    ⇒ 此前**路由 handler 的函数体从未被任何测试执行过**。
    ⇒ 这条直接调用路由函数，断言响应**透传**服务给的判定。
    """
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

    class _Matcher:
        async def suggest(self, **kwargs):
            return StylePresetSuggestion(
                value=None, reason="候选都不合适", source=SOURCE_AI
            )

    resp = await suggest_style_preset(
        "n-1",
        novel_service=_NovelService(),
        bible_service=_BibleService(),
        matcher=_Matcher(),
    )

    assert resp.source == SOURCE_AI, "路由没有透传 source"
    assert resp.assessed is True, "路由没有透传 assessed"
    assert resp.matched is False
    assert resp.value is None
    assert resp.reason == "候选都不合适", "路由没有透传 reason"


@pytest.mark.asyncio
async def test_the_route_passes_a_matched_verdict_through():
    """⚠ **B1**：路由**不许自己编** ``value`` / ``label`` / ``body``。

    ⚠ 载荷必须是「**匹配到了**」的那一种：若载荷本身 ``value=None``，
    则「路由把 value 写死成 None」是**不可观测的**（结果一样）⇒ 守卫抓不到。
    ⇒ 这里用**命中**的载荷（``value="mystery"``）⇒ 路由自己编就会被抓。
    """
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

    class _Matcher:
        async def suggest(self, **kwargs):
            return StylePresetSuggestion(
                value="mystery",
                label="悬疑·线索回收",
                icon="疑",
                body="【文风公约·悬疑】……",
                reason="线索回收与真凶反转是主线",
                source=SOURCE_AI,
            )

    resp = await suggest_style_preset(
        "n-1",
        novel_service=_NovelService(),
        bible_service=_BibleService(),
        matcher=_Matcher(),
    )

    assert resp.value == "mystery", "路由没有透传 value"
    assert resp.label == "悬疑·线索回收", "路由没有透传 label"
    assert resp.body == "【文风公约·悬疑】……", "路由没有透传 body"
    assert resp.matched is True
    assert resp.source == SOURCE_AI
    assert resp.assessed is True


@pytest.mark.asyncio
async def test_the_route_does_not_turn_an_unassessed_result_into_a_match():
    """⚠ **B1**（反向）：服务说「未评估」时，路由**不许**把它变成「匹配到了」。"""
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

    class _Matcher:
        async def suggest(self, **kwargs):
            return StylePresetSuggestion(
                value=None, reason="模型调用失败，未评估", source=SOURCE_NONE
            )

    resp = await suggest_style_preset(
        "n-1",
        novel_service=_NovelService(),
        bible_service=_BibleService(),
        matcher=_Matcher(),
    )

    assert resp.source == SOURCE_NONE, "路由把「未评估」改写成了别的来源"
    assert resp.assessed is False
    assert resp.matched is False
    assert resp.value is None


# ═══════════════════════════════════════════════════════════════════
# B2 · 超时是**真的**（不是画像里的装饰品）
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_slow_model_is_cut_off_at_the_hard_timeout(monkeypatch):
    """⚠ **B2**：模型慢 ⇒ 必须**自己**放弃（超时按「未评估」处理，不编造）。

    复审查明：画像里的 ``timeout_seconds`` / ``retries`` **全仓无人读**
    ⇒ 真实上限在 provider 层（~900s）⇒ 而前端 30s 就放弃
    ⇒ 「用户看到失败、后端还在烧 token」。
    ⇒ 服务必须自己有一道**真**的硬闸。
    """
    import application.world.services.style_preset_matcher as mod

    class _SlowLLM:
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, config):
            self.calls += 1
            await asyncio.sleep(5)
            return SimpleNamespace(content="{}")

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway())
    # ⚠ 复审 S2：超时值现在从 config/performance.yaml 读 ⇒ 打桩**解析函数**
    monkeypatch.setattr(mod, "resolve_match_timeout_seconds", lambda: 0.05)
    llm = _SlowLLM()
    svc = mod.StylePresetMatcher(llm)

    suggestion = await svc.suggest(novel_id="n-1")

    assert llm.calls == 1, "模型压根没被调用（探针无效）"
    assert suggestion.source == SOURCE_NONE, "超时被当成判定了"
    assert suggestion.value is None
    assert "超时" in suggestion.reason, f"超时的理由没说清：{suggestion.reason}"


def test_the_dead_profile_keys_are_gone():
    """⚠ **B2**：画像里那两个**无人读**的键必须删掉（留着就是骗人）。

    复审的原话：「上一份审查简报就信了」⇒ 说明留着它们会持续误导。
    """
    import yaml

    profiles = yaml.safe_load(PROFILES.read_text(encoding="utf-8")) or {}

    offenders = [
        name
        for name, profile in profiles.items()
        if isinstance(profile, dict)
        and ("timeout_seconds" in profile or "retries" in profile)
    ]
    assert not offenders, (
        f"这些画像还留着全仓无人读的键（timeout_seconds / retries）：{offenders}"
    )


# ═══════════════════════════════════════════════════════════════════
# B3 · explicit AI 名单不被「含有已知名」过滤
# ═══════════════════════════════════════════════════════════════════


def _kernel(known: list[str], locations: set[str] | None = None):
    from application.character.services.character_narrative_kernel import (
        CharacterNarrativeKernel,
    )

    class _Char:
        def __init__(self, name):
            self.name = name

    kernel = CharacterNarrativeKernel.__new__(CharacterNarrativeKernel)
    kernel._non_character_words = set()
    kernel._bible_location_names = lambda novel_id: set(locations or set())
    return kernel, _Char


def test_the_explicit_ai_cast_is_not_filtered_by_the_known_name_rule():
    """⚠ **B3**：AI **明确点名**的角色不该被「含有已知名」误杀。

    复审的发现：explicit 名单与正则候选被拼进同一个循环 ⇒ 「含有」过滤
    对 explicit **同样生效** ⇒ AI 点的「林风华」（含已知名「林风」）被丢
    ⇒ **G-05 的另一半补救（路由传 mentioned_names）被自己废掉了**。
    """
    kernel, _Char = _kernel(["林风"])

    candidates = kernel.detect_new_character_candidates(
        "n1",
        4,
        "",
        [_Char("林风")],
        scene_director={"characters": ["林风华"]},
    )
    names = {c.name for c in candidates}

    assert "林风华" in names, "AI 明确点名的角色被「含有已知名」过滤误杀了"


def test_the_explicit_ai_cast_still_cannot_reintroduce_a_known_character():
    """⚠ **B3**（边界）：explicit 里**恰好就是**已知名 ⇒ 仍不许重提。

    「过滤只对正则兜底生效」不等于「explicit 可以复活已册角色」——
    已册角色走的是下面那条精确匹配（``name in known_names``）。
    """
    kernel, _Char = _kernel(["林风"])

    candidates = kernel.detect_new_character_candidates(
        "n1",
        4,
        "",
        [_Char("林风")],
        scene_director={"characters": ["林风"]},
    )
    names = {c.name for c in candidates}

    assert "林风" not in names, "已册角色被 explicit 名单重新提出来了"


def test_the_regex_fallback_still_drops_the_glued_noise():
    """⚠ **B3**（边界）：正则兜底的「粘连噪声」仍要丢（别把过滤整个拆掉）。"""
    kernel, _Char = _kernel(["林风"])

    candidates = kernel.detect_new_character_candidates(
        "n1", 4, "林风遇见沈照。", [_Char("林风")]
    )
    names = {c.name for c in candidates}

    assert "林风遇见" not in names, "粘连噪声又被放回来了"
    assert "沈照" in names, "真名被误伤了"


# ═══════════════════════════════════════════════════════════════════
# B4 · 默认生产路径也走唯一入口
# ═══════════════════════════════════════════════════════════════════


def test_the_default_writing_path_uses_the_shared_entry():
    """⚠ **B4**：默认写作路径必须走 ``resolve_chapter_words``（不许再绕）。"""
    code = _strip_comments(WRITING_DELEGATE.read_text(encoding="utf-8"))

    assert "resolve_chapter_words(" in code, "默认写作路径没走唯一入口"
    assert "or runner.DEFAULT_TARGET_WORDS" not in code, (
        "默认写作路径仍在用 ``or`` 兜底 ⇒ 绕过 500..20000 钳制"
    )


def test_the_default_path_really_is_the_one_we_guarded():
    """⚠ **B4**：上面那条守的**确实是默认路径**（否则守了个冷门分支）。

    ⚠ 必须查**完整 import 语句** ⇒ 只查 ``"writing_delegate" in code`` 会被
    ``legacy_writing_delegate`` **子串命中** ⇒ 假绿。
    """
    code = _strip_comments(ENGINE_DAEMON.read_text(encoding="utf-8"))

    assert (
        "from engine.runtime.writing_delegate import get_story_pipeline_mode" in code
    ), "守护进程没走 writing_delegate ⇒ 上一条守的可能不是默认路径"


def test_the_fallback_path_also_uses_the_shared_entry():
    """应急回退路径（legacy）同样走唯一入口（此前已改，守住它）。"""
    path = ROOT / "engine/runtime/legacy_writing_delegate.py"
    code = _strip_comments(path.read_text(encoding="utf-8"))

    assert "resolve_chapter_words(" in code, "legacy 回退路径没走唯一入口"


# ═══════════════════════════════════════════════════════════════════
# 建议项 S2 / S5 / S6 / S7 / S8 / S9 / §4
# ═══════════════════════════════════════════════════════════════════


def test_a_float_word_count_is_not_worse_than_before():
    """⚠ **S2**：``3000.0`` 此前会因 ``int("3000.0")`` 抛异常而落到 2500
    ⇒ **比改造前更差**（改造前 ``or 2500`` 会保留 3000.0）。"""
    assert resolve_chapter_words(3000.0) == 3000
    assert resolve_chapter_words("3000.0") == 3000
    assert resolve_chapter_words("3000") == 3000
    assert resolve_chapter_words(99999.0) == 20_000
    assert resolve_chapter_words("abc") == 2500


def test_dropped_t0_slots_are_recorded():
    """⚠ **S5**：装不下的槽位被**舍弃**时必须留痕（此前是清零且无记录）。"""
    slots = {
        # 「a」正好装满预算 ⇒ 「b」装不下 ⇒ 被**舍弃**（而不是被截半）
        "a": ContextSlot(name="a", tier=PriorityTier.T0_CRITICAL, content="x" * 200, tokens=200),
        "b": ContextSlot(name="b", tier=PriorityTier.T0_CRITICAL, content="y" * 5000, tokens=5000),
    }
    log: list[str] = []

    truncate_t0_slots(slots, 200, chars_per_token_zh=1.0, compression_log=log)

    assert slots["b"].tokens == 0, "装不下的槽位没被舍弃"
    assert log, "被舍弃的槽位没有记进 compression_log ⇒ 静默"
    assert "b" in log[0], f"日志没点名被舍弃的槽位：{log}"


def test_the_truncation_marker_names_the_dropped_sections():
    """⚠ **§4**：FACT_LOCK 被截时，**点名被截掉的小节**。

    复审的独立发现：``fact_lock`` 截到 2250 字符时，
    白名单/死亡锁/核心关系都保住，但**身份锁死**与**核心事件时间线**被整段截掉
    ⇒ 而标记只说「仅是部分内容」⇒ 模型**不知道「身份锁」这一节存在**
    ⇒ 它不会去遵守它。
    """
    content = (
        "角色白名单（只可使用以下有名字的角色）：\n   允许: 甲\n\n"
        "身份锁死：\n   乙的真实身份是丙\n\n"
        "核心事件时间线（不可矛盾）：\n   第 3 章：丁已死\n"
    )
    headers = (
        "角色白名单（只可使用以下有名字的角色）：",
        "身份锁死：",
        "核心事件时间线（不可矛盾）：",
    )
    slot = ContextSlot(
        name="fact_lock",
        tier=PriorityTier.T0_CRITICAL,
        content=content,
        tokens=1000,
        # 20 tokens × 1.5 = 30 字符 ⇒ 白名单标题（21 字）保住，
        # 而「身份锁死：」在第 41 字 ⇒ 整段被截掉。
        max_tokens=20,
        priority=120,
        section_headers=headers,
    )

    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)

    text = slot.content
    assert TRUNCATION_MARKER in text
    assert "被截掉的小节" in text, "截断没说清哪几节没了"
    # ⚠ 复审 B-1：点名现在在**开头** ⇒ 取名要用「标记本身」那一段，
    #   不能 split 整个内容（那样会把保留部分也算进去）。
    #   ⚠ 外层是【】⇒ 小节标题里的「）」不会把标记切坏。
    match = re.search(r"【被截掉的小节：(.+?)】", text, re.S)
    assert match, "标记格式不对（外层应为【】）"
    named = match.group(1)
    assert "身份锁死" in named, "没点名被截掉的小节"
    assert "核心事件时间线" in named, "没点名被截掉的小节"
    # 保住的节不该被点名成「被截掉」
    assert "角色白名单" not in named
    # 且标记里的名单应与「实际缺掉的标题」**一致**
    kept = text.split(TRUNCATION_MARKER)[0]
    expected = {h for h in headers if h in content and h not in kept}
    for header in expected:
        assert header.rstrip("：") in named, f"被截掉的小节里没有点名 {header}"
    for header in headers:
        if header not in expected:
            assert header.rstrip("：") not in named, (
                f"保住的节被误报成被截掉：{header}"
            )


def test_the_fact_lock_slot_declares_its_section_headers():
    """⚠ **§4**：FACT_LOCK 槽位必须**声明**它的分节标题（否则点名机制用不上）。"""
    from application.engine.services.memory_engine import FACT_LOCK_SECTION_HEADERS

    assert "身份锁死：" in FACT_LOCK_SECTION_HEADERS
    assert "核心事件时间线（不可矛盾）：" in FACT_LOCK_SECTION_HEADERS

    path = ROOT / "application/engine/services/context_budget_allocator.py"
    code = _strip_comments(path.read_text(encoding="utf-8"))
    assert "section_headers=" in code, "FACT_LOCK 槽位没有声明分节标题"


def test_the_write_side_trim_count_is_shown():
    """⚠ **S6**：写入侧因规模上限丢弃的条数，此前只落库、从不展示。"""
    from engine.core.value_objects.emotion_ledger import (
        EmotionalWound,
        EmotionLedger,
    )

    ledger = EmotionLedger.create_empty()
    for i in range(25):
        ledger = ledger.add_wound(EmotionalWound(description=f"伤{i}", impact="影响"))
    assert ledger.trimmed_items == 5

    section = ledger.to_t0_section()
    assert "未保留" in section, "写入侧丢弃的条数没有在注入文本里说出来"
    # ⚠ 复审 S11：措辞要与注入侧那两句**区分开**
    #   （「写入时即被丢弃」≠「这次没列」）⇒ 断言关键的区分词。
    assert "另有 5 条记录在**写入账本时**即被丢弃" in section, (
        f"没说清丢了几条、也没与「未列」区分开：{section.splitlines()[-1]}"
    )


def test_a_null_value_means_the_ai_said_no_match():
    """⚠ **S8**：``{"value": null, "reason": ...}`` 必须算「AI 判都不匹配」。

    此前 ``value: str`` ⇒ ``null`` 过不了 pydantic ⇒ 落成「未评估」
    ⇒ **「AI 说都不匹配」这一态几乎不可达**（而 ``null`` 是模型更常用的写法）。
    """
    payload = StylePresetMatchLlmPayload.model_validate(
        {"value": None, "reason": "候选都不合适"}
    )
    suggestion = normalize_suggestion(payload.model_dump(), preset_lookup=get_style_preset)

    assert suggestion.source == SOURCE_AI
    assert suggestion.value is None
    assert suggestion.assessed is True


def test_an_alias_is_accepted_as_a_match():
    """⚠ **S9**：模型照抄**别名**（``仙侠``）也该命中（别名在清单内）。

    复审的发现：模型看到的是候选行里的别名，但 ``get_style_preset`` 只认
    ``value`` ⇒ 一份完全正确的回答会被判成「清单外的值 ⇒ 未评估」。
    """
    preset = get_style_preset("仙侠")

    assert preset is not None, "别名没被接受"
    assert preset.value == "xianxia_hot"


def test_the_aliases_are_unique_across_the_presets():
    """⚠ **S9**：别名必须**唯一**（否则「按别名匹配」有歧义）。"""
    seen: dict[str, str] = {}
    for preset in get_style_presets():
        for alias in preset.aliases:
            assert alias not in seen, (
                f"别名「{alias}」同时属于 {seen.get(alias)} 与 {preset.value}"
            )
            seen[alias] = preset.value


def test_the_plan_envelope_clamps_the_word_count():
    """⚠ **S7**：``PlanningEnvelope`` 的上界要与规范一致（此前 100000）。

    ⚠ 复审 S6：区间与解析现在**跟唯一入口一致**（[500, 20000]、`float` 解析）⇒
    探针：10 → 500、0 → 2500、``"3000.0"`` → 3000（与 ``resolve_chapter_words`` 逐位一致）。
    """
    from application.engine.dag.plan.schema import PlanningEnvelope

    assert PlanningEnvelope(target_chapter_words=99999).target_chapter_words == 20_000
    assert PlanningEnvelope(target_chapter_words=10).target_chapter_words == 500
    assert PlanningEnvelope(target_chapter_words=0).target_chapter_words == 2500
    assert PlanningEnvelope(target_chapter_words="3000.0").target_chapter_words == 3000
    assert PlanningEnvelope().target_chapter_words == 2500


def test_no_bare_ellipsis_reaches_the_model_visible_prompts():
    """⚠ **S12**：截断**从不静默**不是全仓的 —— 这两处此前仍是裸 ``"..."``。

    ⚠ 复审 S10：判据只认**双引号** ⇒ ``+ '...'`` / ``+ f"..."`` 都漏
    ⇒ 现在认**任意引号**与 **f/r 前缀**。
    """
    for rel in (
        "application/blueprint/services/beat_sheet_service.py",
        "application/core/services/scene_generation_service.py",
    ):
        code = _strip_comments((ROOT / rel).read_text(encoding="utf-8"))
        hits = re.findall(r"""(?:f|r|rf|fr)?["']\.\.\.["']""", code)
        assert not hits, (
            f"{rel} 里还有裸的省略号字面量（{hits}）⇒ 它会进模型可见提示词"
        )


def test_the_style_preset_values_are_the_only_accepted_identifiers():
    """清单封闭的**基准**：模型返回的标识符必须来自清单（不许回落）。"""
    values = style_preset_values()

    assert len(values) == 6
    assert len(set(values)) == len(values)
    for value in values:
        assert get_style_preset(value) is not None
