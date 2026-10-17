from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
    _extract_outer_json_value,
    _incremental_macro_parts_trustworthy,
    _try_parse_parts_from_llm_buffer,
    get_macro_plan_progress,
)
from domain.ai.value_objects.prompt import Prompt
from domain.novel.value_objects.generation_preferences import GenerationPreferences


def _make_service() -> ContinuousPlanningService:
    return ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )


def test_quick_macro_prompt_for_long_book_uses_leading_volume_detail():
    svc = _make_service()

    prompt = svc._build_quick_macro_prompt({"premise": "主角在高武世界成长。"}, 500)

    assert "只为开篇前导卷规划幕节点" in prompt.system
    assert "后续卷的幕节点留给写作过程中动态生成" in prompt.system
    assert "开篇前导卷标题" in prompt.user
    assert "前 1-2 部" not in prompt.system


def test_parse_llm_response_repairs_truncated_macro_plan_json():
    svc = _make_service()

    response = """```json
{
  "parts": [
    {
      "title": "第一部",
      "volumes": [
        {
          "title": "卷一",
          "acts": [
            {
              "title": "初入京城",
              "description": "主角进入京城，卷入风暴",
              "core_conflict": "必须在权斗中站稳脚跟"
            }
          ]
        }
      ]
    }
  ],
  "theme": "权谋成长"
"""

    result = svc._parse_llm_response(response)

    assert result["theme"] == "权谋成长"
    assert result["parts"][0]["volumes"][0]["acts"][0]["title"] == "初入京城"


def test_parse_llm_response_repairs_unterminated_string():
    svc = _make_service()

    response = """{
  "parts": [
    {
      "title": "第一部",
      "volumes": [
        {
          "title": "卷一",
          "acts": [
            {
              "title": "初入京城",
              "description": "主角进入京城",
              "core_conflict": "站稳脚跟"
            },
            {
              "title": "风暴将至",
              "description": "主角发现
"""

    result = svc._parse_llm_response(response)

    acts = result["parts"][0]["volumes"][0]["acts"]
    assert len(acts) == 2
    assert acts[0]["title"] == "初入京城"
    assert acts[1]["title"] == "风暴将至"


def test_parse_llm_response_repairs_missing_comma_between_fields():
    svc = _make_service()

    response = """{
  "parts": [
    {
      "title": "第一部"
      "volumes": [
        {
          "title": "卷一",
          "acts": []
        }
      ]
    }
  ],
  "theme": "权谋成长"
}"""

    result = svc._parse_llm_response(response)

    assert result["theme"] == "权谋成长"
    assert result["parts"][0]["title"] == "第一部"
    assert result["parts"][0]["volumes"][0]["title"] == "卷一"


def test_extract_outer_json_value_prefers_object_root_over_leading_array():
    text = '["noise"] {"parts": [], "theme": "x"}'

    result = _extract_outer_json_value(text)

    assert result == '{"parts": [], "theme": "x"}'


def test_incremental_macro_parts_trustworthy_rejects_single_char_titles():
    bad = [
        {
            "title": "第一部",
            "volumes": [
                {"title": "卷一", "acts": [{"title": "X", "description": ""}]},
            ],
        }
    ]
    assert _incremental_macro_parts_trustworthy(bad) is False

    ok = [
        {
            "title": "第一部",
            "volumes": [
                {"title": "卷一", "acts": [{"title": "初入山门", "description": ""}]},
            ],
        }
    ]
    assert _incremental_macro_parts_trustworthy(ok) is True


def test_try_parse_parts_from_llm_buffer_accepts_complete_valid_minimal_json():
    raw = '{"parts": [{"title": "第一部", "volumes": [{"title": "卷一", "acts": [{"title": "序幕", "description": ""}]}]}], "theme": "x"}'
    parts = _try_parse_parts_from_llm_buffer(raw)
    assert parts is not None
    assert parts[0]["title"] == "第一部"
    assert parts[0]["volumes"][0]["acts"][0]["title"] == "序幕"


def test_act_planning_prompt_passes_locked_contract_variables_to_cpms(monkeypatch):
    prefs = GenerationPreferences(
        locked_genre="综合言情 / 都市言情·甜宠",
        locked_world_preset="现代都市关系场域",
        locked_special_requirements="核心回报是亲密关系推进。",
    )
    novel_repo = Mock()
    novel_repo.get_by_id.return_value = Mock(generation_prefs=prefs)
    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
        novel_repository=novel_repo,
    )
    act_node = Mock(
        id="act-1",
        novel_id="novel-1",
        title="第一幕：纸钱与樱树",
        description="苏念把樱花看成纸钱，摩挲母亲留下的旧红绳。",
    )
    captured = {}

    def fake_render(_contract, variables):
        captured.update(variables)
        return Prompt(system="system", user=str(variables))

    monkeypatch.setattr(
        ContinuousPlanningService,
        "_render_contract_prompt",
        staticmethod(fake_render),
    )

    prompt = svc._build_act_planning_prompt(
        act_node=act_node,
        bible_context={},
        previous_summary=None,
        chapter_count=6,
    )

    assert captured["genre_label"] == "综合言情 / 都市言情·甜宠"
    assert captured["world_preset"] == "现代都市关系场域"
    assert captured["special_requirements"] == "核心回报是亲密关系推进。"
    assert "漂移禁令" not in captured["context"]
    assert "禁止把" not in captured["context"]
    assert prompt.user == str(captured)


@pytest.mark.asyncio
async def test_stream_macro_llm_text_repairs_partial_text_when_stream_breaks():
    async def broken_stream(_prompt, _config):
        yield """{
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
        raise RuntimeError("peer closed connection")

    svc = _make_service()
    svc.llm_service.stream_generate = broken_stream

    raw = await svc._stream_macro_llm_text("novel-1", Mock(), Mock())
    parsed = svc._parse_llm_response(raw)

    acts = parsed["parts"][0]["volumes"][0]["acts"]
    assert acts[0]["title"] == "第一幕：灰鸦街醒来"
    assert acts[1]["title"] == "第二幕：廉价训练舱"


@pytest.mark.asyncio
async def test_generate_macro_plan_precise_mode_repairs_missing_act_fields_and_keeps_llm_chapters():
    llm_service = AsyncMock()
    llm_service.generate = AsyncMock(side_effect=[
        """{
          "node_updates": [
            {"node_id": "P1", "title": "寒门燃灯", "description": "寒门少年被卷入京师风暴"},
            {"node_id": "V1_1", "title": "初入京城", "description": "立足与试探"},
            {
              "node_id": "A1_1_1",
              "title": "雪夜叩门",
              "description": "主角深夜入京，撞见命案",
              "estimated_chapters": 7,
              "plot_points": ["进京", "撞见命案"],
              "setup_for": ["A1_1_2"],
              "payoff_from": []
            },
            {
              "node_id": "A1_1_2",
              "title": "朝堂余烬",
              "description": "主角被迫接触权贵",
              "estimated_chapters": 9,
              "narrative_goal": "建立主角与朝堂的冲突面",
              "plot_points": ["见权贵", "受逼迫"],
              "key_characters": ["主角-棋子"],
              "key_locations": ["都察院-压力源"],
              "emotional_arc": "紧张→压抑",
              "setup_for": ["A1_1_3"],
              "payoff_from": ["A1_1_1"]
            }
          ]
        }""",
        """{
          "node_updates": [
            {
              "node_id": "A1_1_1",
              "narrative_goal": "把主角拖入主线阴谋",
              "key_characters": ["主角-闯入者"],
              "key_locations": ["京城-漩涡入口"],
              "emotional_arc": "戒备→惊惧"
            }
          ]
        }""",
    ])
    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=llm_service,
    )
    svc._get_bible_context = Mock(return_value={})

    result = await svc.generate_macro_plan(
        novel_id="novel-1",
        target_chapters=100,
        structure_preference={"parts": 1, "volumes_per_part": 5, "acts_per_volume": 5},
    )

    parts = result["structure"]
    assert len(parts) == 1
    assert len(parts[0]["volumes"]) == 5
    assert all(len(volume["acts"]) == 5 for volume in parts[0]["volumes"])

    first_volume = parts[0]["volumes"][0]
    assert parts[0]["title"] == "寒门燃灯"
    assert first_volume["title"] == "初入京城"
    assert first_volume["acts"][0]["title"] == "雪夜叩门"
    assert first_volume["acts"][1]["title"] == "朝堂余烬"
    assert first_volume["acts"][2]["title"] == "第3幕"
    assert first_volume["acts"][0]["narrative_goal"] == "把主角拖入主线阴谋"
    assert first_volume["acts"][0]["key_characters"] == ["主角-闯入者"]
    assert first_volume["acts"][0]["key_locations"] == ["京城-漩涡入口"]
    assert first_volume["acts"][0]["emotional_arc"] == "戒备→惊惧"

    # B-03：幕级章数**保留 LLM 原值，不再等比归一到 target**。
    # 这里断言的正是「不归一」—— 旧实现会把 7/9 按权重缩放到总和 100，
    # 那等于在落库前静默改写 AI 对每幕章数的判断。
    all_acts = [act for volume in parts[0]["volumes"] for act in volume["acts"]]
    assert first_volume["acts"][0]["estimated_chapters"] == 7
    assert first_volume["acts"][1]["estimated_chapters"] == 9
    budget = check_act_chapter_budget(all_acts, 100)
    assert budget["ok"] is False, "占位幕未被补足时本就不该守恒"
    assert budget["warning"]
    assert llm_service.generate.await_count == 2

    progress = get_macro_plan_progress("novel-1")
    assert progress["status"] == "completed"
    assert progress["current"] == 5
    assert progress["total"] == 5


# ==================== 动态建幕换卷（parent_volume_id）====================

import application.blueprint.services.continuous_planning_service as _cp_module
from application.blueprint.services.act_chapter_budget import check_act_chapter_budget
from domain.structure.story_node import NodeType


def _current_act_node():
    return SimpleNamespace(
        id="act-7",
        number=7,
        parent_id="vol-1",
        order_index=41,
        node_type=SimpleNamespace(value="act"),
    )


def test_next_act_prompt_injects_volume_switch_note_for_new_volume(monkeypatch):
    """换卷建幕：prompt 必须携带新卷的阶段定位，让幕内容衔接新卷承诺。"""
    captured = {}

    def fake_render(_contract, variables):
        captured.update(variables)
        return Prompt(system="system", user=str(variables))

    monkeypatch.setattr(
        ContinuousPlanningService, "_render_contract_prompt", staticmethod(fake_render)
    )
    svc = _make_service()
    current_act = SimpleNamespace(
        id="act-7", number=7, parent_id="vol-1", title="第七幕", description="收束反噬线"
    )
    target_volume = SimpleNamespace(
        id="vol-2", number=2, title="因果反噬的焦痕", description="反噬开始具象化"
    )

    svc._build_next_act_prompt_with_dual_track(current_act, {}, target_volume=target_volume)

    assert "【卷切换】" in captured["context_block"]
    assert "因果反噬的焦痕" in captured["context_block"]
    assert "反噬开始具象化" in captured["context_block"]


def test_next_act_prompt_no_switch_note_for_same_volume(monkeypatch):
    captured = {}

    def fake_render(_contract, variables):
        captured.update(variables)
        return Prompt(system="system", user=str(variables))

    monkeypatch.setattr(
        ContinuousPlanningService, "_render_contract_prompt", staticmethod(fake_render)
    )
    svc = _make_service()
    current_act = SimpleNamespace(
        id="act-7", number=7, parent_id="vol-1", title="第七幕", description="收束反噬线"
    )
    same_volume = SimpleNamespace(id="vol-1", number=1, title="卷一", description="")

    svc._build_next_act_prompt_with_dual_track(current_act, {}, target_volume=same_volume)

    assert "【卷切换】" not in captured["context_block"]


@pytest.mark.asyncio
async def test_create_next_act_auto_default_keeps_current_parent():
    """不传 parent_volume_id（手动「创建下一幕」入口）：保持挂当前父卷的历史语义。"""
    svc = _make_service()
    svc.story_node_repo.get_by_id = AsyncMock(return_value=_current_act_node())
    svc._get_bible_context = Mock(return_value={})
    gen_info = AsyncMock(return_value={"title": "第八幕", "description": "推进"})
    svc._generate_next_act_info = gen_info
    svc.story_node_repo.save = AsyncMock()

    result = await svc.create_next_act_auto("n-1", "act-7")

    saved = svc.story_node_repo.save.await_args.args[0]
    assert saved.parent_id == "vol-1"
    assert saved.number == 8
    assert saved.order_index == 42
    assert gen_info.await_args.kwargs.get("target_volume") is None
    assert result["next_act"]["parent_id"] == "vol-1"


@pytest.mark.asyncio
async def test_create_next_act_auto_with_parent_volume_id_switches_volume():
    """守护进程传入选卷结果：新幕挂到指定卷，并把目标卷交给 prompt 构建。"""
    svc = _make_service()
    target_volume = SimpleNamespace(
        id="vol-2",
        number=2,
        title="因果反噬的焦痕",
        description="反噬开始具象化",
        node_type=SimpleNamespace(value="volume"),
    )
    svc.story_node_repo.get_by_id = AsyncMock(
        side_effect=[_current_act_node(), target_volume]
    )
    svc._get_bible_context = Mock(return_value={})
    gen_info = AsyncMock(return_value={"title": "第八幕", "description": "推进"})
    svc._generate_next_act_info = gen_info
    svc.story_node_repo.save = AsyncMock()

    result = await svc.create_next_act_auto("n-1", "act-7", parent_volume_id="vol-2")

    saved = svc.story_node_repo.save.await_args.args[0]
    assert saved.parent_id == "vol-2"
    assert saved.number == 8
    assert saved.order_index == 42
    assert gen_info.await_args.kwargs["target_volume"] is target_volume
    assert result["next_act"]["parent_id"] == "vol-2"


@pytest.mark.asyncio
async def test_create_next_act_auto_falls_back_when_volume_missing():
    """指定父卷不存在/非卷节点：回退挂当前父卷，不阻断建幕。"""
    svc = _make_service()
    svc.story_node_repo.get_by_id = AsyncMock(
        side_effect=[_current_act_node(), None]
    )
    svc._get_bible_context = Mock(return_value={})
    gen_info = AsyncMock(return_value={"title": "第八幕", "description": "推进"})
    svc._generate_next_act_info = gen_info
    svc.story_node_repo.save = AsyncMock()

    await svc.create_next_act_auto("n-1", "act-7", parent_volume_id="vol-x")

    saved = svc.story_node_repo.save.await_args.args[0]
    assert saved.parent_id == "vol-1"
    assert gen_info.await_args.kwargs["target_volume"] is None


@pytest.mark.asyncio
async def test_confirm_act_planning_refreshes_volume_chapter_ranges(monkeypatch):
    """动态补幕后必须回填卷/部聚合章节范围，否则卷摘要/卷完结诊断钩子永不触发。"""
    svc = _make_service()
    act_node = SimpleNamespace(
        id="act-7",
        novel_id="n-1",
        order_index=41,
        node_type=NodeType.ACT,
        chapter_start=None,
        chapter_end=None,
        chapter_count=None,
    )
    svc.story_node_repo.get_by_id = AsyncMock(return_value=act_node)
    svc.chapter_element_repo = Mock()
    svc.story_node_repo.get_children_sync = Mock(
        return_value=[
            SimpleNamespace(id="chapter-n-1-chapter-4", number=4, node_type=NodeType.CHAPTER),
            SimpleNamespace(id="chapter-n-1-chapter-5", number=5, node_type=NodeType.CHAPTER),
        ]
    )
    svc._normalize_act_chapter_row = Mock(
        side_effect=lambda raw, act_local_index: {"title": raw["title"]}
    )
    svc._merged_elements_dict = Mock(return_value={})
    svc._create_elements_from_data = Mock(return_value=[])
    svc._write_chapter_plan_variables = Mock()
    update_ranges = AsyncMock()
    svc.story_node_repo.update_chapter_ranges = update_ranges

    monkeypatch.setattr(_cp_module, "validate_lightweight_act_plan", Mock(return_value=[]))
    monkeypatch.setattr(
        _cp_module, "collect_structure_chapter_numbers", Mock(return_value={1, 2, 3, 4, 5})
    )

    recorded: dict = {}

    class _RecordingTxn:
        def __enter__(self):
            recorded["entered"] = True
            return self

        def __exit__(self, *exc):
            recorded["exc"] = exc[0]
            return False

    svc._write_txn = _RecordingTxn

    result = await svc.confirm_act_planning(
        act_id="act-7", chapters=[{"title": "章一"}, {"title": "章二"}]
    )

    # 同号替换：本幕旧章占 4-5，新规划 2 章沿用起始号 4-5
    assert result["success"] is True
    assert act_node.chapter_start == 4
    assert act_node.chapter_end == 5
    assert act_node.chapter_count == 2
    assert recorded["entered"] is True
    assert recorded["exc"] is None
    update_ranges.assert_awaited_once_with("n-1")


async def test_confirm_act_planning_rejects_number_clash_with_later_acts(monkeypatch):
    """新规划章数扩张撞上后续幕占用的章号时显式报错，而非静默产生同号双节点。"""
    svc = _make_service()
    act_node = SimpleNamespace(
        id="act-7",
        novel_id="n-1",
        order_index=41,
        node_type=NodeType.ACT,
        chapter_start=None,
        chapter_end=None,
        chapter_count=None,
    )
    svc.story_node_repo.get_by_id = AsyncMock(return_value=act_node)
    svc.story_node_repo.get_children_sync = Mock(
        return_value=[
            SimpleNamespace(id="chapter-n-1-chapter-4", number=4, node_type=NodeType.CHAPTER),
        ]
    )
    monkeypatch.setattr(_cp_module, "validate_lightweight_act_plan", Mock(return_value=[]))
    monkeypatch.setattr(
        _cp_module, "collect_structure_chapter_numbers", Mock(return_value={4, 7, 8})
    )

    svc._write_txn = Mock()
    with pytest.raises(ValueError, match="已被其他幕的章节占用"):
        await svc.confirm_act_planning(
            act_id="act-7",
            chapters=[{"title": f"章{i}"} for i in range(5)],  # 新 5 章：4-8 撞幕C 的 7-8
        )

    svc._write_txn.assert_not_called()
