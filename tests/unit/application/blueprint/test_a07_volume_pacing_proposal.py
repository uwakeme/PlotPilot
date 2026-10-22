"""A-07：卷内节奏建议由 **AI 提案**（取代代码写死的结构论）。

背景
----
登记表 A-07 写：「体量档黑盒结构论注入」——「每卷宜安排 2~3 个大高潮节点」
「卷末留强钩子」整段结构论写死在
``application/core/v1_length_tiers.py::build_v1_structure_black_box_hint``，
拼进用户梗概前缀，**用户看不见、也改不了**。

核实（2026-10-22）补充
---------------------
那段结构论的**写路径**早已在 commit ``97ed95e9`` 删除
（``novel_service`` 现在只存作者原文）⇒ 该函数成了**零生产调用方的死函数**，
结构论在生产上**既不让用户看、也不让 AI 看**。

⇒ 本条真正要补的不是「删死代码」，而是**把那个创作决策还给 AI**。

设计（第一版做错过，教训在文件末尾）
--------------------------------------
初版为它**新建了一个独立 CPMS 节点**（``planning-volume-pacing-proposal``），
在精密模式主路径上**多打一次 LLM**。⇒ 既有测试的
``AsyncMock(side_effect=[...])`` 是**固定队列** ⇒ 多一次调用把队列打乱
⇒ 一批精密模式测试直接 ``StopAsyncIteration``。

⇒ 改为**并入 B-05 的 ``planning-structure-proposal`` 节点**：
**同一次**提案顺带给出卷内节奏 ⇒ 不额外打一次 LLM，
既省 token，也不打断既有调用序列。

守卫要点
--------
1. **出口**：卷内节奏必须真的进 ``<PACING_GUIDE>``（不是只造了个对象）；
2. **夹紧**：上限独立生效（``pacing_clamped=True`` 时必须说出口）；
3. **来源可分辨**：AI 提案 / 代码兜底 / 未评估三态可分辨；
4. **不编造**：判不出来不给数字、不冒充 AI 的判断；
5. **不额外调用**：并入 B-05 节点 ⇒ LLM 调用次数不变。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from application.blueprint.services import structure_proposal as structure_proposal_module
from application.blueprint.services.structure_proposal import (
    FALLBACK_CLIMAXES_PER_VOLUME,
    MAX_CLIMAXES_PER_VOLUME,
    SOURCE_AI,
    SOURCE_FALLBACK,
    StructureGridProposal,
    StructureProposalService,
    clamp_structure_grid,
)
from application.blueprint.services.structure_proposal_schema import (
    StructureProposalLlmPayload,
)
from infrastructure.ai.prompt_contracts.structure_proposal import (
    STRUCTURE_PROPOSAL_CONTRACT,
)


# ── 夹紧（代码保留的不变量） ─────────────────────────────────────────

def test_climaxes_are_clamped_to_the_upper_limit():
    """上限独立生效（不许模型提议「每卷 50 个高潮」）。"""
    proposal = clamp_structure_grid(
        {
            "parts": 3,
            "volumes_per_part": 3,
            "acts_per_volume": 3,
            "climaxes_per_volume": 999,
        },
        target_chapters=300,
    )

    assert proposal.climaxes_per_volume == 6
    assert proposal.pacing_clamped is True, "被夹过必须说出口，不许静默"
    assert proposal.source == SOURCE_AI


def test_climaxes_below_one_are_clamped_up():
    proposal = clamp_structure_grid(
        {
            "parts": 3,
            "volumes_per_part": 3,
            "acts_per_volume": 3,
            "climaxes_per_volume": 0,
        },
        target_chapters=300,
    )

    assert proposal.climaxes_per_volume == 1
    assert proposal.pacing_clamped is True


def test_valid_proposal_is_not_clamped():
    proposal = clamp_structure_grid(
        {
            "parts": 3,
            "volumes_per_part": 3,
            "acts_per_volume": 3,
            "climaxes_per_volume": 2,
            "hook_style": "证物指向下一个嫌疑人",
        },
        target_chapters=300,
    )

    assert proposal.climaxes_per_volume == 2
    assert proposal.pacing_clamped is False
    assert proposal.source == SOURCE_AI
    assert proposal.hook_style == "证物指向下一个嫌疑人"


# ── 三态：AI / 兜底 / 未评估 ─────────────────────────────────────────

def test_unusable_number_does_not_invent_one():
    """判不出来 ⇒ 用兜底值，**不编造**一个「模型说的」数字。"""
    proposal = clamp_structure_grid(
        {
            "parts": 3,
            "volumes_per_part": 3,
            "acts_per_volume": 3,
            "climaxes_per_volume": None,
        },
        target_chapters=300,
    )

    assert proposal.climaxes_per_volume == 3
    # 网格是 AI 给的，但**卷内节奏**判不出来 ⇒ 兜底必须**明说**
    assert proposal.source == SOURCE_AI
    assert "代码兜底" in proposal.as_pacing_guide()


def test_fallback_guide_says_it_is_a_fallback():
    """兜底必须**明说**是兜底 —— 不把兜底冒充成 AI 的判断。

    ⚠ 这条要盯的是**文案本身**：把源码里那句「（代码兜底，AI 未给出…）」删掉后，
    兜底对象照样返回、``source`` 照样是 ``fallback`` ——
    只有直接读**渲染出来的文本**才抓得住。
    """
    proposal = StructureGridProposal(3, 3, 3, source=SOURCE_FALLBACK)

    guide = proposal.as_pacing_guide()
    assert "代码兜底" in guide
    assert "AI 未给出" in guide


def test_fallback_marker_lives_in_the_source_not_only_in_a_test():
    """兜底标记必须写在**实现**里（不然换个实现就悄悄冒充成 AI 的判断）。

    ⚠ 第 3 批复审 S9：这条原先**读整个实现文件**，
    而「代码兜底」这个字符串**在注释里也有** ⇒ 把运行时的标记删掉、
    只留注释，这条守卫照样绿 ⇒ 它钉的是「文件里有这四个字」，
    不是「运行时会输出这四个字」。

    ⇒ 现在**剥掉注释**再查 ⇒ 只剩注释里的会被判失败。
    （上面的 ``as_pacing_guide()`` 那条才是**行为级**断言，两条互补。）
    """
    import re

    source = Path(structure_proposal_module.__file__).read_text(encoding="utf-8")
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_line = re.sub(r"//[^\n]*", "", without_block)
    assert "代码兜底" in without_line, (
        "实现的**代码**里没有兜底标记（只在注释里不算）⇒ 兜底会冒充成 AI 的判断"
    )


def test_ai_guide_does_not_claim_to_be_a_fallback():
    proposal = StructureGridProposal(3, 3, 3, source=SOURCE_AI, climaxes_per_volume=3, pacing_source=SOURCE_AI)

    guide = proposal.as_pacing_guide()
    assert "代码兜底" not in guide
    assert "3" in guide


# ── 输出契约 ─────────────────────────────────────────────────────────

def test_bool_is_not_accepted_as_a_count():
    """``True`` 不是 1 —— pydantic 的 lax 模式会把 true 读成 1。"""
    with pytest.raises(Exception):
        StructureProposalLlmPayload(climaxes_per_volume=True).model_dump()


def test_numeric_string_is_accepted():
    payload = StructureProposalLlmPayload(climaxes_per_volume="3").model_dump()
    assert payload["climaxes_per_volume"] == 3


# ── 出口：提案真的进 <PACING_GUIDE> ──────────────────────────────────

def test_proposal_lands_in_the_pacing_guide_of_the_macro_prompt():
    """**出口守卫**：AI 的卷内节奏建议必须真的进 ``<PACING_GUIDE>``。

    这条是本条的核心 —— 光有提案对象不算数，
    它必须出现在**生产提示词**里，否则还是「写对了但没接线」。
    """
    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    service = ContinuousPlanningService.__new__(ContinuousPlanningService)
    prompt = service._build_precise_macro_prompt(
        bible_context={"premise": "一个悬疑故事"},
        target_chapters=100,
        structure_preference={"parts": 3, "volumes_per_part": 3, "acts_per_volume": 3},
        skeleton={"parts": []},
        volume_pacing=StructureGridProposal(
            3,
            3,
            3,
            source=SOURCE_AI,
            climaxes_per_volume=4,
            pacing_source=SOURCE_AI,
            hook_style="卷末停在证物指向下一个嫌疑人的地方",
            reason="每卷承载一次完整追查闭环",
        ),
    )

    text = prompt.system + "\n" + prompt.user
    assert "<PACING_GUIDE>" in text
    assert "证物指向下一个嫌疑人" in text, "AI 给的卷末钩子没进提示词"
    assert "代码兜底" not in text, "AI 提案不该被写成兜底"


def test_pacing_guide_without_proposal_has_no_invented_pacing():
    """没有提案时（``None``），**不许**凭空生成一条节奏建议。"""
    from application.blueprint.services.continuous_planning_service import (
        ContinuousPlanningService,
    )

    service = ContinuousPlanningService.__new__(ContinuousPlanningService)
    prompt = service._build_precise_macro_prompt(
        bible_context={"premise": "一个悬疑故事"},
        target_chapters=100,
        structure_preference={"parts": 3, "volumes_per_part": 3, "acts_per_volume": 3},
        skeleton={"parts": []},
        volume_pacing=None,
    )

    text = prompt.system + "\n" + prompt.user
    assert "<PACING_GUIDE>" in text
    assert "高潮节点" not in text, "没有提案却写出了卷内节奏建议"


# ── CPMS 接线 ────────────────────────────────────────────────────────

def test_contract_exposes_the_pacing_limit():
    """契约必须把上限告诉模型（否则模型不知道能提议到多少）。"""
    assert "max_climaxes_per_volume" in STRUCTURE_PROPOSAL_CONTRACT.variables_schema.model_fields


def test_prompt_package_declares_the_pacing_variables():
    root = Path(__file__).resolve().parents[4]
    node_dir = root / "infrastructure/ai/prompt_packages/nodes/planning-structure-proposal"
    assert node_dir.is_dir(), f"缺少 prompt 包目录: {node_dir}"
    for name in ("package.yaml", "system.md", "user.md"):
        assert (node_dir / name).is_file(), f"缺少 {name}"

    system_md = (node_dir / "system.md").read_text(encoding="utf-8")
    assert "{max_climaxes_per_volume}" in system_md, "system.md 没把上限告诉模型"
    assert "climaxes_per_volume" in system_md
    assert "hook_style" in system_md

    package_yaml = (node_dir / "package.yaml").read_text(encoding="utf-8")
    assert "max_climaxes_per_volume" in package_yaml


def test_no_separate_pacing_node_was_created():
    """A-07：卷内节奏**并入** B-05 节点 ⇒ 不该有独立节点。

    独立节点会在精密模式主路径上**多打一次 LLM**
    ⇒ 既有测试的固定队列 stub 直接 ``StopAsyncIteration``。
    """
    from infrastructure.ai.prompt_keys import ALL_KEYS

    assert "planning-volume-pacing-proposal" not in ALL_KEYS
    root = Path(__file__).resolve().parents[4]
    assert not (
        root
        / "infrastructure/ai/prompt_packages/nodes/planning-volume-pacing-proposal"
    ).exists(), "不该有独立的卷内节奏节点"


# ── 死代码已清 ───────────────────────────────────────────────────────

def test_dead_black_box_builder_is_gone():
    """A-07：死函数已删（写路径早没了，它只是零调用方的遗留）。"""
    from application.core import v1_length_tiers

    assert not hasattr(v1_length_tiers, "build_v1_structure_black_box_hint")


def test_legacy_prefix_is_still_stripped():
    """但**读侧**清理必须留着（存量库兼容）。"""
    from application.core.v1_length_tiers import strip_generated_premise_prefixes

    legacy = (
        "【系统内部·叙事结构规划（体量档：标准商业连载）（勿向读者展示本段标题与标签）】\n"
        "规划目标体量：约 1,000,000 字。\n"
        "宏观节奏：每卷宜安排 2～3 个大高潮节点，卷末留强钩子。\n"
        "\n"
        "作者真正的梗概"
    )
    assert strip_generated_premise_prefixes(legacy) == "作者真正的梗概"


def test_context_assembler_uses_the_shared_strip_implementation():
    """**同一逻辑只写一份**：`context_assembler` 不许再手写一份 strip。

    它原先是第二份实现（只认「【系统内部·叙事结构规划」，漏了
    「【类型：…】/「世界观基调：…】」两条）⇒ 与共享实现行为不一致。

    ⚠ 两个坑：
    1. 这条要盯的是「有没有自己那套逻辑」，不是「结果对不对」——
       分叉实现对**多数**输入结果一样 ⇒ 行为级断言抓不住；
    2. 断言**必须避开注释**——那串字面量在自己的 docstring 里也会出现
       （踩过一次：查全文 ⇒ 命中的是自己写的说明文字）。
       ⇒ 这里只查**代码行**（去掉注释与 docstring）。
    """
    from application.engine.services import context_assembler as assembler_module

    source = Path(assembler_module.__file__).read_text(encoding="utf-8")
    body = source.split("def _strip_internal_premise_prefix", 1)[-1]
    body = body[: body.find("\n    def ", 1) if "\n    def " in body else len(body)]
    code_lines = []
    in_doc = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""'):
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    assert "strip_generated_premise_prefixes" in code, (
        "context_assembler 又自己手写了一份 strip ⇒ 同一逻辑写两份"
    )
    assert "【系统内部·叙事结构规划" not in code, (
        "context_assembler 里还有手写的 strip 逻辑"
    )


def test_context_assembler_strip_handles_both_prefix_kinds():
    """共享实现比原先那份**多认**两种前缀（这是合并带来的行为修正）。"""
    from application.engine.services.context_assembler import ContextAssembler

    legacy_type = "【类型：都市】\n\n作者梗概正文"
    assert ContextAssembler._strip_internal_premise_prefix(legacy_type) == "作者梗概正文"

    legacy_preset = "【世界观基调：冷硬】\n\n作者梗概正文"
    assert ContextAssembler._strip_internal_premise_prefix(legacy_preset) == "作者梗概正文"


def test_the_pacing_limits_are_pinned_by_value():
    """常量本身的值必须被钉住。"""
    assert MAX_CLIMAXES_PER_VOLUME == 6
    assert FALLBACK_CLIMAXES_PER_VOLUME == 3
