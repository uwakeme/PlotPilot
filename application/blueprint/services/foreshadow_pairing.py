"""幕内伏笔 plant/resolve 配对校验（C-01 的兜底安全网）

背景
----
`infrastructure/ai/prompt_packages/nodes/planning-act/user.md:29,36` 对伏笔节奏
有四条硬约束，但**全部无代码校验**：

    伏笔操作 foreshadow_action 必选其一：plant、resolve、plant_and_resolve、none。
    none 仅限纯动作或过渡章节，每幕不超过 2 章。
    伏笔节奏：本幕内种下的伏笔，至少 1 条需要在本幕或下一幕回收；
              不能连续 2 章都是 none；最后一章必须 resolve 或 plant_and_resolve。

`chapter_planning_policy.validate_lightweight_act_plan` 只校验章数、章号与
5 个必填字段（number/title/main_event/handoff×2），所以这四条约束实际上
**只靠模型自觉**。

实测（2026-10-17，生产库）
------------------------
586 个章节点的 ``metadata['act_chapter_plan']`` 带 ``foreshadow_action``，
取值分布 ``plant_and_resolve`` 261 / ``plant`` 250 / ``resolve`` 67 / ``none`` 8；
按 ``parent_id`` 精确分组成 111 幕、其中 105 幕 ≥3 章 —— **5 条规则全部零违例**。

⇒ **模型一直遵守，代码从不校验。** 所以把本模块接进热路径是低风险的，
同时为 C-01「允许 AI 报『本幕只需 N 章』，走增量补章」提供兜底：
放松章数硬校验之后，结构完整性由这里接住。

能力边界（重要）
----------------
本模块只校验**单幕之内**的约束。「本幕种下的伏笔在下一幕回收」需要跨幕状态，
本模块**不校验**。不要把它当成「伏笔配对完整性」的全部 —— 那是跨幕不变量，
需要单独的跨幕校验器。

另外，单个章节计划里只有 ``foreshadow_action``，**没有伏笔 ID**，
所以 R5 只能做**聚合**判断（本幕有 plant 动作 ⇒ 本幕至少有一次 resolve 动作），
无法验证「具体哪条伏笔被回收了」。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

#: 合法取值，与 planning-act/user.md 保持一致
FORESHADOW_ACTIONS: frozenset[str] = frozenset(
    {"plant", "resolve", "plant_and_resolve", "none"}
)

#: 会「种下」伏笔的动作
PLANTING_ACTIONS: frozenset[str] = frozenset({"plant", "plant_and_resolve"})

#: 会「回收」伏笔的动作
RESOLVING_ACTIONS: frozenset[str] = frozenset({"resolve", "plant_and_resolve"})

#: 每幕允许的 none 章数上限（提示词：每幕不超过 2 章）
MAX_NONE_PER_ACT = 2


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value) == 0
    return False


def _read_action(raw: Mapping[str, Any]) -> tuple[str, bool]:
    """返回 (动作, 是否缺失或非法)。缺失/空一律按「缺失」处理，不猜默认值。"""
    value = raw.get("foreshadow_action")
    if _blank(value):
        return "", True
    action = str(value).strip()
    return action, action not in FORESHADOW_ACTIONS


def validate_foreshadow_pairing(
    chapters: Any,
    *,
    max_none_per_act: int = MAX_NONE_PER_ACT,
) -> list[str]:
    """校验一整幕的伏笔节奏。

    Args:
        chapters: 幕内章节计划序列（按章号顺序），元素需含 ``foreshadow_action``。
        max_none_per_act: 允许的 ``none`` 章数上限。

    Returns:
        错误描述列表；**空列表表示通过**。语义与
        ``validate_lightweight_act_plan`` 保持一致，便于同处汇总。

    校验项：
        R1 每个章节的 ``foreshadow_action`` 必须是四个合法值之一
        R2 ``none`` 章数不超过上限
        R3 不能连续 2 章都是 ``none``
        R4 最后一章必须 ``resolve`` 或 ``plant_and_resolve``
        R5 本幕若有 plant 动作，本幕至少要有一次 resolve 动作
    """
    if not isinstance(chapters, (list, tuple)):
        return ["chapters must be a list"]

    errors: list[str] = []
    actions: list[str] = []
    invalid_positions: list[int] = []

    for index, raw in enumerate(chapters, start=1):
        if not isinstance(raw, Mapping):
            errors.append(f"chapter {index} must be an object")
            actions.append("")
            continue
        action, invalid = _read_action(raw)
        if invalid:
            if not action:
                errors.append(
                    f"chapter {index} missing foreshadow_action "
                    f"(必须是 {'/'.join(sorted(FORESHADOW_ACTIONS))} 之一)"
                )
            else:
                errors.append(
                    f"chapter {index} has invalid foreshadow_action: {action!r}"
                )
            invalid_positions.append(index)
            actions.append(action or "")
        else:
            actions.append(action)

    if not actions:
        return errors

    # R2 —— none 章数
    none_count = actions.count("none")
    if none_count > max_none_per_act:
        errors.append(
            f"foreshadow none count {none_count} exceeds limit {max_none_per_act}"
        )

    # R3 —— 不可连续 2 章 none
    for i in range(len(actions) - 1):
        if actions[i] == "none" and actions[i + 1] == "none":
            errors.append(
                f"chapters {i + 1}-{i + 2} are consecutive none "
                f"(提示词禁止连续 2 章 none)"
            )
            break

    # R4 —— 末章必须回收
    last = actions[-1]
    if last and last not in RESOLVING_ACTIONS:
        errors.append(
            f"last chapter must be resolve or plant_and_resolve, got {last!r}"
        )

    # R5 —— 有种必得有收（聚合判断；跨幕配对不在本模块职责内）
    if any(a in PLANTING_ACTIONS for a in actions) and not any(
        a in RESOLVING_ACTIONS for a in actions
    ):
        errors.append(
            "act plants foreshadowings but never resolves any "
            "(提示词要求本幕种下的伏笔至少 1 条在本幕或下一幕回收；"
            "跨幕配对需另行校验)"
        )

    return errors
