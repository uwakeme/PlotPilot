"""冲突检测服务 - 检测大纲与设定库的冲突

H-02b：**「实体-动作对」的抽取交给 AI**，冲突判定仍留在代码里。

改造前「谁做了什么」是**代码用一张写死的关键词表**猜出来的
（``magic_keywords`` 8 条 + ``weapon_keywords`` 4 条）：

- 表里没有的写法一律**漏报**（「烈焰」「抽刀」「掷矛」「掌心凝火」…）；
- 「什么算用了火系魔法」是**代码代做的语义判断**；
- 表里的值是 ``fire_magic`` / ``use_sword`` 这种**封闭英文 id**，
  而设定库里的属性值是**中文**（「水系」「长剑」）⇒ 中间靠一张映射表对齐，
  而那张表只认 5 系 + 3 武器 ⇒ 本书用别的系永远查不出来。

现在：

- **AI 抽**「谁做了什么」（新 CPMS 节点 ``conflict-action-extraction``）——
  动作类别是封闭枚举（``magic`` / ``weapon``），魔法值统一「X系」形式
  （与设定库 ``magic_type`` 同口径），武器值写武器名；
- **代码只做不越界的校验**（在 ``conflict_action_extraction_schema`` 里）：
  实体白名单、类别封闭枚举、值格式、证据必须字面出现在大纲里；
- **冲突判定仍在代码里** —— 那是「设定值 vs 动作值」两个字符串的机械比较，
  不该也不必交给模型；
- **兜底**：AI 不可用 / 输出不合契约 / 给的全是废数据 ⇒ **未评估** ⇒
  退回这张关键词表（**与改造前逐位一致**），来源标记可分辨。
  但 **AI 合法地说「这段没有动作」不算未评估** ⇒ 不回退。

核心理念：大纲绝对意志 + 异步后置审查
- 不阻断生成流程
- 只在生成后提供非侵入式批注
- 把最终决策权留给作者
"""
import logging
from typing import Dict, List, Optional, Tuple

from application.audit.dtos.ghost_annotation import GhostAnnotation
from application.audit.services.conflict_action_extraction_schema import (
    ACTION_KIND_MAGIC,
    ACTION_KIND_WEAPON,
    EntityAction,
)
from application.audit.services.conflict_clause_attribution import (
    assign_clause_spans,
    clause_spans,
    split_into_clauses as _split_into_sentences,
)
from application.engine.dtos.scene_director_dto import SceneDirectorAnalysis

logger = logging.getLogger(__name__)


#: 关键词表（**兜底路径专用**）：格式 (关键词, 动作类别, 归一化属性值)。
#:
#: 这张表不再是「抽取器」——正常路径由 AI 抽（``ConflictActionExtractor``）。
#: 它只在 AI 未评估时兜底，行为与改造前**逐位一致**。
#: 归一化值与设定库同口径：魔法「X系」、武器名 ⇒ 比较时不需要第二张映射表。
_KEYWORD_TABLE: Tuple[Tuple[str, str, str], ...] = (
    ("火球", ACTION_KIND_MAGIC, "火系"),
    ("火系", ACTION_KIND_MAGIC, "火系"),
    ("火焰", ACTION_KIND_MAGIC, "火系"),
    ("水球", ACTION_KIND_MAGIC, "水系"),
    ("水系", ACTION_KIND_MAGIC, "水系"),
    ("冰系", ACTION_KIND_MAGIC, "冰系"),
    ("雷电", ACTION_KIND_MAGIC, "雷系"),
    ("风系", ACTION_KIND_MAGIC, "风系"),
    ("拔剑", ACTION_KIND_WEAPON, "剑"),
    ("挥剑", ACTION_KIND_WEAPON, "剑"),
    ("射箭", ACTION_KIND_WEAPON, "弓"),
    ("开枪", ACTION_KIND_WEAPON, "枪"),
)


def _trigger_corroborates(keyword: str, scene_triggers: List[str]) -> bool:
    """表的关键词是否被场记抽出的敏感词汇佐证。

    场记抽的是自由中文（「火球术」「拔剑」之类），表里是具体字面
    （「火球」「拔剑」）。判据是**互相包含** —— AI 指的是同一个词族就算
    佐证。AI 没提到的词族一律不查（那是 AI 的判断，不该被绕过）。
    """
    for trigger in scene_triggers:
        if keyword in trigger or trigger in keyword:
            return True
    return False


class ConflictDetectionService:
    """冲突检测服务

    检测章节大纲与设定库（Bible）之间的冲突：
    - 设定冲突：大纲中的行为与实体当前状态矛盾
    - 角色不一致：角色行为与性格设定不符
    - 时间线错误：事件顺序或时间不合理

    核心理念：大纲绝对意志 + 异步后置审查
    - 不阻断生成流程
    - 只在生成后提供非侵入式批注
    - 把最终决策权留给作者
    """

    def detect(
        self,
        outline: str,
        entity_states: Dict[str, Dict],
        name_to_entity_id: Dict[str, str],
        scene_director: Optional[SceneDirectorAnalysis] = None,
        entity_actions: Optional[Dict[str, List[EntityAction]]] = None,
    ) -> List[GhostAnnotation]:
        """检测冲突并返回批注列表

        Args:
            outline: 章节大纲
            entity_states: 实体当前状态字典 {entity_id: {attribute: value}}
            name_to_entity_id: 实体名称到 ID 的映射 {name: entity_id}
            scene_director: 场记分析结果（可选，用于提取实体和行为）
            entity_actions: **已抽好的**「实体-动作对」
                （H-02b：AI 抽的结果）。为 ``None`` ⇒ 走关键词表兜底。

        Returns:
            GhostAnnotation 列表
        """
        annotations = []

        try:
            # 如果没有实体状态，无法检测冲突
            if not entity_states:
                logger.debug("No entity states provided, skipping conflict detection")
                return annotations

            # 从大纲中提取实体和行为
            if entity_actions is None:
                # 兜底：关键词表（AI 未评估时）
                entity_actions = self._extract_entity_actions(
                    outline, name_to_entity_id, scene_director
                )

            # 检测每个实体的行为是否与状态冲突
            for entity_name, actions in entity_actions.items():
                entity_id = name_to_entity_id.get(entity_name)
                if not entity_id:
                    continue

                current_state = entity_states.get(entity_id, {})
                if not current_state:
                    continue

                # 检测设定冲突
                conflicts = self._check_setting_conflicts(
                    entity_name, entity_id, actions, current_state
                )
                annotations.extend(conflicts)

            logger.info(f"Conflict detection completed: {len(annotations)} annotations")
            return annotations

        except Exception as e:
            logger.error(f"Conflict detection failed: {e}", exc_info=True)
            return annotations

    def _extract_entity_actions(
        self,
        outline: str,
        name_to_entity_id: Dict[str, str],
        scene_director: Optional[SceneDirectorAnalysis] = None
    ) -> Dict[str, List[EntityAction]]:
        """**兜底**路径：用关键词表从大纲中提取每个实体做了什么。

        正常路径不走这里 —— 走 AI（``ConflictActionExtractor``）抽好的动作。
        本函数保留，是因为 AI 未评估时必须有**与改造前逐位一致**的保守行为。

        Args:
            outline: 章节大纲
            name_to_entity_id: 实体名称到 ID 的映射
            scene_director: 场记分析结果（可选）

        Returns:
            {entity_name: [EntityAction, ...]}

        归属规则（旧实现的两个结构性误报源）：

        1. **不再跟**整篇大纲**比。旧代码里关键词是与整篇文本比的，
           所以大纲里出现一次「拔剑」，**每个**在场实体都记得
           `use_sword`，再拿每个人自己的 `weapon`/`magic_type` 去比 ⇒ 不持剑的角色
           也拿到一条「设定冲突」。
        2. 动作归给**小句**（逗号级）里的主体。中文大纲常常一逗到底，
           按整句切会让两个人共享一个动作。
           小句里没有实体名但以连接词开头（「然后」「接着」…）时，
           主体沿用上一小句。

        关于 `scene_director`：**它不参与判定**。场记抽的
        `trigger_keywords` 是**语义类别**（「魔法」），而表里是**字面**
        （「火球」），互包不上 ⇒ 拿它过滤等于**漏报**（真冲突被静默丢掉）。
        它的词只进日志，供人工复核时对照。
        """
        entity_actions: Dict[str, List[EntityAction]] = {}

        # 场记的判断记进日志（供人工复核时对照），但**不用它过滤**。
        if scene_director is not None:
            raw_triggers = getattr(scene_director, "trigger_keywords", None) or []
            if isinstance(raw_triggers, str):
                raw_triggers = [raw_triggers]
            scene_triggers = [str(t).strip() for t in raw_triggers if str(t).strip()]
            if scene_triggers:
                logger.debug(
                    "冲突检测：场记抽出的敏感词汇 %s（仅记录，不用作过滤："
                    "场记给的是语义类别，表里是字面，互包不上）",
                    scene_triggers,
                )

        entity_clauses = assign_clause_spans(clause_spans(outline), name_to_entity_id)

        for entity_name, ranges in entity_clauses.items():
            clauses = [outline[lo:hi] for lo, hi in ranges]
            actions: List[EntityAction] = []
            for keyword, kind, value in _KEYWORD_TABLE:
                if any(keyword in clause for clause in clauses):
                    actions.append(EntityAction(kind=kind, value=value))
            if actions:
                entity_actions[entity_name] = actions

        return entity_actions

    def _check_setting_conflicts(
        self,
        entity_name: str,
        entity_id: str,
        actions: List[EntityAction],
        current_state: Dict
    ) -> List[GhostAnnotation]:
        """检查设定冲突

        比较的是**两个字符串**：设定库里的属性值 vs 大纲里的动作值。
        两者同口径（魔法「X系」、武器名）⇒ 互相包含即同一属性。

        Args:
            entity_name: 实体名称
            entity_id: 实体 ID
            actions: 实体在大纲中的行为列表
            current_state: 实体当前状态

        Returns:
            GhostAnnotation 列表
        """
        annotations = []

        # 检测魔法系统冲突
        magic_type = current_state.get("magic_type") or current_state.get("魔法")
        if magic_type:
            for action in actions:
                if action.kind != ACTION_KIND_MAGIC:
                    continue
                action_magic_cn = action.value
                if isinstance(magic_type, str):
                    if action_magic_cn not in magic_type and magic_type not in action_magic_cn:
                        annotations.append(GhostAnnotation(
                            type="setting_conflict",
                            severity="warning",
                            message=f"设定库中 {entity_name} 为 [{magic_type}]，此处使用了 [{action_magic_cn}]",
                            entity_id=entity_id,
                            entity_name=entity_name,
                            expected=magic_type,
                            actual=action_magic_cn,
                        ))

        # 检测武器冲突
        weapon = current_state.get("weapon") or current_state.get("武器")
        if weapon:
            for action in actions:
                if action.kind != ACTION_KIND_WEAPON:
                    continue
                action_weapon_cn = action.value
                if isinstance(weapon, str):
                    if action_weapon_cn not in weapon and weapon not in action_weapon_cn:
                        annotations.append(GhostAnnotation(
                            type="setting_conflict",
                            severity="warning",
                            message=f"设定库中 {entity_name} 的武器为 [{weapon}]，此处使用了 [{action_weapon_cn}]",
                            entity_id=entity_id,
                            entity_name=entity_name,
                            expected=weapon,
                            actual=action_weapon_cn,
                        ))

        return annotations
