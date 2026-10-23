import type { BibleDTO, StyleNoteDTO } from '@/api/bible'
import {
  getDimensionFieldOrder,
  getWorldbuildingDimensionKeys,
  getWorldbuildingFieldLabel,
} from '@/domain/worldbuilding/contract'

/** A-06：维度键的**字面量联合**（编译期用）。 */
export type WorldbuildingDimKey =
  | 'core_rules'
  | 'geography'
  | 'society'
  | 'culture'
  | 'daily_life'

/**
 * A-06：运行时值改为从 **bundle 派生**（此前这里是手写的第二份数组）。
 * ⚠ 类型联合是字面量（编译期需要），**不保证**与 bundle 一致
 * ⇒ 由守卫 `tests/unit/application/world/test_a06_worldbuilding_contract_single_source.py`
 *   断言两者相同。
 */
export const WB_DIMS = getWorldbuildingDimensionKeys() as readonly WorldbuildingDimKey[]

export type WorldbuildingDraftShape = Record<WorldbuildingDimKey, Record<string, string>>

export interface OrderedWorldbuildingField {
  key: string
  value: string
}

export function createEmptyBible(): BibleDTO {
  return {
    id: '',
    novel_id: '',
    characters: [],
    world_settings: [],
    locations: [],
    timeline_notes: [],
    style_notes: [],
  }
}

export function emptyWorldbuildingShape(): WorldbuildingDraftShape {
  return {
    core_rules: {},
    geography: {},
    society: {},
    culture: {},
    daily_life: {},
  }
}

export function canonicalWorldbuildingField(dim: WorldbuildingDimKey, field: string): string {
  const key = String(field || '').trim()
  return getDimensionFieldOrder(dim).includes(key) ? key : ''
}

export function worldbuildingFieldTitle(_dim: WorldbuildingDimKey, field: string): string {
  return getWorldbuildingFieldLabel(field)
}

export function orderedWorldbuildingFields(
  data: WorldbuildingDraftShape,
  dim: WorldbuildingDimKey,
  opts: { includeEmpty?: boolean } = {},
): OrderedWorldbuildingField[] {
  const block = data[dim] || {}
  const ordered = getDimensionFieldOrder(dim)
  const keys = [
    ...ordered,
    ...Object.keys(block).filter(key => canonicalWorldbuildingField(dim, key) && !ordered.includes(key)),
  ]
  const fields = keys.map(key => ({ key, value: String(block[key] ?? '') }))
  return opts.includeEmpty ? fields : fields.filter(field => field.value.trim().length > 0)
}

export function worldbuildingFromWorldSettings(
  settings: { name: string; description?: string }[] | undefined,
): WorldbuildingDraftShape {
  const out = emptyWorldbuildingShape()
  const dimSet = new Set<string>(WB_DIMS)
  for (const setting of settings || []) {
    const dot = setting.name.indexOf('.')
    if (dot < 0) continue
    const dim = setting.name.slice(0, dot)
    const key = setting.name.slice(dot + 1)
    if (!dimSet.has(dim) || !key) continue
    out[dim as WorldbuildingDimKey][key] = (setting.description || '').trim()
  }
  return out
}

export function mergeWorldbuildingRawBlocks(
  out: WorldbuildingDraftShape,
  raw: Record<string, unknown>,
) {
  for (const dim of WB_DIMS) {
    const block = raw[dim]
    if (typeof block === 'string') continue
    if (block && typeof block === 'object') {
      const normalized: Record<string, string> = {}
      for (const [key, value] of Object.entries(block as Record<string, unknown>)) {
        const text = String(value ?? '').trim()
        if (!text) continue
        const field = canonicalWorldbuildingField(dim, key)
        if (!field) continue
        normalized[field] = text
      }
      out[dim] = { ...out[dim], ...normalized }
    }
  }
}

export function normalizeWorldbuildingFromApi(
  raw: Record<string, unknown> | null | undefined,
): WorldbuildingDraftShape {
  const out = emptyWorldbuildingShape()
  if (!raw || typeof raw !== 'object') return out
  const dimensions = raw.dimensions
  if (dimensions && typeof dimensions === 'object') {
    mergeWorldbuildingRawBlocks(out, dimensions as Record<string, unknown>)
  }
  const content = raw.worldbuilding
  if (content && typeof content === 'object') {
    mergeWorldbuildingRawBlocks(out, content as Record<string, unknown>)
  }
  mergeWorldbuildingRawBlocks(out, raw)
  return out
}

export function hasWorldbuildingContent(slices: WorldbuildingDraftShape): boolean {
  return Object.values(slices).some(dim =>
    Object.values(dim).some(value => String(value ?? '').trim().length > 0),
  )
}

export function mergeWorldbuildingDisplay(
  fromApi: WorldbuildingDraftShape,
  fromBibleSettings: WorldbuildingDraftShape,
): WorldbuildingDraftShape {
  const out = emptyWorldbuildingShape()
  for (const dim of WB_DIMS) {
    out[dim] = { ...fromBibleSettings[dim], ...fromApi[dim] }
  }
  return out
}

export function styleConventionFromBible(bible: BibleDTO): string {
  const maybeWithStyle = bible as BibleDTO & { style?: string }
  if (maybeWithStyle.style && String(maybeWithStyle.style).trim()) {
    return String(maybeWithStyle.style).trim()
  }
  const notes: StyleNoteDTO[] = bible.style_notes || []
  const contentOnly = notes
    .map(note => (note.content || '').trim())
    .filter(Boolean)
  return contentOnly.length ? contentOnly.join('\n\n') : ''
}
