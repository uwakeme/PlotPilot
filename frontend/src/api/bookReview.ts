/**
 * 全书终审 API —— 分幕精审(book-act-review)+ 汇总报告(book-review-synthesis)
 */
import { apiClient } from './config'

export type ReviewSeverity = 'critical' | 'major' | 'minor'
export type ReviewCategory = 'logic' | 'character' | 'foreshadow' | 'pacing' | 'style'

export interface ActFinding {
  severity: ReviewSeverity | string
  category: ReviewCategory | string
  chapter_number: number
  quote: string
  problem: string
  suggestion: string
  suggested_instruction: string
}

export interface ActReviewPayload {
  act_number: number
  act_title: string
  chapter_start: number
  chapter_end: number
  chapter_count: number
  summary: string
  quality_score: number
  findings: ActFinding[]
}

export interface TopIssue {
  priority: number
  severity: ReviewSeverity | string
  category: ReviewCategory | string
  chapter_numbers: number[]
  title: string
  detail: string
  quote: string
  suggestion: string
  suggested_instruction: string
}

export interface PromiseVerdict {
  planted_chapter: number
  description: string
  verdict: 'resolved_in_text' | 'open_ending' | 'dangling' | string
  reason: string
  evidence_chapter: number | null
}

export interface FinalReport {
  headline: string
  overall_score: number
  scores: Record<string, number>
  top_issues: TopIssue[]
  promise_verdicts: PromiseVerdict[]
  act_summaries: Array<{ act_number: number; summary: string }>
}

export interface ActProgress {
  act_number: number
  title: string
  chapter_start: number
  chapter_end: number
  reviewed: boolean
  review?: {
    act_number: number
    quality_score: number | null
    summary: string | null
    finding_count: number
    updated_at: string | null
  }
}

export interface BookReviewReport {
  novel_id: string
  acts: ActProgress[]
  reviewed_act_count: number
  total_act_count: number
  final_report: Partial<FinalReport>
  final_generated_at: string | null
}

export const bookReviewApi = {
  /** POST /api/v1/novels/{id}/book-review/acts/{act_number}/review */
  reviewAct: (novelId: string, actNumber: number) =>
    apiClient.post<{ ok: boolean; act: ActReviewPayload }>(
      `/novels/${novelId}/book-review/acts/${actNumber}/review`,
      {},
    ) as unknown as Promise<{ ok: boolean; act: ActReviewPayload }>,

  /** POST /api/v1/novels/{id}/book-review/synthesize */
  synthesize: (novelId: string) =>
    apiClient.post<{ ok: boolean; report: FinalReport }>(
      `/novels/${novelId}/book-review/synthesize`,
      {},
    ) as unknown as Promise<{ ok: boolean; report: FinalReport }>,

  /** GET /api/v1/novels/{id}/book-review/report */
  getReport: (novelId: string) =>
    apiClient.get<BookReviewReport>(
      `/novels/${novelId}/book-review/report`,
    ) as unknown as Promise<BookReviewReport>,
}
