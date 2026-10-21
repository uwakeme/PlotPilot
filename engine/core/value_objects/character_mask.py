"""CharacterMask值对象 — 折叠地质叠层后的当前面具快照

核心用途：
- 在T0层注入，控制角色行为的四维一致性
- 行为一致性判断由 **AI** 承担（H-03）——
  见 application/character/services/character_belief_check.py；
  本值对象只负责折叠并提供事实（信念/禁忌/旧伤/语体）。
- 由Character.compute_mask()计算生成

注入格式示例：
[角色状态锁定 - 林羽（第50章当前阶段）]
当前核心信念：只有力量能保护自己，轻信必死。
当前语言指纹：短句为主，陈述语气，透着背叛后的阴沉。
身上带着的旧伤：左肩曾被恩师刺伤，极其排斥别人站在他左后方。
绝对禁忌：绝不杀手无寸铁之人。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass(frozen=True)
class CharacterMask:
    """角色当前面具 — 折叠地质叠层后的快照

    在T0层注入，控制角色行为的四维一致性：
    1. core_belief：决定价值选择
    2. moral_taboos：决定底线
    3. voice_profile：决定台词风格
    4. active_wounds：决定条件反射
    """
    character_id: str
    name: str
    core_belief: str
    moral_taboos: List[str] = field(default_factory=list)
    voice_style: str = "default"
    sentence_pattern: str = "mixed"
    metaphors: List[str] = field(default_factory=list)
    active_wounds: List[Dict[str, str]] = field(default_factory=list)
    chapter_number: int = 0

    def to_t0_fact_lock(self) -> str:
        """生成T0层Fact Lock注入格式"""
        lines = [f"[角色状态锁定 - {self.name}（第{self.chapter_number}章当前阶段）]"]
        lines.append(f"当前核心信念：{self.core_belief}")

        voice_parts = []
        if self.voice_style != "default":
            voice_parts.append(f"风格：{self.voice_style}")
        if self.sentence_pattern != "mixed":
            voice_parts.append(f"句式：{self.sentence_pattern}")
        if self.metaphors:
            voice_parts.append(f"隐喻：{'、'.join(self.metaphors[:3])}")
        if voice_parts:
            lines.append(f"当前语言指纹：{'；'.join(voice_parts)}")

        for wound in self.active_wounds:
            desc = wound.get("description", "")
            trigger = wound.get("trigger", "")
            effect = wound.get("effect", "")
            if desc:
                lines.append(f"身上带着的旧伤：{desc}（触发：{trigger} → {effect}）")

        if self.moral_taboos:
            lines.append(f"绝对禁忌：{'、'.join(self.moral_taboos)}")

        return "\n".join(lines)

    @classmethod
    def from_character_dict(cls, data: Dict[str, Any], chapter_number: int = 0) -> CharacterMask:
        """从Character.compute_mask()的输出创建CharacterMask"""
        voice_data = data.get("voice_profile", {})
        wounds_data = data.get("active_wounds", [])

        # 处理wounds格式
        normalized_wounds = []
        for w in wounds_data:
            if isinstance(w, dict):
                normalized_wounds.append(w)
            elif isinstance(w, str):
                normalized_wounds.append({"description": w, "trigger": "", "effect": ""})

        return cls(
            character_id=data.get("character_id", ""),
            name=data.get("name", ""),
            core_belief=data.get("core_belief", ""),
            moral_taboos=data.get("moral_taboos", []),
            voice_style=voice_data.get("style", "default"),
            sentence_pattern=voice_data.get("sentence_pattern", "mixed"),
            metaphors=voice_data.get("metaphors", []),
            active_wounds=normalized_wounds,
            chapter_number=chapter_number,
        )
