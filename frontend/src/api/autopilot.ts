import { apiRoutes } from './endpoints'
import { fetchJson, fetchOk, fetchUrl, HttpError, type FetchJsonOptions } from './http'

/** 章后审计快照（守护进程 audit 阶段写入，前端面板消费） */
export interface AutopilotChapterAudit {
  chapter_number: number
  /** 张力三态：null = 未评估（打分没跑/失败），0 = 评估过且张力就是 0 */
  tension: number | null
  drift_alert: boolean
  similarity_score: number | null
  /** 三态：true=已同步 / false=同步失败 / null=未评估（章后管线超时） */
  narrative_sync_ok: boolean | null
  vector_stored?: boolean
  foreshadow_stored?: boolean
  triples_extracted?: boolean
  causal_edges_stored?: boolean
  character_mutations_stored?: boolean
  debt_updated?: boolean
  evolution_snapshot_ok?: boolean
  character_reconcile_ok?: boolean
  quality_scores?: Record<string, number>
  issues?: Array<{ severity: string; message: string }>
  at: string | null
}

/**
 * 托管运行状态（GET /autopilot/status）。
 *
 * 字段来自守护进程 `_update_shared_state(novel_id, ...)` 的写入面
 *（阶段/节拍/字数/审阅/错误摘要等）。后端字段增删不影响编译期检查的
 * 部分（未列字段）走 `[key: string]: unknown` 兜底；消费方对兜底字段
 * 仍需 Number()/String() 防御式解析，已列字段可直接使用。
 */
export interface AutopilotStatus {
  novel_id?: string
  // 阶段与子步骤
  current_stage?: string
  writing_substep?: string
  writing_substep_label?: string
  last_stable_stage?: string
  active_pipeline_step?: string
  active_pipeline_run_id?: string
  autopilot_recovery_reason?: string
  autopilot_run_epoch?: number
  // 运行/状态
  autopilot_status?: string
  autopilot_last_error_summary?: string
  autopilot_llm_failure_count?: number
  requires_ai_review?: boolean
  has_active_invocation?: boolean
  active_invocation_session_id?: string
  active_invocation_operation?: string
  active_invocation_status?: string
  autopilot_pause_reason?: string
  // 幕/章节进度
  current_act?: number
  current_act_title?: string
  current_act_description?: string
  current_chapter_number?: number
  current_chapter_in_act?: number
  planned_micro_beats?: number | string
  current_beat_index?: number
  accumulated_words?: number
  chapter_target_words?: number
  target_words_per_chapter?: number
  target_chapters?: number
  completed_chapters?: number
  last_chapter_tension?: number | string
  // 章后审计/管线
  last_chapter_audit?: AutopilotChapterAudit | null
  aftermath_live_status?: string
  aftermath_live_chapter_number?: number
  story_pipeline_substep?: string
  // 兜底：后端新增字段
  [key: string]: unknown
}

export interface AutopilotStartRequest {
  max_auto_chapters: number
  target_chapters: number
  target_words_per_chapter: number
}

export interface AutopilotResumeResponse {
  current_stage?: string
  message?: string
  [key: string]: unknown
}

export interface AutopilotErrorRecord {
  message: string
  timestamp: string
  context?: string
}

export interface AutopilotCircuitBreakerData {
  status: 'closed' | 'open' | 'half_open'
  error_count: number
  max_errors: number
  last_error?: AutopilotErrorRecord
  error_history?: AutopilotErrorRecord[]
}

export const autopilotApi = {
  getStatus(novelId: string, options?: FetchJsonOptions): Promise<AutopilotStatus> {
    return fetchJson<AutopilotStatus>(apiRoutes.autopilot.status(novelId), options)
  },

  start(novelId: string, data: AutopilotStartRequest): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.start(novelId), {
      method: 'POST',
      body: data,
    })
  },

  stop(novelId: string, timeoutMs?: number): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.stop(novelId), {
      method: 'POST',
      timeoutMs,
    })
  },

  resume(novelId: string): Promise<AutopilotResumeResponse> {
    return fetchJson<AutopilotResumeResponse>(apiRoutes.autopilot.resume(novelId), {
      method: 'POST',
    })
  },

  getCircuitBreaker(novelId: string): Promise<AutopilotCircuitBreakerData> {
    return fetchJson<AutopilotCircuitBreakerData>(apiRoutes.autopilot.circuitBreaker(novelId))
  },

  resetCircuitBreaker(novelId: string): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.circuitBreakerReset(novelId), {
      method: 'POST',
    })
  },

  streamUrl(novelId: string, afterSeq?: number): string {
    const params = afterSeq && afterSeq > 0 ? { after_seq: afterSeq } : undefined
    return fetchUrl(apiRoutes.autopilot.stream(novelId, params))
  },

  logStreamUrl(novelId: string): string {
    return fetchUrl(apiRoutes.autopilot.logStream(novelId))
  },
}

export function isAutopilotNotFoundError(error: unknown): boolean {
  return error instanceof HttpError && error.status === 404
}

export function isAutopilotHttpError(error: unknown): boolean {
  return error instanceof HttpError
}

export function getAutopilotHttpStatus(error: unknown): number | null {
  return error instanceof HttpError ? error.status : null
}

export function getAutopilotErrorDetail(error: unknown): string {
  if (!(error instanceof HttpError)) return ''
  const body = error.body
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    return typeof detail === 'string' ? detail : ''
  }
  return ''
}
