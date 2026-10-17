"""A-01：支线（sublines）的 AI 优先与不变量校验。

背景
----
``setup_main_plot_suggestion_service`` 里，LLM 没给 ``sublines`` 时，代码会
**整条编造**：id / name / role / purpose / description / guard 全部代写，
``merge_chapter`` 用 ``target_chapters * 0.22 / 0.3 / 0.26 / 0.25`` 四个公式算。
于是「模型规划了一条支线」与「代码随手补了一条」在数据上完全一样。

登记表 A-01 给的两个选项是「删兜底」或「强制 AI 填」——
本模块取前者：兜底**标记**而不**编造**，并补上登记表列的
不变量「merge_chapter ≤ 目标章数、晚于引入章」。

不变量
------
- ``merge_chapter`` 必须 ≤ 目标章数（支线不能比全书还长）
- ``merge_chapter`` 必须 > 引入章（不能「在引入的同一章汇流」）
- 模型没给 ``merge_chapter`` ⇒ 记 0 会同时违反上面两条且**静默** ——
  故这里显式判为未评估，而不是塞 0。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

SOURCE_AI = "ai"
SOURCE_NONE = "none"


def normalize_merge_chapter(raw: Any) -> Optional[int]:
    """读取模型给的汇流章；不可用时返回 ``None``（**不是 0**）。

    原实现在这里塞 0，而 0 同时违反「≤ 目标章数」与「晚于引入章」两条不变量，
    属于「用看起来正常的数字掩盖没测」。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _read_merge_chapter(subline: Dict[str, Any]) -> Any:
    """取汇流章原始值。

    ⚠️ **不能用** ``sub.get("merge_chapter") or sub.get("target_chapter")``：
    ``0`` 在 Python 里是 falsy，AI 明确给出「第 0 章汇流」会被当成「没给」。
    而「AI 说了 0（越界）」与「AI 没说」是**两件不同的事**，必须能分辨。
    """
    value = subline.get("merge_chapter")
    if value is None or (isinstance(value, str) and not value.strip()):
        value = subline.get("target_chapter")
    return value


def check_subline(
    subline: Dict[str, Any],
    *,
    target_chapters: int,
    introduced_chapter: int = 0,
) -> List[str]:
    """校验单条支线的不变量，返回错误描述列表（空 = 通过）。"""
    errors: List[str] = []
    name = str(subline.get("name") or subline.get("id") or "支线")

    merge = normalize_merge_chapter(_read_merge_chapter(subline))
    if merge is None:
        errors.append(
            f"支线「{name}」未给出 merge_chapter（汇流章）—— "
            f"不得用 0 代替，0 会同时违反「≤ 目标章数」与「晚于引入章」"
        )
        return errors

    if merge <= introduced_chapter:
        errors.append(
            f"支线「{name}」汇流章 {merge} 不晚于引入章 {introduced_chapter}"
        )
    if merge > int(target_chapters):
        errors.append(
            f"支线「{name}」汇流章 {merge} 超过目标总章数 {target_chapters}"
        )
    return errors


def normalize_sublines(
    raw: Any,
    *,
    target_chapters: int,
    introduced_chapter: int = 0,
) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """规范化模型的 ``sublines``。

    Returns:
        ``(支线列表, 来源, 错误列表)``

    - 来源 ``"ai"``：模型给了至少一条
    - 来源 ``"none"``：模型没给 —— 此时支线列表**为空**，**不编造**。

    ``errors`` 是需要回传给调用方的结构性问题（不抛异常，
    让上层决定是提示用户还是重新生成）。
    """
    if not isinstance(raw, list) or not raw:
        return [], SOURCE_NONE, []

    out: List[Dict[str, Any]] = []
    errors: List[str] = []
    for index, item in enumerate(raw[:4], start=1):
        if not isinstance(item, dict):
            continue
        errors.extend(
            check_subline(
                item,
                target_chapters=target_chapters,
                introduced_chapter=introduced_chapter,
            )
        )
        role = str(item.get("role") or "sub").strip().lower()
        if role not in ("sub", "dark"):
            role = "sub"
        merge = normalize_merge_chapter(_read_merge_chapter(item))
        out.append(
            {
                "id": str(item.get("id") or f"subline_{index}")[:80],
                "name": str(item.get("name") or item.get("title") or f"支线 {index}")[:160],
                "role": role,
                "purpose": str(item.get("purpose") or "")[:800],
                "description": str(item.get("description") or "")[:1200],
                "guard": str(item.get("guard") or "")[:400],
                # 没给汇流章就是 None，不塞 0
                "merge_chapter": merge,
                "merge_chapter_source": SOURCE_AI if merge is not None else SOURCE_NONE,
            }
        )
    return out, (SOURCE_AI if out else SOURCE_NONE), errors
