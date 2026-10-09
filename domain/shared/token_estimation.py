"""统一 token 估算（B5 收敛的唯一真源）。

此前 4 处实现口径各异：
- context_budget_allocator.estimate_tokens：中英加权（最精细，1.5/4.0 字每 token）
- context_builder.estimate_tokens：委托 allocator ✓
- beat_sheet_service 内联：len(text)/1.5 恒按中文
- memory_orchestrator_impl 内联：len(text)//2

收敛后统一用中英加权版（中文 1.5 字/token、英文 4 字/token，按占比加权）。
"""
from __future__ import annotations

CHARS_PER_TOKEN_ZH = 1.5  # 中文：1 token ≈ 1.5 字符
CHARS_PER_TOKEN_EN = 4.0  # 英文：1 token ≈ 4 字符


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（中英混合加权）。"""
    if not text:
        return 0

    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    total_chars = len(text)
    if total_chars == 0:
        return 0

    chinese_ratio = chinese_chars / total_chars
    zh_tokens = chinese_chars / CHARS_PER_TOKEN_ZH
    en_tokens = (total_chars - chinese_chars) / CHARS_PER_TOKEN_EN
    return int(zh_tokens * chinese_ratio + en_tokens * (1 - chinese_ratio) + 0.5)
