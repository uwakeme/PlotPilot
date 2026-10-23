"""J-02：文风预设**语义匹配** —— 清单是 SSOT，判不出来不许回落。

背景
----
登记表 J-02 要的是「AI 语义匹配**或**用户自选」。第 4 批把「前端替作者决定」
那条（默认注入 ``presets[0]``）删掉了，但**「AI 语义匹配」那一步一直不存在**：
那 6 个文风公约预设只写在前端（``marketStylePresets.ts`` 的数组字面量），
后端拿不到清单 ⇒ 实现不了。

本文件钉住三组不变量
--------------------
1. **清单是 SSOT**：后端读 YAML、前端读由同一份 YAML 生成的 bundle
   ⇒ 不存在「两份真相」；
2. **判不出来不许回落成第一个预设**（回落 = 复活第 4 批删掉的那条），
   且「AI 说都不匹配」与「未评估」**可分辨**；
3. **正文不由模型写**（模型只给标识符，正文由代码从清单取）。

⚠ **前端零测试设施**（只有 ``npm run build``）⇒ 涉及前端的断言是
**文件内容级断言**，不是行为级断言。这条局限写在这里，不假装它更强。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from application.engine.services.ai_participation_settings import (
    AI_PARTICIPATION_FEATURES,
    ai_participation_enabled,
)
from application.world.services.style_preset_match_schema import (
    SOURCE_AI,
    SOURCE_NONE,
    normalize_suggestion,
)
from application.world.style_preset_catalog import (
    get_style_preset,
    get_style_presets,
    style_preset_values,
)
from infrastructure.ai.prompt_keys import ALL_KEYS, BIBLE_STYLE_PRESET_MATCH

ROOT = Path(__file__).resolve().parents[4]
YAML_PATH = ROOT / "shared/taxonomy/style_presets_cn_v1.yaml"
BUNDLE_PATH = ROOT / "frontend/src/domain/style/presets.bundle.json"
FRONTEND_PRESETS_TS = ROOT / "frontend/src/constants/marketStylePresets.ts"
FRONTEND_API_TS = ROOT / "frontend/src/api/bible.ts"
BIBLE_PANEL = ROOT / "frontend/src/components/panels/BiblePanel.vue"
BIBLE_ROUTE = ROOT / "interfaces/api/v1/world/bible.py"
NODE_DIR = ROOT / "infrastructure/ai/prompt_packages/nodes/bible-style-preset-match"
PROFILES = ROOT / "config/generation_profiles.yaml"


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _code_only(text: str) -> str:
    """剥掉整行注释（只留代码）。

    ⚠ 不剥注释的话，「解释此前怎么做的」注释会把守卫自己命中。
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*", "<!--", "*/")):
            continue
        kept.append(line)
    return "\n".join(kept)


# =====================================================================
# 一、清单是 SSOT（前后端同源）
# =====================================================================


def test_the_preset_list_lives_in_shared_taxonomy():
    """清单住在 ``shared/taxonomy``（SSOT），不是前端代码里。"""
    assert YAML_PATH.is_file(), "shared/taxonomy/style_presets_cn_v1.yaml 不存在"

    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8")) or {}
    presets = data.get("presets")
    assert isinstance(presets, list) and presets, "清单里没有预设"

    for preset in presets:
        for field in ("value", "label", "icon", "body"):
            assert str(preset.get(field) or "").strip(), (
                f"预设 {preset.get('value')!r} 缺 {field}"
            )
        assert isinstance(preset.get("aliases"), list)
        assert isinstance(preset.get("keywords"), list)


def test_the_frontend_bundle_is_generated_from_that_yaml():
    """前端 bundle 必须与 YAML **逐字段一致**（bundle 是生成物，不是手写的）。"""
    yaml_doc = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    bundle_doc = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))

    assert bundle_doc == yaml_doc, (
        "前端 bundle 与 shared/taxonomy 的 YAML 不一致 ⇒ "
        "有人手改了生成物（或改了 YAML 却没重新生成 bundle）"
    )


def test_the_frontend_no_longer_hardcodes_the_preset_list():
    """前端那份 TS **不许再手写**预设清单。"""
    code = _code_only(_read(FRONTEND_PRESETS_TS))

    # 任何一条预设的字面量都不许出现在前端源码里
    for preset in get_style_presets():
        assert f"'{preset.label}'" not in code, (
            f"预设 {preset.value} 的 label 又被手写进前端了"
        )
        assert f"'{preset.body}'" not in code, (
            f"预设 {preset.value} 的 body 又被手写进前端了"
        )

    assert "STYLE_PRESETS" in code, "前端没有改从 bundle 读清单"


def test_the_backend_reads_the_same_list():
    """后端读出来的清单必须与 YAML 一致（不是另一份硬编码）。"""
    yaml_doc = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8")) or {}
    expected = yaml_doc.get("presets") or []

    actual = get_style_presets()
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected):
        assert got.value == want["value"]
        assert got.label == want["label"]
        assert got.icon == want["icon"]
        assert got.body == want["body"]
        assert list(got.aliases) == list(want.get("aliases") or [])
        assert list(got.keywords) == list(want.get("keywords") or [])


def test_the_backend_and_the_frontend_agree_on_the_identifiers():
    """前后端认的 ``value`` 集合必须一致（否则 AI 推荐的 id 前端找不到）。"""
    bundle_doc = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    frontend_values = tuple(p["value"] for p in bundle_doc.get("presets") or [])

    assert style_preset_values() == frontend_values


# =====================================================================
# 二、判不出来不许回落（这是 J-02 最要紧的一条）
# =====================================================================


def test_a_value_outside_the_list_is_not_assessed():
    """模型返回清单外的 value ⇒ **未评估**（枚举封闭，不回落成第一个）。"""
    suggestion = normalize_suggestion(
        {"value": "xianxia_hot_v2", "reason": "听起来很像"},
        preset_lookup=get_style_preset,
    )

    assert suggestion.source == SOURCE_NONE, "清单外的值被当成了判定"
    assert suggestion.value is None, "清单外的值被塞进了结果"
    assert suggestion.body == "", "清单外的值带出了正文"


def test_the_empty_value_means_ai_said_no_match():
    """空 value + 给了理由 ⇒ **AI 明确判「都不匹配」**（这是判定，不是未评估）。"""
    suggestion = normalize_suggestion(
        {"value": "", "reason": "这本书是硬核科幻战争，六个预设都不合适"},
        preset_lookup=get_style_preset,
    )

    assert suggestion.source == SOURCE_AI
    assert suggestion.value is None
    assert suggestion.assessed is True
    assert suggestion.matched is False
    assert "科幻" in suggestion.reason


def test_an_empty_answer_with_no_reason_is_not_assessed():
    """空 value + 没理由 ⇒ 什么都没说 ⇒ 未评估。"""
    suggestion = normalize_suggestion(
        {"value": "", "reason": ""},
        preset_lookup=get_style_preset,
    )

    assert suggestion.source == SOURCE_NONE
    assert suggestion.assessed is False


def test_ai_saying_no_and_not_being_evaluated_are_distinguishable():
    """「AI 说都不匹配」与「没评估」必须**可分辨**。"""
    said_no = normalize_suggestion(
        {"value": "", "reason": "都不合适"}, preset_lookup=get_style_preset
    )
    not_assessed = normalize_suggestion(
        {"value": "不存在的预设", "reason": "x"}, preset_lookup=get_style_preset
    )

    assert said_no.source == SOURCE_AI
    assert not_assessed.source == SOURCE_NONE
    assert said_no.source != not_assessed.source
    # 两者的 value 都是 None ⇒ 只能靠 source 区分 ⇒ source 必须不同
    assert said_no.value is None and not_assessed.value is None


# =====================================================================
# 三、正文不由模型写
# =====================================================================


def test_the_body_comes_from_the_catalog_not_from_the_model():
    """命中的正文必须来自清单（模型说什么就写什么 = 代码替模型说话）。"""
    suggestion = normalize_suggestion(
        {"value": "xianxia_hot", "reason": "宗门升级线"},
        preset_lookup=get_style_preset,
    )

    assert suggestion.source == SOURCE_AI
    assert suggestion.value == "xianxia_hot"
    assert suggestion.body == get_style_preset("xianxia_hot").body
    assert suggestion.label == get_style_preset("xianxia_hot").label


def test_a_model_supplied_body_is_ignored():
    """模型就算多嘴给了 body，也**不许**被采纳。"""
    suggestion = normalize_suggestion(
        {
            "value": "xianxia_hot",
            "reason": "宗门升级线",
            "body": "模型自己编的公约",
        },
        preset_lookup=get_style_preset,
    )

    assert suggestion.body == get_style_preset("xianxia_hot").body
    assert "模型自己编的公约" not in suggestion.body


# =====================================================================
# 四、服务行为
# =====================================================================


class _FakeGateway:
    """打桩网关：**校验**仍走真实契约（不变量是真被测的）。"""

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
async def test_the_service_returns_the_matched_preset(monkeypatch):
    svc, gateway = _service(
        monkeypatch,
        json.dumps(
            {"value": "mystery", "reason": "线索回收与真凶反转是主线"},
            ensure_ascii=False,
        ),
    )

    suggestion = await svc.suggest(novel_id="n-1", genre="悬疑", premise="一场密室杀人")

    assert suggestion.source == SOURCE_AI
    assert suggestion.matched is True
    assert suggestion.value == "mystery"
    assert suggestion.body == get_style_preset("mystery").body
    # 题材与梗概要进提示词
    assert gateway.variables["genre"] == "悬疑"
    assert gateway.variables["premise"] == "一场密室杀人"


@pytest.mark.asyncio
async def test_the_candidates_shown_to_the_model_carry_no_preset_body(monkeypatch):
    """候选行只给**题材特征**，不给完整公约正文。

    ⚠ 给了正文有两个坏处：撑爆上下文；模型会去「抄公约」而不是「判题材」。
    """
    svc, gateway = _service(
        monkeypatch, json.dumps({"value": "mystery", "reason": "x"}, ensure_ascii=False)
    )

    await svc.suggest(novel_id="n-1", genre="悬疑")

    candidates = gateway.variables["candidates"]
    for preset in get_style_presets():
        assert preset.value in candidates, f"候选里缺 {preset.value}"
        assert preset.body not in candidates, (
            f"候选里混进了 {preset.value} 的完整正文"
        )


@pytest.mark.asyncio
async def test_the_service_reports_not_assessed_when_the_call_fails(monkeypatch):
    import application.world.services.style_preset_matcher as mod

    class _BoomLLM:
        async def generate(self, prompt, config):
            raise RuntimeError("endpoint down")

    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway())
    svc = mod.StylePresetMatcher(_BoomLLM())

    suggestion = await svc.suggest(novel_id="n-1")

    assert suggestion.source == SOURCE_NONE
    assert suggestion.value is None
    assert suggestion.body == ""


@pytest.mark.asyncio
async def test_the_master_switch_off_means_no_model_call(monkeypatch):
    """总闸关闭 ⇒ 一次模型都不调，且如实标成「未评估」。"""
    import application.world.services.style_preset_matcher as mod
    from types import SimpleNamespace as NS

    monkeypatch.setattr(
        "application.engine.services.ai_participation_settings.get_config",
        lambda: NS(ai_participation={"master_enabled": False, "disabled_features": []}),
    )
    monkeypatch.setattr(mod, "get_prompt_gateway", lambda: _FakeGateway())
    llm = _StubLLM(json.dumps({"value": "mystery", "reason": "x"}, ensure_ascii=False))
    svc = mod.StylePresetMatcher(llm)

    suggestion = await svc.suggest(novel_id="n-1")

    assert llm.calls == 0, "总闸关闭后仍然打了模型"
    assert suggestion.source == SOURCE_NONE
    assert suggestion.value is None


# =====================================================================
# 五、接线
# =====================================================================


def test_the_node_key_is_registered():
    assert BIBLE_STYLE_PRESET_MATCH in ALL_KEYS, "新节点 key 没登记进 ALL_KEYS"
    assert "style_preset_match" in AI_PARTICIPATION_FEATURES, "能力没登记进总闸表"


def test_the_node_package_exists():
    for name in ("package.yaml", "system.md", "user.md"):
        assert (NODE_DIR / name).is_file(), f"提示词包缺 {name}"


def test_the_generation_profile_is_low_temperature():
    """这是**判读题** ⇒ 温度要低（要稳）。"""
    profiles = yaml.safe_load(PROFILES.read_text(encoding="utf-8")) or {}
    profile = profiles.get("style_preset_match")

    assert isinstance(profile, dict), "缺 style_preset_match 生成画像"
    assert float(profile.get("temperature", 1.0)) <= 0.3, "判读题的温度不该高"


def test_the_prompt_files_hold_no_maintainer_notes():
    """提示词模板里**只能有给模型看的内容**。

    ⚠ 变更理由 / issue 编号一旦进了模板，就变成模型看到的一条「规则」。
    """
    for name in ("system.md", "user.md"):
        text = (NODE_DIR / name).read_text(encoding="utf-8")
        for marker in ("J-02", "登记表", "G-04", "E-07", "变异", "守卫"):
            assert marker not in text, (
                f"{name} 里出现了面向维护者的说明「{marker}」⇒ 它会随提示词喂给模型"
            )


# =====================================================================
# 六、API 契约与前端入口
# =====================================================================


def test_the_api_endpoint_exists():
    """新接口存在，且是 POST。"""
    from interfaces.api.v1.world.bible import router

    hits = [
        route
        for route in router.routes
        if getattr(route, "path", "").endswith(
            "/novels/{novel_id}/bible/style-preset-suggestion"
        )
    ]
    assert hits, "没有 style-preset-suggestion 路由"
    assert "POST" in hits[0].methods


def test_the_response_model_keeps_the_three_states_apart():
    """响应必须能区分「匹配到 / AI 说都不匹配 / 未评估」。"""
    from interfaces.api.v1.world.bible import StylePresetSuggestionResponse

    fields = set(StylePresetSuggestionResponse.model_fields)
    for field in ("value", "source", "matched", "assessed", "reason"):
        assert field in fields, f"响应模型缺 {field}"


def test_the_frontend_calls_the_new_endpoint():
    api = _code_only(_read(FRONTEND_API_TS))
    assert "suggestStylePreset" in api
    # ⚠ 必须剥掉注释再查路径 —— 否则命中的会是**注释里**那行
    #   （docstring 里也写了同一个路径）⇒ 判据落在注释上 ⇒ 守卫假绿。
    #   （第 9 批变异验证 M18 实测：改错代码里的 URL 时这条守卫当时照样绿。）
    assert "/bible/style-preset-suggestion" in api, "前端调的不是那个接口"


def test_the_frontend_never_writes_the_ai_recommendation_into_the_bible():
    """AI 的推荐**只许预选**，写进 Bible 的仍然只有作者点的「应用」。"""
    code = _code_only(_read(BIBLE_PANEL))

    assert "stylePresetSuggestion.value.body" not in code, (
        "AI 推荐返回的 body 被直接写进了 Bible"
    )
    assert "stylePresetSuggestion.body" not in code, (
        "AI 推荐返回的 body 被直接写进了 Bible"
    )
    # 唯一写 style_notes 的地方仍然是「应用预设」那一条
    assert code.count("state.value.style_notes = preset.body") == 1, (
        "写文风的路径不止「应用预设」这一条了"
    )


def test_when_ai_says_no_match_the_picker_is_cleared():
    """AI 说「都不匹配」时**清空选择**。

    ⚠ 否则选择器里预选着的还是第一个预设，作者顺手一点「应用」
    就把 AI 刚说不合适的公约套上了 —— 那是假出口。

    ⚠ 必须**逐分支**钉住：两个分支里都写着同一行
    ``selectedStylePresetValue.value = ''`` ⇒ 只查「函数体里有没有这一行」
    的话，删掉其中一支照样绿 ⇒ **假绿**。
    （第 9 批变异验证 M20 实测：删掉「AI 说都不匹配」那一支时，
    这条守卫当时是绿的。）
    """
    text = _read(BIBLE_PANEL)
    start = text.find("const requestStylePresetSuggestion")
    assert start > 0, "找不到 requestStylePresetSuggestion"
    body = text[start : start + 2000]

    clearing = "selectedStylePresetValue.value = ''"

    # 分支一：AI 判了「都不匹配」（assessed 且没匹配上）
    assessed_at = body.find("} else if (res.assessed) {")
    else_at = body.find("} else {", assessed_at)
    assert assessed_at > 0 and else_at > assessed_at, "找不到「AI 判了都不匹配」那一支"
    # ⚠ 窗口必须**切在分支边界**（下一个 ``} else {``）——
    #   固定 300 字符会把**下一支**的内容一起框进来
    #   ⇒ 删掉这一支的清空时，另一支的同一行仍能命中 ⇒ 守卫假绿。
    assessed_branch = body[assessed_at:else_at]
    assert clearing in assessed_branch, (
        "AI 说「都不匹配」时没有清空选择 ⇒ 作者一点「应用」就套上了预选的那个"
    )

    # 分支二：未评估（判不出来）同样不该预选
    catch_at = body.find("} catch", else_at)
    else_branch = body[else_at : catch_at if catch_at > 0 else else_at + 300]
    assert clearing in else_branch, (
        "未评估时预选着的还是第一个预设 ⇒ 作者一点「应用」就套上了它"
    )
