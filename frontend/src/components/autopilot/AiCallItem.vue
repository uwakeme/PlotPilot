<script setup lang="ts">
import { computed, ref } from 'vue'
import { NTag } from 'naive-ui'
import { STAGE_BY_KEY } from '@/constants/aiCallStages'
import {
  splitCodeSegments,
  type CodeSegment,
  type TextSegment,
} from '@/utils/aiStreamSegments'
import { highlightCode } from '@/utils/codeHighlight'
import type { AiCall } from '@/stores/aiStreamReduce'

const props = defineProps<{ call: AiCall }>()

const open = ref(false)
const showInput = ref(true)
const showSystem = ref(false)
const showOutput = ref(true)

/** 阶段语义决定配色：写作 / 规划 / 校验 / 同步 各用一色，扫一眼就知道性质。 */
const stageSemantic = computed(() => STAGE_BY_KEY[props.call.stage]?.semantic ?? 'none')

const stageTitle = computed(() => {
  const label = props.call.stageLabel || STAGE_BY_KEY[props.call.stage]?.label
  return label || props.call.stage || 'AI 调用'
})

const statusType = computed(() => {
  if (props.call.status === 'error') return 'error'
  if (props.call.status === 'done') return 'success'
  return 'info'
})

const statusLabel = computed(() => {
  if (props.call.status === 'error') return '失败'
  if (props.call.status === 'done') return '完成'
  return '进行中'
})

const elapsedLabel = computed(() => {
  if (props.call.status === 'running') return '…'
  const ms = props.call.latencyMs
  return ms == null ? '—' : `${(ms / 1000).toFixed(1)}s`
})

const tokenLabel = computed(() => {
  const parts: string[] = []
  if (props.call.response) parts.push(`${props.call.response.length} 字`)
  if (props.call.tokenIn != null) parts.push(`入 ${props.call.tokenIn}`)
  if (props.call.tokenOut != null) parts.push(`出 ${props.call.tokenOut}`)
  return parts.join(' · ') || '—'
})

const inputChars = computed(() =>
  (props.call.promptSystem?.length ?? 0) + (props.call.promptUser?.length ?? 0),
)

const timeLabel = computed(() => {
  const d = new Date(props.call.ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
})

/**
 * 已渲染好的分段：代码段多带一份高亮后的 HTML。
 *
 * 写成联合类型而不是 `interface RenderedSegment extends AiSegment`：
 * AiSegment 本身就是联合，interface 只能继承「成员静态可知」的对象类型。
 * 用 `kind` 收窄后，模板里 `seg.kind === 'code'` 分支上 `seg.html` 依然可见。
 */
type RenderedSegment = (CodeSegment & { html?: string }) | TextSegment

/**
 * 输出的分段视图。
 *
 * **流式进行中不做高亮**：delta 每来一次就重渲染一次，而中途的 JSON 本身是
 * 残缺的（截图中那句「最后一场苏晴…被苏晴」就是中途状态），高亮既没意义
 * 也在白烧 CPU。等 end 到达、status 变成 done / error 才一次性高亮。
 */
const outputSegments = computed<RenderedSegment[]>(() => {
  const segments = splitCodeSegments(props.call.response ?? '')
  const settled = props.call.status !== 'running'
  return segments.map((seg) =>
    seg.kind === 'code' && settled
      ? { ...seg, html: highlightCode(seg.text, seg.lang) }
      : seg,
  )
})
</script>

<template>
  <div class="call" :class="[`is-${call.status}`, `sem-${stageSemantic}`]">
    <button class="head" type="button" :aria-expanded="open" @click="open = !open">
      <span class="dot" :class="`dot-${call.status}`" />
      <span class="stage" :title="call.stage || undefined">{{ stageTitle }}</span>
      <n-tag size="tiny" :type="statusType" :bordered="false" round>{{ statusLabel }}</n-tag>
      <span class="spacer" />
      <span class="meta">{{ timeLabel }} · {{ elapsedLabel }}</span>
    </button>

    <div v-if="open" class="body">
      <div class="kv">
        <span>模型</span><span>{{ call.model || '—' }}</span>
      </div>
      <div class="kv">
        <span>Token</span><span>{{ tokenLabel }}</span>
      </div>

      <!-- 输入：中性色导轨，表示「发出去的」 -->
      <section class="sect sect-in">
        <button
          class="sect-head" type="button"
          :aria-expanded="showInput"
          @click="showInput = !showInput"
        >
          <span class="sect-name">输入</span>
          <span class="sect-note">{{ inputChars }} 字</span>
          <span class="caret">{{ showInput ? '▾' : '▸' }}</span>
        </button>
        <div v-if="showInput" class="sect-body">
          <div class="sub">
            <button
              class="sub-head" type="button"
              :aria-expanded="showSystem"
              @click="showSystem = !showSystem"
            >
              系统提示词
              <span class="sub-note">{{ call.promptSystem?.length ?? 0 }} 字</span>
              <span class="caret">{{ showSystem ? '▾' : '▸' }}</span>
            </button>
            <div v-if="showSystem" class="text mono">{{ call.promptSystem }}</div>
          </div>
          <div class="sub">
            <div class="sub-head static">用户提示词</div>
            <div class="text mono">{{ call.promptUser }}</div>
          </div>
        </div>
      </section>

      <!-- 输出：品牌色导轨 + 极淡底色，表示「回来的成品」，与输入明确区分 -->
      <section class="sect sect-out">
        <button
          class="sect-head" type="button"
          :aria-expanded="showOutput"
          @click="showOutput = !showOutput"
        >
          <span class="sect-name">输出</span>
          <span class="sect-note">{{ call.response ? `${call.response.length} 字` : '—' }}</span>
          <span class="caret">{{ showOutput ? '▾' : '▸' }}</span>
        </button>
        <div v-if="showOutput" class="sect-body">
          <div v-if="call.status === 'running' && !call.response" class="waiting">
            等待模型返回…
          </div>
          <template v-else>
            <div v-for="(seg, i) in outputSegments" :key="i" class="seg">
              <pre v-if="seg.kind === 'code'" class="ai-code"><code
                v-if="seg.html" class="hljs" v-html="seg.html"
              ></code><code v-else>{{ seg.text }}</code></pre>
              <div v-else class="text">{{ seg.text }}</div>
            </div>
          </template>
        </div>
      </section>

      <div v-if="call.error" class="err">{{ call.error }}</div>
    </div>
  </div>
</template>

<style scoped>
.call {
  border-bottom: 1px solid var(--app-divider);
  border-left: 2px solid var(--sem-color, transparent);
  transition: background var(--app-transition);
}
.call.is-running { background: var(--color-brand-light); }

/* semantic → 导轨色。写作绿、规划蓝、校验紫、同步橙，与仪表条呼应。 */
.sem-write { --sem-color: var(--color-success); }
.sem-plan { --sem-color: var(--color-info); }
.sem-audit { --sem-color: var(--color-purple); }
.sem-sync { --sem-color: var(--color-warning); }
.sem-generate { --sem-color: var(--color-brand); }

.head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 8px 12px 8px 10px; background: none; border: 0; cursor: pointer;
  font: inherit; font-size: var(--font-size-sm); color: inherit; text-align: left;
}
.head:hover { background: var(--app-surface-subtle); }
.head:focus-visible { outline: 2px solid var(--color-brand); outline-offset: -2px; }

.dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.dot-running { background: var(--color-brand); animation: breathe 1.4s ease-in-out infinite; }
.dot-done { background: var(--color-success); }
.dot-error { background: var(--color-danger); }
@keyframes breathe {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: .35; transform: scale(.82); }
}

.stage {
  font-weight: 600; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; max-width: 46%;
}
.spacer { flex: 1; }
.meta {
  color: var(--app-text-muted); font-size: var(--font-size-xs);
  font-family: var(--font-mono); font-variant-numeric: tabular-nums; white-space: nowrap;
}

.body {
  padding: 2px 12px 12px; display: flex; flex-direction: column; gap: 8px;
}
.kv {
  display: flex; justify-content: space-between; gap: 12px;
  font-size: var(--font-size-xs); color: var(--app-text-muted);
}
.kv > :last-child {
  color: var(--app-text-secondary);
  font-family: var(--font-mono);
  text-align: right; word-break: break-all;
}

/* ── 输入 / 输出：两个结构完全对称的分区，靠导轨色与底色区分 ── */
.sect { border-radius: var(--app-radius-sm); overflow: hidden; }
.sect-in { border-left: 2px solid var(--app-border-strong); background: var(--app-surface-subtle); }
.sect-out { border-left: 2px solid var(--color-brand); background: var(--color-brand-light); }

.sect-head {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 6px 8px; background: none; border: 0; cursor: pointer;
  font: inherit; text-align: left; color: inherit;
}
.sect-head:hover { background: var(--app-surface-subtle); }
.sect-out .sect-head:hover { background: var(--color-brand-light); }
.sect-name {
  font-size: var(--font-size-xs); font-weight: 700;
  letter-spacing: .06em; color: var(--app-text-primary);
}
.sect-out .sect-name { color: var(--color-brand); }
.sect-note {
  font-size: var(--font-size-xs); color: var(--app-text-muted);
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
}
.caret { margin-left: auto; font-size: 10px; color: var(--app-text-muted); }

.sect-body { padding: 2px 8px 8px; display: flex; flex-direction: column; gap: 8px; }
.sect-body:empty { display: none; }

.sub-head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 2px 0; background: none; border: 0; cursor: pointer;
  font: inherit; font-size: var(--font-size-xs); font-weight: 600;
  color: var(--app-text-secondary); text-align: left;
}
.sub-head.static { cursor: default; }
.sub-head:not(.static):hover { color: var(--color-brand); }
.sub-note {
  font-weight: 400; color: var(--app-text-muted);
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
}

.text {
  font-size: var(--font-size-xs); line-height: 1.65;
  white-space: pre-wrap; word-break: break-word;
  max-height: 280px; overflow-y: auto; margin-top: 4px;
  color: var(--app-text-primary);
}
.mono { font-family: var(--font-mono); }

/* ── 围栏代码块 ──────────────────────────────────────────────────
   模型的结构化输出几乎总被包在 ```json 里。之前整段按纯文本渲染，
   围栏字符本身也照原样显示，读者得自己把 ```json 剥掉才能看到结构。 */
.seg { min-width: 0; }
.ai-code {
  margin: 4px 0 0;
  padding: 8px 10px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-sm);
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  line-height: 1.6;
  color: var(--app-text-primary);
  white-space: pre;
  overflow-x: auto;
  max-height: 280px;
  overflow-y: auto;
}
.ai-code code { font: inherit; background: none; padding: 0; }

/* hljs 只吐出 class，全仓没有任何地方引它的主题 CSS，颜色在这里自己给。
   映射到 app 的 token，这样 light / dark / anchor 三套主题自动跟随。 */
.hljs-attr { color: var(--color-info); }
.hljs-string { color: var(--color-success); }
.hljs-number { color: var(--color-purple); }
.hljs-literal { color: var(--color-warning); }
.hljs-punctuation { color: var(--app-text-muted); }
.hljs-comment { color: var(--app-text-muted); font-style: italic; }
.hljs-title,
.hljs-built_in,
.hljs-type { color: var(--color-brand); }
.hljs-meta { color: var(--app-text-secondary); }
.hljs-tag { color: var(--color-danger); }
.hljs-emphasis { font-style: italic; }
.hljs-strong { font-weight: 600; }

/* 只做透明度呼吸。不能用 .dot 的 breathe——那个关键帧带 transform: scale()，
   作用在整行文字上会让提示语缩放抖动。 */
.waiting {
  font-size: var(--font-size-xs); color: var(--color-brand);
  padding: 6px 0; animation: fade-pulse 1.6s ease-in-out infinite;
}
@keyframes fade-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: .4; }
}

.err {
  font-size: var(--font-size-xs); color: var(--color-danger);
  background: var(--color-danger-dim);
  border-radius: var(--app-radius-sm);
  padding: 6px 8px;
}

@media (prefers-reduced-motion: reduce) {
  .dot-running, .waiting { animation: none; }
  .call { transition: none; }
}</style>
