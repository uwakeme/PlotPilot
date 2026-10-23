import contractBundle from './contract.bundle.json'

type WorldbuildingDimensionConfig = {
  label?: string
  fields?: Record<string, string>
  scope_hints?: Record<string, string>
}

type WorldbuildingContractBundle = {
  dimensions?: Record<string, WorldbuildingDimensionConfig>
  json_key_labels?: Record<string, string>
  /** A-06：字段短标签（此前只手写在本文件 ⇒ 两份真相；现下沉到契约 YAML） */
  field_labels?: Record<string, string>
}

const contract = contractBundle as WorldbuildingContractBundle

export function getDimensionFieldOrder(dimKey: string): string[] {
  return Object.keys(contract.dimensions?.[dimKey]?.fields || {})
}

/** A-06：全部维度键（从 bundle 派生 ⇒ 不再手写第二份数组） */
export function getWorldbuildingDimensionKeys(): string[] {
  return Object.keys(contract.dimensions || {})
}

export function getWorldbuildingDimensionLabel(dimKey: string): string {
  return contract.dimensions?.[dimKey]?.label || getWorldbuildingLabel(dimKey)
}

export function getWorldbuildingFieldLabel(fieldKey: string): string {
  return contract.field_labels?.[fieldKey] || getWorldbuildingLabel(fieldKey)
}

export function getWorldbuildingLabel(key: string): string {
  const direct = contract.json_key_labels?.[key]
  if (direct) return direct

  for (const dimension of Object.values(contract.dimensions || {})) {
    const fieldLabel = dimension.fields?.[key]
    if (fieldLabel) return fieldLabel
  }

  return key
}
