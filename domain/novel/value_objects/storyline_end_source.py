"""故事线「预估收尾章」的来源词表 —— 单一事实源。

`storylines.estimated_chapter_end_source` 这一列的取值集合。
**值本身就是唯一事实源**：不另设「已评估」标记 —— 值 + 标记两个事实源会漂移
（第九轮复审已把 `last_chapter_tension_evaluated` 那种标记撤掉过一次）。

取值：

| 值 | 含义 |
|---|---|
| `ai` | 模型给了收尾章，且未晚于当前章 ⇒ 直接采用模型原值 |
| `ai_adjusted` | 模型给了但被代码改写（抬到不变量下界）⇒ 最终取值不是模型原值 |
| `fallback` | 模型没给 / 不可解析 / 越界，代码兜底 |
| `manual` | **人工**设定或改写（用户创建故事线，或手动改了收尾章） |
| `''` | 旧数据未标注 —— **不等于** `ai`，读端必须显式判 |

⚠ 空串是「未标注」，不是「未知来源」。默认填 `ai` 会把未标注的旧数据
读成「这是模型的判断」。

放在 domain 层的原因：这是**领域概念**（一个领域值的来源），
`domain/novel/services/storyline_manager.py` 要用它 ——
domain 不得反向依赖 application。
"""
from __future__ import annotations

#: 模型给了值，且代码没改过。
SOURCE_AI = "ai"

#: 模型给了值，但**被代码改写**（钳位、`int()` 截断、抬到不变量下界）。
#: 最终值不是模型原值，不能标 `ai` —— 那是落库后的假来源。
SOURCE_AI_ADJUSTED = "ai_adjusted"

#: 模型没给 / 不可解析 / 非有限值 / 超出上限，代码兜底。
SOURCE_FALLBACK = "fallback"

#: 人工设定或改写：用户创建故事线，或手动改了预估收尾章。
SOURCE_MANUAL = "manual"

#: 旧数据未标注。**不是**「未知来源」，也不是 `ai`。
SOURCE_UNMARKED = ""

#: 全部合法取值（不含 `SOURCE_UNMARKED` —— 它是「没标」而不是一种来源）。
ALL_SOURCES = (SOURCE_AI, SOURCE_AI_ADJUSTED, SOURCE_FALLBACK, SOURCE_MANUAL)
