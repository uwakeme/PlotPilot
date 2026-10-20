"""A-05b：Bible 语义归类 —— 读模型给的字段，**不猜**。

改造前
------
- ``_infer_character_importance``：靠关键词猜角色重要度
  （含「主角」→ primary，含「导师/宿敌/反派」→ secondary，其余 minor）
- ``_map_location_kind``：靠关键词把中文 type 映射到英文枚举
- 关系谓词：字符串旧格式靠关键词归并（师徒/朋友/敌对/家人/同事）

而模型**已经给了**这些信息：
- ``bible-characters`` 的 schema 里 ``role`` 就是「主角/对立角色/盟友/次要角色」
- ``bible-locations`` 的 schema 里 ``type`` 就是「城市/建筑/区域/特殊场所/秘境」

⇒ 代码在**重复猜模型已经说过的信息**，而且猜错。生产库实测：4 个「特殊场所」
（周队办公室、陆深工位与隔断走廊、宋扬画像室、梦境镜厅 —— 全是室内）被关键词
表判成 ``realm``（秘境/领域）。

还有一处**同一逻辑写两份**：``_map_location_kind`` 在
``auto_bible_generator`` 与 ``bible_location_triple_sync`` 各有一份，后者多认
英文 ``city`` ⇒ 同一地点在两条路径上分类可能不同。

本模块的口径
------------
模型给什么就是什么；**不在合法词表里 ⇒ 未评估（空串）**，不用关键词表补。
三态可分辨：「模型这么说」/「模型没给」/「模型给了非法值」。
"""
from __future__ import annotations

from typing import Any

#: 角色重要度（与前端 CHARACTER_IMPORTANCE_META 对齐）
CHARACTER_IMPORTANCE_VALUES = ("primary", "secondary", "minor")
#: 地点重要度（与前端 LOCATION_IMPORTANCE_META 对齐）
LOCATION_IMPORTANCE_VALUES = ("core", "important", "normal")
#: 地点类型（与前端 LOCATION_TYPE_META 对齐）
LOCATION_TYPE_VALUES = ("city", "region", "building", "faction", "realm")


def _pick(value: Any, allowed: tuple[str, ...]) -> str:
    """取值；不在合法词表里 ⇒ 空串（未评估）。**不猜、不回落**。"""
    text = str(value or "").strip().lower()
    return text if text in allowed else ""


def normalize_character_importance(value: Any) -> str:
    """角色重要度：primary / secondary / minor；模型没给 ⇒ 空串。"""
    return _pick(value, CHARACTER_IMPORTANCE_VALUES)


def normalize_location_importance(value: Any) -> str:
    """地点重要度：core / important / normal；模型没给 ⇒ 空串。"""
    return _pick(value, LOCATION_IMPORTANCE_VALUES)


def normalize_location_type(value: Any) -> str:
    """地点类型：city / region / building / faction / realm；模型没给 ⇒ 空串。

    ⚠ **不再做中文→英文的关键词映射**：那份表把「特殊场所」判成 ``realm``
    （秘境），而它其实是室内场景 —— 关键词猜的是**语义**，模型已经给过答案。
    """
    return _pick(value, LOCATION_TYPE_VALUES)
