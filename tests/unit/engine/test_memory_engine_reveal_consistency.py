"""reveal_chapter 的门控口径必须全仓一致。

背景：POV 防火墙里 `reveal_chapter` 为 `None` 的语义由字段文档定义 ——
`application/world/dtos/bible_dto.py:21`「reveal_chapter: 揭示章节号，
None 表示总是可见」。`engine/core/entities/character.py:290` 的参考实现
`is_profile_visible` 也按此实现。

此前 `application/engine/services/memory_engine.py:274-276` 额外要求
`reveal_chapter is not None`，于是 `None` 的角色隐藏面**永远不出现** ——
与另外 4 处（context_builder / context_budget_allocator / entities.character /
character_narrative_kernel）**相反**。

后果不是泄密，而是**设定矛盾**：同一次写作里，AI 一路被告知「他是卧底」，
另一路被告知「他没有隐藏身份」。分歧只在 `None` 时暴露，而 `None` 恰好是
「这个角色不打算设悬念」的默认情形，也就是绝大多数角色。

本文件用 `Character.is_profile_visible` 作参考实现做交叉验证 ——
两处对同一个角色、同一章号必须给出同一个可见性结论。
"""
from application.engine.services.memory_engine import FactLockBuilder
from domain.character.value_objects.character_id import CharacterId
from engine.core.entities.character import Character


class _Char:
    """`_build_identity_lines` 只读这 5 个属性，形状按真实使用点写死。"""

    def __init__(self, *, name, public_profile="", hidden_profile="",
                 reveal_chapter=None, mental_state="NORMAL"):
        self.name = name
        self.public_profile = public_profile
        self.hidden_profile = hidden_profile
        self.reveal_chapter = reveal_chapter
        self.mental_state = mental_state


def _identity_text(char, chapter):
    # `_build_identity_lines` 属于 FactLockBuilder（memory_engine.py:118），
    # 不是 Facade 的 MemoryEngine —— 用 __new__ 绕开初始化，只调纯函数。
    builder = FactLockBuilder.__new__(FactLockBuilder)
    lines = builder._build_identity_lines([char], chapter)
    return "\n".join(lines)


def _reference_visible(char_kwargs, chapter):
    """参考实现：engine/core/entities/character.py:284 is_profile_visible。"""
    ref = Character(
        character_id=CharacterId("c1"),
        name="阿止",
        public_profile=char_kwargs.get("public_profile", ""),
        hidden_profile=char_kwargs.get("hidden_profile", ""),
        reveal_chapter=char_kwargs.get("reveal_chapter"),
    )
    text = ref.is_profile_visible(chapter)
    return bool(char_kwargs.get("hidden_profile")) and char_kwargs.get("hidden_profile") in text


# ── 缺陷本体 ────────────────────────────────────────────────────────────────

def test_no_reveal_chapter_means_always_visible():
    """reveal_chapter=None 的角色，隐藏面必须出现（字段文档 + 参考实现都这么定）。"""
    text = _identity_text(
        _Char(name="阿止", public_profile="剑修", hidden_profile="真实身份是魔宗卧底",
              reveal_chapter=None),
        1,
    )
    assert "真实身份是魔宗卧底" in text, (
        "reveal_chapter=None 时隐藏面缺席 —— 与 bible_dto 字段说明"
        "「None 表示总是可见」及 Character.is_profile_visible 相反（口径分裂）"
    )


def test_agrees_with_reference_implementation_before_reveal():
    kwargs = {"public_profile": "剑修", "hidden_profile": "真实身份是魔宗卧底",
              "reveal_chapter": 40}
    text = _identity_text(_Char(name="阿止", **kwargs), 39)
    assert "真实身份是魔宗卧底" not in text, "第 39 章不该出现第 40 章才揭示的秘密"
    assert _reference_visible(kwargs, 39) is False, "参考实现也不认为可见 —— 断言本身写错了"


def test_agrees_with_reference_implementation_at_reveal_chapter():
    kwargs = {"public_profile": "剑修", "hidden_profile": "真实身份是魔宗卧底",
              "reveal_chapter": 40}
    text = _identity_text(_Char(name="阿止", **kwargs), 40)
    assert "真实身份是魔宗卧底" in text, "到揭示章节了秘密还没出现"
    assert _reference_visible(kwargs, 40) is True


def test_agrees_with_reference_implementation_far_after_reveal():
    kwargs = {"public_profile": "剑修", "hidden_profile": "真实身份是魔宗卧底",
              "reveal_chapter": 40}
    text = _identity_text(_Char(name="阿止", **kwargs), 300)
    assert _reference_visible(kwargs, 300) is True
    assert "真实身份是魔宗卧底" in text


# ── 反向守卫：不许把闸门修成「总是显示」 ────────────────────────────────────

def test_before_reveal_chapter_still_hidden():
    """门控没被整体拆掉 —— 揭示前仍必须隐藏。"""
    text = _identity_text(
        _Char(name="阿止", public_profile="剑修", hidden_profile="真实身份是魔宗卧底",
              reveal_chapter=40),
        5,
    )
    assert "真实身份是魔宗卧底" not in text, "泄密封印被拆了"


def test_character_without_hidden_profile_is_unaffected():
    text = _identity_text(_Char(name="路人甲", public_profile="茶贩", reveal_chapter=None), 3)
    assert "路人甲" in text
    assert "[已揭露]" not in text, "没有隐藏面却打出了已揭露标记"


def test_public_profile_still_present_regardless_of_reveal():
    text = _identity_text(
        _Char(name="阿止", public_profile="剑修", hidden_profile="卧底", reveal_chapter=40), 1
    )
    assert "剑修" in text
