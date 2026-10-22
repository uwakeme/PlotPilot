import type { PlotOutlineDTO } from '@/api/workflow'
import type { InvocationVariableBinding } from '@/api/aiInvocation'
import { extractBoundOutputMaps, parseJsonLikeRecord } from '@/utils/invocationOutput'

export type PlotOutlineStatus = 'idle' | 'creating' | 'reviewing' | 'generating' | 'committing' | 'done' | 'error'
export type PlotOutlineProgressState = 'pending' | 'active' | 'done'

export interface PlotOutlineProgressItem {
  key: string
  label: string
  desc: string
  state: PlotOutlineProgressState
}

export const PLOT_OUTLINE_META_KEYS = new Set(['stage_plan'])
export const PLOT_STAGE_META_KEYS = new Set(['phase', 'label', 'range_percent', 'chapter_start', 'chapter_end', 'key_goals'])

export const PLOT_FIELD_LABELS: Record<string, string> = {
  main_story_overview: '故事主线概述',
  core_conflict: '核心冲突',
  expected_ending: '预期结局',
  summary: '阶段任务',
}

const PLOT_OVERVIEW_KEYS = ['main_story_overview', 'outline_main', 'main_axis', 'overview', 'story_overview', '故事主线概述', '主线概述', '故事概述']
const PLOT_ENDING_KEYS = ['expected_ending', 'ending_expect', 'ending_expectation', 'expectedEnding', 'ending', 'finale', '预期结局', '预期结尾', '结局预期', '故事最终走向']
const PLOT_CONFLICT_KEYS = ['core_conflict', 'coreConflict', 'conflict', 'main_conflict', '核心冲突', '核心矛盾', '核心对抗']
const PLOT_STAGE_KEYS = ['stage_plan', 'stages', '阶段规划']

const LEGACY_STAGE_KEY_ALIASES = [
  ['stage_opening_1_15', 'stage_opening', 'opening'],
  ['stage_develop_15_40', 'stage_develop', 'development'],
  ['stage_deepen_40_70', 'stage_deepen', 'deepening'],
  ['stage_climax_70_90', 'stage_climax', 'climax'],
  ['stage_end_90_100', 'stage_end', 'stage_ending', 'ending'],
] as const

/**
 * 阶段兜底标签（**按位置取，不是固定 5 段表**）。
 *
 * J-03：本文件原先有一份自己的 5 段表（`STAGE_PHASE_META`）并按下标
 * `STAGE_PHASE_META[index]` 强制回贴 `phase` / `label`。那意味着后端刚放开的
 * 阶段数与阶段名，一到前端又被改回固定 5 个名字 —— 两处真相源必然漂移。
 *
 * 现在阶段名与阶段数都由后端决定（见
 * `application/blueprint/services/stage_plan_invariants.py`）：
 * 阶段数 ∈ [3, 8]，断点首尾相接，phase / label 来自 AI。
 * 前端只做**渲染**：自己的值优先，缺失时才按位置兜底。
 */
const STAGE_FALLBACK_LABELS = [
  '起始', '展开', '推进', '深化', '高潮', '收束', '过渡', '尾声',
] as const

function stageFallback(index: number): { phase: string; label: string } {
  const label = STAGE_FALLBACK_LABELS[index] ?? `阶段${index + 1}`
  return { phase: `stage_${index + 1}`, label: `${label}阶段` }
}

const PHASE_ALIASES: Record<string, string> = {
  opening: 'opening',
  open: 'opening',
  start: 'opening',
  beginning: 'opening',
  setup: 'opening',
  开篇: 'opening',
  开篇阶段: 'opening',
  开局: 'opening',
  起始: 'opening',
  development: 'development',
  develop: 'development',
  rising: 'development',
  rising_action: 'development',
  发展: 'development',
  发展阶段: 'development',
  展开: 'development',
  deepening: 'deepening',
  deepen: 'deepening',
  middle: 'deepening',
  mid: 'deepening',
  深化: 'deepening',
  深化阶段: 'deepening',
  深入: 'deepening',
  climax: 'climax',
  peak: 'climax',
  high: 'climax',
  高潮: 'climax',
  高潮阶段: 'climax',
  爆发: 'climax',
  ending: 'ending',
  end: 'ending',
  finale: 'ending',
  resolution: 'ending',
  收尾: 'ending',
  收尾阶段: 'ending',
  结尾: 'ending',
  结局: 'ending',
}

export function createEmptyPlotOutline(): PlotOutlineDTO {
  return {
    main_story_overview: '',
    core_conflict: '',
    expected_ending: '',
    stage_plan: [],
  }
}

function coerceChapterNumber(value: unknown): number | undefined {
  if (value === undefined || value === null || value === '') return undefined
  const number = Number(value)
  if (!Number.isFinite(number) || number <= 0) return undefined
  return Math.floor(number)
}

function canonicalPhase(value: unknown): string {
  const text = String(value || '').trim().toLowerCase().replace(/[-\s]+/g, '_')
  return PHASE_ALIASES[text] || ''
}

/**
 * 规范化阶段列表 —— J-03 后，前端只渲染，不改写。
 *
 * 后端已保证：阶段数 ∈ [3, 8]、断点首尾相接、phase / label 来自 AI。
 * 因此这里**不再按下标回贴**任何东西，只在字段缺失时按位置兜底。
 *
 * ⚠ **前端不再自己算章节区间**（第 3 批复审 B2 修）：
 *
 * 此前这里有一份自己的均分公式，而它把「当前段之后还剩几段」
 * 误当成「当前段是不是最后一段」（`index === remaining`）——
 * 奇数阶段数时**中间那段**被误判成最后一段、一口吞掉尾部，
 * 后面几段落到书的外面：
 *
 *     total=100 stages=5 ⇒ (1,20) (21,40) (41,100) (101,101) (102,102)
 *
 * 而后端的三条不变量（阶段数 ∈[3,8] / 首尾相接 / 末段终点 ≥ 目标）
 * **全过** ⇒ 一份「有一段在书外」的阶段计划被静默写进库。
 *
 * 现在：AI / 后端给了区间就用，没给就**留空**（不编造）——
 * 后端在保存时用 `stage_plan_invariants.chapter_ranges` 补齐，
 * 断点口径只有一处，前端不再有第二份。
 */
function normalizeStagePlanRanges(
  stagePlan: PlotOutlineDTO['stage_plan'],
  totalChapters: number,
): PlotOutlineDTO['stage_plan'] {
  return stagePlan.map((stage, index) => {
    const fallbackStage = stageFallback(index)
    const rawStart = coerceChapterNumber(stage.chapter_start)
    const rawEnd = coerceChapterNumber(stage.chapter_end)
    const next = {
      ...stage,
      // **不改写** —— 阶段名与阶段数由后端 / AI 决定
      phase: stage.phase || fallbackStage.phase,
      label: stage.label || fallbackStage.label,
      // **不编造区间** —— 断点由 AI / 后端决定；缺失就留空，
      // 由后端在保存时补齐（前端不持有第二份区间算法）。
      //
      // ⚠ **R2**（第 3 批三次复审）：此前用 `keepManualRange`（要求**两端都有**
      //   且 start ≤ end）⇒ 作者只填了「起始章」时**两端一起被丢掉**
      //   ⇒ 刚敲的数字**静默消失**。现在**两端各自保留**
      //   （非法组合交给保存门禁报错，不在这里悄悄丢弃）。
      chapter_start: rawStart,
      chapter_end: rawEnd,
    }
    return {
      ...next,
      range_percent: buildStageRangePercentLabel(next, Math.max(totalChapters, next.chapter_end || 0)) || stage.range_percent,
    }
  })
}


export function parsePlotLabeledSections(text: string): Record<string, string> {
  const source = String(text || '').trim()
  if (!source) return {}
  const labels = ['阶段任务', '冲突变化', '角色成长', '关键剧情节点', '关键剧情', '核心冲突', '预期结局']
  const pattern = new RegExp(`(${labels.join('|')})\\s*[：:]`, 'g')
  const matches = [...source.matchAll(pattern)]
  if (matches.length < 2) return {}
  const fields: Record<string, string> = {}
  for (let i = 0; i < matches.length; i++) {
    const match = matches[i]
    const key = match[1]
    const start = (match.index || 0) + match[0].length
    const end = i + 1 < matches.length ? matches[i + 1].index || source.length : source.length
    const value = source.slice(start, end).trim()
    if (!value) continue
    fields[key === '阶段任务' ? 'summary' : key] = value
  }
  return fields
}

export function clonePlotOutline(outline: PlotOutlineDTO | null | undefined, totalChapters = 100): PlotOutlineDTO {
  if (!outline) return createEmptyPlotOutline()
  return {
    ...outline,
    main_story_overview: outline.main_story_overview || '',
    core_conflict: outline.core_conflict || '',
    expected_ending: outline.expected_ending || '',
    stage_plan: normalizeStagePlanRanges((outline.stage_plan || []).map(stage => ({
      ...stage,
      ...parsePlotLabeledSections(stage.summary || ''),
      label: stage.label || '',
      range_percent: stage.range_percent || '',
      summary: parsePlotLabeledSections(stage.summary || '').summary || stage.summary || '',
      key_goals: Array.isArray(stage.key_goals) ? [...stage.key_goals] : [],
    })), totalChapters),
  }
}

export function getPlotOutlineTopFieldKeys(outline: PlotOutlineDTO): string[] {
  const record = outline as unknown as Record<string, unknown>
  const keys = Object.keys(record).filter(key => !PLOT_OUTLINE_META_KEYS.has(key))
  const preferred = ['main_story_overview', 'core_conflict', 'expected_ending']
  return [
    ...preferred.filter(key => keys.includes(key)),
    ...keys.filter(key => !preferred.includes(key)),
  ]
}

export function plotFieldLabel(key: string): string {
  return PLOT_FIELD_LABELS[key] || key
}

export function plotFieldText(
  target: Record<string, unknown> | PlotOutlineDTO | PlotOutlineDTO['stage_plan'][number],
  key: string,
): string {
  const value = (target as Record<string, unknown>)[key]
  if (value === undefined || value === null) return ''
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

export function updatePlotField(
  target: Record<string, unknown> | PlotOutlineDTO | PlotOutlineDTO['stage_plan'][number],
  key: string,
  value: string,
) {
  ;(target as Record<string, unknown>)[key] = value
}

export function stageContentFieldKeys(stage: PlotOutlineDTO['stage_plan'][number]): string[] {
  const record = stage as unknown as Record<string, unknown>
  const keys = Object.keys(record).filter(key => !PLOT_STAGE_META_KEYS.has(key))
  return [
    ...(['summary', '冲突变化', '角色成长', '关键剧情节点'] as string[]).filter(key => keys.includes(key)),
    ...keys.filter(key => !['summary', '冲突变化', '角色成长', '关键剧情节点'].includes(key)),
  ]
}

export function buildStageRangePercentLabel(
  stage: { chapter_start?: number; chapter_end?: number; range_percent?: string },
  totalChapters: number,
): string {
  const total = Math.max(1, totalChapters)
  const start = typeof stage.chapter_start === 'number' ? stage.chapter_start : 0
  const end = typeof stage.chapter_end === 'number' ? stage.chapter_end : 0
  if (start <= 0 || end <= 0) return stage.range_percent || ''
  const startPercent = Math.max(1, Math.min(100, Math.floor(((start - 1) / total) * 100)))
  const endPercent = Math.max(startPercent, Math.min(100, Math.floor((end / total) * 100)))
  return `${startPercent}-${endPercent}%`
}

export function buildEditablePlotOutlinePayload(
  editableOutline: PlotOutlineDTO,
  totalChapters: number,
): PlotOutlineDTO {
  return {
    ...editableOutline,
    main_story_overview: editableOutline.main_story_overview.trim(),
    core_conflict: editableOutline.core_conflict.trim(),
    expected_ending: editableOutline.expected_ending.trim(),
    stage_plan: editableOutline.stage_plan.map(stage => ({
      ...stage,
      chapter_start: typeof stage.chapter_start === 'number' ? stage.chapter_start : undefined,
      chapter_end: typeof stage.chapter_end === 'number' ? stage.chapter_end : undefined,
      range_percent: buildStageRangePercentLabel(stage, totalChapters) || stage.range_percent,
      summary: String(stage.summary || '').trim(),
      key_goals: (stage.key_goals || []).map(item => String(item || '').trim()).filter(Boolean),
    })),
  }
}

export function validateEditablePlotOutline(outline: PlotOutlineDTO, totalChapters = 0): string {
  const topRecord = outline as unknown as Record<string, unknown>
  const hasTopContent = Object.entries(topRecord).some(([key, value]) =>
    !PLOT_OUTLINE_META_KEYS.has(key) && String(value ?? '').trim().length > 0,
  )
  if (!hasTopContent) return '请至少保留一项总纲内容'
  if (!outline.stage_plan.length) return '请保留并填写阶段规划'
  // ⚠ **区分「区间为空」与「区间非法」**（第 3 批二次复审 N4）：
  //   B2 之后前端**不再编造**区间（AI 不给区间、前端不自算）⇒ 未保存前
  //   区间本来就是空的；而后端在保存时用 `chapter_ranges` 补齐。
  //   ⇒ 若这里继续要求「每段都得有数字区间」，请求**根本发不出去**
  //   ⇒ **向导第 4 步过不去**，后端那份补齐逻辑永远用不上。
  //   ⇒ 现在：**为空 = 交给后端补**（不拦）；**有值但非法**（<1 或倒挂）才拦。
  //
  // ⚠ **R0**（第 3 批三次复审）：「两端都空」与「两端都填」是两种合法状态，
  //   但**混用**（只填了第一段、其余留空）会让后端把「作者写的」与
  //   「系统算的」**混着用** ⇒ 首尾不相接 ⇒ 报错信息不透明。
  //   ⇒ 现在：**要么全部不填**（系统按目标章数分配），
  //   **要么全部填**（自己掌控断点）⇒ 不许混用。
  const stagesWithRange = outline.stage_plan.filter(
    stage => stage.chapter_start !== undefined || stage.chapter_end !== undefined,
  )
  if (stagesWithRange.length > 0 && stagesWithRange.length !== outline.stage_plan.length) {
    const missing = outline.stage_plan.find(
      stage => stage.chapter_start === undefined && stage.chapter_end === undefined,
    )
    return `请为所有阶段填写起止章节，或全部留空由系统按章数分配（缺「${missing?.label || '阶段'}」）`
  }
  const invalidStageRange = outline.stage_plan.find((stage) => {
    const start = stage.chapter_start
    const end = stage.chapter_end
    if (start === undefined && end === undefined) return false
    if (start === undefined || end === undefined) return true
    return start < 1 || end < 1 || start > end
  })
  if (invalidStageRange) return `请检查${invalidStageRange.label || '阶段'}的起止章节`
  // ⚠ **R3**（第 3 批三次复审）：手填路径要求**最后一段正好落在目标章数上**
  //   （后端 I3 已收紧为「末段终点 == 目标章数」）⇒ 提前说清，别让作者
  //   填完才看到后端的报错。
  const lastStage = outline.stage_plan[outline.stage_plan.length - 1]
  const lastEnd = lastStage?.chapter_end
  if (lastEnd !== undefined && totalChapters > 0 && lastEnd !== totalChapters) {
    return `最后一段的结束章应为 ${totalChapters}（当前 ${lastEnd}）`
  }
  const emptyStage = outline.stage_plan.find(stage => stageContentFieldKeys(stage).every(key => !plotFieldText(stage, key).trim()))
  if (emptyStage) return `请填写${emptyStage.label || '阶段'}的规划内容`
  return ''
}

function pickPlotString(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = record[key]
    if (value !== undefined && value !== null && String(value).trim()) {
      return String(value).trim()
    }
  }
  return ''
}

function pickPlotValue(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    const value = record[key]
    if (value !== undefined && value !== null && value !== '') return value
  }
  return undefined
}

/**
 * 旧格式 `{opening:..., development:...}` → 阶段数组。
 *
 * J-03：原先这里也用 `STAGE_PHASE_META[index]` 按下标取名。旧格式的**键本身
 * 就是阶段名**，所以正确的做法是从**匹配到的那个键**推导，而不是查一张位置表 ——
 * 这样既去掉了镜像，语义也更准（原先第 3 个键无论叫什么都会被叫成「深化阶段」）。
 */
function normalizeLegacyStagePlan(stagePlan: unknown): PlotOutlineDTO['stage_plan'] {
  if (!stagePlan || typeof stagePlan !== 'object' || Array.isArray(stagePlan)) return []
  const record = stagePlan as Record<string, unknown>
  const stages: PlotOutlineDTO['stage_plan'] = []
  LEGACY_STAGE_KEY_ALIASES.forEach((aliases) => {
    let matchedKey = ''
    let value: unknown
    for (const key of aliases) {
      const candidate = record[key]
      if (candidate !== undefined && candidate !== null && candidate !== '') {
        matchedKey = key
        value = candidate
        break
      }
    }
    if (!matchedKey) return

    // 阶段名来自**匹配到的旧键**，别名表只做归一，不做「按下标套名」
    const phase = canonicalPhase(matchedKey) || matchedKey
    const fallback = stageFallback(stages.length)
    const source = (value && typeof value === 'object' && !Array.isArray(value))
      ? (value as Record<string, unknown>)
      : null

    if (source) {
      stages.push({
        ...(source as unknown as PlotOutlineDTO['stage_plan'][number]),
        phase: String(source.phase || phase),
        label: String(source.label || fallback.label),
        range_percent: String(source.range_percent || ''),
      })
    } else {
      stages.push({
        phase,
        label: fallback.label,
        range_percent: '',
        summary: String(value).trim(),
        key_goals: [],
      })
    }
  })
  return stages.filter(stage => String(stage.summary || '').trim())
}

export function normalizePlotOutlineShape(value: unknown, totalChapters = 100): PlotOutlineDTO | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const stagePlan = pickPlotValue(record, PLOT_STAGE_KEYS)
  const normalizedStagePlan = Array.isArray(stagePlan)
    ? stagePlan as PlotOutlineDTO['stage_plan']
    : normalizeLegacyStagePlan(stagePlan)
  return {
    ...(record as Partial<PlotOutlineDTO>),
    main_story_overview: pickPlotString(record, PLOT_OVERVIEW_KEYS),
    expected_ending: pickPlotString(record, PLOT_ENDING_KEYS),
    core_conflict: pickPlotString(record, PLOT_CONFLICT_KEYS),
    stage_plan: normalizeStagePlanRanges(normalizedStagePlan, totalChapters),
  }
}

export function normalizePlotOutlineFromBindings(
  source: Record<string, unknown>,
  bindings: InvocationVariableBinding[],
  totalChapters = 100,
): PlotOutlineDTO | null {
  const { byVariableKey } = extractBoundOutputMaps(source, bindings)
  const direct = byVariableKey['plot.outline']
  if (direct && typeof direct === 'object') return normalizePlotOutlineShape(direct, totalChapters)
  const stagePlan = byVariableKey['plot.stage_plan']
  const overview = byVariableKey['plot.main_story_overview']
  const ending = byVariableKey['plot.expected_ending']
  const conflict = byVariableKey['plot.core_conflict']
  if (!stagePlan && !overview && !ending && !conflict) return null
  return normalizePlotOutlineShape({
    main_story_overview: overview,
    expected_ending: ending,
    core_conflict: conflict,
    stage_plan: stagePlan,
  }, totalChapters)
}

export function extractPlotOutlineFromResult(
  result: Record<string, unknown>,
  outputBindings: InvocationVariableBinding[] = [],
  totalChapters = 100,
): PlotOutlineDTO | null {
  const direct = result.plot_outline
  if (direct && typeof direct === 'object') return normalizePlotOutlineShape(direct, totalChapters)
  if (outputBindings.length) {
    const boundDirect = normalizePlotOutlineFromBindings(result, outputBindings, totalChapters)
    if (boundDirect?.stage_plan?.length) return boundDirect
  }
  const continuation = result.continuation
  if (continuation && typeof continuation === 'object') {
    const continuationRecord = continuation as Record<string, unknown>
    const fromContinuation = continuationRecord.plot_outline
    if (fromContinuation && typeof fromContinuation === 'object') return normalizePlotOutlineShape(fromContinuation, totalChapters)
    if (outputBindings.length) {
      const boundContinuation = normalizePlotOutlineFromBindings(continuationRecord, outputBindings, totalChapters)
      if (boundContinuation?.stage_plan?.length) return boundContinuation
    }
    const normalizedContinuation = normalizePlotOutlineShape(continuationRecord, totalChapters)
    if (normalizedContinuation?.main_story_overview && normalizedContinuation.stage_plan?.length) return normalizedContinuation
  }
  const acceptedContent = result.accepted_content
  if (typeof acceptedContent === 'string' && acceptedContent.trim()) {
    const parsedRecord = parseJsonLikeRecord(acceptedContent)
    if (parsedRecord) {
      if (outputBindings.length) {
        const boundAccepted = normalizePlotOutlineFromBindings(parsedRecord, outputBindings, totalChapters)
        if (boundAccepted?.stage_plan?.length) return boundAccepted
      }
      if (parsedRecord.plot_outline) {
        return normalizePlotOutlineShape(parsedRecord.plot_outline, totalChapters)
      }
      const normalizedAccepted = normalizePlotOutlineShape(parsedRecord, totalChapters)
      if (normalizedAccepted?.main_story_overview && normalizedAccepted.stage_plan?.length) return normalizedAccepted
    }
  }
  return null
}
