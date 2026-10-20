"""H-02：冲突检测必须把动作归给**那个角色**，而不是在场所有人。

## 问题

`_extract_entity_actions` 的关键词表是跟**整篇大纲**比，不是跟该实体的行为比：

```python
for entity_name in name_to_entity_id.keys():
    if entity_name.lower() not in outline_lower: continue
    for keyword, action_type in magic_keywords:
        if keyword in outline:          # ← 整篇大纲！
            actions.append(action_type)
```

后果：大纲里出现一次「拔剑」，**每个**在场实体都记 `use_sword`；
`_check_setting_conflicts` 再拿每个角色自己的 `weapon`/`magic_type` 去比 ——
于是任何不佩剑的角色都拿到一条「设定冲突」批注。

而且场记分析（`scene_director`）已经把「会触发设定的敏感道具或法术词汇」
抽出来了（`trigger_keywords`），它**被传进来了却从不被读**。

## 改法

1. 动作按**句内共现**归属：实体名与关键词出现在同一句才算它做的。
2. 有场记时只用场记佐证过的词族（AI 决定本场什么重要），没有则退回内置表。
3. 内置表保留，但它的角色降级为**词义映射**（火系 对 水系 角色意味着什么），
   不再兼当检测启发式。

⚠ 词表辨析：内置表是**字面**匹配（「拔剑」不匹配「拔剑」）；
   `scene_director.action_types` 是自由中文（「近身搏斗」），**不是**冲突检测要的
   `fire_magic`/`use_sword` 枚举 ⇒ 用不了；可用的是 `trigger_keywords`。
`scene_director.action_types` 是自由中文（「近身搏斗」），
**不是**冲突检测要的 `fire_magic`/`use_sword` 枚举 ⇒ 用不了；
可用的是 `trigger_keywords`。
"""
import sys
from types import SimpleNamespace

import pytest

from application.audit.services.conflict_detection_service import (
    ConflictDetectionService,
    _split_into_sentences,
    _trigger_corroborates,
)


def _entity(name, entity_id, **state):
    return name, {"magic_type": state.get("magic_type", ""), "weapon": state.get("weapon", "")}


def _scene_director(triggers=None, characters=None):
    return SimpleNamespace(
        trigger_keywords=list(triggers or []),
        characters=list(characters or []),
        action_types=[],
    )


# ======================================================================
#  1. 归属：动作必须归给那个角色
# ======================================================================


def test_action_is_not_attributed_to_every_entity_in_the_outline():
    """大纲里一次「拔剑」不该让在场所有人都会使剑。

    这是本条的核心：旧实现把整篇大纲的关键词算到每个实体头上。
    """
    svc = ConflictDetectionService()
    outline = "陈小拔剑，李四仓皇后退。"
    name_to_entity_id = {"陈小": "c1", "李四": "c2"}

    actions = svc._extract_entity_actions(outline, name_to_entity_id)

    assert actions.get("陈小") == ["use_sword"], f"陈小应被记为用剑：{actions}"
    assert "李四" not in actions, (
        f"李四没拔剑却被记了动作 —— 这就是全篇扫描的误报：{actions}"
    )


def test_magic_is_attributed_only_to_the_character_who_cast_it():
    svc = ConflictDetectionService()
    outline = "李四掌心凝出一团火球。陈小被火光逼退。"
    name_to_entity_id = {"陈小": "c1", "李四": "c2"}

    actions = svc._extract_entity_actions(outline, name_to_entity_id)

    assert actions.get("李四") == ["fire_magic"], actions
    assert "陈小" not in actions, f"陈小是挨打的那个，不该被记为施法：{actions}"


def test_entity_absent_from_the_outline_gets_nothing():
    svc = ConflictDetectionService()
    outline = "陈小拔剑。"
    name_to_entity_id = {"陈小": "c1", "李四": "c2"}

    actions = svc._extract_entity_actions(outline, name_to_entity_id)

    assert set(actions) == {"陈小"}


# ======================================================================
#  2. 场记的 trigger_keywords 要真的被用上
# ======================================================================


def test_scene_director_does_not_gate_the_detection():
    """场记**不能**用来收窄关键词表 —— 那是漏报。

    场记抽的 `trigger_keywords` 是**语义类别**（「魔法」），
    而内置表是**字面量**（「火球」）。互包不上 ⇒ 拿场记去过滤，
    等于把真冲突静默丢掉。

    既有契约也是这么写的（`test_conflict_detection_service.py` 里
    「即使有场记分析，冲突检测仍然基于大纲内容」）。

    真正的「LLM 抽取实体-动作对」需要新的 CPMS 节点（第 2 批 P1），
    那时才能让 AI 参与**判定**而不是参与**过滤**。
    """
    svc = ConflictDetectionService()
    outline = "陈小拔剑，李四放出一团火球。"
    name_to_entity_id = {"陈小": "c1", "李四": "c2"}
    scene = _scene_director(triggers=["魔法"])

    actions = svc._extract_entity_actions(outline, name_to_entity_id, scene)

    # 场记说「魔法」时，火球这条**仍然要查**（否则真冲突被漏掉）
    assert actions.get("李四") == ["fire_magic"], actions
    assert actions.get("陈小") == ["use_sword"], actions


def test_scene_director_triggers_land_in_the_log(caplog):
    """场记的敏感词汇要进日志（供人工复核批注时对照）。

    ⚠ 断言落在**日志内容**上，不是「函数体里出现过 scene_director 这个名字」
    —— 那种 AST 判定太弱：把 `if scene_director is not None:` 改成 `if False:`
    之后名字仍算被 Load，断言照样绿。
    """
    import logging as _logging

    svc = ConflictDetectionService()
    outline = "陈小拔剑。"
    name_to_entity_id = {"陈小": "c1"}
    scene = _scene_director(triggers=["拔剑"])

    with caplog.at_level(_logging.DEBUG, logger="application.audit.services.conflict_detection_service"):
        actions = svc._extract_entity_actions(outline, name_to_entity_id, scene)

    assert actions.get("陈小") == ["use_sword"], actions
    assert any("拔剑" in r.getMessage() for r in caplog.records), (
        "场记抽出的敏感词汇没有进日志 —— AI 的判断留不下痕迹"
    )


def test_scene_director_trigger_keywords_may_be_more_specific():
    """场记抽的词可以比表里的更具体（互含即佐证）。"""
    svc = ConflictDetectionService()
    outline = "陈小掌心凝出火球术。"
    name_to_entity_id = {"陈小": "c1"}
    scene = _scene_director(triggers=["火球术"])

    actions = svc._extract_entity_actions(outline, name_to_entity_id, scene)

    assert actions.get("陈小") == ["fire_magic"], actions


def test_without_scene_director_the_builtin_table_still_works():
    """没有场记时退回内置表 —— 不能让这条变「检不出任何东西」。"""
    svc = ConflictDetectionService()
    outline = "陈小拔剑。"
    name_to_entity_id = {"陈小": "c1"}

    actions = svc._extract_entity_actions(outline, name_to_entity_id, None)

    assert actions.get("陈小") == ["use_sword"], actions


def test_empty_scene_director_triggers_fall_back_to_the_table():
    """场记在场但没抽出敏感词 ⇒ 退回表，而不是查都不查。

    「没抽到」与「本场确实没有敏感行为」在数据上同形，
    退回表是保守选择（宁可多查，不漏报）。
    """
    svc = ConflictDetectionService()
    outline = "陈小拔剑。"
    name_to_entity_id = {"陈小": "c1"}

    actions = svc._extract_entity_actions(outline, name_to_entity_id, _scene_director(triggers=[]))

    assert actions.get("陈小") == ["use_sword"], actions


def test_scene_director_is_actually_read():
    """反向守卫：scene_director 参数必须被函数体读到（之前是纯摆设）。"""
    import ast
    import inspect
    import pathlib

    import application.audit.services.conflict_detection_service as mod

    src = pathlib.Path(mod.__file__).read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_extract_entity_actions":
            loads = {
                x.id for x in ast.walk(node)
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)
            }
            assert "scene_director" in loads, (
                "scene_director 仍未被读取 —— 场记的判断被完全绕过"
            )
            return
    raise AssertionError("找不到 _extract_entity_actions")


# ======================================================================
#  3. 端到端：批注不再误报到别人头上
# ======================================================================


def test_no_bogus_setting_conflict_for_an_unarmed_character():
    """完整走 detect()：佩剑的人不该给不佩剑的人发「设定冲突」批注。

    旧实现下，outline 里有一次「拔剑」，佩剑角色与不佩剑角色**都**会被
    标成用剑，于是「不佩剑」那个角色会拿到一条假的设定冲突。
    """
    svc = ConflictDetectionService()
    outline = "陈小拔剑，李四仓皇后退。"
    entity_states = {
        "c1": {"magic_type": "", "weapon": "长剑"},
        "c2": {"magic_type": "", "weapon": ""},
    }
    name_to_entity_id = {"陈小": "c1", "李四": "c2"}

    annotations = svc.detect(
        outline=outline,
        entity_states=entity_states,
        name_to_entity_id=name_to_entity_id,
    )

    assert not annotations, f"不该有任何设定冲突批注，却拿到 {annotations}"


def test_real_conflict_is_still_reported():
    """反向：真正的冲突仍要报出来 —— 不能把检测改没了。"""
    svc = ConflictDetectionService()
    outline = "李四掌心凝出一团火球。"
    entity_states = {
        "c2": {"magic_type": "水系", "weapon": ""},
    }
    name_to_entity_id = {"李四": "c2"}

    annotations = svc.detect(
        outline=outline,
        entity_states=entity_states,
        name_to_entity_id=name_to_entity_id,
    )

    assert annotations, "水系角色放火球是真冲突，必须报出来"
    assert annotations[0].type == "setting_conflict"


# ======================================================================
#  4. 辅助函数
# ======================================================================


def test_sentence_splitting_keeps_entities_with_their_keywords():
    sentences = _split_into_sentences("陈小拔剑。李四放火球！陈小后退？")
    assert len(sentences) == 3, sentences
    assert "拔剑" in sentences[0]
    assert "火球" in sentences[1]


def test_clause_level_splitting_separates_two_characters():
    """中文大纲常常一逗到底 —— 归属粒度必须到小句，否则两人共享一个动作。"""
    clauses = _split_into_sentences("陈小拔剑，李四仓守后退。")
    assert len(clauses) == 2, clauses
    assert "陈小" in clauses[0] and "拔剑" in clauses[0]
    assert "李四" in clauses[1] and "拔剑" not in clauses[1]


def test_connective_clause_inherits_the_previous_subject():
    """「李明释放火球术，然后拔剑攻击」是**同一个人**做两件事。

    小句切分后第二段没有主体 ⇒ 主体沿用上一小句。
    少了这条继承，既有测试 `test_detect_multiple_conflicts`
    （李明释放火球术，然后拔剑攻击 ⇒ 2 条冲突）就会挂。
    """
    svc = ConflictDetectionService()
    outline = "李明释放火球术，然后拔剑攻击"
    name_to_entity_id = {"李明": "char-001"}

    actions = svc._extract_entity_actions(outline, name_to_entity_id)

    assert actions.get("李明") == ["fire_magic", "use_sword"], actions


def test_trigger_corroboration_is_bidirectional():
    assert _trigger_corroborates("火球", ["火球术"])
    assert _trigger_corroborates("火球术", ["火球"])
    assert not _trigger_corroborates("拔剑", ["火球"])
