"""B-06：规划深度档位的口径、**夹紧**与来源。

改造前
------
规划深度是**目标章数的纯函数**（``continuous_planning_service``）：

- ``target_chapters > 500`` → ``framework``（只铺到部/卷）
- ``target_chapters > 100`` → ``partial``
- 否则 → ``full``（铺到幕）

而深度决定的是**要求模型输出的结构精细度**（三套 JSON schema）
⇒ 档位既不是创作判断，也没法按这本书的实际形态调。

现在
----
**AI 自评**（新节点 ``planning-depth-assessment``）：它看题材 / 梗概 / 目标章数，
判断「这本书的结构需要铺到多细才够用」。

硬约束保留
----------
章数给出的档位是**上限**（能到多细）：章数越大越粗
⇒ **不许**选比上限更细的档（那会让输出规模随章数一起炸），
这是登记表 B-06 的约束「防 schema 过大撑爆上下文」。
被夹过时 ``clamped=True`` ⇒ 调用方**必须**能知道（不许静默）。

降级纪律
--------
AI 不可用 / 输出不合契约 / 没给 ⇒ **用章数阈值那档**
（与改造前**逐位一致**），来源标 ``threshold``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

#: 只铺到部 / 卷
DEPTH_FRAMEWORK = "framework"
#: 铺到部 / 卷，再往下少量
DEPTH_PARTIAL = "partial"
#: 铺到幕（最细）
DEPTH_FULL = "full"

#: 三档（从粗到细）
DEPTHS: Tuple[str, ...] = (DEPTH_FRAMEWORK, DEPTH_PARTIAL, DEPTH_FULL)
#: 粗 → 细 的次序（数字越大越细）
DEPTH_ORDER = {DEPTH_FRAMEWORK: 0, DEPTH_PARTIAL: 1, DEPTH_FULL: 2}

#: 来源标记
SOURCE_AI = "ai"
SOURCE_THRESHOLD = "threshold"


@dataclass(frozen=True)
class PlanningDepth:
    """本次实际使用的规划深度（连同来源与是否被夹紧）。"""

    #: 实际使用的档位
    depth: str = DEPTH_FULL
    #: 来源（``ai`` / ``threshold``）
    source: str = SOURCE_THRESHOLD
    #: 是否被「章数上限」夹过（被夹 ⇒ 调用方要能知道，不许静默）
    clamped: bool = False


def depth_cap_for(target_chapters: int) -> str:
    """章数给出的**上限**（这一本书最多能铺到多细）。

    与改造前的阈值**逐位一致**（>500 → framework / >100 → partial / 否则 full），
    但语义从「档位」变成「上限」—— 它是**安全边界**，不再替 AI 做决定。
    """
    chapters = int(target_chapters or 0)
    if chapters > 500:
        return DEPTH_FRAMEWORK
    if chapters > 100:
        return DEPTH_PARTIAL
    return DEPTH_FULL


def clamp_depth(requested: Optional[str], cap: str) -> Tuple[str, bool]:
    """把请求的档位夹到**不比上限更细**的范围内。

    Returns:
        ``(depth, clamped)`` —— ``clamped=True`` 表示请求被夹过。
    """
    if requested not in DEPTHS:
        return cap, False
    if DEPTH_ORDER[requested] <= DEPTH_ORDER[cap]:
        return requested, False
    return cap, True


def resolve_planning_depth(
    target_chapters: int,
    requested_depth: Optional[str] = None,
) -> PlanningDepth:
    """解析本次实际使用的规划深度。

    - AI / 调用方给了合法档位 ⇒ 用它，但**不许比章数上限更细**（夹了要标出来）；
    - 没给 / 给的不合法 ⇒ 用章数阈值那档（与改造前**逐位一致**），来源 ``threshold``。
    """
    cap = depth_cap_for(target_chapters)
    if requested_depth not in DEPTHS:
        return PlanningDepth(depth=cap, source=SOURCE_THRESHOLD, clamped=False)
    depth, clamped = clamp_depth(requested_depth, cap)
    return PlanningDepth(depth=depth, source=SOURCE_AI, clamped=clamped)
