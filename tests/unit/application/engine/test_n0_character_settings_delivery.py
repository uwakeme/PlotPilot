"""N-0：角色设定（公开身份/职能）必须真的进到正文提示词里。

背景 —— 这是**真实跑一本真书**才暴露出来的结构性缺陷：

Bible 里 7 个角色，**正文里 4 个被重定义**（含把 Bible 里的
「白塔旧案主谋」写成报社好领导）。查第 1 章的**真实提示词快照**：
``放映师 0 / 主谋 0 / 探员 0 / 中间人 0 / 总经理 0``
⇒ 提示词里**只有角色名**（白名单），没有任何角色设定
⇒ 模型无从知道「贺鸣珂是白塔前放映师」⇒ 只能猜 ⇒ 漂移是必然的。

两处独立断链（本文件两条都守住）：

1. **落库侧**：一次性 ``stage="all"`` 走 ``bible-all`` 提示词，而那个提示词
   **没有输出 schema**（``package.yaml`` 指向的契约模块压根不存在）
   ⇒ 模型自由发挥 ⇒ 返回形状每次都不一样 ⇒ 落库再全也落不全。
   ⇒ 改成「一次性 = 依次跑三个分阶段」（三个分阶段提示词各自都有完整 schema）。
2. **构建侧**：``FactLockBuilder`` 的五节里**没有一节装角色公开身份**
   ⇒ 即使 Bible 有字段也进不了提示词 ⇒ 加「角色设定（不可更改）」一节。

⚠ 守卫纪律：
   * 走**真实**的 builder / 落库路径 / 截断函数，不复刻任何一层的形状；
   * 断言**模型可见文本**里真的有职能关键词（这才是目标）；
   * 每条都要能被抓（变异验证见 ``review-briefs/_mut_n0_character_settings.py``）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.engine.services.memory_engine import (
    CHARACTER_SETTING_MAX_CHARACTERS,
    FACT_LOCK_HEADER_CHARACTER_SETTINGS,
    FACT_LOCK_SECTION_HEADERS,
    FactLockBuilder,
)

#: 载荷：与仓库里其它用例都不同的名字（避免「碰巧命中」）
ROLE_KEYWORD = "白塔前放映师"          # 贺鸣珂的职能
OTHER_KEYWORD = "永安影业总经理"       # 梁佩声的职能
PUBLIC_KEYWORD = "对其他角色可见的身份"  # 公开面


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


def _bible_with_settings() -> _Bible:
    return _Bible(
        [
            _Char(
                "贺鸣珂",
                description=f"{ROLE_KEYWORD}、最后一卷胶片的主人 - 瘦高，左耳轻微失聪",
                public_profile=PUBLIC_KEYWORD,
            ),
            _Char(
                "梁佩声",
                description=f"{OTHER_KEYWORD}、白塔旧案主谋 - 语气温和，衣饰考究",
            ),
        ]
    )


# ======================================================================
#  A. 事实锁：角色设定这一节存在，且装的是「职能」
# ======================================================================


def test_the_fact_lock_carries_the_character_settings():
    """事实锁必须有「角色设定」一节，且含角色的**职能**。

    ⚠ 这一节是 N-0 的核心：没有它，模型只知道「有这个人」。
    """
    builder = FactLockBuilder.__new__(FactLockBuilder)
    text = builder._build_from_bible(_bible_with_settings(), current_chapter=1)

    assert FACT_LOCK_HEADER_CHARACTER_SETTINGS in text, "事实锁没有「角色设定」这一节"
    assert ROLE_KEYWORD in text, "事实锁里没有角色的职能（模型无从知道他是干什么的）"
    assert OTHER_KEYWORD in text
    assert PUBLIC_KEYWORD in text, "事实锁里没有角色的公开面"


def test_the_new_section_header_is_in_the_truncation_headers():
    """新节标题必须在 ``FACT_LOCK_SECTION_HEADERS`` 里。

    ⚠ 不在的话，截断时的「点名被截掉的小节」**认不出这一节**
    ⇒ 模型看不到它没了 ⇒ 静默失效（这正是 §4 那个病灶）。
    """
    assert FACT_LOCK_HEADER_CHARACTER_SETTINGS in FACT_LOCK_SECTION_HEADERS


def test_the_audit_side_uses_the_same_source():
    """审计侧（``build_facts``）必须与生成侧**同源**。

    否则审计比的与生成时被锁的不是一回事 ⇒ 口径对不上，结论也就对不上。
    """
    builder = FactLockBuilder.__new__(FactLockBuilder)
    facts = builder._facts_from_bible(_bible_with_settings(), current_chapter=1)

    assert any("角色设定" in f for f in facts), facts
    assert any(ROLE_KEYWORD in f for f in facts), facts


def test_the_omitted_characters_are_named_not_silently_dropped():
    """角色太多装不下时，**如实点名**（不许静默丢）。"""
    characters = [
        _Char(f"角色{i}", description=f"职能{i} - 说明{i}") for i in range(CHARACTER_SETTING_MAX_CHARACTERS + 3)
    ]
    builder = FactLockBuilder.__new__(FactLockBuilder)
    text = builder._build_from_bible(_Bible(characters), current_chapter=1)

    assert "未注入" in text, "超出的角色被静默丢掉了"
    assert f"角色{CHARACTER_SETTING_MAX_CHARACTERS}" in text


def test_a_bible_without_any_settings_says_so():
    """一个角色的设定都没有 ⇒ **如实说没有**，但不许暗示「没有约束」。"""
    builder = FactLockBuilder.__new__(FactLockBuilder)
    text = builder._build_from_bible(_Bible([_Char("无名氏")]), current_chapter=1)

    assert FACT_LOCK_HEADER_CHARACTER_SETTINGS in text
    assert "暂无角色设定" in text


def test_the_truncated_setting_says_it_was_cut():
    """截断必须**留痕**（「砍掉就要说」）。

    模型看到半句话（"…再听证人的措辞；她最"）会当成**完整设定** ⇒
    必须让它知道这句话没写完。
    """
    long_description = ROLE_KEYWORD + "补" * 200
    builder = FactLockBuilder.__new__(FactLockBuilder)
    text = builder._build_from_bible(
        _Bible([_Char("贺鸣珂", description=long_description)]), current_chapter=1
    )

    line = [l for l in text.splitlines() if "贺鸣珂 =" in l][0]
    assert "…" in line, f"截断没有留痕：{line}"
    assert ROLE_KEYWORD in line, f"截断把职能也砍掉了：{line}"


# ======================================================================
#  B. 端到端：模型**实际看到**的文本里有职能
# ======================================================================


def test_the_surviving_text_still_carries_the_settings_after_truncation():
    """经过真实的截断后，职能关键词仍在（截断不该把它切没）。

    走**真实**的 ``mark_truncated``（正文侧那一刀）⇒ 不复刻它的形状。
    """
    from application.engine.services.context_budget_policy import mark_truncated

    builder = FactLockBuilder.__new__(FactLockBuilder)
    text = builder._build_from_bible(_bible_with_settings(), current_chapter=1)

    # 正文侧的事实锁上限（``prose_context_settings.DEFAULT_MAX_CHARS_PER_SLOT``）
    from application.engine.services.prose_context_settings import (
        DEFAULT_MAX_CHARS_PER_SLOT,
    )

    kept = mark_truncated(
        text,
        DEFAULT_MAX_CHARS_PER_SLOT,
        section_headers=FACT_LOCK_SECTION_HEADERS,
    )

    assert ROLE_KEYWORD in kept, "截断把角色职能切没了 ⇒ 模型看不到「他是干什么的」"
    assert FACT_LOCK_HEADER_CHARACTER_SETTINGS in kept


# ======================================================================
#  C. 落库侧：一次性路径必须走分阶段，且角色字段全套落库
# ======================================================================


def _generator():
    from application.world.services.auto_bible_generator import AutoBibleGenerator

    bible_service = Mock()
    bible_service.get_bible_by_novel = Mock(
        return_value=SimpleNamespace(characters=[], world_settings=[])
    )
    return AutoBibleGenerator(llm_service=Mock(), bible_service=bible_service), bible_service


@pytest.mark.asyncio
async def test_the_one_shot_path_runs_the_three_stages_in_order(monkeypatch):
    """``stage="all"`` 必须**依次**跑三个分阶段。

    ⚠ 此前它走 ``bible-all`` 一次性提示词（无 schema ⇒ 输出形状不稳定）。
    ⇒ 这条守住「薄路径已消失」。
    """
    gen, _ = _generator()
    calls: list[str] = []

    async def _wb(self, novel_id, premise, target_chapters):
        calls.append("worldbuilding")
        return {"style": "s", "worldbuilding": {}}

    async def _ch(self, novel_id, premise, target_chapters):
        calls.append("characters")
        return {"characters": []}

    async def _lo(self, novel_id, premise, target_chapters):
        calls.append("locations")
        return {"locations": []}

    async def _bible_all(self, premise, target_chapters):
        raise AssertionError("stage='all' 不该再走 bible-all 一次性提示词")

    monkeypatch.setattr(
        "application.world.services.auto_bible_generator.AutoBibleGenerator._run_stage_worldbuilding", _wb
    )
    monkeypatch.setattr(
        "application.world.services.auto_bible_generator.AutoBibleGenerator._run_stage_characters", _ch
    )
    monkeypatch.setattr(
        "application.world.services.auto_bible_generator.AutoBibleGenerator._run_stage_locations", _lo
    )
    monkeypatch.setattr(
        "application.world.services.auto_bible_generator.AutoBibleGenerator._generate_bible_data", _bible_all
    )

    result = await gen.generate_and_save(novel_id="n-1", premise="p", target_chapters=3, stage="all")

    assert calls == ["worldbuilding", "characters", "locations"], calls
    assert result["style"] == "s"
    assert "characters" in result and "locations" in result


@pytest.mark.asyncio
async def test_the_character_payload_is_saved_with_every_field(monkeypatch):
    """角色的**全套字段**必须落库（公开面 / 隐藏面 / 揭露章 / 关系 / 声线…）。

    ⚠ 这正是「关系锁 / 身份锁 / 死亡锁」空着的根因：
    模型返回了，落库路径只传了 name + description ⇒ 其余全丢。
    """
    gen, bible_service = _generator()

    payload = {
        "characters": [
            {
                "name": "贺鸣珂",
                "role": "次要角色",
                "description": "白塔前放映师",
                "gender": "男",
                "age": "三十上下",
                "appearance": "瘦高",
                "personality": "沉默守诺",
                "background": "在白塔放映多年",
                "public_profile": "对其他角色可见的身份",
                "hidden_profile": "早已决定把某些人放进危险位置",
                "reveal_chapter": 7,
                "mental_state": "NORMAL",
                "mental_state_reason": "开场",
                "verbal_tic": "先看鞋",
                "idle_behavior": "转烟盒",
                "core_belief": "胶片比人诚实",
                "core_motivation": "把最后一卷胶片交出去",
                "inner_lack": "不肯承认自己早就在选牺牲品",
                "moral_taboos": ["不说谎"],
                "voice_profile": {"style": "克制"},
                "active_wounds": [{"description": "旧伤", "trigger": "放映声", "effect": "失聪"}],
                "relationships": [{"target": "林见秋", "relation": "对立", "description": "张力"}],
            }
        ]
    }

    async def _gen_characters(self, premise, target_chapters, worldbuilding):
        return payload

    monkeypatch.setattr(
        "application.world.services.auto_bible_generator.AutoBibleGenerator._generate_characters",
        _gen_characters,
    )

    await gen.generate_and_save(novel_id="n-1", premise="p", target_chapters=3, stage="characters")

    assert bible_service.add_character.called, "角色没有落库"
    kwargs = bible_service.add_character.call_args.kwargs
    for field in (
        "public_profile",
        "hidden_profile",
        "reveal_chapter",
        "relationships",
        "verbal_tic",
        "idle_behavior",
        "core_belief",
        "moral_taboos",
        "voice_profile",
        "active_wounds",
        "core_motivation",
        "inner_lack",
    ):
        assert field in kwargs, f"落库时没有传 {field}"
        assert kwargs[field] not in (None, "", [], {}), f"{field} 落了空值"
    assert kwargs["public_profile"] == "对其他角色可见的身份"
    assert kwargs["reveal_chapter"] == 7
    assert kwargs["relationships"] == payload["characters"][0]["relationships"]
