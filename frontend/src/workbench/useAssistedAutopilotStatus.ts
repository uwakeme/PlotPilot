import { ref, watch, type Ref } from 'vue'
import { type AutopilotStatus } from '@/api/autopilot'
import { useAutopilotStatusStore } from '@/stores/autopilotStatusStore'

export interface UseAssistedAutopilotStatusOptions {
  slug: Ref<string>
  enabled: Ref<boolean>
  onStatus: (status: AutopilotStatus) => void
}

/**
 * 辅助模式托管状态：薄封装 autopilotStatusStore（单一轮询链）。
 *
 * 旧实现自带一条 useAdaptivePolling 轮询——与 AutopilotPanel/DAGView
 * 并存时同一本书 2-3 条并行轮询、各持不同时刻快照。现在轮询收敛到
 * store，本 composable 只做「订阅 + 退避状态透出」。
 */
export function useAssistedAutopilotStatus(options: UseAssistedAutopilotStatusOptions) {
  const store = useAutopilotStatusStore()
  const stoppedForNotFound = ref(false)
  const failureCount = ref(0)
  const isPolling = ref(false)

  let unsubscribe: (() => void) | null = null

  function teardown() {
    if (unsubscribe) {
      unsubscribe()
      unsubscribe = null
    }
    isPolling.value = false
  }

  function start() {
    teardown()
    if (!options.enabled.value || !options.slug.value) return
    const novelId = options.slug.value
    unsubscribe = store.subscribe(novelId, (value) => {
      options.onStatus(value)
      if (value.autopilot_status === undefined && value.novel_id === undefined) {
        // 空/异常载荷不计
      }
    })
    isPolling.value = store.isPolling
    stoppedForNotFound.value = false
  }

  function resetBackoff() {
    stoppedForNotFound.value = false
    failureCount.value = 0
  }

  function stop() {
    teardown()
  }

  watch(
    () => options.enabled.value,
    (enabled) => {
      if (!enabled) {
        stop()
        return
      }
      resetBackoff()
      start()
    },
    { immediate: true },
  )

  watch(
    () => options.slug.value,
    () => {
      resetBackoff()
      start()
    },
  )

  return {
    failureCount,
    isPolling,
    isExecuting: isPolling,
    stoppedForNotFound,
    resetBackoff,
    start,
    stop,
    refresh: (novelId?: string) => store.refresh(novelId ?? options.slug.value),
  }
}
