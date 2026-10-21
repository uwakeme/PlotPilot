/**
 * 终局收束章数预算 —— **前端唯一一份**。
 *
 * ⚠ 权威值在**后端**：`domain/novel/value_objects/generation_preferences.py`
 * 的 `FINALE_BUDGET_MIN / MAX / DEFAULT`（写路径真的会 `clamp_finale_budget`，
 * 越界值会被后端钳回）。
 *
 * 前端这份只是 **UI 兜底**（别让用户填了越界值就发出去），**不是事实源**。
 * 复审 §3.2d：同一常量曾散落在 4 处（两个组件各一份初值 + 一份钳制 + 一份兜底）
 * ⇒ 现在只此一份，组件从这儿取。
 *
 * 例外：驾驶舱（`AutopilotPanel`）那条路径的 min/max **由后端提案接口给**
 * （`GET /novels/{id}/target-budget-proposal` 的 `finale_budget_min/max`），
 * 不用这里的值 —— 那条路径已经把后端的值接上了。
 */
export const FINALE_BUDGET_MIN = 5
export const FINALE_BUDGET_MAX = 200
export const FINALE_BUDGET_DEFAULT = 20

/** 把用户填的预算钳到 [min, max]；空/非法值回落默认值。 */
export function clampFinaleBudget(value: unknown): number {
  const n = Number(value)
  if (!Number.isFinite(n) || n === 0) return FINALE_BUDGET_DEFAULT
  return Math.max(FINALE_BUDGET_MIN, Math.min(FINALE_BUDGET_MAX, n))
}
