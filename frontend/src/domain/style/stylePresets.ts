import presetsBundle from './presets.bundle.json'

/** 一个市场向「文风公约」预设。 */
export type StylePreset = {
  /** 稳定标识符（AI 匹配返回的就是它） */
  value: string
  /** 面板显示名（如「修仙·升级打脸」） */
  label: string
  /** 面板显示的单字图标 */
  icon: string
  /** 会被写进 Bible 的文风公约正文 */
  body: string
  /** 题材别名 */
  aliases: string[]
  /** 题材关键词 */
  keywords: string[]
}

type StylePresetBundle = {
  presets?: StylePreset[]
}

const bundle = presetsBundle as StylePresetBundle

/**
 * 全部文风预设（来自 `shared/taxonomy/style_presets_cn_v1.yaml`）。
 *
 * ⚠ J-02：此前这份清单**手写在本仓库的前端**（`marketStylePresets.ts` 的数组字面量）
 * ⇒ 后端做「AI 语义匹配」时拿不到它，要么再抄一份（两份真相），
 * 要么让前端把清单传给后端（⇒ 模型的判读输入由客户端决定）。
 * ⇒ 现在下沉到 shared/taxonomy，后端直接读 YAML、前端读由同一份 YAML 生成的 bundle。
 */
export const STYLE_PRESETS: StylePreset[] = bundle.presets ?? []
