/**
 * AI 调用实时流的连接管理 + 状态。
 *
 * 归约逻辑全在 `@/stores/aiStreamReduce`（纯函数，可脱离框架实跑验证）；
 * 这里只负责 EventSource 生命周期、重连退避和把结果塞进 Pinia。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { aiStreamApi } from '@/api/aiStream'
import { applyEvent, sortCalls, type AiCall, type AiStreamEvent } from './aiStreamReduce'

const RECONNECT_BASE_MS = 1_000
const RECONNECT_MAX_MS = 30_000

export const useAiStreamStore = defineStore('aiStream', () => {
  const callsBySpan = ref(new Map<string, AiCall>())
  const connection = ref<'idle' | 'connecting' | 'connected' | 'reconnecting'>('idle')
  const degraded = ref(false)

  let _source: EventSource | null = null
  let _retryTimer: ReturnType<typeof setTimeout> | null = null
  let _retries = 0
  let _activeNovelId: string | null = null
  let _consumers = 0

  const calls = computed(() => sortCalls(callsBySpan.value))
  const runningCount = computed(() => calls.value.filter((c) => c.status === 'running').length)

  function ingest(event: AiStreamEvent): void {
    if (event.type === 'connected') {
      degraded.value = event.queue !== 'mp_queue'
      return
    }
    callsBySpan.value = applyEvent(callsBySpan.value, event)
  }

  function scheduleReconnect(): void {
    if (_activeNovelId === null) return
    connection.value = 'reconnecting'
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** _retries, RECONNECT_MAX_MS)
    _retries += 1
    _retryTimer = setTimeout(() => connect(_activeNovelId!), delay)
  }

  function connect(novelId: string): void {
    if (_source) _source.close()
    connection.value = _retries === 0 ? 'connecting' : 'reconnecting'

    const source = new EventSource(aiStreamApi.url(novelId))
    _source = source

    // 陈旧源守卫：被 close() 掉的旧连接仍可能迟到地触发 handler。
    // 没有这三行，迟到的 onerror 会把刚建好的 source 关掉。
    source.onopen = () => {
      if (_source !== source) return
      connection.value = 'connected'
      _retries = 0
    }
    source.onmessage = (raw) => {
      if (_source !== source) return
      try {
        ingest(JSON.parse(raw.data) as AiStreamEvent)
      } catch {
        // 单条事件解析失败不影响后续
      }
    }
    source.onerror = () => {
      if (_source !== source) return
      source.close()
      _source = null
      scheduleReconnect()
    }
  }

  function subscribe(novelId: string): void {
    // 幂等：同一本书重复订阅不重复计数，否则消费者计数漂移、最后一个退订后连接关不掉
    if (_activeNovelId === novelId && _source) return
    _consumers += 1
    _activeNovelId = novelId
    _retries = 0
    callsBySpan.value = new Map()
    connect(novelId)
  }

  function unsubscribe(): void {
    _consumers = Math.max(0, _consumers - 1)
    if (_consumers > 0) return
    if (_retryTimer) {
      clearTimeout(_retryTimer)
      _retryTimer = null
    }
    _source?.close()
    _source = null
    _activeNovelId = null
    connection.value = 'idle'
  }

  function clear(): void {
    callsBySpan.value = new Map()
  }

  return { calls, runningCount, connection, degraded, subscribe, unsubscribe, clear }
})