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
    <button class="head" type="button" @click="open = !open">
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
.call { border-bottom: 1px solid var(--n-divider-color, rgba(26,29,33,.06)); }
.head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 8px 10px; background: none; border: 0; cursor: pointer;
  font-size: 12px; color: inherit; text-align: left;
}
.head:hover { background: rgba(127,127,127,.06); }
.dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.dot-running { background: #35567e; animation: pulse 1.1s ease-in-out infinite; }
.dot-done { background: #52c41a; }
.dot-error { background: #ef4444; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .25; } }
.stage { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.spacer { flex: 1; }
.meta { color: #909399; font-size: 11px; }
.body { padding: 4px 10px 10px; display: flex; flex-direction: column; gap: 6px; }
.kv { display: flex; justify-content: space-between; font-size: 11px; color: #909399; }
.block-title {
  background: none; border: 0; padding: 0; cursor: pointer;
  font-size: 11px; color: #6b7280; text-align: left;
}
.block-title.static { cursor: default; }
.text {
  font-size: 11px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  max-height: 200px; overflow-y: auto; margin-top: 3px;
}
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.dim { color: #909399; }
.err { font-size: 11px; color: #ef4444; }
</style>