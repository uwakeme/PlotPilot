/**
 * AI 调用流的纯数据模型：事件类型 + 归约函数。
 *
 * **本文件零 import，这是刻意的。** 前端没有测试框架（package.json 里没有
 * vitest/jest/playwright），但 `typescript` 已经是 devDependency——零依赖的模块
 * 可以 `npx tsc` 单独编译后用 node 断言脚本实跑，在不引入任何测试框架的前提下
 * 拿到可执行证据。混进 `vue` / `pinia` / `EventSource` 就没这个待遇了。
 *
 * 三条去重/幂等规则，都来自「同一本书的一次 LLM 调用会在弹窗里被看到多次」：
 *
 * 1. **`start` 永不重置已有条目。** 连接建立时后端会先回填最近若干条历史
 *    （含 start 与 end），而 `ai_stream_bus` 队列里可能还压着同一次调用的
 *    实时消息。若 `start` 把 response 清空、把 status 打回 running，一次已经
 *    完成的调用会在列表里闪一下「思考中」。span_id 是 uuid4，不会复用，
 *    所以「已存在的 start」必然是重放。
 * 2. **`delta` 只在 status === 'running' 时追加。** 同上：如果历史回填已经把
 *    end 送达，队列里迟到的 delta 再拼上去，正文会变成两份。
 * 3. **`end` 是终态，幂等。** 重复送达同一条 end 不改变任何东西。
 *
 * 后端对一次调用只给一个 span_id：`provider_factory` 发布 start/end/error 都用
 * `request_span_id`；库里虽然是两行，`_db_event_span_id` 会把响应/错误行折回
 * 父 span。所以这里按 `event.span_id` 聚合即可，两条路径自然对齐。
 */

export type AiStreamEventType = 'connected' | 'ping' | 'start' | 'delta' | 'end' | 'error'

export interface AiStreamEvent {
  type: AiStreamEventType
  novel_id?: string
  trace_id?: string
  span_id?: string
  stage?: string
  stage_label?: string
  model?: string
  profile?: string
  ts?: number | string
  queue?: 'mp_queue' | 'db_poll'
  message?: string
  prompt?: { system?: string; user?: string }
  delta?: string
  response?: string
  token_in?: number | null
  token_out?: number | null
  latency_ms?: number | null
  error?: string
}

export type AiCallStatus = 'running' | 'done' | 'error'

export interface AiCall {
  spanId: string
  traceId: string
  ts: number
  stage: string
  stageLabel: string
  model: string
  profile: string
  status: AiCallStatus
  promptSystem: string
  promptUser: string
  response: string
  tokenIn: number | null
  tokenOut: number | null
  latencyMs: number | null
  error: string
}

export const MAX_CALLS = 100
export const MAX_RESPONSE_CHARS = 16_000

export function toSortKey(ts: number | string | undefined, fallback: number): number {
  if (typeof ts === 'number') return ts
  if (typeof ts === 'string') {
    const parsed = Date.parse(ts)
    return Number.isNaN(parsed) ? fallback : parsed / 1000
  }
  return fallback
}

function emptyCall(spanId: string, ts: number): AiCall {
  return {
    spanId,
    traceId: '',
    ts,
    stage: '',
    stageLabel: '',
    model: '',
    profile: '',
    status: 'running',
    promptSystem: '',
    promptUser: '',
    response: '',
    tokenIn: null,
    tokenOut: null,
    latencyMs: null,
    error: '',
  }
}

function clamp(text: string): string {
  return text.length > MAX_RESPONSE_CHARS ? text.slice(-MAX_RESPONSE_CHARS) : text
}

function trimmed(map: Map<string, AiCall>): Map<string, AiCall> {
  if (map.size <= MAX_CALLS) return map
  const ordered = sortCalls(map)
  const keep = new Set(ordered.slice(0, MAX_CALLS).map((c) => c.spanId))
  return new Map([...map.entries()].filter(([id]) => keep.has(id)))
}

/** 按 ts 倒序（新的在前）。ts 相同时用 spanId 兜底，保证顺序稳定。 */
export function sortCalls(calls: Map<string, AiCall>): AiCall[] {
  return [...calls.values()].sort((a, b) => b.ts - a.ts || a.spanId.localeCompare(b.spanId))
}

/**
 * 归约一条事件。返回**新**的 Map，不改传入的那个。
 *
 * `connected` / `ping` 不进 calls，返回原 Map 引用，让调用方可以跳过无谓的重渲染。
 */
export function applyEvent(calls: Map<string, AiCall>, event: AiStreamEvent): Map<string, AiCall> {
  if (event.type === 'ping' || event.type === 'connected') return calls

  const spanId = event.span_id
  if (!spanId) return calls

  const existing = calls.get(spanId)
  const ts = toSortKey(event.ts, existing?.ts ?? Date.now() / 1000)
  const base = existing ?? emptyCall(spanId, ts)
  let next: AiCall

  switch (event.type) {
    case 'start':
      // 规则 1：只补空字段，绝不动 status / response / token。
      next = {
        ...base,
        ts,
        traceId: base.traceId || event.trace_id || '',
        stage: base.stage || event.stage || '',
        stageLabel: base.stageLabel || event.stage_label || '',
        model: base.model || event.model || '',
        profile: base.profile || event.profile || '',
        promptSystem: base.promptSystem || event.prompt?.system || '',
        promptUser: base.promptUser || event.prompt?.user || '',
      }
      break

    case 'delta': {
      // 规则 2：已终结的条目不再追加，否则历史回填 + 队列迟到 delta 会写出两份正文。
      if (base.status !== 'running') return calls
      const merged = base.response + (event.delta ?? '')
      next = { ...base, ts, response: clamp(merged) }
      break
    }

    case 'end':
      // 规则 3：终态权威，重复送达无副作用。
      next = {
        ...base,
        ts,
        status: event.error ? 'error' : 'done',
        response: clamp(event.response ?? base.response),
        tokenIn: event.token_in ?? base.tokenIn,
        tokenOut: event.token_out ?? base.tokenOut,
        latencyMs: event.latency_ms ?? base.latencyMs,
        error: event.error ?? '',
      }
      break

    case 'error':
      next = { ...base, ts, status: 'error', error: event.error ?? base.error }
      break

    default:
      return calls
  }

  const result = new Map(calls)
  result.set(spanId, next)
  return trimmed(result)
}