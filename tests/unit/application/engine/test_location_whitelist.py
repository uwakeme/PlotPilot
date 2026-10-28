"""第 31 批：**地点白名单**（与「角色白名单」同构，两侧同源）。

背景（第 30 批留下的缺口）
--------------------------
审计侧拿地点当基准，生成侧却只给**本场**地点的描述 ⇒ 模型压根不知道
「有名字的地点」全集 ⇒ 可能**自造**地点 ⇒ 而自造地点在审计侧**审不到**
（基准里没有它 ⇒ 无从判断它是「未登记的」）。

⇒ 修法（与「角色白名单」完全同构）：
* **生成侧**：事实锁加一节「地点白名单（只可使用以下地点，不得自造）」；
* **审计侧**：同一份清单进基准（走 ``build_facts`` ⇒ **同一份来源** ⇒ 不会漂）。

⚠ 预算纪律：地点白名单**必须排在身份锁死前面** ⇒ 否则它会把
第 24 批刚修好的硬约束（8/8 个 POV 防火墙）挤掉。
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from application.engine.services.context_budget_policy import mark_truncated
from application.engine.services.memory_engine import (
    FACT_LOCK_HEADER_IDENTITY,
    FACT_LOCK_HEADER_LOCATION_WHITELIST,
    FACT_LOCK_SECTION_HEADERS,
    FactLockBuilder,
    character_names,
    location_names,
    location_whitelist_lines,
    whitelist_lines,
)

#: 载荷：与仓库里其它用例都不同的名字（避免「碰巧命中」）
CITY = "临渊城"
HARBOR = "沉舟渡"
DOCK = "落雁埠"
#: 与生产书同量级的地点数（生产书 11 个）
LOCATION_NAMES = [CITY, HARBOR, DOCK] + [f"边地{index}" for index in range(8)]


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
    def __init__(self, characters, locations=None):
        self.characters = characters
        self.locations = locations if locations is not None else [
            SimpleNamespace(name=name, description=f"{name}的描述", location_type="city")
            for name in LOCATION_NAMES
        ]
        self.timeline_notes = []


def _builder():
    return FactLockBuilder.__new__(FactLockBuilder)


def _characters(count: int = 8):
    return [
        _Char(
            f"角色{index}",
            description=f"定位{index} - " + "描述" * 60,
            public_profile="房里挂着名目的探员，同僚眼里的冷面笔杆子。" + f"{index}",
            hidden_profile=(
                "第四张戏票此刻就在她大衣内袋，她尚未声张；她天生耳聪，是块收声人的料，"
                f"{index}。（POV防火墙：她的视角在后台认人之前，不得确知送票人身份。）"
            ),
            mental_state=f"绷紧的孤勇{index}",
        )
        for index in range(count)
    ]


# ── ① 生成侧：事实锁里有地点白名单 ───────────────────────────────────────


def test_the_fact_lock_contains_the_location_whitelist():
    """⚠ 模型必须知道「有名字的地点」全集 ⇒ 否则会自造。"""
    builder = _builder()
    text = builder._build_from_bible(_Bible(_characters()), current_chapter=1)

    assert FACT_LOCK_HEADER_LOCATION_WHITELIST in text, "事实锁里没有地点白名单"
    assert CITY in text and HARBOR in text, "地点白名单里没有地点名"
    assert "不得自造" in text or "禁止" in text, "地点白名单没有说明「不许自造」"


def test_the_location_whitelist_sits_before_the_identity_lock():
    """⚠ **预算优先级**：地点白名单在身份锁**前面** ⇒ 不会把硬约束挤掉。"""
    builder = _builder()
    text = builder._build_from_bible(_Bible(_characters()), current_chapter=1)

    assert text.index(FACT_LOCK_HEADER_LOCATION_WHITELIST) < text.index(
        FACT_LOCK_HEADER_IDENTITY
    ), "地点白名单排到了身份锁死后面 ⇒ 会挤掉硬约束"


def test_the_hard_constraints_survive_the_prose_budget():
    """⚠ 加了地点白名单之后，**8/8 个 POV 防火墙**仍然全到（第 24 批的成果）。"""
    builder = _builder()
    text = builder._build_from_bible(_Bible(_characters()), current_chapter=1)

    kept = mark_truncated(text, 2000, section_headers=FACT_LOCK_SECTION_HEADERS)
    firewalls = [line for line in kept.splitlines() if "POV防火墙" in line]

    assert len(firewalls) == 8, f"只活下来 {len(firewalls)}/8 个防火墙"
    assert FACT_LOCK_HEADER_LOCATION_WHITELIST in kept, "地点白名单被挤掉了"


# ── ② 审计侧：**同一份**（不许分叉） ─────────────────────────────────────


def test_the_audit_side_uses_the_same_location_whitelist():
    """⚠ 审计基准里的地点白名单必须与生成侧**同一份** ⇒ 不会漂。"""
    builder = _builder()
    bible = _Bible(_characters())

    generated = builder._build_from_bible(bible, current_chapter=1)
    facts = builder._facts_from_bible(bible, current_chapter=1)

    joined = "\n".join(facts)
    assert FACT_LOCK_HEADER_LOCATION_WHITELIST in joined, "审计侧没有地点白名单"
    for name in LOCATION_NAMES:
        assert name in generated and name in joined, f"地点 {name} 两边不一致"


def test_the_whitelist_helpers_are_shared():
    """⚠ 两个白名单的**取数与渲染**都必须共用同一份 helper。"""
    bible = _Bible(_characters())

    assert location_names(bible) == LOCATION_NAMES
    lines = location_whitelist_lines(location_names(bible))
    assert lines[0] == FACT_LOCK_HEADER_LOCATION_WHITELIST
    assert all(name in lines[1] for name in LOCATION_NAMES)
    # 角色白名单同构（防止只给地点写、角色没有）
    assert whitelist_lines(character_names(bible))[0].startswith("角色白名单")


# ── ③ 没有地点时**不许**造空节 ─────────────────────────────────────────


def test_no_location_section_when_the_bible_has_none():
    """⚠ Bible 没有地点 ⇒ **不产生**这一节（空节是假信号 + 白占预算）。"""
    builder = _builder()
    bible = _Bible(_characters(), locations=[])

    text = builder._build_from_bible(bible, current_chapter=1)
    facts = builder._facts_from_bible(bible, current_chapter=1)

    assert FACT_LOCK_HEADER_LOCATION_WHITELIST not in text
    assert not any(FACT_LOCK_HEADER_LOCATION_WHITELIST in f for f in facts)
    assert location_whitelist_lines([]) == []


def test_the_section_header_is_declared_for_truncation_naming():
    """⚠ 新节必须进 ``FACT_LOCK_SECTION_HEADERS`` ⇒ 截断时才能**点名**。"""
    assert FACT_LOCK_HEADER_LOCATION_WHITELIST in FACT_LOCK_SECTION_HEADERS
    # 位置：紧跟角色白名单（两个「只可使用」清单相邻）
    assert FACT_LOCK_SECTION_HEADERS.index(
        FACT_LOCK_HEADER_LOCATION_WHITELIST
    ) == FACT_LOCK_SECTION_HEADERS.index(
        [h for h in FACT_LOCK_SECTION_HEADERS if h.startswith("角色白名单")][0]
    ) + 1
    assert re.match(r"^地点白名单", FACT_LOCK_HEADER_LOCATION_WHITELIST)
