"""MemoryOrchestrator抽象接口 — 记忆编排器

核心职责：
- update_emotion_ledger：更新情绪账本
- manage_foreshadow：管理伏笔生命周期
- restore_state：从 checkpoint 恢复引擎状态

G-04-b：原先这里还有 ``assemble_context``（组装写作上下文的三层记忆模型）
与配套的 ``TokenBudget`` / ``ContextAssembly``，但那条**读回路径零生产调用方**
（生产上下文的装配由 ``application/engine/services/context_budget_allocator.py``
承担）⇒ 已删除，避免两套并存。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any

from engine.core.entities.story import StoryId
from engine.core.value_objects.emotion_ledger import EmotionLedger
from engine.core.entities.foreshadow import ForeshadowStatus


class ForeshadowAction:
    """伏笔操作"""
    PLANT = "plant"           # 埋设
    REFERENCE = "reference"   # 引用
    AWAKEN = "awaken"         # 唤醒
    RESOLVE = "resolve"       # 回收
    ABANDON = "abandon"       # 放弃


class MemoryOrchestrator(ABC):
    """记忆编排器抽象接口

    核心职责：
    - EmotionLedger 的更新
    - 伏笔生命周期的管理
    - 从 checkpoint 恢复引擎状态
    """

    @abstractmethod
    async def update_emotion_ledger(
        self,
        story_id: StoryId,
        chapter_number: int,
        chapter_content: str,
    ) -> EmotionLedger:
        """更新情绪账本

        从章节内容中提取：
        - EmotionalWound：核心损失
        - EmotionalBoon：核心获得
        - PowerShift：势能转化
        - OpenLoop：未解悬念

        Args:
            story_id: 故事ID
            chapter_number: 章节号
            chapter_content: 章节内容

        Returns:
            更新后的情绪账本
        """
        ...

    @abstractmethod
    async def manage_foreshadow(
        self,
        story_id: StoryId,
        foreshadow_id: str,
        action: str,
        chapter_number: int = 0,
    ) -> None:
        """管理伏笔生命周期

        Args:
            story_id: 故事ID
            foreshadow_id: 伏笔ID
            action: 操作类型(plant/reference/awaken/resolve/abandon)
            chapter_number: 当前章节号
        """
        ...

    @abstractmethod
    async def restore_state(
        self,
        story_id: StoryId,
        character_masks: Dict[str, Any],
        emotion_ledger: Dict[str, Any],
        active_foreshadows: List[str],
        outline: str = "",
        recent_summary: str = "",
    ) -> None:
        """从 Checkpoint 恢复引擎状态（用于 checkout/rollback）

        Args:
            story_id: 故事ID
            character_masks: 角色面具字典
            emotion_ledger: 情绪账本字典
            active_foreshadows: 活跃伏笔 ID 列表
            outline: 大纲文本
            recent_summary: 近期摘要文本
        """
        ...
