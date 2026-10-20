"""冲突检测服务 - 检测大纲与设定库的冲突"""
import logging
from typing import List, Dict, Optional
from application.audit.dtos.ghost_annotation import GhostAnnotation
from application.engine.dtos.scene_director_dto import SceneDirectorAnalysis

logger = logging.getLogger(__name__)


def _split_into_sentences(text: str) -> List[str]:
    """把中文正文切成**小句**，供「动作与实体同小句」的就近归属用。

    粒度取到小句（逗号级）而不是整句，理由是中文大纲的典型结构就是
    「陈小拔出长剑，李四仓守后退」这样一逗到底 —— 按整句切会让两人都被
    记上同一个动作，而按小句切才能分清谁做的。

    切分点：句末标点 。！？；… 与换行，加上小句分隔符 ，、：。
    引号内的句号不特殊处理 —— 大纲文本短，误切只会让归属更保守
    （更少误报），方向是安全的。
    """
    if not text:
        return []
    out: List[str] = []
    buf: List[str] = []
    for ch in text:
        buf.append(ch)
        if ch in "。！？；…，、：\n":
            clause = "".join(buf).strip()
            if clause:
                out.append(clause)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _assign_clauses_to_entities(
    clauses: List[str], name_to_entity_id: Dict[str, str]
) -> Dict[str, List[str]]:
    """把小句分配给实体，并处理「连接词主体沿用」。

    中文大纲常写「李明释放火球术，然后拔剑攻击」——同一个人做两件事，
    但「然后拔剑攻击」那个小句里**没有主语**。这时主体沿用上一小句。
    """
    result: Dict[str, List[str]] = {}
    last_subject: Optional[str] = None
    for clause in clauses:
        mentioned = [n for n in name_to_entity_id if n and n in clause]
        if not mentioned and last_subject is not None and clause.startswith(
            _CLAUSE_CONNECTIVES
        ):
            mentioned = [last_subject]
        for name in mentioned:
            result.setdefault(name, []).append(clause)
        if mentioned:
            # 一句里有多个实体时取第一个作为主体（推理粗，但不会造成误报）
            last_subject = mentioned[0]
    return result


_CLAUSE_CONNECTIVES = (
    "然后", "接着", "继而", "随后", "紧接着",
    "又", "并", "同时", "而后", "随即", "接着就",
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
        scene_director: Optional[SceneDirectorAnalysis] = None
    ) -> List[GhostAnnotation]:
        """检测冲突并返回批注列表

        Args:
            outline: 章节大纲
            entity_states: 实体当前状态字典 {entity_id: {attribute: value}}
            name_to_entity_id: 实体名称到 ID 的映射 {name: entity_id}
            scene_director: 场记分析结果（可选，用于提取实体和行为）

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
            entity_actions = self._extract_entity_actions(outline, name_to_entity_id, scene_director)

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
    ) -> Dict[str, List[str]]:
        """从大纲中提取**每个实体**做了什么。

        Args:
            outline: 章节大纲
            name_to_entity_id: 实体名称到 ID 的映射
            scene_director: 场记分析结果（可选）

        Returns:
            {entity_name: [action1, action2, ...]}

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
        真正的「LLM 抽取实体-动作对」需要新的 CPMS 节点（第 2 批 P1）。
        """
        entity_actions = {}

        # 关键词 → 行动类型的**词义映射**（保留）：
        # 它解释的是「火系对一个水系角色意味着什么」，
        # 而不是「哪些词算行为」。
        magic_keywords = [
            ("火球", "fire_magic"),
            ("火系", "fire_magic"),
            ("火焰", "fire_magic"),
            ("水球", "water_magic"),
            ("水系", "water_magic"),
            ("冰系", "ice_magic"),
            ("雷电", "lightning_magic"),
            ("风系", "wind_magic"),
        ]
        weapon_keywords = [
            ("拔剑", "use_sword"),
            ("挥剑", "use_sword"),
            ("射箭", "use_bow"),
            ("开枪", "use_gun"),
        ]
        keyword_table = magic_keywords + weapon_keywords

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

        entity_clauses = _assign_clauses_to_entities(
            _split_into_sentences(outline), name_to_entity_id
        )

        for entity_name, clauses in entity_clauses.items():
            actions = []
            for keyword, action_type in keyword_table:
                if any(keyword in clause for clause in clauses):
                    actions.append(action_type)
            if actions:
                entity_actions[entity_name] = actions

        return entity_actions

    def _check_setting_conflicts(
        self,
        entity_name: str,
        entity_id: str,
        actions: List[str],
        current_state: Dict
    ) -> List[GhostAnnotation]:
        """检查设定冲突

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
                if action.endswith("_magic"):
                    action_magic = action.replace("_magic", "")
                    # 简单映射
                    magic_map = {
                        "fire": "火系",
                        "water": "水系",
                        "ice": "冰系",
                        "lightning": "雷系",
                        "wind": "风系",
                    }
                    action_magic_cn = magic_map.get(action_magic, action_magic)

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
                if action.startswith("use_"):
                    action_weapon = action.replace("use_", "")
                    weapon_map = {
                        "sword": "剑",
                        "bow": "弓",
                        "gun": "枪",
                    }
                    action_weapon_cn = weapon_map.get(action_weapon, action_weapon)

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
