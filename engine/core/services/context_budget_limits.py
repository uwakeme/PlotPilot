"""上下文构建的**全局 token 上限**（预算口径的唯一定义点）。

为什么放在这里
--------------
这个常量有 **6** 个曾经的默认参数引用它，而它们的模块层级各不相同：

- ``application/engine/services/context_builder.py``（主路径）
- ``application/engine/services/context_budget_allocator.py``（预算分配）
- ``application/engine/services/context_budget_models.py``（预算模型）
- ``application/engine/dtos/scene_director_dto.py``（DTO 默认值）
- ``engine/core/services/memory_orchestrator.py``（``engine.core``，**不能**依赖
  ``application``）

⇒ 放在 ``application`` 会被「``context_builder`` ↔ allocator」互相 import 成环；
放在 ``engine/core`` 又让 ``application`` 反向依赖它没问题、但 ``domain`` 仍进不来。
⇒ **``engine/core/services`` 是唯一两边都能引用的层**（application → engine 是
正确的方向；engine.core 内部引用自己也不越层）。

三条不变量
----------
1. **预算是多少只由这一处决定** —— 降级路径与主路径**同一个值**
   （D-06：此前降级是 20000、主路径 35000 ⇒ 降级时上下文被**静默削薄 43%**，
   而调用方只看到一条 warning）。
2. 任何模块**不许**再写死这个数字 ⇒ 守卫
   ``tests/unit/application/engine/test_context_budget_single_source.py``
   会扫全仓（call keyword / 位置参数默认值 / 关键字专用参数默认值**三种形状**）。
3. 各处默认参数**引用**本常量，而不是各自复制一份。
"""

from __future__ import annotations

#: 上下文构建的全局 token 上限（第 41 批：35000 → 60000）。
#:
#: ⚠ 为什么抬（用户 2026-10-29 拍板「真正吃 1M 上下文窗口」）：
#:   * 事实锁槽位上限 4000 → **10000 tokens**（第 41 批）⇒ T0 需求涨到约 11800
#:     ⇒ 旧的 40% 保护线（35000 × 40% = 14000）只剩 16% 余量
#:     ⇒ 下一档要抬时立刻顶破 ⇒ 提前把总预算抬到 60000（40% 线 = **24000**）。
#:   * T1~T3（近章 / 向量召回 / 伏笔 / 节拍…）额度 21000 → 36000
#:     ⇒ 事实锁变大后它们不会被挤掉。
#:
#: ⚠ **今天它不改变任何东西**（实测：当前书 pack 用 12889/35000 = 37%、
#:   长书 9073/35000 = 26% ⇒ 从来没满过）⇒ 这一步是**给将来留天花板**，
#:   不是现在就变贵。实际用量由**内容**决定，不由这个数决定。
DEFAULT_CONTEXT_MAX_TOKENS = 60000
