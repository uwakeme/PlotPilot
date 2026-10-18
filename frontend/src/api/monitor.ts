/**
 * 监控大盘 API
 */

import type { AxiosRequestConfig } from 'axios'

import { apiClient } from './config'
import { apiRoutes } from './endpoints'
import { fetchJson } from './http'

export interface TensionPoint {
  chapter: number
  tension: number
  title: string
  evaluated?: boolean
}

export interface TensionCurveStats {
  avg_tension: number
  max_tension: number
  min_tension: number
  variance: number
  is_flat: boolean
  evaluated_count: number
  unevaluated_count: number
  consecutive_low: number
}

export interface TensionCurveResponse {
  novel_id: string
  points: TensionPoint[]
  stats: TensionCurveStats | null
}

/**
 * 文风漂移。
 *
 * `drift_score` 是**漂移度**（0-1，越高越糟），与后端 similarity 相反。
 * `evaluated=false` 时 `drift_score` 为 `null` —— 这表示「没测过」，
 * 绝不能当成 0（0 会被读成「测过了，文风完美」）。
 */
export interface VoiceDriftApiResponse {
  novel_id: string
  drift_score: number | null
  status: 'unevaluated' | 'normal' | 'warning' | 'critical'
  evaluated: boolean
  sample_count: number
  last_scored_chapter: number | null
  /** 本次评分的真实计算时刻；`null` = 未评估。绝不能用「前端拉取时刻」顶替。 */
  computed_at?: string | null
  /** 告警阈值（0-1 刻度）。前端**不得**另定一套 —— 那样同一个数会得到两种结论。 */
  warning_threshold?: number
  critical_threshold?: number
  reason: string
}

export const monitorApi = {
  getTensionCurve(novelId: string, config?: AxiosRequestConfig): Promise<TensionCurveResponse> {
    return apiClient.get(
      apiRoutes.monitor.tensionCurve(novelId),
      config,
    ) as unknown as Promise<TensionCurveResponse>
  },

  getVoiceDrift(novelId: string): Promise<VoiceDriftApiResponse> {
    return fetchJson<VoiceDriftApiResponse>(apiRoutes.monitor.voiceDrift(novelId))
  },
}
