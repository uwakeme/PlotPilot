"""Recent chapter excerpt policy for generation context."""
from __future__ import annotations

from typing import Any, Iterable

#: N-1 章「给全文」的**显式**上限（字符）——第 47 批。
#:
#: ⚠ 为什么要有它：此前「短章给全文」的判定是**隐式**耦合在
#:   ``head_chars + tail_chars`` 上的 ⇒ ⇒ 谁把 ``tail_chars`` 一调
#:   （它是「章末保留量」这个**另一个**含义的参数），
#:   就悄悄把「给全文」变成了「给首尾」⇒ ⇒ **两个含义互相牵制**。
#: ⇒ ⇒ 现在：全文阈值**独立**（默认 3000 字，覆盖生产书 1540~2476 的章长）
#: ⇒ 「给全文」与「章末保留多少」**互不影响**。
DEFAULT_PREV_FULL_TEXT_MAX_CHARS = 3000


def excerpt_immediate_previous_chapter(
    content: str,
    *,
    head_chars: int,
    tail_chars: int,
    full_text_max_chars: int = DEFAULT_PREV_FULL_TEXT_MAX_CHARS,
) -> str:
    """Render the N-1 chapter.

    ⚠ 档位（按**全文长度**）：
      * ``len(raw) <= full_text_max_chars`` ⇒ **给全文**（标注「本章全文」）
        ⇒ 模型看得见上一章的**中段**（不只首尾）⇒ 语感/节奏/人物状态都能接上；
      * 否则 ⇒ 章首略览 + 章末节选（带省略号，明确告知中间被省略）。
    """
    raw = (content or "").strip()
    if not raw:
        return ""
    if full_text_max_chars > 0 and len(raw) <= full_text_max_chars:
        return f"【上一章全文，供本章开头承接】\n{raw}"
    if len(raw) <= tail_chars:
        return f"【章末节选，供本章开头承接】\n{raw}"
    if len(raw) <= head_chars + tail_chars:
        return f"【章末节选，供本章开头承接】\n{raw}"
    head = raw[:head_chars]
    tail = raw[-tail_chars:]
    return (
        f"【章首略览】\n{head}……\n"
        f"【章末节选，供本章开头承接】\n{tail}"
    )


def build_recent_chapters_context(
    chapters: Iterable[Any],
    *,
    chapter_number: int,
    limit: int = 5,
    current_beat_index: int = 0,
    prev_head_chars: int,
    prev_tail_chars: int,
    older_head_chars: int,
    prev_full_text_max_chars: int = DEFAULT_PREV_FULL_TEXT_MAX_CHARS,
) -> str:
    """Build the recent-chapter block used by dynamic T2 context.

    ⚠ ``prev_full_text_max_chars`` 与 ``prev_head_chars`` / ``prev_tail_chars``
    **互不影响**（第 47 批）⇒ 「要不要给全文」不再被「章末保留多少」牵制。
    """
    all_chapters = list(chapters)
    recent = sorted(
        [chapter for chapter in all_chapters if chapter.number < chapter_number],
        key=lambda chapter: chapter.number,
        reverse=True,
    )[:limit]

    prev_num = chapter_number - 1
    prev2_num = chapter_number - 2
    lines = ["【最近章节】"]

    for chapter in reversed(recent):
        lines.append(f"\n第 {chapter.number} 章：{chapter.title}")
        body = (chapter.content or "").strip()
        if not body:
            continue
        if chapter.number == prev_num:
            excerpt = excerpt_immediate_previous_chapter(
                chapter.content or "",
                head_chars=prev_head_chars,
                tail_chars=prev_tail_chars,
                full_text_max_chars=prev_full_text_max_chars,
            )
            if excerpt:
                lines.append(excerpt)
            continue
        if chapter.number == prev2_num:
            tail_n = prev_tail_chars // 2
            tail = body[-tail_n:] if len(body) > tail_n else body
            lines.append(f"【章末节选，供跨章一致性参考】\n{tail}")
            continue
        preview = body[:older_head_chars]
        if len(body) > older_head_chars:
            preview = f"{preview}..."
        lines.append(f"【章首预览】\n{preview}")

    if current_beat_index > 0:
        current_chapter = next(
            (chapter for chapter in all_chapters if chapter.number == chapter_number),
            None,
        )
        if current_chapter and current_chapter.content:
            current_content = current_chapter.content.strip()
            if current_content:
                continuation_preview = (
                    current_content[-2000:]
                    if len(current_content) > 2000
                    else current_content
                )
                lines.append("\n【本章已生成（断点续写上下文）】")
                lines.append(f"当前节拍索引: {current_beat_index}")
                lines.append(f"已生成 {len(current_content)} 字")
                lines.append("---")
                lines.append(continuation_preview)

    return "\n".join(lines)
