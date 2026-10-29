"""第 24 批：**身份锁的预算与截断**（根因：硬约束没送到模型）。

背景（生产实测，第 21 批）
--------------------------
第 5 章正文提示词里，事实锁的「身份锁死」这一节**被从中间切断** ⇒
**后 4 个角色（郑铁珊/魏长庚/贺九/郎世安）的硬约束根本没送到模型**
（已揭露的秘密 + POV 防火墙）⇒ 模型可以自由发挥
⇒ 而第 21 批 A1 审计抓到的「郑铁珊被无解释赋予写票能力」「郑铁珊的性别矛盾」
**根因就在这里**（审计侧拿的是**完整**的基准 ⇒ 审得出、生成时没锁住）。

⚠ 三件事必须同时成立：
  ① **POV 防火墙完整保留**（限长只作用于秘密主体 ⇒ 不许砍到防火墙中间）
  ② **截断按行边界**（不许切半条约束 ⇒ 半句防火墙会被当成完整设定）
  ③ **截断在开头留痕**（模型必须知道「本块不完整」
     ⇒ 结尾那个标记会被下游的二次截断切掉）

⚠ 守卫纪律：走**真实**的 builder / 真实的截断函数，不复刻任何一层的形状。
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from application.engine.services.context_budget_policy import (
    TRUNCATION_MARKER,
    TRUNCATION_NOTICE,
    mark_truncated,
)
from application.engine.services.memory_engine import (
    FACT_LOCK_SECTION_HEADERS,
    IDENTITY_MENTAL_MAX_CHARS,
    IDENTITY_PUBLIC_MAX_CHARS,
    IDENTITY_SECRET_MAX_CHARS,
    FactLockBuilder,
)

#: 与仓库里其它用例都不同的名字（避免「碰巧命中」）
SECRET_MARK = "第四张戏票此刻就在她大衣内袋"
POV_MARK = "她的视角在后台认人之前，不得确知送票人身份"
PUBLIC_MARK = "房里挂「特别员」名目的女探员"


class _Char:
    def __init__(self, name, **kw):
        self.name = name
        self.description = kw.get("description", "")
        self.relationships = kw.get("relationships", [])
        self.public_profile = kw.get("public_profile", "")
        self.hidden_profile = kw.get("hidden_profile", "")
        self.reveal_chapter = kw.get("reveal_chapter", None)
        self.mental_state = kw.get("mental_state", "")


class _Bible:
    def __init__(self, characters, timeline_notes=None):
        self.characters = characters
        self.timeline_notes = timeline_notes or []


def _builder():
    return FactLockBuilder.__new__(FactLockBuilder)


def _hidden(secret: str, pov: str) -> str:
    return f"{secret}。（POV防火墙：{pov}。）"


# ── ① POV 防火墙完整保留 ───────────────────────────────────────────────


def _identity_line(text: str, name: str) -> str:
    """取**身份锁死**那一节里的那一行（⚠ 不是「角色设定」节的同名行）。"""
    in_identity = False
    for line in text.splitlines():
        if "身份锁死" in line:
            in_identity = True
            continue
        if in_identity:
            if not line.strip():
                break
            if line.strip().startswith(name):
                return line
    return ""


def test_the_pov_firewall_is_kept_whole_even_when_the_secret_is_truncated():
    """⚠ 限长只作用于**秘密主体** ⇒ POV 防火墙**不许**被砍掉或砍半。"""
    builder = _builder()
    long_secret = SECRET_MARK + "补" * 300
    bible = _Bible(
        [
            _Char(
                "沈砚青",
                public_profile=PUBLIC_MARK,
                hidden_profile=_hidden(long_secret, POV_MARK),
            )
        ]
    )

    text = builder._build_from_bible(bible, current_chapter=1)

    assert POV_MARK in text, "POV 防火墙被砍掉了 ⇒ 违反即人物失格的那条约束没了"
    # 秘密主体被限长了
    line = _identity_line(text, "沈砚青")
    assert line, "身份锁死那一节里没有这一行"
    assert SECRET_MARK in line, "限长把秘密的开头也砍了"
    assert "…" in line, "秘密主体被限长却没有留痕"


def test_the_secret_body_is_limited():
    """秘密主体限长 ⇒ 事实锁不会因为秘密太长而把整节挤爆。"""
    builder = _builder()
    bible = _Bible(
        [_Char("甲", hidden_profile=_hidden(SECRET_MARK + "补" * 300, POV_MARK))]
    )

    text = builder._build_from_bible(bible, current_chapter=1)

    line = [l for l in text.splitlines() if "甲 =" in l][0]
    secret_part = line.split("[已揭露]")[1]
    assert len(secret_part) <= IDENTITY_SECRET_MAX_CHARS + len(POV_MARK) + 12


def test_a_character_with_only_a_public_profile_still_has_an_identity_line():
    """⚠ 公开面**不能删** —— 删了会让这种角色**整行消失**（信息丢失）。

    ⚠ 断言必须落在**身份锁死那一节**里：公开面在「角色设定」节里也有
    （限长 60 字）⇒ 只查「全文含这个字符串」会**假绿**。
    """
    builder = _builder()
    bible = _Bible([_Char("乙", public_profile=PUBLIC_MARK)])

    text = builder._build_from_bible(bible, current_chapter=1)

    line = _identity_line(text, "乙")
    assert line, (
        "只有公开面的角色在**身份锁死**那一节里整行消失了"
        f"（全文里有：{PUBLIC_MARK in text}）"
    )
    assert PUBLIC_MARK in line


def test_the_public_profile_is_limited_but_kept():
    """公开面限长（省预算）但**保留**。"""
    builder = _builder()
    long_public = PUBLIC_MARK + "补" * 200
    bible = _Bible([_Char("丙", public_profile=long_public)])

    text = builder._build_from_bible(bible, current_chapter=1)

    line = [l for l in text.splitlines() if "丙 =" in l][0]
    assert PUBLIC_MARK in line
    assert len(line) <= IDENTITY_PUBLIC_MAX_CHARS + 40


def test_the_mental_state_is_limited():
    """心理状态限长（最容易从别处推 ⇒ 先砍它）。"""
    builder = _builder()
    bible = _Bible([_Char("丁", mental_state="心口发紧" * 60)])

    text = builder._build_from_bible(bible, current_chapter=1)

    line = [l for l in text.splitlines() if "丁 =" in l][0]
    assert len(line) <= IDENTITY_MENTAL_MAX_CHARS + 20


# ── ② 截断按行边界（不切半条约束）──────────────────────────────────────


def test_the_truncation_never_cuts_a_line_in_half():
    """⚠ 半条约束 ⇒ 模型会把半句当成完整设定 ⇒ 比整条缺失更危险。

    ⚠ 判据要**精确**：保留的每一行必须是原文某一行的**完整**副本
    （只查行首会假绿 —— 半行照样以「角色7 = …」开头）。
    """
    original_lines = [
        f"   角色{index} = 公开面 | [已揭露] 秘密 | （POV防火墙：约束{index}。）"
        for index in range(20)
    ]
    content = "\n".join(original_lines)

    kept = mark_truncated(content, 300, section_headers=FACT_LOCK_SECTION_HEADERS)

    body = kept.split(TRUNCATION_NOTICE, 1)[-1]
    # ⚠ 结尾那个 TRUNCATION_MARKER 是**截断机制自己加的**（不是原文的一行）
    #   ⇒ 判「半行」时必须先排除它，否则会把机制标记误判成半条约束。
    assert TRUNCATION_MARKER.strip() in body, kept
    body = body.split(TRUNCATION_MARKER, 1)[0]
    for line in body.splitlines():
        if not line.strip():
            continue
        assert line in original_lines, f"半行被送进了模型：{line!r}"
    for line in body.splitlines():
        if "POV防火墙" in line:
            assert re.search(r"（POV防火墙：[^）]*）", line), f"POV 防火墙被切半了：{line!r}"


def test_a_line_is_dropped_whole_rather_than_half():
    """切到行边界 ⇒ 被丢的行**整行**没了（不是半句）。"""
    content = "\n".join(f"   角色{index} = 秘密{index}" for index in range(10))

    kept = mark_truncated(content, 120, section_headers=FACT_LOCK_SECTION_HEADERS)

    body = kept.split(TRUNCATION_NOTICE, 1)[-1]
    kept_roles = {line.strip().split(" ")[0] for line in body.splitlines() if "角色" in line}
    assert kept_roles, kept
    # 每个保留下来的角色行都完整（没有「秘密5」这种半截）
    for line in body.splitlines():
        if "角色" in line:
            assert line.strip().endswith(tuple("0123456789")) or "…" in line, line


# ── ③ 开头留痕（模型必须知道「本块不完整」）──────────────────────────────


def test_the_truncation_leaves_a_notice_at_the_beginning():
    """⚠ 结尾那个标记会被下游的二次截断切掉 ⇒ 开头必须**明说**。"""
    content = "角色白名单（只可使用以下有名字的角色）：\n   允许: 甲\n\n身份锁死：\n   乙 = 秘密\n"

    kept = mark_truncated(content, 30, section_headers=FACT_LOCK_SECTION_HEADERS)

    assert kept.startswith("【"), kept[:80]
    assert TRUNCATION_NOTICE.strip() in kept[:200], "开头没有留痕"
    assert kept.index(TRUNCATION_NOTICE) < 200, "留痕不在开头 ⇒ 下游任何一次截断都可能切掉它"


def test_the_notice_does_not_accumulate_when_truncated_again():
    """幂等：重新截断**替换**而不是叠加（否则模型看到好几块一样的标记）。"""
    content = "身份锁死：\n   乙 = " + "秘密" * 200

    once = mark_truncated(content, 100, section_headers=FACT_LOCK_SECTION_HEADERS)
    twice = mark_truncated(once, 80, section_headers=FACT_LOCK_SECTION_HEADERS)

    assert twice.count(TRUNCATION_NOTICE) == 1, twice[:200]


def test_the_missing_sections_are_named_after_a_line_level_cut():
    """行级回退 ⇒ 被丢掉的小节**必须**被点名（点名不许撒谎）。"""
    content = (
        "角色白名单（只可使用以下有名字的角色）：\n   允许: 甲\n\n"
        "身份锁死：\n   乙 = 秘密\n\n"
        "核心事件时间线（不可矛盾）：\n   [第 3 章] 丁已死\n"
    )

    kept = mark_truncated(content, 40, section_headers=FACT_LOCK_SECTION_HEADERS)

    assert "被截掉的小节" in kept, kept
    # ⚠ 刀口可能落在「标题在、内容没到」的位置 ⇒ 那一节必须被点名
    assert "身份锁死" in kept, kept[:200]
    assert "核心事件时间线" in kept, kept[:200]


# ── ④ 多角色回归：没有半条约束（第 21 批实测的那种切法）─────────────────


def test_many_characters_produce_no_half_constraints():
    """⚠ 生产实测的形状：多角色 ⇒ 刀口落在某个角色中间 ⇒ 半条约束。

    ⇒ 现在：被丢的行**整行**没了，**没有**半条。

    ⚠ 判据要**精确**：保留的每一行必须是原文某一行的**完整**副本
    （只看行首会假绿 —— 半行照样以「角色7 = …」开头）。
    """
    builder = _builder()
    characters = [
        _Char(
            f"角色{index}",
            public_profile=PUBLIC_MARK + f"{index}",
            hidden_profile=_hidden(SECRET_MARK + f"{index}", f"约束{index}"),
            mental_state=f"心理{index}",
        )
        for index in range(8)
    ]
    text = builder._build_from_bible(_Bible(characters), current_chapter=1)

    kept = mark_truncated(text, 900, section_headers=FACT_LOCK_SECTION_HEADERS)

    body = kept.split(TRUNCATION_NOTICE, 1)[-1].split(TRUNCATION_MARKER, 1)[0]
    original_lines = {line for line in text.splitlines() if line.strip()}
    for line in body.splitlines():
        if not line.strip():
            continue
        assert line in original_lines, f"半行被送进了模型：{line!r}"
    for line in body.splitlines():
        if "POV防火墙" in line:
            # ⚠ 防火墙这一段必须**完整**（「（POV防火墙：…）」成对）
            assert re.search(r"（POV防火墙：[^）]*）", line), f"POV 防火墙被切半了：{line!r}"


def test_the_audit_side_uses_the_same_identity_lines():
    """⚠ 生成侧与审计侧**同源**（否则审计比的与生成时锁的不是一回事）。"""
    builder = _builder()
    bible = _Bible(
        [_Char("戊", public_profile=PUBLIC_MARK, hidden_profile=_hidden(SECRET_MARK, POV_MARK))]
    )

    facts = builder._facts_from_bible(bible, current_chapter=1)

    assert any(POV_MARK in fact for fact in facts), facts


# ── ⑤ 根因守卫：硬约束**整段**送达（第 24 批的核心）─────────────────────


def _production_shaped_bible(character_count: int = 8) -> _Bible:
    """生产书形状的样本（多角色 × 长描述/公开面/秘密/POV 防火墙）。

    ⚠ 形状来自**真实生产书**（``prodrealtest-001``）的实测：
    事实锁原始 3876 字 ⇒ 正文侧上限 2000 字
    ⇒ 刀口正好落在「身份锁死」这一节**中间** ⇒ 只有 4/8 个防火墙活下来。

    ⚠ 第 41 批：两级上限抬到 **10000 tokens / 12000 字** ⇒ 原来这个 8 人样本
      只有 2588 字 ⇒ **不再超过上限** ⇒ 这条守卫会「因前提失效而空过」
      ⇒ 现在补一段**时间线尾巴**（生产长书就是这个形状：
      **硬约束在前、流水账在后**）⇒ 刀口落在时间线里
      ⇒ 8/8 个防火墙仍然全在，而**截断照样发生**（场景没被稀释掉）。
    """
    characters = []
    for index in range(character_count):
        characters.append(
            _Char(
                f"角色{index}",
                description=f"定位{index} - " + "描述" * 30,
                public_profile=PUBLIC_MARK + f"{index}" + "公开面" * 12,
                hidden_profile=_hidden(
                    SECRET_MARK + f"{index}" + "秘密" * 25,
                    POV_MARK + f"{index}" + "防火墙" * 4,
                ),
                mental_state=f"心理{index}" + "状态" * 8,
            )
        )
    timeline = [
        SimpleNamespace(timestamp=f"第 {index} 章", event="某件事发生了。" * 4)
        for index in range(1, 300)
    ]
    return _Bible(characters, timeline_notes=timeline)


def test_every_identity_constraint_reaches_the_model_within_the_prose_budget():
    """⚠ **根因守卫**（第 24 批的核心）：**每个**角色的 POV 防火墙都必须送达。

    ⚠ 走**真实链路**：分配器那一刀（``MAX_FACT_LOCK_TOKENS`` tokens）
      → **真实的**正文 builder（每块上限）⇒ 断言的是**模型实际看得见**的文本。

    ⚠ **前提随修复而变**（这条守卫的演化史）：
      * 第 24 批：原始事实锁 3876 字 > 上限 2000 字 ⇒ 刀口落在「身份锁死」中间
        ⇒ 后 4 个角色的防火墙**没送到模型** ⇒ 那就是根因；
      * 第 41 批：两级上限抬到 10000 tokens / 12000 字 ⇒ 生产形状不再超限；
      * 第 42 批：时间线按预算收口 ⇒ 生产形状的事实锁**整条送得下**。
      ⇒ ⇒ 现在断言的是**修复后的状态**：**压根不被切** ⇒ 8/8 个防火墙全在。
      ⇒ ⚠ 「被切时不许切半防火墙」这个机制由本文件上面的
        ``test_many_characters_produce_no_half_constraints`` 守着（用人为样本）。
    """
    from application.engine.services.context_budget_allocator import (
        ContextBudgetAllocator,
    )
    from application.engine.services.context_budget_models import (
        BudgetAllocation,
        ContextSlot,
        PriorityTier,
    )
    from application.engine.services.context_budget_policy import cap_slots_by_own_limit
    from application.engine.services.context_builder import ContextBuilder
    from application.engine.services.prose_context_settings import (
        get_prose_context_runtime_settings,
    )

    builder = _builder()
    text = builder._build_from_bible(_production_shaped_bible(), current_chapter=1)

    prose_cap = get_prose_context_runtime_settings().max_chars_per_slot
    assert len(text) <= prose_cap, (
        f"样本前提变了（原始事实锁 {len(text)} 字，已超过正文上限 {prose_cap}）"
        "⇒ 这条该改成「被切时也不许丢防火墙」"
    )

    slot = ContextSlot(
        name="绝对事实边界(FACT_LOCK)",
        tier=PriorityTier.T0_CRITICAL,
        content=text,
        tokens=9999,
        max_tokens=ContextBudgetAllocator.MAX_FACT_LOCK_TOKENS,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )
    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)
    prose = ContextBuilder._build_hard_facts(BudgetAllocation(slots={"fact_lock": slot}))

    # ⚠ 生产形状现在**整条送达** ⇒ 不该出现任何截断留痕
    assert "已按上限截断" not in prose, "生产形状的事实锁被截断了（根因复发）"

    for index in range(8):
        assert f"{POV_MARK}{index}" in prose, (
            f"角色{index} 的 POV 防火墙没送到模型 ⇒ 它可以自由发挥"
            f"（这就是生产实测抓到的「能力越界 / 性别矛盾」的根因）"
        )
    # ⚠ 防火墙不许被切半（半句防火墙会被当成完整设定）
    for index in range(8):
        assert re.search(
            rf"（POV防火墙：{re.escape(POV_MARK)}{index}[^）]*）", prose
        ), f"角色{index} 的防火墙被切半了"
    # ⚠ 模型必须知道「本块被截断了」⇒ 否则它以为事实锁是完整的
    # ⚠ 第 42 批：生产形状**不再被截断** ⇒ 反过来断言「没有截断留痕」
    #   （有留痕反而说明它被切了 ⇒ 与上面那条矛盾 ⇒ 是回归信号）
    assert TRUNCATION_NOTICE.strip() not in prose, (
        "生产形状的事实锁不该被截断 ⇒ 却出现了截断留痕"
    )


def test_a_header_without_content_is_not_left_in_the_block():
    """⚠ 「标题在、内容没到」⇒ **标题不许留在内容里**。

    ⚠ 否则模型看到「身份锁死：」后面**空着** ⇒ 它以为这一节本来就是空的
    ⇒ **静默失效**（连「这一节被截掉了」的提示都没有）。

    ⚠ 守卫纪律：光靠「点名」抓不到这条 ⇒ 点名是**另一层**的职责
    （本批新增的「部分被切的小节也点名」会让点名照样通过）
    ⇒ 这条必须**单独**钉住「内容里不许留空标题」。
    """
    content = (
        "角色白名单（只可使用以下有名字的角色）：\n   允许: 甲\n\n"
        "身份锁死：\n   乙 = 秘密\n"
    )
    # ⚠ 刀口正好落在「身份锁死：」标题**之后**、内容**之前**
    cut_at = content.index("身份锁死：") + len("身份锁死：") + 1

    kept = mark_truncated(content, cut_at, section_headers=FACT_LOCK_SECTION_HEADERS)

    body = kept.split(TRUNCATION_NOTICE, 1)[-1].split(TRUNCATION_MARKER, 1)[0]
    assert "身份锁死：" not in body, (
        "「身份锁死：」这个空标题留在内容里 ⇒ 模型以为这一节本来就是空的"
    )
    naming = kept.split(TRUNCATION_NOTICE, 1)[0]
    assert "身份锁死" in naming, "被点名机制没认出这一节没了"


def test_a_section_cut_in_half_is_still_named():
    """⚠ **标题还在、内容只剩一半** ⇒ **也要点名**。

    ⚠ 此前只报「整节没了」⇒ 刀口切在一节**中间**时（调序后实测：
    「角色设定」被切掉一半）标题还在 ⇒ 点名机制认不出来 ⇒
    模型以为「这一节就这几个角色」⇒ **静默失效**。
    """
    content = (
        "角色白名单（只可使用以下有名字的角色）：\n   允许: 甲\n\n"
        "角色设定（不可更改）：\n   乙 = 职能一\n   丙 = 职能二\n   丁 = 职能三\n\n"
        "身份锁死：\n   戊 = 秘密\n"
    )
    # ⚠ 刀口**落在「角色设定」这一节中间**（由内容算出 ⇒ 不写死数字）
    cut_at = content.index("   丁 = 职能三") + 5

    kept = mark_truncated(content, cut_at, section_headers=FACT_LOCK_SECTION_HEADERS)

    assert "被截掉的小节" in kept, kept
    naming = kept.split(TRUNCATION_NOTICE, 1)[0]
    assert "角色设定" in naming, (
        "「角色设定」被切了一半却没被点名 ⇒ 模型以为这一节就这几个角色"
    )
    assert "身份锁死" in naming, "「身份锁死」整节没了却没被点名"


def test_a_constraint_line_is_dropped_whole_rather_than_half():
    """⚠ 复审自抓的洞：刀口可能落在**一条约束**中间 ⇒ 半条约束送给模型。

    ⇒ 现在：约束行（长度 ≤ 预算的一半）**整行**丢掉，不送半行。
    """
    first = "   甲 = " + "秘密" * 20
    constraint = "   乙 = 秘密短句"
    content = "身份锁死：\n" + first + "\n" + constraint + "\n   丙 = 短"

    # ⚠ 刀口落在**「乙」这一行中间**（由内容算出 ⇒ 不写死数字）
    cut_at = content.index(constraint) + len(constraint) - 2
    kept = mark_truncated(content, cut_at, section_headers=FACT_LOCK_SECTION_HEADERS)

    body = kept.split(TRUNCATION_NOTICE, 1)[-1].split(TRUNCATION_MARKER, 1)[0]
    kept_lines = [line for line in body.splitlines() if line.strip()]
    for line in kept_lines:
        assert line in {"身份锁死：", first, "   丙 = 短"}, f"半行被送进了模型：{line!r}"
    # ⚠ 「乙」这一行要么**整行在**、要么**整行不在** ⇒ 不许是它的一部分
    for line in kept_lines:
        assert not (constraint.startswith(line) and line != constraint), (
            f"约束行被切了一半就送出去了：{line!r}"
        )


def test_a_giant_line_is_cut_rather_than_dropped_whole():
    """⚠ 反向边界：**超长行**（比预算的一半还长）不许整行丢掉。

    ⚠ 实测逼出来的：白名单一行 400 个角色 = 2290 字 > 预算 2250 字
    ⇒ 整行丢掉 = 整个白名单都没了（实测：2250 字预算最后只剩 **18 字**）
    ⇒ 这种行是**清单**（不是一条约束）⇒ 半截仍然可读 ⇒ 只能切在字上。
    """
    giant = "   允许: " + "、".join(f"角色{i}" for i in range(200))
    content = "角色白名单（只可使用以下有名字的角色）：\n" + giant + "\n   禁止: 造新人"

    cut_at = len(giant) // 2
    kept = mark_truncated(content, cut_at, section_headers=FACT_LOCK_SECTION_HEADERS)

    body = kept.split(TRUNCATION_NOTICE, 1)[-1].split(TRUNCATION_MARKER, 1)[0]
    # ⚠ 预算**不许被空着**：超长行被切 ⇒ 保留的内容仍然有实质
    assert len(body.strip()) > cut_at // 2, (
        f"超长行被整行丢掉了 ⇒ 预算空了一大半（实际保留 {len(body.strip())} 字）"
    )
    assert "角色0" in body, "超长清单被丢掉 ⇒ 角色名单没了"
