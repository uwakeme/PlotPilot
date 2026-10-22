"""类型开篇画像加载与解析。

本模块只读取 `shared/taxonomy/opening_pattern_profiles_cn_v1.yaml` 中的配置资产，
不在代码里维护具体题材套路。调用方拿到的是可审计的 Variable Hub 变量块。

A-08
----
读者契约的**文本**原先写死在本模块（四条「第一屏必须…」），
与上面那句「不在代码里维护具体题材套路」自相矛盾
⇒ 文本已下沉到配置资产的 ``generic_contract`` 段，代码只负责读取与组装。

画像缺失时的行为也改了：**降级到通用契约**并如实标记 ``source_level="generic"``，
不再 ``raise`` 阻塞生成（新材料可以开书了）。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml


DEFAULT_OPENING_PROFILE_BUNDLE_ID = "opening_pattern_profiles_cn_v1"


class OpeningProfileError(ValueError):
    """开篇画像解析失败。"""


@dataclass(frozen=True)
class GenreOpeningProfile:
    """Variable Hub 可消费的类型开篇画像。"""

    genre_major: str
    genre_theme: str
    source_level: str
    opening_profile: Mapping[str, Any]
    reader_contract: Mapping[str, Any]
    rhythm_constraints: Mapping[str, Any]

    def as_variables(self) -> dict[str, Any]:
        """返回稳定变量别名。"""
        return {
            "genre_opening_profile": dict(self.opening_profile),
            "genre_reader_contract": dict(self.reader_contract),
            "genre_rhythm_constraints": dict(self.rhythm_constraints),
        }


def opening_profile_yaml_path(bundle_id: str = DEFAULT_OPENING_PROFILE_BUNDLE_ID) -> Path:
    """返回 shared/taxonomy 下的开篇画像配置路径。"""
    here = Path(__file__).resolve()
    root = here.parents[3]
    return root / "shared" / "taxonomy" / f"{bundle_id}.yaml"


def load_opening_profile_bundle_dict(bundle_id: str = DEFAULT_OPENING_PROFILE_BUNDLE_ID) -> dict[str, Any]:
    """加载开篇画像配置。"""
    path = opening_profile_yaml_path(bundle_id)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OpeningProfileError(f"开篇画像配置格式错误: {path}")
    return data


@lru_cache(maxsize=4)
def get_opening_profile_bundle_cached(bundle_id: str = DEFAULT_OPENING_PROFILE_BUNDLE_ID) -> dict[str, Any]:
    return load_opening_profile_bundle_dict(bundle_id)


def split_genre_label(genre_label: str) -> tuple[str, str]:
    """兼容前端/外部输入中的一级、二级分类分隔符。"""
    text = str(genre_label or "").strip()
    if not text:
        return "", ""
    for sep in ("/", "／", "-", "—", ">", "→"):
        if sep in text:
            parts = [part.strip() for part in text.split(sep) if part.strip()]
            if not parts:
                return "", ""
            if len(parts) == 1:
                return parts[0], ""
            return parts[0], " / ".join(parts[1:])
    return text, ""


def _generic_contract(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """读配置资产里的**通用**读者契约。

    A-08：读者契约的文本原先写死在本模块（四条「第一屏必须…」），
    与模块 docstring「不在代码里维护具体题材套路」的声明自相矛盾。
    ⇒ 文本下沉到 ``shared/taxonomy/opening_pattern_profiles_cn_v1.yaml``
    的 ``generic_contract`` 段；代码只负责**读取与组装**。

    配置里读不到 ⇒ 返回空契约（**不编造**）。
    """
    raw = bundle.get("generic_contract")
    if not isinstance(raw, Mapping):
        return {}
    contract: dict[str, Any] = {
        "first_screen_hook": str(raw.get("first_screen_hook") or "").strip(),
        "chapter_1_payoff": str(raw.get("chapter_1_payoff") or "").strip(),
        "chapter_3_lock": str(raw.get("chapter_3_lock") or "").strip(),
        "chapter_10_loop": str(raw.get("chapter_10_loop") or "").strip(),
        "reader_promise": [
            str(item).strip()
            for item in (raw.get("reader_promise") or [])
            if str(item).strip()
        ],
    }
    return {k: v for k, v in contract.items() if v not in ("", [])}


def _profile_parts(
    raw: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    reader_promise = list(raw.get("reader_promise") or [])
    opening_mechanism = dict(raw.get("opening_mechanism") or {})
    rhythm_constraints = dict(raw.get("rhythm_constraints") or {})
    metric_snapshot = dict(raw.get("metric_snapshot") or {})
    opening_profile = {
        "reader_promise": reader_promise,
        "opening_mechanism": opening_mechanism,
        "metric_snapshot": metric_snapshot,
    }
    # A-08：契约文本来自配置资产的通用契约段；题材自己的 reader_promise 覆盖它。
    reader_contract = _generic_contract(bundle)
    reader_contract["reader_promise"] = reader_promise or reader_contract.get(
        "reader_promise", []
    )
    return opening_profile, reader_contract, rhythm_constraints


def resolve_opening_profile(
    genre_label: str,
    *,
    bundle: Mapping[str, Any] | None = None,
    strict: bool = True,
) -> GenreOpeningProfile | None:
    """按类型标签解析开篇画像。

    解析顺序：
    1. `一级 / 二级` 精确匹配；
    2. 只传二级时，在所有二级分类中唯一匹配；
    3. 同一一级分类的 `primary_defaults`。

    未命中时不拼硬编码提示词；`strict=True` 会直接阻塞调用方。
    """
    major, theme = split_genre_label(genre_label)
    data = dict(bundle or get_opening_profile_bundle_cached())
    profiles = data.get("profiles") or {}
    primary_defaults = data.get("primary_defaults") or {}
    primary_aliases = data.get("primary_aliases") or {}
    if isinstance(primary_aliases, Mapping) and major in primary_aliases:
        major = str(primary_aliases.get(major) or major)

    raw: Mapping[str, Any] | None = None
    source_level = ""
    if major and theme:
        raw = ((profiles.get(major) or {}).get(theme) if isinstance(profiles.get(major), Mapping) else None)
        source_level = "secondary"
    if raw is None and major and not theme:
        matches: list[tuple[str, Mapping[str, Any]]] = []
        for candidate_major, secondaries in profiles.items():
            if not isinstance(secondaries, Mapping):
                continue
            candidate = secondaries.get(major)
            if isinstance(candidate, Mapping):
                matches.append((str(candidate_major), candidate))
        if len(matches) == 1:
            major, theme = matches[0][0], major
            raw = matches[0][1]
            source_level = "secondary_alias"
    if raw is None and major:
        candidate = primary_defaults.get(major)
        if isinstance(candidate, Mapping):
            raw = candidate
            source_level = "primary_default"

    if raw is None:
        # A-08：画像缺失**不再阻塞生成**。
        #
        # 改造前：``strict=True`` 直接 raise ⇒ 新题材（无画像）根本开不了书。
        # 但这条 raise 在生产上**没有调用方能触达**（resolve_opening_profile
        # 当时零生产调用）⇒ 登记表的「禁止开书」是纸面风险，不是线上风险。
        #
        # 现在：降级到配置资产里的**通用契约**（``generic_contract``），
        # 并用 ``source_level="generic"`` **如实标记**来源
        # ⇒「用了通用契约」与「用了该题材画像」在数据上可分辨。
        generic = _generic_contract(data)
        if strict and not generic:
            # 连通用契约都读不到 ⇒ 配置资产坏了，此时确实该挡一下。
            raise OpeningProfileError(
                f"类型开篇画像缺失且通用契约不可用: {genre_label or '(空)'}"
            )
        if not generic:
            return None
        opening_profile = {
            "reader_promise": list(generic.get("reader_promise") or []),
            "opening_mechanism": {},
            "metric_snapshot": {},
            "genre_major": major,
            "genre_theme": theme,
            "source_level": "generic",
        }
        return GenreOpeningProfile(
            genre_major=major,
            genre_theme=theme,
            source_level="generic",
            opening_profile=opening_profile,
            reader_contract=generic,
            rhythm_constraints={},
        )

    opening_profile, reader_contract, rhythm_constraints = _profile_parts(raw, data)
    opening_profile.update(
        {
            "genre_major": major,
            "genre_theme": theme,
            "source_level": source_level,
        }
    )
    return GenreOpeningProfile(
        genre_major=major,
        genre_theme=theme,
        source_level=source_level,
        opening_profile=opening_profile,
        reader_contract=reader_contract,
        rhythm_constraints=rhythm_constraints,
    )
