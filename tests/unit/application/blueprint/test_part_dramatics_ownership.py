"""各部戏剧功能不许由代码预设（B-01）。

改造前 `_build_precise_macro_prompt` 里有一段按 index/parts 硬编码的
三段式：

```
if index == 1:        role, demand = "起源", "紧凑、抛出核心悬念、建立主角目标"
elif index == parts:  role, demand = "决战", "收束主线、终极对决、完成情绪兑现"
else:                 role, demand = "发展/深渊", "扩大容量、多线叙事、推动主角重大转变"
```

外加 `<ACT_PACING>` 的「前 1/3 铺垫，中 1/3 发展并设置小高潮，后 1/3 爆发
并收束」。

这段文字渲染进的是 **system prompt**（`planning-precise-macro/system.md:11`
的 `{pacing_guide}`），而且原文就放在 `# 精密推演铁律` 标题下 ——
用最高优先级告诉模型「必须按起源-发展-决战三段式走」。

**一个曾经把这条判成「不成立」的教训**：第一版探针只查 `prompt.user`，
发现「起源」「情节要求」「PACING」全都不在里面，差点得出「这段代码是死的」
的错误结论。实际它在 `prompt.system` 里。凡是验证「这段内容有没有进模型」，
必须把 system 和 user 一起看 —— 只看一半就会得出恰好相反的答案。

本文件锁两件事：
1. 代码写死的三段式措辞不再出现在**任何**提示词段落里；
2. 事实（各部章数分配 / 总幕数 / 平均每幕章数）仍然在 —— 不许连预算一起删掉。
"""
import ast
import pathlib
import sys

import pytest

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)


CODE_WRITTEN = [
    "起源",
    "决战",
    "发展/深渊",
    "情节要求",
    "抛出核心悬念",
    "终极对决",
    "情绪兑现",
    "前 1/3 铺垫",
    "中 1/3 发展",
    "后 1/3 爆发",
    "ACT_PACING",
]


def _prompt(parts: int = 3):
    from unittest.mock import Mock

    svc = ContinuousPlanningService(
        story_node_repo=Mock(), chapter_element_repo=Mock(), llm_service=Mock()
    )
    skeleton = {
        "parts": [
            {
                "node_id": f"P{i}",
                "volumes": [{"node_id": f"V{i}_1", "acts": [{"node_id": f"A{i}_1_1"}]}],
            }
            for i in range(1, parts + 1)
        ]
    }
    return svc._build_precise_macro_prompt(
        {"characters": [], "timeline_notes": []},
        target_chapters=120,
        structure_preference={
            "parts": parts, "volumes_per_part": 1, "acts_per_volume": 1
        },
        skeleton=skeleton,
    )


@pytest.mark.parametrize("parts", [1, 2, 3, 5])
def test_no_code_written_three_stage_wording_reaches_any_prompt_section(parts):
    """起源/决战/发展-深渊 与三段式节奏建议不得出现在 system 或 user 任何一段。"""
    prompt = _prompt(parts)
    whole = f"{prompt.system}\n{prompt.user}"

    hits = [w for w in CODE_WRITTEN if w in whole]
    assert hits == [], f"代码代写的创作判断仍进了提示词: {hits}"


def test_chapter_budget_facts_are_still_present():
    """不许为了去掉创作判断把章数预算一起删掉 —— 那是代码该守的不变量。

    B-02 之后**留在**提示词里的事实是：全书总章数、部数、总幕数、平均每幕。
    **逐部的章数分配**交给了 AI（那是创作判断）⇒ 「第N部：分配 X 章」不该再出现。
    """
    prompt = _prompt(3)

    assert "PACING_GUIDE" in prompt.system
    assert "全书共 120 章" in prompt.system
    assert "分给 3 部" in prompt.system
    assert "总幕数：3 幕" in prompt.system
    # B-02：逐部分配不再由代码喂给模型
    assert "第1部：分配" not in prompt.system
    assert "第3部：分配" not in prompt.system


def test_the_allocation_sums_to_target_chapters():
    """各部章数之和 = 目标章数 —— 但**由 AI 给**，代码只核对不改写（B-02）。

    B-02 之后：
    - 提示词**不再**逐部给章数（那是代码代做的分配）；
    - 各部之和的不变量由**提示词的要求** + **代码的部级预算校验**保证；
    - 校验**只告警、不改写**（与幕级 B-03 同纪律：
      纠正等于替 AI 做决定）。
    """
    from application.blueprint.services.act_chapter_budget import (
        check_level_chapter_budget,
    )

    prompt = _prompt(3)
    assert "第1部：分配" not in prompt.system

    # 各部之和正确 ⇒ 无告警
    ok = check_level_chapter_budget(
        [{"estimated_chapters": 40}, {"estimated_chapters": 60}, {"estimated_chapters": 20}],
        120,
        level_label="部",
    )
    assert ok["ok"] is True
    assert ok["warning"] == ""

    # 不符 ⇒ 告警但**不改写**（保留 AI 原值）
    bad = check_level_chapter_budget(
        [{"estimated_chapters": 10}, {"estimated_chapters": 10}, {"estimated_chapters": 10}],
        120,
        level_label="部",
    )
    assert bad["ok"] is False
    assert "部级章数之和" in bad["warning"]


def test_dramatic_function_is_explicitly_handed_to_the_model():
    """改造后要**明说**这段由模型自己定，否则模型可能沿用旧模板的惯性。"""
    prompt = _prompt(3)

    assert "由你依据剧情自行判断" in prompt.system
    assert "戏剧定位由你决定" in prompt.system


def test_budget_no_longer_sits_under_the_iron_law_heading():
    """`{pacing_guide}` 原先挂在「精密推演铁律」下面。

    铁律是最高优先级。即便内容只剩事实，挂在那个标题下仍会暗示「这是命令」。
    改造后它有自己的一级标题，且戏剧定位另有明示。
    """
    prompt = _prompt(2)

    iron_law = prompt.system.split("# 精密推演铁律")[1].split("#")[0]
    assert "PACING_GUIDE" not in iron_law, "章数预算仍挂在铁律标题下"
    assert "# 章数预算" in prompt.system


def test_source_no_longer_hardcodes_the_three_stage_roles():
    """静态守卫：三个 role 常量与 demand 文本不得被人日后写回来。

    用 ast 精确扫字符串常量，不用 `contains` 子串匹配 ——
    本文件自己也含这些词，用子串匹配会自我命中。
    """
    import application.blueprint.services.continuous_planning_service as mod

    src = pathlib.Path(mod.__file__).read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    # 只查「作为代码里的字符串常量」，docstring 里的历史说明不算
    forbidden = {"起源", "决战", "发展/深渊", "前 1/3 铺垫，中 1/3 发展并设置小高潮，后 1/3 爆发并收束。"}
    assert not (constants & forbidden), f"三段式常量回来了: {constants & forbidden}"


def _repo_root() -> pathlib.Path:
    """从服务模块的位置往上四级到仓库根。

    不用 `infrastructure.ai.prompt_packages.__file__` —— 那是 namespace
    package，`__file__` 是 None（第一次跑就是这么炸的）。
    """
    src = pathlib.Path(ContinuousPlanningService.__module__.replace(".", "/"))
    module_file = pathlib.Path(sys.modules[ContinuousPlanningService.__module__].__file__)
    del src
    return module_file.parent.parent.parent.parent


def test_prompt_package_still_renders_pacing_guide():
    """反向守卫：`{pacing_guide}` 必须仍被 system.md 引用。

    第一版探针就是因为只查 user 而误判「这段代码是死的」。
    这条测试把那次教训固化成断言 —— 变量名被删或引用被去掉时立刻报警。
    """
    pkg = _repo_root() / "infrastructure/ai/prompt_packages/nodes/planning-precise-macro/system.md"
    assert pkg.exists(), f"找不到 prompt 包: {pkg}"
    text = pkg.read_bytes().decode("utf-8-sig")

    assert "{pacing_guide}" in text, "system.md 不再引用 pacing_guide，变量变成死参数"
