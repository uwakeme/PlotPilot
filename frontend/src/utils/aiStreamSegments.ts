/**
 * 把一段原始文本切成「普通文本 / 围栏代码」的分段序列。
 *
 * **本文件零 import，这是刻意的**——和 `stores/aiStreamReduce.ts` 同一个理由：
 * 前端没有测试框架，但 `typescript` 已是 devDependency，零依赖模块可以单独
 * `tsc` 编译后用 node 断言实跑，在不引入测试框架的前提下拿到可执行证据。
 *
 * 存在的理由：LLM 的结构化输出（JSON 契约、剧本、审查报告）几乎总是被模型
 * 包在 ```json 围栏里。之前弹窗把整段当纯文本渲染，围栏本身也照原样显示，
 * 读者要在脑子里把 ```json 三个字符剥掉才能看到结构。
 *
 * 刻意**不**接 marked：markdown 渲染器会把散文里的 `#`、`**`、`_` 一并重新
 * 解释，而弹窗是「原始输出」观察窗，读者要看到的应当是模型字面吐出的东西。
 * 这里只认围栏代码块，其余原样保留。
 */

/** 一段围栏代码。lang 是围栏紧跟的语言标记，已转小写，可能为空。 */
export interface CodeSegment {
  kind: 'code'
  text: string
  lang: string
}

/** 一段普通文本。 */
export interface TextSegment {
  kind: 'text'
  text: string
}

export type AiSegment = TextSegment | CodeSegment

/**
 * 行首围栏：允许最多 3 个前导空格（CommonMark 规则），3+ 个反引号或波浪号。
 * 围栏后只允许一个语言标记，且行尾不能再有别的东西——
 * 这样 ```json {"a":1} 这种「同行内容」不会被误当成围栏，保持原样。
 */
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`~]*)[ \t]*$/

/** 空字符串没有可显示的分段，避免调用方渲染出一个空的文本块。 */
export function splitCodeSegments(source: string): AiSegment[] {
  if (!source) return []

  const segments: AiSegment[] = []
  let textLines: string[] = []
  let codeLines: string[] | null = null
  let fenceChar = ''
  let fenceLen = 0
  let lang = ''

  const flushText = (): void => {
    if (!textLines.length) return
    const text = textLines.join('\n')
    // 纯空白段不渲染，否则围栏之间会多出空行块。
    if (text.trim()) segments.push({ kind: 'text', text })
    textLines = []
  }

  for (const line of source.split('\n')) {
    const matched = FENCE_RE.exec(line)
    const char = matched?.[1]?.[0] ?? ''
    const len = matched?.[1]?.length ?? 0

    if (!matched) {
      if (codeLines) codeLines.push(line)
      else textLines.push(line)
      continue
    }

    if (codeLines === null) {
      flushText()
      codeLines = []
      fenceChar = char
      fenceLen = len
      lang = (matched[2] || '').toLowerCase()
      continue
    }

    // 闭合：必须是同种围栏字符，且长度不短于开围栏（否则 ``` 无法关闭 ~~~~）
    if (char === fenceChar && len >= fenceLen && !matched[2]) {
      segments.push({ kind: 'code', text: codeLines.join('\n'), lang })
      codeLines = null
      fenceChar = ''
      fenceLen = 0
      lang = ''
      continue
    }

    // 形似围栏但不能闭合：字符不同、长度不足、后面还跟着语言标记。
    // 这些行属于代码内容本身，原样保留。
    codeLines.push(line)
  }

  if (codeLines !== null) {
    // 未闭合的围栏：流式输出中途很常见，剩余内容仍按代码块渲染。
    segments.push({ kind: 'code', text: codeLines.join('\n'), lang })
  } else {
    flushText()
  }

  return segments
}

/** 整段是否只有一个代码块——用于决定要不要省掉分段包装。 */
export function isSingleCodeBlock(segments: AiSegment[]): boolean {
  return segments.length === 1 && segments[0]?.kind === 'code'
}
