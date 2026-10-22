"""E-01b：章前剧本的**结构由 AI 选**（七段降为可选契约之一）。

改造前，「七段执行剧本」的**结构词汇表**是代码写死的
（渲染器把固定的七个字段映射成固定的七段标题）⇒ 两个后果：

- 不适用的段也得占位 —— 模型没填就渲染成「（待补充）」
  （生产库实测 592 章里 14 章命中：「主角状态变化」空 14 次、
  「角色关键决策」空 9 次、「爽点/反转设计」空 9 次）；
- 装不下七段的章节（纯对白章、闪回章、揭密章）**没有地方可写**。

改造后

- 模型可以在 ``chapter_plan.plan_sections`` 里**自选段落**；
- **空段不再渲染**（不再有「（待补充）」这种代码代写的占位）；
- 正文提示词**结构无关**（按「细纲里列出的段落/条目」锚定）；
- **代码保留的不变量「大纲必须落地」**（登记表 E-01）：
  渲染结果必须**至少有一段有内容**，否则不落空细纲。

开关
----
``config/performance.yaml`` 的 ``chapter_plan_structure.enabled``
（**默认关闭**）⇒ 关闭时渲染与章前剧本提示词**逐位一致**。
"""
from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.chapter_plan_renderer import (
    has_rendered_plan_content,
    render_chapter_execution_plan,
)
from application.blueprint.services.chapter_plan_structure import (
    chapter_plan_structure_enabled,
    plan_structure_prompt_flag,
)
from application.blueprint.services.chapter_planning_policy import (
    has_rendered_chapter_execution_plan,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PREPLAN_PROMPT = (
    REPO_ROOT
    / "infrastructure/ai/prompt_packages/nodes/planning-chapter-preplan/user.md"
)
PROSE_PROMPT = (
    REPO_ROOT
    / "infrastructure/ai/prompt_packages/nodes/chapter-prose-generation/user.md"
)

STANDARD_MARKERS = (
    "一、开篇切入点",
    "二、场景转换列表",
    "三、关键对话",
    "四、剧情事件链",
    "五、角色关键决策",
    "六、爽点/反转设计",
    "七、主角状态变化",
)


def _plan(**overrides):
    plan = {
        "opening_entry": "雨夜叩门",
        "scene_transitions": [
            {"scene": "客栈", "location": "城南", "cast": ["阿七"], "purpose": "起冲突"}
        ],
        "key_dialogues": [],
        "event_chain": [{"phase": "触发", "content": "陌生人找上门"}],
        "character_decisions": [],
        "payoff_reversals": [],
        "protagonist_state_change": {},
    }
    plan.update(overrides)
    return plan


def _render_template(path: pathlib.Path, **variables) -> str:
    """用 Jinja 渲染提示词模板（条件块的行为必须真的被验证）。"""
    from jinja2 import Environment

    env = Environment(
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    template = env.from_string(path.read_bytes().decode("utf-8"))
    return template.render(
        chapter_number=1,
        chapter_title="雨夜叩门",
        act_chapter_plan="{}",
        continuity_context="",
        finale_directive="",
        foreshadow_context="",
        pacing_directive="",
        **variables,
    )


# =====================================================================
# 一、开关：默认关闭
# =====================================================================


def test_the_switch_defaults_to_off():
    """结构开关**默认关闭** ⇒ 行为与改造前逐位一致。"""
    assert chapter_plan_structure_enabled() is False
    assert plan_structure_prompt_flag() == ""


def test_the_switch_lives_in_performance_yaml():
    """开关必须在 ``config/performance.yaml`` 里（可灰度、可回退）。"""
    text = (
        REPO_ROOT / "config/performance.yaml"
    ).read_bytes().decode("utf-8")

    assert re.search(
        r"chapter_plan_structure:\s*\n\s*enabled:\s*false", text
    ), "结构开关的默认值必须是 false（关）"


# =====================================================================
# 二、关闭时：逐位一致
# =====================================================================


def test_the_off_path_keeps_the_standard_seven_sections():
    """关闭 ⇒ 标准七段 + 空段占位（与改造前逐位一致）。"""
    rendered = render_chapter_execution_plan(
        _plan(), allow_custom_sections=False, drop_empty_sections=False
    )

    for marker in STANDARD_MARKERS:
        assert marker in rendered
    assert "（待补充）" in rendered


def test_the_off_path_ignores_the_custom_sections():
    """关闭 ⇒ 模型即便给了 ``plan_sections`` 也不采纳（逐位一致）。"""
    plan = _plan(
        plan_sections=[{"title": "闪回：三天前", "items": ["他还在那个码头上"]}]
    )

    rendered = render_chapter_execution_plan(
        plan, allow_custom_sections=False, drop_empty_sections=False
    )

    assert "闪回：三天前" not in rendered
    for marker in STANDARD_MARKERS:
        assert marker in rendered


def test_the_preplan_prompt_is_unchanged_when_the_switch_is_off():
    """关闭 ⇒ 章前剧本提示词与改造前**逐位一致**（条件块整块不渲染）。

    ⚠ 实测过：关闭态渲染结果与改造前的模板**逐字节一致**
    （连空行都不差）⇒ 这里把改动的几条**原文**钉住，防止条件块的
    else 分支被悄悄改写。
    """
    rendered = _render_template(PREPLAN_PROMPT, plan_structure_enabled="")

    assert "必须且只能覆盖七段字段" in rendered
    assert "这一条是硬的" in rendered
    assert "plan_sections" not in rendered
    # 条件块的 else 分支必须逐字保留改造前的写法
    assert "2. chapter_plan 会由系统渲染成七段细纲；禁止输出" in rendered
    assert "6. 正文模型会按 event_chain 顺序扩写" in rendered
    assert "5. chapter_plan 必须且只能覆盖七段字段" in rendered


# =====================================================================
# 三、打开时：结构由 AI 选
# =====================================================================


def test_the_custom_sections_are_rendered_when_allowed():
    plan = _plan(
        plan_sections=[
            {"title": "雨夜叩门", "items": ["客栈里多了一个不该出现的人"]},
            {"title": "闪回：三天前", "items": ["他还在那个码头上"]},
        ]
    )

    rendered = render_chapter_execution_plan(
        plan, allow_custom_sections=True, drop_empty_sections=True
    )

    assert "一、雨夜叩门：" in rendered
    assert "二、闪回：三天前：" in rendered
    assert "闪回：三天前" not in STANDARD_MARKERS


def test_empty_sections_are_dropped_and_the_rest_are_renumbered():
    """空段不渲染（不再有「（待补充）」），且序号**连续**（不许断号）。"""
    rendered = render_chapter_execution_plan(
        _plan(), allow_custom_sections=True, drop_empty_sections=True
    )

    assert "（待补充）" not in rendered
    assert "五、角色关键决策" not in rendered
    assert "六、爽点/反转设计" not in rendered
    # 剩下三段（一、二、三）重新连续编号
    assert "一、开篇切入点" in rendered
    assert "二、场景转换列表" in rendered
    assert "三、剧情事件链" in rendered
    assert "四、" not in rendered


def test_an_empty_custom_section_is_dropped():
    plan = _plan(
        plan_sections=[
            {"title": "有内容", "items": ["一句话"]},
            {"title": "没有内容", "items": []},
        ]
    )

    rendered = render_chapter_execution_plan(
        plan, allow_custom_sections=True, drop_empty_sections=True
    )

    assert "有内容" in rendered
    assert "没有内容" not in rendered


def test_the_preplan_prompt_offers_the_custom_structure_when_on():
    """打开 ⇒ 提示词给出 ``plan_sections`` 这条路（七段不再写死）。"""
    rendered = _render_template(PREPLAN_PROMPT, plan_structure_enabled="1")

    assert "plan_sections" in rendered
    assert "必须且只能覆盖七段字段" not in rendered
    # 开启后提示词里不许还留着「七段细纲」「event_chain 顺序」这种自相矛盾的说法
    assert "渲染成七段细纲" not in rendered
    assert "event_chain 顺序扩写" not in rendered


# =====================================================================
# 四、不变量：大纲必须落地
# =====================================================================


def test_a_plan_without_content_is_not_a_usable_plan():
    """只有标题行、或只有占位符 ⇒ 不算一份能落地的剧本。"""
    headers_only = "\n".join(f"{marker}：" for marker in STANDARD_MARKERS)
    placeholders_only = "\n".join(
        f"{marker}：\n（待补充）" for marker in STANDARD_MARKERS
    )

    assert has_rendered_plan_content(headers_only) is False
    # 「（待补充）」是**代码**替模型写的占位符 —— 模型其实没写这一段
    assert has_rendered_plan_content(placeholders_only) is False
    assert has_rendered_plan_content("一、开篇切入点：\n雨夜叩门") is True


@pytest.mark.asyncio
async def test_the_service_refuses_to_persist_a_plan_without_content(monkeypatch):
    """渲染不出内容 ⇒ 生成失败（**不落空细纲**）。"""
    import application.blueprint.services.chapter_preplanning_service as svc_mod
    from application.blueprint.services.chapter_preplanning_service import (
        ChapterPreplanningService,
    )

    class _Gateway:
        def render(self, contract, variables):
            return SimpleNamespace(prompt=SimpleNamespace(system="sys", user="usr"))

    monkeypatch.setattr(svc_mod, "get_prompt_gateway", lambda: _Gateway())

    llm = Mock()
    llm.generate = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(
                {"chapter_plan": {"opening_entry": "", "event_chain": []}},
                ensure_ascii=False,
            )
        )
    )

    svc = ChapterPreplanningService(
        llm_service=llm,
        chapter_repository=Mock(),
        story_node_repo=Mock(),
    )
    monkeypatch.setattr(svc_mod, "get_prompt_gateway", lambda: _Gateway())
    # ⚠ 打开结构开关：空段会被丢掉 ⇒ 渲染不出内容 ⇒ 必须拒收。
    #   （关闭时占位符算内容，与改造前逐位一致 —— 那条路径由别处守。）
    monkeypatch.setattr(svc_mod, "chapter_plan_structure_enabled", lambda: True)

    with pytest.raises(ValueError) as excinfo:
        await svc.ensure_execution_plan(novel_id="n-1", chapter_number=1)

    assert "chapter_preplan_requires_non_empty_execution_plan" in str(excinfo.value)


# =====================================================================
# 五、检测器：结构无关
# =====================================================================


def test_the_detector_accepts_any_numbered_structure():
    """结构由 AI 选 ⇒ 检测器不再要求「七段齐」。"""
    outline = "一、回忆插叙：\n他从码头走回那间屋\n二、正面交锋：\n刀出鞘"

    assert has_rendered_chapter_execution_plan(outline)


def test_the_detector_understands_arabic_section_numbers():
    """超过十段时序号退回阿拉伯数字（检测器必须一样认得）。"""
    outline = "\n".join(f"{index}、第{index}段：" for index in range(1, 13))

    assert has_rendered_chapter_execution_plan(outline)


def test_the_detector_rejects_out_of_order_sections():
    assert not has_rendered_chapter_execution_plan("二、场景转换列表：\n一、开篇切入点：")


def test_the_detector_still_rejects_forbidden_sections():
    outline = "一、开篇切入点：\n二、故事单元检查："

    assert not has_rendered_chapter_execution_plan(outline)


def test_the_detector_rejects_a_lightweight_outline():
    assert not has_rendered_chapter_execution_plan(
        "主事件：推进主事件\n开篇入口：承接前章后果"
    )


# =====================================================================
# 六、正文提示词：结构无关
# =====================================================================


def test_the_prose_prompt_does_not_hardcode_the_seven_sections():
    """正文按「**细纲里列出的段落**」锚定，而不是「七段」。"""
    text = PROSE_PROMPT.read_bytes().decode("utf-8")

    assert "七段结构为唯一剧情骨架" not in text
    assert "以【正文细纲】中的段落为唯一剧情骨架" in text
    assert "【正文细纲】里逐条列出的剧情事件全部必须落地" in text
    assert "章尾必须落在【正文细纲】最后一段的重大变化上" in text


def test_the_bundle_version_was_bumped():
    """改了提示词包 ⇒ bundle 版本必须 bump（否则 seed 不同步，运行时拿旧词）。"""
    import re

    meta = (
        REPO_ROOT / "infrastructure/ai/prompt_packages/bundle_meta.yaml"
    ).read_bytes().decode("utf-8")
    version_line = [ln for ln in meta.split("\n") if ln.startswith("version:")][0]
    version = version_line.split(":", 1)[1].strip().strip("'\"")
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    assert match, f"bundle 版本号格式不对：{version_line}"
    assert tuple(int(x) for x in match.groups()) >= (5, 39, 0), version_line
