"""Runtime prompt context providers.

Some CPMS templates need deterministic system-built values, such as schema
snippets, that are not author-editable Variable Hub facts. Providers registered
here inject those values at render time only; they are intentionally absent
from input bindings, missing-variable review, and Variable Hub snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from application.ai_invocation.dtos import InvocationSpec


@dataclass(frozen=True)
class RuntimePromptContext:
    spec: InvocationSpec
    aliases: Mapping[str, Any]


RuntimePromptProvider = Callable[[RuntimePromptContext], Mapping[str, Any]]

_PROVIDERS: dict[tuple[str, str], RuntimePromptProvider] = {}


def register_runtime_prompt_provider(
    *,
    operation: str,
    node_key: str,
    provider: RuntimePromptProvider,
) -> None:
    _PROVIDERS[(operation, node_key)] = provider


def runtime_prompt_values_for_spec(
    spec: InvocationSpec,
    aliases: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    provider = _PROVIDERS.get((spec.operation, spec.node_key))
    if provider is None:
        provider = _PROVIDERS.get(("", spec.node_key))
    if provider is None:
        return {}
    return dict(provider(RuntimePromptContext(spec=spec, aliases=aliases or {})) or {})


def runtime_prompt_value_names_for_spec(spec: InvocationSpec) -> set[str]:
    return set(runtime_prompt_values_for_spec(spec, {}).keys())


def with_runtime_prompt_values(
    spec: InvocationSpec,
    aliases: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(aliases or {})
    for alias, value in runtime_prompt_values_for_spec(spec, merged).items():
        merged.setdefault(alias, value)
    return merged


def _resolve_opening_profile(context: RuntimePromptContext):
    """按本书的题材标签解析开篇画像。

    A-08：改造前画像**从来没进过模型** —— 三个槽位要么是写死的占位文本、
    要么恒为 ``{}`` ⇒ 配置资产里的开篇画像（15 个一级分类 / 82 个二级）
    整条链是死的。

    解析顺序：
    - 有题材标签 ⇒ 按标签解析（命中题材画像 / 未命中降级通用契约）；
    - 连题材标签都没有 ⇒ 仍给**通用契约**（判不出来就说判不出来，
      不编造题材画像）。

    返回 ``None`` 只在「配置资产里连通用契约都读不到」时发生。
    """
    from application.core.taxonomy.opening_profiles import resolve_opening_profile

    aliases = context.aliases or {}
    genre_label = str(
        aliases.get("novel.genre_label")
        or aliases.get("novel.setup.genre_label")
        or ""
    ).strip()
    return resolve_opening_profile(genre_label, strict=False)


def _opening_profile_variables(context: RuntimePromptContext) -> Mapping[str, Any]:
    """画像三键的**dict 形态**（供契约声明为 ``object`` 的节点使用）。"""
    profile = _resolve_opening_profile(context)
    if profile is None:
        # 配置资产里连通用契约都读不到 ⇒ 明确说「没有画像」，不装成有。
        return {
            "genre_opening_profile": {},
            "genre_reader_contract": {},
            "genre_rhythm_constraints": {},
        }
    return profile.as_variables()


def _bible_worldbuilding_runtime_values(context: RuntimePromptContext) -> Mapping[str, Any]:
    from application.world.worldbuilding_schema import build_fields_desc_for_prompt

    variables = _opening_profile_variables(context)
    opening_profile = variables.get("genre_opening_profile") or {}
    return {
        # worldbuilding JSON 骨架的唯一真源（user.md 的 {fields_desc} 占位内容）
        "fields_desc": build_fields_desc_for_prompt(),
        # ``genre_opening_profile`` 在 bible-worldbuilding 的模板里是**一段可读文本**
        # （package.yaml 声明 ``type: string``）⇒ 这里序列化成文本，
        # 免得 Jinja2 渲染出 Python 字典字面量。
        "genre_opening_profile": _format_opening_profile(opening_profile),
        "genre_reader_contract": variables.get("genre_reader_contract") or {},
        "genre_rhythm_constraints": variables.get("genre_rhythm_constraints") or {},
    }


def _format_opening_profile(opening_profile: Mapping[str, Any]) -> str:
    """把画像 dict 渲染成给模型看的可读文本。

    ⚠ **不编造**：只写画像里真有的字段；没有就写「（该类型未提供开篇画像细节）」。
    """
    if not opening_profile:
        return "（该类型未提供开篇画像细节，按作者梗概与题材基调节奏把握开篇）"

    lines: list[str] = []
    major = str(opening_profile.get("genre_major") or "").strip()
    theme = str(opening_profile.get("genre_theme") or "").strip()
    if major or theme:
        lines.append(f"类型：{major} / {theme}".rstrip(" /"))

    promise = [str(x).strip() for x in (opening_profile.get("reader_promise") or []) if str(x).strip()]
    if promise:
        lines.append("读者承诺：")
        lines.extend(f"- {item}" for item in promise)

    mechanism = opening_profile.get("opening_mechanism")
    if isinstance(mechanism, Mapping) and mechanism:
        lines.append("开篇机制：")
        for key, value in mechanism.items():
            text = str(value).strip()
            if text:
                lines.append(f"- {key}：{text}")

    if not lines:
        return "（该类型未提供开篇画像细节，按作者梗概与题材基调节奏把握开篇）"
    return "\n".join(lines)


register_runtime_prompt_provider(
    operation="bible.setup.worldbuilding",
    node_key="bible-worldbuilding",
    provider=_bible_worldbuilding_runtime_values,
)


def _quick_macro_runtime_values(context: RuntimePromptContext) -> Mapping[str, Any]:
    """极速宏观规划：注入类型开篇画像三键（**dict 形态**）。

    A-08：这三个键在 ``autopilot_planning`` 契约里是 ``derived_config``
    （运行时派生、不落 Variable Hub），但此前**没有任何 provider 给它们赋值**
    ⇒ 契约的默认值 ``{}`` 一直是最终值 ⇒ 画像从来没进过宏观规划。

    与 ``bible-worldbuilding`` 的差别：那个节点把画像渲染成**文本**
    （package.yaml 声明 ``type: string``），这个节点声明的是 ``object``
    ⇒ 这里保持 dict 原样。
    """
    variables = _opening_profile_variables(context)
    return {
        "genre_opening_profile": variables.get("genre_opening_profile") or {},
        "genre_reader_contract": variables.get("genre_reader_contract") or {},
        "genre_rhythm_constraints": variables.get("genre_rhythm_constraints") or {},
    }


register_runtime_prompt_provider(
    operation="autopilot.macro.plan",
    node_key="planning-quick-macro",
    provider=_quick_macro_runtime_values,
)
