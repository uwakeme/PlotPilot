"""第 40 批：事实锁补两类**硬事实** —— 性别/年龄 + 地点的类型与描述。

背景（生产实测，第 40 批）
--------------------------
事实锁此前**只给**角色的 ``description`` ⇒ ``Character.gender`` / ``age``
（生产书 ``prodrealtest-001`` 8/8 个角色**全有值**：女/25、男/56…）
**根本没进模型**；地点白名单**只给名字** ⇒ 模型不知道「这些地方**是什么地方**」
（``Location.description`` 89~132 字/条，全是硬设定：戏院台板下埋九口老铜缸…）。

⇒ 第 21 批生产实测抓到的「郑铁珊（基准=男）被正文写成女性」**根因就在这里**
⇒ 而 A1 设定漂移审计的基准走 ``FactLockBuilder.build_facts`` ⇒ 此前它
**也没有**性别基准 ⇒ 那一类漂移**既锁不住、也审不出**。

本批
----
* **身份锁**每行**最前**加「性别 / 年龄」（各限 8 字）
  ⇒ 放在最前是因为「角色设定」节排在硬约束**之后**、预算紧时**先被切**；
* **地点白名单**每条加「类型 + 一句话描述」（描述限 40 字）
  ⇒ 名字锁「不许自造」，类型+描述锁「这些地方**是什么**」；
* 两侧**同源**：生成侧 ``_build_from_bible`` 与审计侧 ``_facts_from_bible``
  共用**同一个函数 + 同一份输入**（``bible.locations``）⇒ 不会漂。

⚠ 守卫纪律：
  ① 期望值里的口径**写死字面量**（不许用被测模块导出的常量算期望值
     ⇒ 改常量必红，否则是假绿）；
  ② 生产形状的样本按**实测**字段长度造（守卫样本必须与生产一致）；
  ③ 断言落在**身份锁死那一节**里（同名行在「角色设定」节里也有 ⇒ 全文 contains 假绿）。
"""
from __future__ import annotations

from types import SimpleNamespace

from application.engine.services.memory_engine import (
    FACT_LOCK_HEADER_IDENTITY,
    FactLockBuilder,
    location_whitelist_lines,
)
from application.engine.services.prose_context_settings import (
    get_prose_context_runtime_settings,
)

# ── 口径值（写死 ⇒ 改常量必红）────────────────────────────────────────
#: 性别/年龄各限 8 字（生产书 8 个角色合计 26 字 ⇒ 极紧）
GENDER_MAX_CHARS = 8
AGE_MAX_CHARS = 8
#: 地点描述限 40 字/条（生产书 11 条 ≈ 440 字）
LOCATION_DESC_MAX_CHARS = 40

#: 生产实测（2026-10-29，``prodrealtest-001`` 第 5 章）：事实锁 **4667 字**。
#: ⇒ 样本必须复现这个量级，否则守卫验的是空气。
PRODUCTION_FACT_LOCK_CHARS = 4667
#: 样本与生产实测的允许偏差（样本形状漂了就该红）
SHAPE_TOLERANCE = 0.2


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
    def __init__(self, characters, locations=None):
        self.characters = characters
        self.locations = locations if locations is not None else []
        self.timeline_notes = []


def _builder():
    return FactLockBuilder.__new__(FactLockBuilder)


def _identity_line(text: str, name: str) -> str:
    """取**身份锁死**那一节里的那一行（⚠ 「角色设定」节里也有同名行）。"""
    in_identity = False
    for line in text.splitlines():
        if line.strip() == FACT_LOCK_HEADER_IDENTITY:
            in_identity = True
            continue
        if in_identity:
            if not line.strip():
                break
            if line.strip().startswith(name + " "):
                return line.strip()
    return ""


# ── ① 性别 / 年龄进事实锁 ───────────────────────────────────────────────


def test_the_identity_lock_carries_gender_and_age():
    """⚠ 性别/年龄是**极硬**的身份事实（写错即人物失格）⇒ 必须进模型。"""
    builder = _builder()
    bible = _Bible([_Char("郑铁珊", gender="男", age="56")])

    text = builder._build_from_bible(bible, current_chapter=1)

    line = _identity_line(text, "郑铁珊")
    assert line, "身份锁死那一节里没有这一行"
    assert "男" in line and "56" in line, f"性别/年龄没进事实锁：{line!r}"


def test_gender_and_age_come_before_the_public_profile():
    """⚠ **硬度顺序**：性别/年龄必须排在公开面**之前**。

    ⇒ 事实锁被截断时（刀口落在行中间），先被砍掉的应该是**公开面**
    （软设定），不是性别/年龄（硬事实）。
    """
    builder = _builder()
    bible = _Bible(
        [_Char("郑铁珊", gender="男", age="56", public_profile="房里人人称郑探长")]
    )

    line = _identity_line(builder._build_from_bible(bible, current_chapter=1), "郑铁珊")

    assert line.startswith("郑铁珊 = 男 / 56 |"), line
    assert line.index("男 / 56") < line.index("郑探长"), line


def test_the_audit_side_carries_the_same_gender_and_age():
    """⚠ 两侧**同源** ⇒ A1 审计才有性别基准（此前它**没有**）。"""
    builder = _builder()
    bible = _Bible([_Char("郑铁珊", gender="男", age="56")])

    facts = builder._facts_from_bible(bible, current_chapter=1)

    joined = "\n".join(facts)
    assert "郑铁珊 = 男 / 56" in joined, joined


def test_an_overlong_gender_or_age_is_capped_and_marked():
    """⚠ 超长 ⇒ **限长 + 留痕**（不许静默截断 ⇒ 模型会当成完整事实）。"""
    builder = _builder()
    long_gender = "男" * 20
    long_age = "五十六岁" * 8
    bible = _Bible([_Char("郑铁珊", gender=long_gender, age=long_age)])

    line = _identity_line(builder._build_from_bible(bible, current_chapter=1), "郑铁珊")

    assert "…" in line, f"超长性别/年龄没有被留痕：{line!r}"
    assert long_gender not in line, "性别没被限长"
    assert long_age not in line, "年龄没被限长"
    # 限长后仍只占各 8 字（+1 个省略号）
    assert len(line.split("|")[0].split("=")[1].split("/")[0].strip()) <= GENDER_MAX_CHARS + 1
    assert len(line.split("|")[0].split("=")[1].split("/")[1].strip()) <= AGE_MAX_CHARS + 1


def test_the_hard_fact_caps_are_pinned():
    """⚠ 口径**写死** ⇒ 改被测模块的常量必红（否则是假绿）。"""
    from application.engine.services import memory_engine as me

    assert me.IDENTITY_GENDER_MAX_CHARS == GENDER_MAX_CHARS
    assert me.IDENTITY_AGE_MAX_CHARS == AGE_MAX_CHARS
    assert me.LOCATION_DESCRIPTION_MAX_CHARS == LOCATION_DESC_MAX_CHARS


# ── ② 地点白名单带「类型 + 一句话描述」──────────────────────────────────


def test_the_location_whitelist_carries_type_and_description():
    """⚠ 模型必须知道「这些地方**是什么地方**」⇒ 场景写错才有防线。

    ⚠ 锚点必须**唯一**：类型标签带全角括号（描述里不会再出现「（建筑）」）
    ⇒ 否则描述里恰好含「建筑」二字时，这条会**假绿**（变异验证 M4 实测到）。
    """
    lines = location_whitelist_lines(
        [
            SimpleNamespace(
                name="回声戏院",
                location_type="building",
                description="光绪末年盐商程家出资营建的共鸣所在，台板下埋九口老铜缸。",
            )
        ]
    )

    body = "\n".join(lines)
    assert "回声戏院" in body
    assert "（建筑）" in body, f"地点类型没进白名单：{body!r}"
    assert "九口老铜缸" in body, f"地点描述没进白名单：{body!r}"


def test_the_location_type_is_rendered_as_a_chinese_label():
    """⚠ 类型存的是**英文枚举** ⇒ 中文提示词里要翻成**中文标签**。"""
    lines = location_whitelist_lines(
        [
            SimpleNamespace(name="临江市", location_type="city", description="通商埠城。"),
            SimpleNamespace(name="九缸地窖", location_type="realm", description="沉声之处。"),
        ]
    )

    body = "\n".join(lines)
    assert "（城市）" in body, body
    assert "（领域）" in body, body
    # ⚠ 英文枚举**不许**原样混进中文提示词
    assert "city" not in body and "realm" not in body, body


def test_the_location_description_is_capped_and_marked():
    """⚠ 描述限 40 字 ⇒ 砍了要**留痕**（模型得知道这句话没写完）。"""
    long_desc = "九口老铜缸收着满城未散的旧声，欠账的票在此一张张封好声，" * 3

    lines = location_whitelist_lines(
        [SimpleNamespace(name="九缸地窖", location_type="realm", description=long_desc)]
    )

    body = "\n".join(lines)
    assert long_desc not in body, "描述没被限长"
    assert "…" in body, f"描述被限长却没有留痕：{body!r}"


def test_both_sides_render_the_same_location_lines():
    """⚠ 两侧**同一份** ⇒ 生成侧锁的与审计侧比的是同一件事。"""
    builder = _builder()
    locations = [
        SimpleNamespace(name="九缸地窖", location_type="realm", description="戏院地底沉声之处。"),
        SimpleNamespace(name="济生当", location_type="faction", description="只收不问的当铺。"),
    ]
    bible = _Bible([_Char("甲")], locations=locations)

    generated = builder._build_from_bible(bible, current_chapter=1)
    facts = builder._facts_from_bible(bible, current_chapter=1)
    joined = "\n".join(facts)

    for loc in locations:
        assert loc.name in generated and loc.name in joined, loc.name
        assert loc.description in generated and loc.description in joined, loc.name


def test_a_location_without_type_or_description_still_gets_a_line():
    """⚠ 没有类型/描述 ⇒ **只给名字**（不许因为缺字段就整条丢掉）。"""
    lines = location_whitelist_lines(
        [SimpleNamespace(name="无名巷", location_type="", description="")]
    )

    body = "\n".join(lines)
    assert "无名巷" in body, body


# ── ③ 生产形状：补完两类硬事实后，事实锁**仍然整条送达**───────────────


def _production_shaped_bible() -> _Bible:
    """生产书形状的样本（字段长度按 ``prodrealtest-001`` 实测值造）。

    ⚠ 实测（2026-10-29，``prodrealtest-001`` 第 5 章）：
      角色 8 个（名字 3~10 字 / 描述 61~84 字 / 公开面 ~50 字 / 秘密 ~80 字
      ⇒ 限长到 40 / POV ~35 字 / 每人 3 条关系）
      地点 11 个（描述 89~132 字 ⇒ 限长到 40）
      时间线 4 条（~100 字/条）
    ⇒ 补完性别/年龄 + 地点类型/描述后，事实锁 = **4667 字**。

    ⚠ 名字用**长度相当**的虚构名（不复制生产数据），但**长度必须相当**
      ⇒ 长度直接决定各节字符数 ⇒ 名字太短 ⇒ 样本偏小 ⇒ 守卫会空过。
    """
    names = [
        "顾长风",
        "裴二娘（化名裴双）",
        "柳听雪（艺名雪娘）",
        "老瞎子（四十三岁）",
        "韩铁山",
        "苗小满",
        "段九（段守拙，人称九爷）",
        "何守拙",
    ]
    genders = ["女", "男", "女", "男", "男", "男", "男", "男"]
    ages = ["25", "61", "31", "68", "56", "44", "49", "殁年58"]
    rel_types = [
        "上下级/敬疑交加",
        "求教/被拦",
        "互相试探",
        "未察觉的对手",
        "搭档",
        "买卖",
        "案眼遗声",
        "师兄弟/亏欠",
        "隐身的恩人",
        "猎物或听众",
        "旧案的债主",
        "帘后的手",
        "互试互用",
        "不知恩人身份",
        "旧情分",
        "银钱拿捏",
        "拱手不交心",
        "提拔与压制",
        "一桩旧买卖",
        "老上司",
        "未知的债主",
        "同乡",
        "对头",
        "旧识",
    ]
    characters = []
    for index in range(8):
        characters.append(
            _Char(
                names[index],
                description="主角 - 巡捕房唯一的探员，追查旧戏院三桩命案时发现自己就是第四个收票人，"
                "既要揪出送票的人，又忍不住想听懂死人的声音。" + "描述" * 10,
                public_profile="房里挂着名目的探员，同僚眼里的冷面笔杆子，专接没人愿接的陈案碎案，"
                "见了她都自觉把话说圆，从不肯轻易开口。",
                hidden_profile=(
                    "第四张戏票此刻就在她大衣内袋，她尚未声张；她天生耳聪，是块收声人的料，"
                    f"这件事她自己也只是隐约觉出不对，{index}。"
                    "（POV防火墙：她的视角在后台认人之前，不得确知送票人身份；"
                    "第四张票的来历在她的卷宗里只能是「无从查证」。）"
                ),
                mental_state="绷紧的孤勇，不敢松手",
                gender=genders[index],
                age=ages[index],
                relationships=[
                    {"type": rel_types[(index * 3 + k) % len(rel_types)],
                     "target": names[(index + k + 1) % 8]}
                    for k in range(4)
                ],
            )
        )
    locations = [
        SimpleNamespace(
            name=f"地点{index}",
            location_type=["region", "faction", "building", "city", "realm"][index % 5],
            # ⚠ 长度按生产实测（89~132 字）⇒ **限长后**输出不变，
            #   但「限长被拆掉」时会把事实锁顶爆 ⇒ 预算守卫才有牙齿。
            description=(
                "戏院地底沉声之处，九口老铜缸收着满城未散的旧声，欠账的票在此一张张封好声；"
                "老检场人栖身缸间，夜夜贴着头顶木腔听人走路的声音，送票人的巢穴与聆音代价的实证。"
            ),
        )
        for index in range(11)
    ]
    timeline = [
        SimpleNamespace(
            timestamp="乙亥年冬",
            event="旧戏院封声班在大火中焚班，甲字辈七人殁于后台，班簿与铜缸一并压进碎砖里，"
            "此后多年无人再提这出戏的名字，只余每夜台下空座渐多。",
        ),
        SimpleNamespace(
            timestamp="十二年前秋",
            event="沈氏幼子听信水声溺亡于老河湾，案卷当日封存，证词只剩一句「水里有人在唤他」，"
            "此后老河湾的枯水滩涂出养声的乌泥，贴着湾打上来的鱼都带铜腥。",
        ),
        SimpleNamespace(
            timestamp="三年前春",
            event="巡捕房三桩命案并案，卷宗在移交当日只剩被抽走半册的空档，"
            "结案人亲手把三桩案摁成意外，档案房里乙亥年原卷此后再无人调阅。",
        ),
        SimpleNamespace(
            timestamp="上月",
            event="戏院重开，夜里亮灯台下空座渐多，猫不进、野狗不前，"
            "老检场人不领工钱只求一碗饭，铜铃无风自响，引声灯压在摊底从不出亮。",
        ),
    ]
    bible = _Bible(characters, locations=locations)
    bible.timeline_notes = timeline
    return bible


def test_the_production_shaped_fact_lock_is_delivered_whole():
    """⚠ 补了两类硬事实后，事实锁**仍然整条送达**（不触发截断）。

    ⇒ 生产实测：4667 字 < 每块上限 5000 ⇒ **两道刀都不切**。
    ⇒ 这条守卫钉住「补内容不许把硬事实挤出预算」。
    """
    builder = _builder()
    text = builder._build_from_bible(_production_shaped_bible(), current_chapter=5)

    cap = get_prose_context_runtime_settings().max_chars_per_slot
    assert len(text) <= cap, (
        f"生产形状的事实锁变成 {len(text)} 字 > 每块上限 {cap} ⇒ 硬事实会被切掉"
    )


def test_the_production_shaped_sample_matches_the_measured_shape():
    """⚠ **前提守卫**：样本必须与生产实测**同量级**（否则上面那条验的是空气）。"""
    builder = _builder()
    text = builder._build_from_bible(_production_shaped_bible(), current_chapter=5)

    drift = abs(len(text) - PRODUCTION_FACT_LOCK_CHARS) / PRODUCTION_FACT_LOCK_CHARS
    assert drift <= SHAPE_TOLERANCE, (
        f"样本形状漂了：{len(text)} 字 vs 生产实测 {PRODUCTION_FACT_LOCK_CHARS} 字"
        f"（偏差 {drift:.0%}）⇒ 守卫样本必须与生产一致"
    )


def test_every_hard_constraint_of_the_production_shape_survives():
    """⚠ 补完两类硬事实后，**8/8 个 POV 防火墙**仍然全到（第 24 批的成果）。"""
    builder = _builder()
    text = builder._build_from_bible(_production_shaped_bible(), current_chapter=5)

    firewalls = [line for line in text.splitlines() if "POV防火墙" in line]
    assert len(firewalls) == 8, f"只活下来 {len(firewalls)}/8 个防火墙"
