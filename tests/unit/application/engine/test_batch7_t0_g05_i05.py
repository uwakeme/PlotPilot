"""第 7 批守卫：S-06a / G-05 / I-05。

三条都是**独立**的，各有专属牙齿。

S-06a · T0 槽位自己的 max_tokens 必须生效
---------------------------------------------
V9 给每个 T0 槽位都写了上限（fact_lock 1500 / character_anchors 1500 /
context_brief 800 / …，合计 **8320** tokens），但 T0 路径**从不查** max_tokens
⇒ 那些上限形同虚设 ⇒ T0 能涨到 6 万 token
⇒ 「T0 最多占 40% 总预算」的保护**完全失效**（只砍第一个溢出槽位就 break，
其余槽位原封不动且**不计入 total** ⇒ 账面失真）。

⇒ 现在：每个 T0 槽位先按**自己的**上限裁；总量再按 40% 兜底。

G-05 · 已册角色不该被当成新角色重新提出
-------------------------------------------
兜底正则 ``re.findall(r"[一-龥]{2,4}", outline)`` 是**贪婪**的 ⇒
大纲「林风遇见沈照」被切成 ``林风遇见`` + ``沈照``
⇒ **已在册角色**「林风」变成了候选「林风遇见」。

⇒ 现在：候选若**含有**任何已知名 ⇒ 丢弃（**只减不增**，不动贪婪度 ——
非贪婪 ``{2,4}?`` 会把一切切成 2 字块 ⇒ **复姓碎裂**）。

I-05 · 每章字数的兜底只有一个入口，且**必须经过钳制**
-------------------------------------------------------
此前 19 处 ``or 2500`` 全部**绕过** 500..20000 钳制（库里存 99999 也照用）。
⇒ 现在统一走 ``resolve_chapter_words``（先夹逼、再兜底）。
"""
from __future__ import annotations

import re
from pathlib import Path

from application.core.chapter_target_limits import (
    CHAPTER_TARGET_WORDS_MAX,
    CHAPTER_TARGET_WORDS_MIN,
    DEFAULT_CHAPTER_WORDS,
    clamp_chapter_target_words,
    resolve_chapter_words,
)
from application.engine.services.context_budget_models import ContextSlot, PriorityTier
from application.engine.services.context_budget_policy import (
    TRUNCATION_MARKER,
    cap_slots_by_own_limit,
    truncate_t0_slots,
)

ROOT = Path(__file__).resolve().parents[4]


def _t0_slot(name: str, content: str, tokens: int, max_tokens: int) -> ContextSlot:
    return ContextSlot(
        name=name,
        tier=PriorityTier.T0_CRITICAL,
        content=content,
        tokens=tokens,
        max_tokens=max_tokens,
        priority=100,
    )


# ── S-06a · T0 槽位自己的 max_tokens 生效 ────────────────────────────

def test_each_t0_slot_is_capped_by_its_own_limit():
    """核心守卫：T0 槽位**必须**按自己的 max_tokens 裁。

    没有这一条 ⇒ fact_lock 能涨到 6 万 token，V9 写的上限形同虚设。
    """
    slots = {
        "fact_lock": _t0_slot("fact_lock", "字" * 97650, 65100, 1500),
        "context_brief": _t0_slot("context_brief", "字" * 5000, 3333, 800),
    }

    cap_slots_by_own_limit(slots, chars_per_token_zh=1.5)

    assert slots["fact_lock"].tokens == 1500, "fact_lock 没被裁到自己的上限 1500"
    assert slots["context_brief"].tokens == 800, "context_brief 没被裁到自己的上限 800"
    # 裁剪后必须**带说明**（这些内容会进模型可见上下文）
    assert TRUNCATION_MARKER in slots["fact_lock"].content


def test_t0_total_is_bounded_after_capping():
    """T0 总量必须有界（各槽上限之和），而不是被第一个溢出槽位拖爆。"""
    slots = {
        "lifecycle": _t0_slot("lifecycle", "a" * 900, 600, 600),
        "anchor": _t0_slot("anchor", "b" * 900, 600, 300),
        "fact_lock": _t0_slot("fact_lock", "c" * 97650, 65100, 1500),
        "brief": _t0_slot("brief", "d" * 900, 600, 800),
    }
    before = sum(s.tokens for s in slots.values())

    used = truncate_t0_slots(slots, 14000, chars_per_token_zh=1.5)

    after = sum(s.tokens for s in slots.values())
    assert before == 66900, "样本前提变了（before 变了）"
    assert after <= 14000, f"T0 仍超预算：{after}"
    assert used == after, f"账面({used})与实际({after})不一致 ⇒ 记账失真"
    assert slots["fact_lock"].tokens == 1500


def test_slots_beyond_budget_are_dropped_not_left_uncounted():
    """装不下的槽位要**舍弃并计入 total**（此前是原封不动且不计 ⇒ 记账失真）。"""
    slots = {
        "a": _t0_slot("a", "x" * 100, 100, 0),
        "b": _t0_slot("b", "y" * 5000, 5000, 0),
        "c": _t0_slot("c", "z" * 3000, 3000, 0),
    }

    used = truncate_t0_slots(slots, 200, chars_per_token_zh=1.0)

    assert used == 200
    assert sum(s.tokens for s in slots.values()) == 200, (
        "未被计入 total 的槽位还在 ⇒ 账面失真"
    )
    assert slots["c"].tokens == 0, "装不下的槽位没被舍弃"


# ── G-05 · 已册角色不该被重提 ────────────────────────────────────────

def test_known_character_glued_to_verb_is_not_a_candidate():
    """「林风遇见沈照」里的「林风遇见」**不许**成为候选。

    它含已知名「林风」⇒ 是粘连噪声，不是新角色。
    """
    from application.character.services.character_narrative_kernel import (
        CharacterNarrativeKernel,
    )

    class _Char:
        def __init__(self, name):
            self.name = name

    kernel = CharacterNarrativeKernel.__new__(CharacterNarrativeKernel)
    kernel._non_character_words = set()
    kernel._bible_location_names = lambda novel_id: set()

    candidates = kernel.detect_new_character_candidates(
        "n1", 4, "林风遇见沈照。", [_Char("林风")]
    )
    names = {c.name for c in candidates}

    assert "林风遇见" not in names, "已册角色被当成新角色重新提出"
    assert "林风" not in names, "已册角色不该出现在候选里"
    # ⚠ 真名**照常**被提取（只减不增 ⇒ 不动贪婪度）
    assert "沈照" in names, "真名被误伤了"


def test_known_location_glued_to_verb_is_not_a_candidate():
    """地点名同样要防粘连。"""
    from application.character.services.character_narrative_kernel import (
        CharacterNarrativeKernel,
    )

    class _Char:
        def __init__(self, name):
            self.name = name

    kernel = CharacterNarrativeKernel.__new__(CharacterNarrativeKernel)
    kernel._non_character_words = set()
    kernel._bible_location_names = lambda novel_id: {"天枢城"}

    candidates = kernel.detect_new_character_candidates(
        "n1", 4, "天枢城传来消息。", [_Char("林风")]
    )
    names = {c.name for c in candidates}

    assert "天枢城传来" not in names, "已册地点被粘成噪声候选"


def test_greedy_matching_is_deliberately_kept():
    """⚠ 贪婪度**故意不改** —— 非贪婪会把一切切成 2 字块 ⇒ 复姓碎裂。

    这条把「为什么没改」钉住，防止后来人「顺手优化」成非贪婪。

    ⚠ 贪婪的实际切法：``{2,4}`` 优先吃满 4 字 ⇒
    「欧阳修在此」切成 ``欧阳修在``（4 字，后面的「此」只剩 1 字不成匹配）；
    非贪婪则切成 ``欧阳`` + ``修在`` ⇒ **复姓碎了**。
    """
    assert re.findall(r"[一-龥]{2,4}", "欧阳修在此") == ["欧阳修在"], (
        "贪婪行为变了 ⇒ 复姓会被切成 2 字块"
    )
    # 非贪婪：一切都是 2 字块 ⇒ **复姓碎了**（所以不能用它）
    assert re.findall(r"[一-龥]{2,4}?", "欧阳修在此") == ["欧阳", "修在"], (
        "非贪婪会把复姓切碎 ⇒ 所以不能用它"
    )


def test_the_allocator_actually_calls_the_capping_step():
    """allocator 必须**真的调用**按上限裁剪那一步。

    ⚠ 判据要盯**那一句调用**，不是「文件里出现过这个名字」——
    ``truncate_t0_slots`` 内部也有一次幂等的裁剪 ⇒ 把 allocator 里的调用去掉
    行为上仍生效（不可观测变异）⇒ 但 allocator 那次调用负责
    **记日志与 compression_log** ⇒ 少了它就「没人知道被裁了」。

    ⇒ 所以这里断言的是**可观测性**：allocator 必须自己调一次并记下来。
    """
    path = ROOT / "application/engine/services/context_budget_allocator.py"
    code = _strip_comments(path.read_text(encoding="utf-8"))

    assert "t0_capped = self._cap_t0_slots_by_own_limit(t0_slots)" in code, (
        "allocator 没有自己调按上限裁剪 ⇒ 被裁了也没人知道（缺日志/compression_log）"
    )
    assert "T0 槽位按各自上限裁剪" in code, (
        "裁剪结果没有进 compression_log ⇒ 静默"
    )


def test_scheduler_route_passes_the_explicit_cast():
    """`character_scheduler_routes` 此前**不传** scene_director ⇒ 永远走正则兜底。

    ⇒ 现在要把「本章明确出场的角色名」传进去。

    ⚠ 判据不能只是「文件里出现过 scene_director=」——
    把它改成 ``scene_director=None`` 照样能过（假绿）⇒ 必须查**实参内容**。
    """
    path = ROOT / "interfaces/api/v1/engine/character_scheduler_routes.py"
    code = _strip_comments(path.read_text(encoding="utf-8"))

    assert "scene_director=" in code, "调度路由没有传 scene_director ⇒ 永远走正则"
    # 必须真的把 mentioned_names 塞进去（不是传 None）
    assert "mentioned_names" in code, "没有把 mentioned_names 当作显式名单传进去"
    assert "list(request.mentioned_names)" in code, (
        "scene_director 里没有装 mentioned_names ⇒ 名单仍然是空的"
    )
    assert "scene_director=None" not in code, "scene_director 被写死成 None 了"


# ── I-05 · 每章字数统一入口 ─────────────────────────────────────────

def test_resolve_chapter_words_clamps_before_defaulting():
    """越界值必须**先夹逼**（此前那 19 处兜底完全绕过钳制）。"""
    assert resolve_chapter_words(99999) == CHAPTER_TARGET_WORDS_MAX
    assert resolve_chapter_words(1) == CHAPTER_TARGET_WORDS_MIN
    assert resolve_chapter_words(None) == DEFAULT_CHAPTER_WORDS
    assert resolve_chapter_words(0) == DEFAULT_CHAPTER_WORDS
    assert resolve_chapter_words("") == DEFAULT_CHAPTER_WORDS
    assert resolve_chapter_words("abc") == DEFAULT_CHAPTER_WORDS


def test_default_is_2500_so_behaviour_is_unchanged():
    """兜底值仍是 2500 ⇒ **逐位一致**（这是「修口径」而不是「改行为」）。"""
    assert DEFAULT_CHAPTER_WORDS == 2500
    assert resolve_chapter_words(None) == 2500


#: **章目标字数**的字段名（扫「绕过钳制的兜底」时只看这些）。
#:
#: ⚠ 不含泛指的 ``target_words``（那可能是**节拍级**的字数，如
#:   ``beat_contracts`` / ``estimated_words``）—— 它们的兜底值
#:   （600 之类）不是「每章目标字数」，不在本章钳制口径内。
CHAPTER_TARGET_FIELD = re.compile(
    r"target_words_per_chapter|target_chapter_words|target_word_count", re.I
)
#: 非零的数字兜底（``or 0`` 是「未设置」，不是「兜底默认值」⇒ 不算）。
NUMERIC_FALLBACK = re.compile(r"or +[1-9][0-9]*")


def test_no_scattered_word_count_fallbacks_remain():
    """全仓（生产代码）不许再有「``or <数字>``」这种绕过钳制的兜底。

    ⚠ 复审 S1：此前只查**字面量** ``or 2500`` ⇒ ``or 3000``、双空格 ``or  2500``
    都漏（verifier 实测绿）⇒ 改成查「**任何**非零数字兜底出现在
    **章目标字数**字段上」。

    ⚠ 扫描要**剥掉注释**再查 ⇒ 否则「注释里写着 ``or 2500``」会误报，
    而注释里的字面量恰恰是最容易误导人的地方。

    ⚠ ``or 0`` 不算：它的语义是「没设置」（0），不是「兜底成某个字数」
    ⇒ 那些位置（回填/统计/回显）是**如实回显库里的值**，不归本章钳制口径。
    """
    import subprocess

    proc = subprocess.run(
        ["git", "ls-files", "--", "application/", "engine/", "interfaces/"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    offenders: list[str] = []
    for rel in (proc.stdout or "").splitlines():
        rel = rel.strip()
        if not rel.endswith(".py") or "chapter_target_limits" in rel:
            continue
        path = ROOT / rel
        try:
            code = _strip_comments(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for number, line in enumerate(code.splitlines(), 1):
            if not NUMERIC_FALLBACK.search(line):
                continue
            if not CHAPTER_TARGET_FIELD.search(line):
                continue
            offenders.append(f"{rel}:{number}: {line.strip()}")

    assert not offenders, (
        "还有绕过钳制的兜底（章目标字数上出现 ``or <非零数字>``）：\n"
        + "\n".join(offenders)
    )


def test_v1_length_tiers_uses_the_shared_constants():
    """``v1_length_tiers`` 的 100 / 2500 曾是**第二份副本** ⇒ 现在引用常量。"""
    path = ROOT / "application/core/v1_length_tiers.py"
    code = _strip_comments(path.read_text(encoding="utf-8"))

    assert "DEFAULT_TARGET_CHAPTERS" in code, "目标章数没用共享常量"
    assert "resolve_chapter_words(" in code, "每章字数没用共享入口"
    # 不许再写回字面量
    assert "else 100" not in code, "目标章数又写回了字面量 100"


# ── 工具 ─────────────────────────────────────────────────────────────

def _strip_comments(source: str) -> str:
    """剥掉注释与 docstring（判据不能落在注释上）。"""
    text = re.sub(r'"""(?:.|\n)*?"""', "", source)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return text
