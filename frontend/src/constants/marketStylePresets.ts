/**
 * 市场向「文风公约」预设（用户不可自填底层 Prompt，仅选模板）。
 * 与后端生成链路配合时，梗概 + 赛道/世界观已在建档时写入 novel.premise。
 *
 * ⚠ J-02：清单本身**不在这个文件里** —— 它住在
 * `shared/taxonomy/style_presets_cn_v1.yaml`（SSOT），前端读的是由那份 YAML
 * 生成的 bundle（`@/domain/style/stylePresets`）。
 * 此前它手写在这里（数组字面量）⇒ 后端做「AI 语义匹配」时拿不到它，
 * 只能再抄一份（两份真相）。
 */
import { STYLE_PRESETS } from '@/domain/style/stylePresets'

export interface MarketStylePreset {
  label: string
  value: string
  body: string
  icon: string
  aliases: string[]
  keywords: string[]
}

function normalizeStyleText(text: string): string {
  return (text || '').replace(/\s+/g, '').trim()
}

function styleHeading(text: string): string {
  const t = (text || '').trim()
  const bracket = t.match(/^【文风公约[·:：-]?([^】]+)】/)
  if (bracket?.[1]) return bracket[1].trim()
  const plain = t.match(/^文风(?:承袭|采用|偏向|定位)?([^：:；;，,。]{2,12})/)
  return plain?.[1]?.trim() || ''
}

function scorePreset(styleNotes: string, preset: MarketStylePreset): number {
  const normalized = normalizeStyleText(styleNotes)
  const heading = styleHeading(styleNotes)
  const body = normalizeStyleText(preset.body)
  if (!normalized) return 0
  if (normalized === body) return 1000
  if (normalized.startsWith(body.slice(0, Math.min(body.length, 30)))) return 900

  let score = 0
  for (const alias of preset.aliases) {
    const a = normalizeStyleText(alias)
    if (!a) continue
    if (normalizeStyleText(heading).includes(a)) score += 120
    if (normalized.includes(a)) score += 45
  }
  for (const keyword of preset.keywords) {
    const k = normalizeStyleText(keyword)
    if (k && normalized.includes(k)) score += 18
  }
  return score
}

/** 全部内置文风预设（来自 shared/taxonomy 的 SSOT）。 */
export const MARKET_STYLE_PRESETS: MarketStylePreset[] = STYLE_PRESETS

/**
 * 按关键词给内置文风预设打分，返回命中的预设 value。
 *
 * ⚠ J-02：这条**只管显示**（面板上那个「内置模板 / 自定义公约」的标签），
 * **不决定文风归属** —— 登记表把它写成「前端替 AI 判定文风归属」是**因果讲反了**：
 * 真正替作者做决定的是「默认注入 presets[0]」那条（已删）。
 * ⇒ 这里的分数只影响标签怎么显示，永远不会被写进 Bible。
 *
 * ⚠ 「决定归属」的那一步现在由**后端**的 AI 语义匹配做
 * （`POST /api/v1/bible/novels/{id}/bible/style-preset-suggestion`），
 * 且它**只推荐、不写库** ⇒ 作者点「应用」才生效。
 */
export function matchPresetValue(styleNotes: string): string | null {
  const scored = MARKET_STYLE_PRESETS
    .map(preset => ({ preset, score: scorePreset(styleNotes, preset) }))
    .sort((a, b) => b.score - a.score)
  const best = scored[0]
  return best && best.score >= 45 ? best.preset.value : null
}

export function getMarketStylePresetIcon(value?: string | null): string {
  return MARKET_STYLE_PRESETS.find(preset => preset.value === value)?.icon || '文'
}
