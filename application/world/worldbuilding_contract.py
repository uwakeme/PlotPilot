"""世界观字段契约配置读取。"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml


_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "shared"
    / "taxonomy"
    / "worldbuilding_contract_cn_v1.yaml"
)


@dataclass(frozen=True)
class WorldbuildingContract:
    """世界观字段契约。"""

    dimensions: Dict[str, Dict[str, Any]]
    field_scope_hints: Dict[str, Dict[str, str]]
    json_key_labels: Dict[str, str]
    #: 字段的**短标签**（UI 展示用，如「力量体系」）。
    #:
    #: A-06：此前这 16 个短标签只**手写在前端**（``contract.ts`` 的
    #: ``FIELD_SHORT_LABELS``）⇒ 同一批字段名/标签有两份真相
    #: （YAML 里的 ``dimensions[*].fields`` 是**描述**，前端那份是**短标签**）。
    #: ⇒ 现在下沉到契约（SSOT），前端从 bundle 读。
    field_labels: Dict[str, str] = dataclass_field(default_factory=dict)


def worldbuilding_dimension_keys() -> tuple[str, ...]:
    """世界观维度键（顺序 = 契约里的顺序）。

    A-06：此前全仓有 **4 处**手写副本
    （``variable_hub.py`` / ``variable_backfill.py`` / ``world_nodes.py`` ×2 /
    ``bible_setup_continuation.py``）⇒ 契约一改，副本不会跟着改
    （本仓最常见的「同一逻辑写两份」）。
    ⇒ 统一走这里。
    """
    return tuple(get_worldbuilding_contract().dimensions.keys())


def worldbuilding_dimension_label(dim_key: str) -> str:
    """维度的中文 label（取不到就回退到键名）。"""
    contract = get_worldbuilding_contract()
    dim = contract.dimensions.get(dim_key) or {}
    return str(dim.get("label") or dim_key).strip() or dim_key


def worldbuilding_field_label(field_key: str) -> str:
    """字段的短标签（取不到就回退到键名）。"""
    contract = get_worldbuilding_contract()
    label = contract.field_labels.get(field_key, "").strip()
    if label:
        return label
    for dim in contract.dimensions.values():
        if field_key in (dim.get("fields") or {}):
            return field_key
    return field_key


def _coerce_text_mapping(value: object) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


@lru_cache(maxsize=1)
def get_worldbuilding_contract() -> WorldbuildingContract:
    """读取世界观字段契约配置。"""
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    raw_dimensions = data.get("dimensions") if isinstance(data, dict) else {}
    dimensions: Dict[str, Dict[str, Any]] = {}
    scope_hints: Dict[str, Dict[str, str]] = {}

    if isinstance(raw_dimensions, dict):
        for dim_key, raw_dim in raw_dimensions.items():
            if not isinstance(raw_dim, dict):
                continue
            key = str(dim_key).strip()
            fields = _coerce_text_mapping(raw_dim.get("fields"))
            if not key or not fields:
                continue
            dimensions[key] = {
                "label": str(raw_dim.get("label") or key).strip(),
                "fields": fields,
            }
            scope_hints[key] = _coerce_text_mapping(raw_dim.get("scope_hints"))

    labels = _coerce_text_mapping(data.get("json_key_labels") if isinstance(data, dict) else {})
    # A-06：字段短标签下沉到契约（此前只在前端手写 ⇒ 两份真相）。
    field_labels = _coerce_text_mapping(
        data.get("field_labels") if isinstance(data, dict) else {}
    )
    return WorldbuildingContract(
        dimensions=dimensions,
        field_scope_hints=scope_hints,
        json_key_labels=labels,
        field_labels=field_labels,
    )
