<script setup lang="ts">
import { computed, ref } from 'vue'
import { NTag } from 'naive-ui'
import type { AiCall } from '@/stores/aiStreamReduce'

const props = defineProps<{ call: AiCall }>()

const open = ref(false)
const showPromptSystem = ref(false)

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
</script>

<template>
  <div class="call" :class="`is-${call.status}`">
    <button class="head" type="button" :aria-expanded="open" @click="open = !open">
      <span class="dot" :class="`dot-${call.status}`" />
      <span class="stage">{{ call.stageLabel || call.stage || 'AI 调用' }}</span>
      <n-tag size="tiny" :type="statusType" :bordered="false" round>{{ statusLabel }}</n-tag>
      <span class="spacer" />
      <span class="meta">{{ elapsedLabel }}</span>
    </button>

    <div v-if="open" class="body">
      <div class="kv">
        <span>模型</span><span>{{ call.model || '—' }}</span>
      </div>
      <div class="kv">
        <span>Token</span><span>{{ tokenLabel }}</span>
      </div>

      <div class="block">
        <button class="block-title" type="button" @click="showPromptSystem = !showPromptSystem">
          输入 {{ showPromptSystem ? '▾' : '▸' }}
        </button>
        <div v-if="showPromptSystem && call.promptSystem" class="text mono">
          {{ call.promptSystem }}
        </div>
        <div v-if="call.promptUser" class="text mono">{{ call.promptUser }}</div>
      </div>

      <div class="block">
        <div class="block-title static">输出</div>
        <div v-if="call.status === 'running' && !call.response" class="text dim">思考中…</div>
        <div class="text mono">{{ call.response }}</div>
      </div>

      <div v-if="call.error" class="err">{{ call.error }}</div>
    </div>
  </div>
</template>

<style scoped>
.call {
  border-bottom: 1px solid var(--app-divider);
  transition: background var(--app-transition);
}
.call.is-running { background: var(--color-brand-light); }

.head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 8px 12px; background: none; border: 0; cursor: pointer;
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
  text-overflow: ellipsis; max-width: 45%;
}
.spacer { flex: 1; }
.meta {
  color: var(--app-text-muted); font-size: var(--font-size-xs);
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
}

.body {
  padding: 4px 12px 12px; display: flex; flex-direction: column; gap: 8px;
}
.kv {
  display: flex; justify-content: space-between; gap: 12px;
  font-size: var(--font-size-xs); color: var(--app-text-muted);
}
.kv > :last-child {
  color: var(--app-text-secondary);
  font-family: var(--font-mono);
  text-align: right;
  word-break: break-all;
}

.block {
  border-left: 2px solid var(--app-divider);
  padding-left: 10px;
}
.block-title {
  background: none; border: 0; padding: 0; cursor: pointer;
  font: inherit; font-size: var(--font-size-xs);
  color: var(--app-text-secondary); text-align: left;
}
.block-title.static { cursor: default; }
.block-title:hover:not(.static) { color: var(--color-brand); }

.text {
  font-size: var(--font-size-xs); line-height: 1.65;
  white-space: pre-wrap; word-break: break-word;
  max-height: 200px; overflow-y: auto; margin-top: 4px;
  color: var(--app-text-primary);
}
.mono { font-family: var(--font-mono); }
.dim { color: var(--app-text-muted); }
.err {
  font-size: var(--font-size-xs); color: var(--color-danger);
  background: var(--color-danger-dim);
  border-radius: var(--app-radius-sm);
  padding: 6px 8px;
}

@media (prefers-reduced-motion: reduce) {
  .dot-running { animation: none; }
  .call { transition: none; }
}
</style>
