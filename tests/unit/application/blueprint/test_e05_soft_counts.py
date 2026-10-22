"""E-05：章前剧本的数量区间 / 阶段枚举是**软建议**，不是硬要求。

改造前
------
``planning-chapter-preplan/user.md`` 把四组东西写成硬要求：

- ``key_plot_points`` **5-10 个**
- ``key_dialogues`` **4-8 组**
- ``event_chain`` **6-10 个**
- ``phase`` **只能用** 触发、升级、爆发、收束

但**代码零校验**（preplan 服务只验「chapter_plan 是对象」与「七段渲染得出来」）
⇒ prompt 在要求模型去凑一批**没有依据的数字**。

而且：

- ``key_plot_points`` 在模型没给时是**从 event_chain 反推的派生物**
  ⇒ 「5-10 个」就算被强校验，也只约束那个派生物；
- ``event_chain`` 的 ``phase`` **没有任何代码消费者**
  （``format_event`` 只把它显示成「事件N（phase）」）⇒ 放宽安全。

改造后：三处数量都改成「通常是 N 个，按本章实际分量给，不必凑数」，
phase 改成建议用词；**七段结构契约在默认路径上仍然是硬的**。

⚠ E-01b（2026-10-22）之后，七段降为「**可选契约之一**」（段落可由 AI 自选），
结构开关默认**关闭** ⇒ 本文件守的「默认路径」仍然成立；
开关打开时的行为由 ``test_e01b_chapter_plan_structure.py`` 守。
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PROMPT_REL = "infrastructure/ai/prompt_packages/nodes/planning-chapter-preplan/user.md"
SERVICE_REL = "application/blueprint/services/chapter_preplanning_service.py"
RENDERER_REL = "application/blueprint/services/chapter_plan_renderer.py"

PHASE_WORDS = ("触发", "升级", "爆发", "收束")


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


def _string_constants(rel: str) -> list[str]:
    tree = ast.parse(_read(rel))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


# ======================================================================
#  A. 提示词侧
# ======================================================================


def test_the_prompt_no_longer_states_the_counts_as_hard_requirements():
    """提示词里不得再用「硬要求」的写法写那三组数量。"""
    text = _read(PROMPT_REL)

    for hard in (
        "抽取 5-10 个",
        "对话 4-8 组",
        "事件链 6-10 个",
        "phase 只能用",
    ):
        assert hard not in text, f"提示词里还留着硬写法：{hard!r}"


def test_the_prompt_marks_the_counts_as_soft():
    """数量要明确写成「通常……按本章实际分量给，不必凑数」。"""
    text = _read(PROMPT_REL)

    assert text.count("不必凑数") >= 3, "三处数量都该写明「不必凑数」"
    assert "通常是 5-10 个" in text
    assert "通常是 4-8 组" in text
    assert "通常是 6-10 个" in text
    assert "建议" in text and "更贴切的说法也可以" in text


def test_the_prompt_keeps_the_seven_section_contract_hard():
    """**七段结构契约仍然是硬的** —— 放宽的是数量与用词，不是结构。

    ⚠ E-01b 之后这句话只对**关闭结构开关时**成立（开关打开时七段降为
    「可选契约之一」，模型可以自选段落）⇒ 本条守的是**默认路径**：
    关闭时提示词里必须仍有「必须且只能覆盖七段字段」这条硬契约。
    """
    text = _read(PROMPT_REL)

    assert "必须且只能覆盖七段字段" in text
    assert "这一条是硬的" in text


# ======================================================================
#  B. 代码侧：不得偷偷把硬约束加回来
# ======================================================================


def test_the_code_does_not_hard_code_the_counts():
    """代码里不得出现那三组数字区间（AST 判字符串常量，注释不算）。"""
    constants = _string_constants(SERVICE_REL)

    for value in constants:
        for token in ("5-10", "4-8", "6-10"):
            assert token not in value, (
                f"代码里又出现了硬数量区间 {token!r}（{value!r}）"
            )


def test_the_code_does_not_validate_the_phase_enum():
    """代码里不得拿那四个阶段词做**硬校验**。

    两处都查（只查一处会漏）：

    1. 没有任何字符串常量**等于**那四个阶段词
       （写成常量元组也是硬编码的枚举 —— 只查比较会漏掉它）；
    2. 没有任何比较拿它们当操作数。

    注释不是 AST 节点 ⇒ 模块里出现在注释中的「收束」不受影响。
    """
    tree = ast.parse(_read(SERVICE_REL))

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in PHASE_WORDS:
            offenders.append(f"L{node.lineno}: 常量 {node.value!r}")
        if not isinstance(node, ast.Compare):
            continue
        for comparator in node.comparators:
            if (
                isinstance(comparator, ast.Constant)
                and isinstance(comparator.value, str)
                and comparator.value in PHASE_WORDS
            ):
                offenders.append(f"L{node.lineno}: 比较 {comparator.value!r}")
    assert not offenders, "代码里又出现了硬编码的阶段枚举：" + "；".join(offenders)


def test_the_execution_plan_contract_is_still_enforced_in_code():
    """⚠ E-01b：**结构**契约从「七段齐」改成「段落剧本 + 至少一段有内容」。

    E-05 放宽的是**数量与用词**；E-01b 放宽的是**结构本身**
    （七段降为可选契约之一，段落由 AI 选）⇒ 代码仍然强制的那条变成
    「渲染结果必须是一份**有内容**的段落剧本」。

    ⇒ 错误码随之改名（``..._seven_section_...`` 已名不副实）。
    """
    constants = _string_constants(SERVICE_REL)

    assert "chapter_preplan_requires_execution_plan" in constants
    assert "chapter_preplan_requires_non_empty_execution_plan" in constants
    assert "chapter_preplan_requires_chapter_plan_object" in constants


# ======================================================================
#  C. 放宽是安全的：phase 没有代码消费者
# ======================================================================


def test_the_renderer_accepts_any_phase_word():
    """phase 换成任何说法，渲染都不能崩（它只被显示）。"""
    from application.blueprint.services.chapter_plan_renderer import format_event

    for phase in ("触发", "升级", "爆发", "收束", "铺垫", "反转", ""):
        rendered = format_event({"phase": phase, "content": "他推门而入。"}, 1)
        assert isinstance(rendered, str) and rendered
        if phase:
            assert phase in rendered


def test_the_renderer_tolerates_a_missing_phase():
    """phase 缺失也要能渲染（软化后不该有硬依赖）。"""
    from application.blueprint.services.chapter_plan_renderer import format_event

    rendered = format_event({"content": "他推门而入。"}, 1)

    assert "他推门而入" in rendered
