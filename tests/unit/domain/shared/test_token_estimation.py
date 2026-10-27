"""第 27 批：token 估算的**双重加权**修正 ⇒ 槽位的上限**真的生效**。

背景（生产实测，第 27 批盘点）
------------------------------
`estimate_tokens` 旧实现是 ``zh_tokens * ratio + en_tokens * (1 - ratio)``，
而 ``zh_tokens`` 本身已含一次中文比例 ⇒ 等价于 **r² 加权**（比例乘了两次）：

* 纯中文 / 纯英文 ⇒ 与线性一致 ⇒ 看不出问题；
* **中英混排**（事实锁、创作契约、角色锚点都是）⇒ **token 被系统性低估**。

实测（生产书事实锁，中文占比 0.72）：3876 字被估成 **1418** tokens
（线性真值 **2133**）⇒ 槽位的 ``max_tokens`` 上限**形同虚设**
⇒ 内容**超量**进槽位 ⇒ 到正文侧（按**字符**上限）才被砍一刀 ⇒ **二次裁剪**。

⇒ 修正：改成线性加权 ``c / 1.5 + (t - c) / 4``
⇒ 纯中文 / 纯英文结果**完全不变** ⇒ 只有混排被修正（往上修）。

⚠ 守卫纪律：走**真实**截断函数（``cap_slots_by_own_limit``）+ **真实**事实锁构建器。
"""
from __future__ import annotations

from application.engine.services.context_budget_models import ContextSlot, PriorityTier
from application.engine.services.context_budget_policy import cap_slots_by_own_limit
from application.engine.services.memory_engine import (
    FACT_LOCK_SECTION_HEADERS,
    FactLockBuilder,
)
from domain.shared.token_estimation import (
    CHARS_PER_TOKEN_EN,
    CHARS_PER_TOKEN_ZH,
    estimate_tokens,
)

#: 事实锁槽位的 T0 上限（与生产一致）
FACT_LOCK_MAX_TOKENS = 1500


# ── ① 公式本身 ─────────────────────────────────────────────────────────


def test_pure_chinese_is_unchanged():
    """纯中文 ⇒ 1.5 字/token（两种口径**完全一致** ⇒ 不受影响）。

    ⚠ 样本必须**真的纯中文**（中文标点「，。：」不在 ``U+4E00-U+9FFF`` 里
    ⇒ 会被算成「非中文」⇒ 样本就不是纯中文了）。
    ⚠ 期望值用**字面量 1.5**，不用导入的常量 ——
    否则改了那个常量，这条守卫**跟着变** ⇒ 永远绿（假绿）。
    """
    text = "身份锁死沈砚青是租界巡捕房的探员她不得确知送票人的身份" * 5

    assert all("\u4e00" <= c <= "\u9fff" for c in text), "样本不是纯中文"
    assert CHARS_PER_TOKEN_ZH == 1.5, "中文口径被改了（1.5 字/token 是**约定**）"
    assert estimate_tokens(text) == int(len(text) / 1.5 + 0.5)


def test_pure_english_is_unchanged():
    """纯英文 ⇒ 4 字/token（同样不变）。

    ⚠ 同样用**字面量 4.0** 算期望值（理由同上）。
    """
    text = "the fact lock must never be cut in the middle of a constraint " * 10

    assert CHARS_PER_TOKEN_EN == 4.0, "英文口径被改了（4 字/token 是**约定**）"
    assert estimate_tokens(text) == int(len(text) / 4.0 + 0.5)


def test_mixed_text_is_weighted_linearly():
    """⚠ 中英混排 ⇒ **线性**加权（两个速率按占比加权平均）。

    ⚠ 旧实现是 r² 加权 ⇒ 对混排**低估** ⇒ 上限形同虚设。
    """
    text = "身份锁死 POV防火墙：沈砚青不得确知送票人身份" + "POV firewall constraint " * 10

    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    # ⚠ 期望值用**字面量**（1.5 / 4.0）⇒ 改了口径常量时这条也会红
    expected = int(chinese / 1.5 + (len(text) - chinese) / 4.0 + 0.5)

    assert estimate_tokens(text) == expected

    # ⚠ 旧口径（r²）会**低估** ⇒ 断言两者不等（否则这条守卫是空过）
    ratio = chinese / len(text)
    legacy = int(
        (chinese / 1.5) * ratio
        + ((len(text) - chinese) / 4.0) * (1 - ratio)
        + 0.5
    )
    assert estimate_tokens(text) != legacy, "混排样本没触发差异 ⇒ 这条守卫验不到东西"


def test_the_estimate_never_undercounts_the_linear_value():
    """⚠ 修正只能**往上**（低估才是病灶）⇒ 不许把口径改得更松。"""
    text = "角色白名单：沈砚青、楚半声；POV firewall 不得确知身份；" * 20

    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    linear = chinese / CHARS_PER_TOKEN_ZH + (len(text) - chinese) / CHARS_PER_TOKEN_EN

    assert estimate_tokens(text) >= int(linear + 0.5) - 1


# ── ② 端到端：槽位的上限**真的生效** ─────────────────────────────────


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
    def __init__(self, characters):
        self.characters = characters
        self.timeline_notes = []


def _production_shaped_fact_lock() -> str:
    """生产形状的事实锁（8 个角色 × 长公开面/秘密/POV 防火墙）。

    ⚠ 长度要**对齐生产**（实测 3876 字）⇒ 否则样本的「临界点」不存在
    ⇒ 上限咬不咬都验不出来（见第 26 批的教训）。
    """
    characters = [
        _Char(
            f"角色{index}",
            description=f"定位{index} - " + "描述" * 100,
            public_profile=(
                "房里挂着名目的探员，同僚眼里的冷面笔杆子，专接没人愿接的陈案碎案，"
                "见了她都自觉把话说圆，"
                + f"第{index}号"
                + "，她办的案子多半是别人推不动的那些"
            ),
            hidden_profile=(
                "第四张戏票此刻就在她大衣内袋，她尚未声张；她天生耳聪，是块收声人的料，"
                "她自己只知道自己的耳朵太好用，送票人选她，未必是要她死，"
                + f"{index}"
                + "。（POV防火墙：她的视角在后台认人之前，不得确知送票人身份；"
                "第四张票的来历在她的卷宗里只能是无从查证。）"
                + "听得多者先忘故人、再损耳力，终至聋聩失声，甚至被所听之声渐渐顶替言语习惯"
            ),
            mental_state=f"绷紧的孤勇{index}",
        )
        for index in range(8)
    ]
    builder = FactLockBuilder.__new__(FactLockBuilder)
    return builder._build_from_bible(_Bible(characters), current_chapter=1)


def test_the_fact_lock_upper_bound_actually_bites():
    """⚠ **核心**：事实锁的 ``max_tokens`` 上限必须**真的生效**。

    ⚠ 旧口径下（r² 加权）它被估成「未超限」⇒ 3876 字**全量**进槽位
    ⇒ 到正文侧（按字符）才被砍 ⇒ **二次裁剪**。
    ⇒ 现在：T0 阶段就裁 ⇒ 而且**带点名**（模型知道哪几节被截）。
    """
    text = _production_shaped_fact_lock()
    # ⚠ 前提要按 **token** 判（不是字符数）⇒ 上限咬的是 token
    assert estimate_tokens(text) > FACT_LOCK_MAX_TOKENS, (
        f"样本估出来只有 {estimate_tokens(text)} tokens（上限 {FACT_LOCK_MAX_TOKENS}）"
        f"⇒ 样本前提不成立"
    )

    slot = ContextSlot(
        name="绝对事实边界(FACT_LOCK)",
        tier=PriorityTier.T0_CRITICAL,
        content=text,
        tokens=estimate_tokens(text),
        max_tokens=FACT_LOCK_MAX_TOKENS,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )
    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)

    assert len(slot.content) < len(text), "事实锁超了上限却**没被裁** ⇒ 上限形同虚设"
    assert "被截掉的小节" in slot.content, "裁了但**没点名** ⇒ 模型不知道哪几节没了"
    assert "身份锁死：" in slot.content, "硬约束（身份锁死）被裁掉了"


def test_a_slot_under_its_limit_is_not_cut():
    """⚠ **防误伤**：没超限的槽位**不许**被裁（口径修正只该影响超限的那些）。"""
    text = (
        "【绝对事实边界（一旦违背即为废稿）】\n\n"
        "角色白名单（只可使用以下有名字的角色）：\n   允许: 甲, 乙, 丙\n\n"
        "身份锁死：\n   甲 = 公开面 | [已揭露] 秘密 | （POV防火墙：不得确知。）\n"
    )
    slot = ContextSlot(
        name="绝对事实边界(FACT_LOCK)",
        tier=PriorityTier.T0_CRITICAL,
        content=text,
        tokens=estimate_tokens(text),
        max_tokens=FACT_LOCK_MAX_TOKENS,
        priority=120,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )
    cap_slots_by_own_limit({"fact_lock": slot}, chars_per_token_zh=1.5)

    assert slot.content == text, "没超限的槽位被裁了 ⇒ 口径修正误伤了正常内容"
