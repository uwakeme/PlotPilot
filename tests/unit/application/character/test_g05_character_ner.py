"""G-05：新角色发现 —— **LLM 做 NER**（正则 + 黑名单作兜底）。

改造前
------
「本章出现了哪些不在 Bible 里的角色」是**代码用正则猜**的：
``re.findall(r"[一-龥]{2,4}", outline)`` ⇒ 任意 2-4 个汉字都成了人名候选。

而且那三处都坏了：

1. **AI 分支在生产上根本没接线** —— 「显式出场名单」来自 ``scene_director``，
   而两个路由都硬传 ``scene_director=None`` ⇒ 登记表说的「LLM 做 NER」
   其实是**待接线**的目标；
2. ``_looks_like_ephemeral`` 扫的是**整篇大纲** —— 大纲里只要出现过
   「侍卫/掌柜/…」里的任何一个词，**本章所有**候选都被判成一次性人物
   ⇒ 真名反而被降级；
3. 截断按**字典序**（``set`` + ``sorted``）⇒ 真名可能被噪声挤掉。

现在
----
- **AI 做 NER**：新节点 ``planning-character-ner`` 读本章大纲，给出
  「本章出场、且不在册的角色」名单（带证据）；
- **证据必须在大纲里真出现过**（代码校验）—— 模型的记忆不算数；
- AI 不可用 / 输出不合契约 ⇒ **未评估** ⇒ 回落原来的正则 + 黑名单；
- ephemeral 判定**只看名字本身**；
- 候选**按显式名单优先的顺序**排，不再字典序。
"""
from __future__ import annotations

import ast
import json
import pathlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.character.services.character_ner import (
    SOURCE_AI,
    SOURCE_NONE,
    CharacterNerService,
)
from application.character.services.character_narrative_kernel import (
    CharacterNarrativeKernel,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
CAST_ROUTE_REL = "interfaces/api/v1/world/cast.py"
KERNEL_REL = "application/character/services/character_narrative_kernel.py"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


# =====================================================================
# 一、AI NER 通道
# =====================================================================


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

    def __init__(self):
        self.variables = {}

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
    import application.character.services.character_ner as mod

    gateway = _FakeGateway()
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: gateway)
    return CharacterNerService(_StubLLM(content)), gateway


@pytest.mark.asyncio
async def test_the_ai_names_are_returned(monkeypatch):
    svc, _ = _service(
        monkeypatch,
        json.dumps(
            {"characters": [{"name": "沈砚", "evidence": "沈砚推门进来"}]},
            ensure_ascii=False,
        ),
    )

    result = await svc.extract(
        novel_id="n-1", outline="沈砚推门进来，把伞收在脚边。"
    )

    assert result.names == ["沈砚"]
    assert result.source == SOURCE_AI


@pytest.mark.asyncio
async def test_the_evidence_must_appear_in_the_outline(monkeypatch):
    """模型记错了 ⇒ 名字不可信 ⇒ 拒收（**不静默**）。"""
    svc, _ = _service(
        monkeypatch,
        json.dumps(
            {"characters": [{"name": "沈砚", "evidence": "沈砚在千里之外"}]},
            ensure_ascii=False,
        ),
    )

    result = await svc.extract(novel_id="n-1", outline="沈砚推门进来。")

    assert result.names == []
    assert result.rejected == ["沈砚"]


@pytest.mark.asyncio
async def test_known_characters_and_locations_are_excluded(monkeypatch):
    """已在册的角色与已知地点不要报。"""
    svc, _ = _service(
        monkeypatch,
        json.dumps(
            {
                "characters": [
                    {"name": "林风", "evidence": "林风推门进来"},
                    {"name": "客栈", "evidence": "客栈推门进来"},
                ]
            },
            ensure_ascii=False,
        ),
    )

    result = await svc.extract(
        novel_id="n-1",
        outline="林风推门进来，客栈很安静。",
        known_names=["林风"],
        location_names=["客栈"],
    )

    assert result.names == []


@pytest.mark.asyncio
async def test_an_unavailable_ai_is_not_assessed(monkeypatch):
    """AI 不可用 ⇒ **未评估**（调用方回落正则兜底）。"""
    import application.character.services.character_ner as mod

    class _BoomLLM:
        async def generate(self, prompt, config):
            raise RuntimeError("endpoint down")

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway())
    svc = CharacterNerService(_BoomLLM())

    result = await svc.extract(novel_id="n-1", outline="沈砚推门进来。")

    assert result.source == SOURCE_NONE
    assert result.names == []


@pytest.mark.asyncio
async def test_a_non_json_answer_is_not_assessed(monkeypatch):
    svc, _ = _service(monkeypatch, "沈砚")

    result = await svc.extract(novel_id="n-1", outline="沈砚推门进来。")

    assert result.source == SOURCE_NONE


@pytest.mark.asyncio
async def test_the_exclusion_lists_reach_the_prompt(monkeypatch):
    """排除名单要进提示词（模型才知道谁不用报）。"""
    svc, gateway = _service(
        monkeypatch,
        json.dumps({"characters": []}, ensure_ascii=False),
    )

    await svc.extract(
        novel_id="n-1",
        outline="沈砚推门进来。",
        known_names=["林风", "秦柔"],
        location_names=["客栈"],
    )

    assert "林风" in gateway.variables["known_names"]
    assert "客栈" in gateway.variables["location_names"]


# =====================================================================
# 二、接线（P0：AI 分支在生产上必须够得着）
# =====================================================================


def test_the_cast_route_passes_the_ai_scene_cast():
    """路由必须把 AI 的名单传进内核（此前硬传 None ⇒ AI 分支是死代码）。"""
    source = _read(CAST_ROUTE_REL)
    tree = ast.parse(source)

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "plan_cast":
            continue
        for kw in node.keywords:
            if kw.arg == "scene_director":
                found = True
                assert "await _ai_scene_cast(" in ast.unparse(kw.value), (
                    "scene_director 必须来自 AI NER，不能是硬编码的 None"
                )
    assert found, "plan_cast 调用里没有 scene_director 参数"


def test_the_ai_branch_is_reachable():
    """AI 名单通道有真实实现（不是空壳）。"""
    source = _read(CAST_ROUTE_REL)

    assert "async def _ai_scene_cast(" in source
    assert "CharacterNerService" in source
    assert "result.is_assessed" in source


# =====================================================================
# 三、内核三处修复
# =====================================================================


@dataclass
class _Char:
    id: str
    name: str
    importance: str = "supporting"

    @property
    def character_id(self):
        return SimpleNamespace(value=self.id)


@dataclass
class _Bible:
    characters: list
    locations: list = field(default_factory=list)


class _BibleRepo:
    def __init__(self, chars, locations=None):
        self.bible = _Bible(chars, locations or [])

    def get_by_novel_id(self, _novel_id):
        return self.bible


class _StoryNodeRepo:
    def get_by_novel_sync(self, novel_id):
        return []


def _kernel(chars=None, locations=None):
    return CharacterNarrativeKernel(
        bible_repository=_BibleRepo(chars or [], locations),
        story_node_repository=_StoryNodeRepo(),
    )


def test_the_ephemeral_check_does_not_look_at_the_outline():
    """⚠ 判据只看**名字**：大纲里出现「侍卫」不能把本章所有候选都判成一次性人物。

    两条都钉：
    - **结构上**：大纲已经不在判据的入参里 ⇒ 从根上杜绝「扫全文」这种写法回来；
    - **行为上**：名字里没有身份词就不是一次性人物。
    """
    import inspect

    kernel = _kernel()

    assert "outline" not in inspect.signature(kernel._looks_like_ephemeral).parameters
    assert kernel._looks_like_ephemeral("沈照") is False


def test_a_role_word_inside_the_name_is_ephemeral():
    """名字本身就带身份词 ⇒ 一次性功能人物。

    ⚠ 直接测规则本身：兜底那条正则 ``[一-龥]{2,4}`` 是**贪婪**的
    （「小二跑过来」会被切成「小二跑过」）⇒ 通过大纲构造不出干净的候选名，
    规则本身要单独钉住。
    """
    kernel = _kernel()

    assert kernel._looks_like_ephemeral("小二") is True
    assert kernel._looks_like_ephemeral("王掌柜") is True
    assert kernel._looks_like_ephemeral("路人甲") is True
    assert kernel._looks_like_ephemeral("沈砚") is False
    assert kernel._looks_like_ephemeral("林风") is False


def test_the_candidates_keep_the_explicit_order_not_dictionary_order():
    """显式名单在前（不再字典序）—— 真名不会被噪声挤掉。"""
    kernel = _kernel()

    candidates = kernel.detect_new_character_candidates(
        "n1",
        4,
        "赵九与甲乙丙丁在一起。",
        [_Char("c1", "林风")],
        scene_director={"characters": ["赵九"]},
    )

    assert candidates[0].name == "赵九"


def test_the_truncation_does_not_drop_the_explicit_names():
    """候选多于上限时，显式（AI 名单）必须活下来。

    ⚠ 隔离用例：要造**超过 10 个**候选（否则截断根本不触发，这条守不到），
    且显式名的字典序要**靠后**（排序一恢复它就会被截掉）。
    """
    kernel = _kernel()
    chunks = [
        f"{a}{b}{c}{d}"
        for a, b, c, d in (
            ("甲", "乙", "丙", "丁"),
            ("戊", "己", "庚", "辛"),
            ("壬", "癸", "子", "丑"),
            ("寅", "卯", "辰", "巳"),
            ("午", "未", "申", "酉"),
            ("戌", "亥", "云", "风"),
            ("霜", "刀", "剑", "影"),
            ("孤", "舟", "野", "鹤"),
            ("枫", "叶", "归", "途"),
            ("雪", "原", "追", "兵"),
            ("漠", "北", "孤", "狼"),
            ("潮", "起", "云", "涌"),
        )
    ]
    outline = "、".join(chunks)

    candidates = kernel.detect_new_character_candidates(
        "n1",
        4,
        outline,
        [_Char("c1", "林风")],
        scene_director={"characters": ["赵九"]},
    )

    names = [c.name for c in candidates]
    assert len(chunks) + 1 > 10, "样本要超过截断上限，否则这条守不到"
    assert len(names) <= 10
    assert "赵九" in names


def test_the_regex_fallback_still_produces_candidates():
    """AI 不可用 ⇒ 回落正则 + 黑名单（逐位一致的老路）。"""
    kernel = _kernel()

    candidates = kernel.detect_new_character_candidates(
        "n1", 4, "林风遇见沈照。", [_Char("c1", "林风")],
    )
    by_name = {c.name: c for c in candidates}

    assert "沈照" in by_name
    assert "林风" not in by_name


# =====================================================================
# 四、注册
# =====================================================================


def test_the_node_and_profile_are_registered():
    from infrastructure.ai.generation_profiles import generation_config_from_profile
    from infrastructure.ai.prompt_keys import ALL_KEYS, PLANNING_CHARACTER_NER
    from infrastructure.ai.prompt_seed.loader import load_seed_bundle

    assert PLANNING_CHARACTER_NER in ALL_KEYS
    _, prompts = load_seed_bundle()
    package_ids = {p.get("id") for p in prompts}
    assert PLANNING_CHARACTER_NER in package_ids
    assert generation_config_from_profile("character_ner") is not None
