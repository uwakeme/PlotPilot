/**
 * 托管运行状态单一数据源（C2 收敛）。
 *
 * 旧状态：/autopilot/status 在 AutopilotPanel / AutopilotDAGView /
 * useAssistedAutopilotStatus 各自轮询，间隔与退避策略各异、各持不同
 * 时刻快照，UI 状态可能互相矛盾。本 store 保证「同一本书同时只有一条
 * 轮询链」，所有消费方读同一份响应式快照。
 */
import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { autopilotApi, isAutopilotNotFoundError, type AutopilotStatus } from '@/api/autopilot'
import { assistedAutopilotPollDelayMs } from '@/workbench/autopilotStatus'

type Listener = (status: AutopilotStatus) => void

export const useAutopilotStatusStore = defineStore('autopilotStatus', () => {
  const statusByNovel = ref<Record<string, AutopilotStatus | null>>({})

  // ── 单一轮询实例（按 novelId 至多一条链）──
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let activeNovelId: string | null = null
  let consumerCount = 0
  let failureCount = 0
  let stoppedForNotFound = false
  const isPolling = ref(false)
  const listenersByNovel = new Map<string, Set<Listener>>()

  function notify(novelId: string, value: AutopilotStatus) {
    const set = listenersByNovel.get(novelId)
    if (!set) return
    for (const fn of set) {
      try {
        fn(value)
      } catch {
        // 单个订阅者异常不影响其他订阅者
      }
    }
  }

  async function pollOnce(novelId: string) {
    try {
      const value = await autopilotApi.getStatus(novelId)
      failureCount = 0
      statusByNovel.value[novelId] = value
      notify(novelId, value)
    } catch (error) {
      if (isAutopilotNotFoundError(error)) {
        stoppedForNotFound = true
        failureCount = 0
        return
      }
      failureCount += 1
    }
  }

  function scheduleNext(novelId: string) {
    if (activeNovelId !== novelId || consumerCount <= 0 || stoppedForNotFound) {
      isPolling.value = false
      return
    }
    pollTimer = setTimeout(async () => {
      if (document.hidden) {
        // 页面不可见：低频保活（30s），可见后立即恢复
        pollTimer = setTimeout(() => void tick(novelId), 30_000)
        return
      }
      await tick(novelId)
    }, assistedAutopilotPollDelayMs(failureCount))
  }

  async function tick(novelId: string) {
    await pollOnce(novelId)
    scheduleNext(novelId)
  }

  function startPolling(novelId: string) {
    if (activeNovelId === novelId && isPolling.value) return
    stopPolling()
    activeNovelId = novelId
    stoppedForNotFound = false
    failureCount = 0
    isPolling.value = true
    void tick(novelId)
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
    isPolling.value = false
    activeNovelId = null
  }

  /** 订阅某本书的状态流；返回退订函数（最后一个订阅者退出时停轮询）。 */
  function subscribe(novelId: string, listener: Listener) {
    let set = listenersByNovel.get(novelId)
    if (!set) {
      set = new Set()
      listenersByNovel.set(novelId, set)
    }
    set.add(listener)
    consumerCount += 1
    startPolling(novelId)
    return () => {
      set!.delete(listener)
      consumerCount -= 1
      if (consumerCount <= 0) {
        stopPolling()
      }
    }
  }

  /** 显式刷新（写操作后调用）；轮询链外的一次性拉取。 */
  async function refresh(novelId: string) {
    await pollOnce(novelId)
  }

  function statusOf(novelId: string) {
    return computed(() => statusByNovel.value[novelId] ?? null)
  }

  // 组件卸载兜底：store 自身不持有组件生命周期，退订由订阅方负责
  watch(
    () => consumerCount,
    (n) => {
      if (n <= 0) stopPolling()
    },
  )

  return {
    statusByNovel,
    isPolling,
    subscribe,
    refresh,
    statusOf,
    stopPolling,
  }
})
