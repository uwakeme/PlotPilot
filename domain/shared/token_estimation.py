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
    """估算文本的 token 数（中英混合加权）。

    ⚠ 第 27 批修正：**加权平均被加权了两次**。

    旧实现是 ``zh_tokens * ratio + en_tokens * (1 - ratio)``，
    而 ``zh_tokens`` 本身就是 ``c / 1.5``（**已经含了一次中文比例**）
    ⇒ 等价于 **r² 加权**（比例被乘了两次）：

    * 纯中文（r=1）⇒ 结果与线性一致 ⇒ 看不出问题；
    * 纯英文（r=0）⇒ 也一致 ⇒ 看不出问题；
    * **中英混排**（r≈0.7）⇒ **token 被系统性低估**。

    ⚠ 实测（生产书事实锁，r≈0.72）：3876 字被估成 **1418** tokens，
    线性口径的真值是 **2133** ⇒ 槽位的 ``max_tokens`` 上限**形同虚设**
    ⇒ 内容**超量**进槽位 ⇒ 到正文侧（按**字符**上限）才被砍一刀
    ⇒ **二次裁剪**，而且第一次裁的时候谁都不知道。

    ⇒ 现在按**线性**加权（两个速率按占比加权平均）：
    ``c / 1.5 + (t - c) / 4``
    ⇒ 纯中文 / 纯英文的结果**完全不变** ⇒ 只有混排被修正（往上修）。
    """
    if not text:
        return 0

    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    total_chars = len(text)
    if total_chars == 0:
        return 0

    zh_tokens = chinese_chars / CHARS_PER_TOKEN_ZH
    en_tokens = (total_chars - chinese_chars) / CHARS_PER_TOKEN_EN
    return int(zh_tokens + en_tokens + 0.5)
