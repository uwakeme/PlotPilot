/**
 * 守护进程监督者状态（**全局**）—— 数据源是 `GET /health`。
 *
 * ⚠ 为什么读 `/health` 而不是 per-novel 的 `/autopilot/status`：
 *   ① 监督者是**进程级**的（一个 API 进程一个监督者）⇒ 不属于某一本书；
 *   ② `/health` **不碰 DB**（只有 uptime + 守护进程状态 + 监督者状态）⇒ 轮询很轻；
 *   ③ 徽标要出现在**书库页**（Home）⇒ 那儿没有 novel 上下文，按书订阅不了。
 *
 * ⚠ 与 `autopilotStatusStore` 的分工：那条链是**按书**的运行状态（写书时才需要）；
 *   本 store 是**全局**的健康/监督者状态 ⇒ 两条链各管各的，互不触发。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { fetchJson } from '@/api/http'
import type { DaemonSupervisorState } from '@/api/autopilot'

/** 轮询间隔（毫秒）。`/health` 很轻 ⇒ 30 秒足够。 */
const POLL_INTERVAL_MS = 30_000

/** 页面不可见时的保活间隔（毫秒）。 */
const HIDDEN_POLL_INTERVAL_MS = POLL_INTERVAL_MS * 3

interface HealthPayload {
  status?: string
  daemon_supervisor?: DaemonSupervisorState
}

export const useDaemonHealthStore = defineStore('daemonHealth', () => {
  const supervisor = ref<DaemonSupervisorState | null>(null)
  const lastCheckedAt = ref<number | null>(null)
  const lastError = ref('')

  let timer: ReturnType<typeof setTimeout> | null = null
  let consumerCount = 0
  let stopped = true

  async function pollOnce() {
    try {
      const payload = await fetchJson<HealthPayload>('/health', { retryOnGet: 0 })
      supervisor.value = payload.daemon_supervisor ?? null
      lastCheckedAt.value = Date.now()
      lastError.value = ''
    } catch (e) {
      // ⚠ 拉不到**不等于**「没有重启过」⇒ 保留上一次的值，别把徽标清成"一切正常"。
      lastError.value = e instanceof Error ? e.message : String(e)
    }
  }

  function scheduleNext() {
    if (stopped || consumerCount <= 0) {
      stop()
      return
    }
    timer = setTimeout(async () => {
      const hidden = typeof document !== 'undefined' && document.hidden
      if (!hidden) await pollOnce()
      timer = setTimeout(async () => {
        await pollOnce()
        scheduleNext()
      }, hidden ? HIDDEN_POLL_INTERVAL_MS : POLL_INTERVAL_MS)
    }, POLL_INTERVAL_MS)
  }

  function start() {
    consumerCount += 1
    if (consumerCount === 1) {
      stopped = false
      void pollOnce()
      scheduleNext()
    }
  }

  function stop() {
    consumerCount = Math.max(0, consumerCount - 1)
    if (consumerCount === 0) {
      stopped = true
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
    }
  }

  return { supervisor, lastCheckedAt, lastError, start, stop, pollOnce }
})
