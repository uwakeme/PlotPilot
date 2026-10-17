"""结构占位与流中断不得静默转正（B-10 / B-12）。

B-10 —— 精密模式先由代码搭出固定骨架（`第1部` / `第1卷` / `第1幕` + 空简介），
补全探针以前只走到幕级，于是部和卷根本没有修复通道：LLM 漏填就是空描述落库，
看起来像一次正常的结构规划。本文件锁定的是**补全清单里到底有没有部/卷**，
以及占位标题（即使简介已被填上）是否仍被点名。

B-12 —— 上游流中断时，已生成的那半截会被解析、修好，然后**当成完整结果返回**
（只有一条 warning）。半截可能有价值，丢掉它不对；但「这是半截」这个事实必须
随结果一起传下去，否则下游（预览、保存、继续生成）会把它当成一次完整生成。

全部是行为断言：不使用 `inspect.getsource` 之类的源码文本断言；测试只经过既有
公开接缝（`generate_macro_plan` / `_stream_macro_llm_text`），不直接依赖新增方法名，
以免「方法还不存在」这种实现细节冒充业务红灯。
"""
from unittest.mock import AsyncMock, Mock

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
    get_macro_plan_progress,
)
from domain.ai.value_objects.prompt import Prompt


# ======================================================================
#  替身
# ======================================================================


def _svc(llm_service=None):
    return ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=llm_service if llm_service is not None else Mock(),
    )


def _no_stream(*_args, **_kwargs):
    """返回没有 `__aiter__` 的值 → 走同步 generate 分支（不模拟流式中断）。"""
    return None


def _capture_renders(monkeypatch):
    """截住每次 CPMS 渲染的变量，返回列表（找补全提示词用）。"""
    captured = []

    def fake_render(_contract, variables):
        captured.append(variables)
        return Prompt(system="system", user=str(variables))

    monkeypatch.setattr(
        ContinuousPlanningService,
        "_render_contract_prompt",
        staticmethod(fake_render),
    )
    return captured


def _repair_block(captured):
    for variables in captured:
        if "incomplete_acts_block" in variables:
            return variables["incomplete_acts_block"]
    raise AssertionError("本轮根本没有生成补全提示词")


# 流中断：上游吐了一半就断线。
_TRUNCATED_STREAM = """{
  "parts": [
    {
      "title": "第一部",
      "volumes": [
        {
          "title": "第一卷",
          "acts": [
            {
              "title": "第一幕：灰鸦街醒来",
              "description": "主角重生后先在灰鸦街求生",
              "core_conflict": "隐藏异常并取得训练资格"
            },
            {
              "title": "第二幕：廉价训练舱",
              "description": "主角争夺第一批训练资源"
        """


async def _broken_stream(_prompt, _config):
    yield _TRUNCATED_STREAM
    raise RuntimeError("peer closed connection")


_COMPLETE_PLAN = """{
  "parts": [
    {
      "title": "第一部：寒门",
      "description": "寒门少年被卷入京师权斗",
      "volumes": [
        {
          "title": "第一卷：入京",
          "description": "从山村到京城的立足过程",
          "acts": [
            {
              "title": "第三幕：雪夜叩门",
              "description": "主角深夜入京，撞见命案",
              "core_conflict": "隐藏身份换取容身之处",
              "narrative_goal": "把主角拖入主线阴谋",
              "plot_points": ["进京", "撞见命案"],
              "key_characters": ["林渊"],
              "key_locations": ["都察院"],
              "emotional_arc": "戒备→惊惧"
            },
            {
              "title": "第四幕：朝堂余烬",
              "description": "主角被迫接触权贵",
              "core_conflict": "在权斗中站稳脚跟",
              "narrative_goal": "建立主角与朝堂的冲突面",
              "plot_points": ["见权贵", "受逼迫"],
              "key_characters": ["林渊", "裴尚书"],
              "key_locations": ["吏部"],
              "emotional_arc": "隐忍→爆发"
            }
          ]
        }
      ]
    }
  ]
}"""


# ======================================================================
#  B-10：补全探针必须覆盖部 / 卷级
# ======================================================================


async def test_precise_repair_covers_part_and_volume_nodes(monkeypatch):
    """LLM 只补好了一幕时，补全清单里必须同时有部、卷、其余幕。

    这是 B-10 的核心：探针以前只走到幕级，部和卷的 `description` 恒空落库，
    看上去就是一次「部部有简介」的结构规划。
    """
    captured = _capture_renders(monkeypatch)
    llm = AsyncMock()
    llm.stream_generate = _no_stream
    llm.generate = AsyncMock(side_effect=[
        """{"node_updates": [
            {
              "node_id": "A1_1_1",
              "title": "雪夜叩门",
              "description": "主角深夜入京",
              "narrative_goal": "把主角拖入主线阴谋",
              "plot_points": ["进京", "撞见命案"],
              "key_characters": ["林渊"],
              "key_locations": ["都察院"],
              "emotional_arc": "戒备→惊惧"
            }
        ]}""",
        """{"node_updates": []}""",
    ])
    svc = _svc(llm)

    await svc.generate_macro_plan(
        novel_id="novel-b10-repair-scope",
        target_chapters=40,
        structure_preference={"parts": 1, "volumes_per_part": 2, "acts_per_volume": 2},
    )

    block = _repair_block(captured)
    assert "P1" in block, f"部级节点没进补全清单：{block}"
    assert "V1_1" in block, f"卷级节点没进补全清单：{block}"
    assert "V1_2" in block, f"卷级节点没进补全清单：{block}"
    assert "A1_2_1" in block, f"其余幕没进补全清单：{block}"
    assert "A1_1_1" not in block, f"已经补全的幕不该再被点名：{block}"


async def test_placeholder_titles_are_named_even_when_descriptions_exist(monkeypatch):
    """简介被填上、标题还停在 `第1部` / `第1卷` 时，同样要点名补全。

    反过来锁住另一半：`第三幕：雪夜叩门` 这类真标题不构成占位。
    """
    captured = _capture_renders(monkeypatch)
    llm = AsyncMock()
    llm.stream_generate = _no_stream
    llm.generate = AsyncMock(side_effect=[
        # LLM 漏填部/卷标题，但把简介和唯一一幕都写全了
        """{"node_updates": [
            {"node_id": "P1", "description": "寒门少年被卷入京师权斗"},
            {"node_id": "V1_1", "description": "从山村到京城的立足过程"},
            {
              "node_id": "A1_1_1",
              "title": "雪夜叩门",
              "description": "主角深夜入京",
              "narrative_goal": "把主角拖入主线阴谋",
              "plot_points": ["进京", "撞见命案"],
              "key_characters": ["林渊"],
              "key_locations": ["都察院"],
              "emotional_arc": "戒备→惊惧"
            }
        ]}""",
        """{"node_updates": []}""",
    ])
    svc = _svc(llm)

    result = await svc.generate_macro_plan(
        novel_id="novel-b10-placeholder-title",
        target_chapters=20,
        structure_preference={"parts": 1, "volumes_per_part": 1, "acts_per_volume": 1},
    )

    block = _repair_block(captured)
    assert "P1" in block, f"标题仍是序号占位的部没有被点名：{block}"
    assert "V1_1" in block, f"标题仍是序号占位的卷没有被点名：{block}"
    assert "title" in block, f"缺失字段没写明是标题：{block}"
    assert "A1_1_1" not in block, f"真标题 + 全字段的幕被误判成占位：{block}"
    # 补全跑完仍是空壳，结果必须承认自己不完整
    assert result.get("incomplete") is True, "占位骨架没被标记为不完整"


async def test_precise_mode_marks_result_incomplete_when_placeholders_remain():
    """补全跑完仍有占位节点时，返回值要带上不完整标记。

    否则接口交出 `success=True` + 一串 `第N部`，看起来与一次正常规划无差别。
    """
    llm = AsyncMock()
    llm.stream_generate = _no_stream
    llm.generate = AsyncMock(side_effect=[
        """{"node_updates": [
            {
              "node_id": "A1_1_1",
              "title": "雪夜叩门",
              "description": "主角深夜入京",
              "narrative_goal": "把主角拖入主线阴谋",
              "plot_points": ["进京", "撞见命案"],
              "key_characters": ["林渊"],
              "key_locations": ["都察院"],
              "emotional_arc": "戒备→惊惧"
            }
        ]}""",
        # 补全这一轮什么都没写回来
        """{"node_updates": []}""",
    ])
    svc = _svc(llm)

    result = await svc.generate_macro_plan(
        novel_id="novel-b10-result-flag",
        target_chapters=20,
        structure_preference={"parts": 1, "volumes_per_part": 1, "acts_per_volume": 1},
    )

    assert result.get("incomplete") is True, (
        "补全后仍有 `第1部`/`第1卷` 占位节点，却没把不完整这件事带出来"
    )
    assert result["success"] is True


# ======================================================================
#  B-12：流中断的半截规划必须带着「不完整」这个事实往下走
# ======================================================================


async def test_interrupted_stream_result_is_marked_incomplete_but_keeps_partial_parts():
    """半截不丢，但必须自报不完整；预览态也要带上同一个事实。"""
    svc = _svc()
    svc.llm_service.stream_generate = _broken_stream
    novel_id = "novel-b12-stream-mark"

    raw = await svc._stream_macro_llm_text(novel_id, Mock(), Mock())
    parsed = svc._parse_llm_response(raw)

    assert parsed.get("incomplete") is True, (
        "流中断后返回的半截规划没有任何不完整标记 —— 下游无从分辨"
    )
    # 半截本身仍然可用，不能为了标明不完整就把它丢掉
    acts = parsed["parts"][0]["volumes"][0]["acts"]
    assert acts[0]["title"] == "第一幕：灰鸦街醒来"
    assert acts[1]["title"] == "第二幕：廉价训练舱"
    # 预览态是 SSE / 轮询读的那一份快照，必须同步带上这个事实
    progress = get_macro_plan_progress(novel_id)
    assert progress.get("preview_parts_incomplete") is True, (
        "预览快照没有标出这是半截，UI 会把它当成完整结构"
    )


async def test_macro_plan_result_exposes_incomplete_after_stream_interrupt():
    """接口层拿到的结果要能分辨「这次规划没跑完」。"""
    svc = _svc()
    svc.llm_service.stream_generate = _broken_stream
    novel_id = "novel-b12-result-flag"

    result = await svc.generate_macro_plan(novel_id=novel_id, target_chapters=60)

    assert result.get("incomplete") is True, (
        "generate_macro_plan 没把「流中断」这件事带给消费方"
    )
    # 已生成的部分仍然交付，不因为标明不完整就回滚
    assert result["success"] is True
    assert result["structure"][0]["title"] == "第一部"
    assert get_macro_plan_progress(novel_id).get("preview_parts_incomplete") is True


# ======================================================================
#  反向守卫：真实内容不得被误判成占位 / 不完整
# ======================================================================


async def test_complete_plan_is_never_marked_incomplete_or_repaired():
    """全部是真内容时：不点名补全、不标不完整、标题原样保留。"""
    llm = AsyncMock()
    llm.stream_generate = _no_stream
    llm.generate = AsyncMock(return_value=_COMPLETE_PLAN)
    svc = _svc(llm)

    result = await svc.generate_macro_plan(
        novel_id="novel-guard-complete-plan",
        target_chapters=60,
    )

    assert "incomplete" in result, "结果里必须显式说明完不完整，不能靠缺省让人猜"
    assert result["incomplete"] is False, "一次完整规划被误标成不完整"
    acts = result["structure"][0]["volumes"][0]["acts"]
    assert [a["title"] for a in acts] == ["第三幕：雪夜叩门", "第四幕：朝堂余烬"]
    # 没有缺字段就不该触发补全这一次额外的 LLM 调用
    assert llm.generate.await_count == 1, "真实内容被当成占位，多跑了一次补全"


async def test_real_titles_in_precise_mode_do_not_trigger_repair():
    """`第一部：寒门` / `第一卷：入京` / `第三幕：雪夜叩门` 这类真标题不是占位。"""
    llm = AsyncMock()
    llm.stream_generate = _no_stream
    llm.generate = AsyncMock(return_value="""{"node_updates": [
        {"node_id": "P1", "title": "第一部：寒门", "description": "寒门少年被卷入京师权斗"},
        {"node_id": "V1_1", "title": "第一卷：入京", "description": "从山村到京城的立足过程"},
        {
          "node_id": "A1_1_1",
          "title": "第三幕：雪夜叩门",
          "description": "主角深夜入京",
          "narrative_goal": "把主角拖入主线阴谋",
          "plot_points": ["进京", "撞见命案"],
          "key_characters": ["林渊"],
          "key_locations": ["都察院"],
          "emotional_arc": "戒备→惊惧"
        }
    ]}""")
    svc = _svc(llm)

    result = await svc.generate_macro_plan(
        novel_id="novel-guard-precise-real-titles",
        target_chapters=20,
        structure_preference={"parts": 1, "volumes_per_part": 1, "acts_per_volume": 1},
    )

    assert llm.generate.await_count == 1, (
        f"带层级前缀的真标题被当成占位，多跑了一次补全：{result}"
    )
    assert result.get("incomplete") is False, "真标题 + 完整字段被误标成不完整"
    assert result["structure"][0]["title"] == "第一部：寒门"
    assert result["structure"][0]["volumes"][0]["acts"][0]["title"] == "第三幕：雪夜叩门"
