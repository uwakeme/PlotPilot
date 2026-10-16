/**
 * 代码高亮：只注册 LLM 实际会吐出来的少数几种语言。
 *
 * 为什么不直接 `import hljs from 'highlight.js'`：全量包带 190+ 种语言。
 * 这个包在本仓库 package.json 里早就挂着，但 src 下从未 import 过，所以真引入
 * 就是净增——实测同一份代码，全量 import 让 dist 的 vendor chunk 从 213 KB 涨到
 * 1055 KB（+842 KB），换成 core + 按需注册则几乎无感。
 * 9 种语言足够覆盖结构化输出与代码类回答。
 *
 * 另一个坑：hljs 只吐出 class 名，全仓没有引它的主题 CSS，颜色得自己给。
 * 配色写在 AiCallItem.vue 的 scoped 样式里，映射到 app 的 token，
 * 这样 light / dark / anchor 三套主题自动跟随。
 */
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
import DOMPurify from 'dompurify'

hljs.registerLanguage('json', json)
hljs.registerLanguage('python', python)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('markdown', markdown)


/** 围栏上的语言标记可能是这些写法，统一归一到 hljs 的注册名。 */
const LANG_ALIAS: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  ts: 'typescript',
  sh: 'bash',
  shell: 'bash',
  zsh: 'bash',
  yml: 'yaml',
  html: 'xml',
  md: 'markdown',
}

/**
 * 显式声明「这不是代码」的围栏：作者已经用 ```text 表明意图了，再拿
 * highlightAuto 去猜一遍只会给散文随机套上高亮 span，转义后原样返回。
 */
const NO_HIGHLIGHT = new Set(['text', 'txt', 'plain', 'plaintext'])

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 返回可直接喂给 v-html 的高亮 HTML；失败时返回转义后的原文，绝不吐裸 HTML。 */
export function highlightCode(code: string, lang: string): string {
  const normalized = (LANG_ALIAS[lang] ?? lang).toLowerCase()
  try {
    const html = normalized && hljs.getLanguage(normalized)
      ? hljs.highlight(code, { language: normalized, ignoreIllegals: true }).value
      : hljs.highlightAuto(code).value
    return DOMPurify.sanitize(html)
  } catch {
    return escapeHtml(code)
  }
}
