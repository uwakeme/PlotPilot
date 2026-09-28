<template>
  <div v-if="open" class="crp">
    <div class="crp-head">
      <strong>AI 章节优化</strong>
      <span class="crp-chapter">第{{ chapterNumber }}章{{ chapterTitle ? ` · ${chapterTitle}` : '' }}</span>
      <button class="crp-close" type="button" title="关闭" @click="close">✕</button>
    </div>

    <div class="crp-input-row">
      <textarea
        v-model="instruction"
        class="crp-instruction"
        rows="2"
        placeholder="告诉 AI 怎么改这一章，例：加强结尾悬念；补上陆深进门的动机；删掉重复交代…"
      />
      <div class="crp-presets">
        <button v-for="preset in PRESETS" :key="preset" type="button" class="crp-preset" @click="applyPreset(preset)">
          {{ preset }}
        </button>
      </div>
    </div>

    <div class="crp-actions">
      <button class="crp-btn crp-btn--primary" type="button" :disabled="busy || !instruction.trim() || !chapterContent.trim()" @click="start">
        {{ busy && !showPreview ? '生成中…' : '发起优化' }}
      </button>
      <button v-if="sessionId && busy" class="crp-btn" type="button" @click="cancel">取消</button>
    </div>

    <p v-if="statusText" class="crp-status" :class="{ 'is-error': isError }">{{ statusText }}</p>

    <div v-if="showPreview" class="crp-preview">
      <div class="crp-pane">
        <div class="crp-pane__head">
          <span>原文</span>
          <small>{{ originalLength }} 字</small>
        </div>
        <textarea :value="chapterContent" readonly class="crp-pane__body" />
      </div>
      <div class="crp-pane crp-pane--new">
        <div class="crp-pane__head">
          <span>修改稿</span>
          <small>{{ revisedLength }} 字</small>
        </div>
        <textarea v-model="editedRevised" class="crp-pane__body" />
      </div>
    </div>
    <p v-if="showPreview" class="crp-hint">可微调修改稿后再采纳；采纳即写回本章（章节收稿状态不变）。</p>

    <div v-if="showPreview" class="crp-actions">
      <button class="crp-btn crp-btn--primary" type="button" :disabled="committing" @click="adopt">
        {{ committing ? '写回中…' : '采纳并写回' }}
      </button>
      <button class="crp-btn" type="button" :disabled="busy" @click="reset">放弃</button>
      <button class="crp-btn" type="button" :disabled="busy" @click="start">改指令重新生成</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { aiInvocationApi } from '../../api/aiInvocation'

const props = defineProps<{
  open: boolean
  novelId: string
  chapterNumber: number
  chapterTitle: string
  chapterContent: string
  prefillInstruction?: string
}>()

const emit = defineEmits<{ close: []; revised: [] }>()

const PRESETS = [
  '润色全文：去 AI 味，短句化，对话独立成段',
  '强化本章结尾的悬念钩子',
  '收紧节奏：删除重复交代与无效过渡',
  '补足人物行为动机，消除逻辑断层',
]

const instruction = ref('')
const sessionId = ref('')
const attemptId = ref('')
const revisedContent = ref('')
const editedRevised = ref('')
const busy = ref(false)
const committing = ref(false)
const statusText = ref('')
const isError = ref(false)
let pollTimer: ReturnType<typeof setTimeout> | null = null

const showPreview = computed(() => Boolean(attemptId.value && revisedContent.value))
const originalLength = computed(() => props.chapterContent.replace(/\s/g, '').length)
const revisedLength = computed(() => editedRevised.value.replace(/\s/g, '').length)

watch(
  () => props.open,
  (open) => {
    if (open) {
      statusText.value = ''
      isError.value = false
      instruction.value = props.prefillInstruction || ''
    } else {
      reset()
    }
  },
)

function applyPreset(preset: string): void {
  instruction.value = instruction.value.trim() ? `${instruction.value.trim()}；${preset}` : preset
}

function close(): void {
  stopPoll()
  emit('close')
}

function reset(): void {
  stopPoll()
  sessionId.value = ''
  attemptId.value = ''
  revisedContent.value = ''
  editedRevised.value = ''
  busy.value = false
  committing.value = false
  statusText.value = ''
  isError.value = false
}

function cancel(): void {
  stopPoll()
  busy.value = false
  statusText.value = '已取消，本次改写未写回。'
}

async function start(): Promise<void> {
  if (!props.chapterContent.trim() || !instruction.value.trim()) return
  reset()
  busy.value = true
  statusText.value = '正在创建优化会话…'
  try {
    const payload = await aiInvocationApi.create({
      operation: 'chapter.revise.interactive',
      node_key: 'chapter-revise',
      policy: 'REVIEW_AFTER_CALL',
      context: { novel_id: props.novelId, chapter_number: props.chapterNumber },
      variables: {
        chapter_number: props.chapterNumber,
        chapter_title: props.chapterTitle || '',
        chapter_content: props.chapterContent,
        user_instruction: instruction.value.trim(),
        style_contract: '',
        continuity_context: '',
      },
    })
    const id = payload.session?.id
    if (!id) throw new Error('会话创建失败：无 session id')
    sessionId.value = id
    statusText.value = 'AI 正在按指令改写本章…'
    poll(id)
  } catch (err) {
    busy.value = false
    isError.value = true
    statusText.value = `发起失败：${extractError(err)}`
  }
}

function poll(sessionIdValue: string): void {
  stopPoll()
  pollTimer = setTimeout(async () => {
    if (sessionId.value !== sessionIdValue) return
    try {
      const payload = await aiInvocationApi.get(sessionIdValue)
      const status = payload.session?.status ?? ''
      if (payload.attempt?.content) {
        revisedContent.value = payload.attempt.content
        editedRevised.value = payload.attempt.content
      }
      if (payload.attempt?.id) attemptId.value = payload.attempt.id
      if (status === 'awaiting_acceptance') {
        busy.value = false
        statusText.value = '改写完成，请对比预览后采纳。'
        return
      }
      if (status === 'blocked' || status === 'failed') {
        busy.value = false
        isError.value = true
        statusText.value = `优化失败：${payload.attempt?.error || status}`
        return
      }
      poll(sessionIdValue)
    } catch (err) {
      busy.value = false
      isError.value = true
      statusText.value = `轮询失败：${extractError(err)}`
    }
  }, 1500)
}

async function adopt(): Promise<void> {
  if (!sessionId.value || !attemptId.value) return
  committing.value = true
  isError.value = false
  try {
    const payload = await aiInvocationApi.accept(sessionId.value, {
      attempt_id: attemptId.value,
      accepted_by: 'user',
      accepted_content: editedRevised.value.trim(),
    })
    if (payload.next_action === 'commit_required' && payload.decision?.id) {
      await aiInvocationApi.commit(sessionId.value, payload.decision.id)
    }
    statusText.value = '已写回本章。'
    emit('revised')
    reset()
  } catch (err) {
    isError.value = true
    statusText.value = `写回失败：${extractError(err)}`
  } finally {
    committing.value = false
  }
}

function stopPoll(): void {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

// 组件卸载（切章/离开工作台）时必须停掉轮询，否则后台会一直空转到会话终态
onUnmounted(stopPoll)

function extractError(err: unknown): string {
  const anyErr = err as { response?: { data?: { detail?: string } }; message?: string }
  return anyErr?.response?.data?.detail || anyErr?.message || String(err)
}
</script>

<style scoped>
.crp { border: 1px solid rgba(128,128,128,0.28); border-radius: 10px; padding: 12px 14px; margin: 0 0 12px; background: rgba(128,128,128,0.04); display: flex; flex-direction: column; gap: 10px; }
.crp-head { display: flex; align-items: center; gap: 8px; }
.crp-chapter { font-size: 12px; opacity: 0.65; flex: 1; }
.crp-close { border: none; background: transparent; cursor: pointer; font-size: 14px; opacity: 0.6; }
.crp-close:hover { opacity: 1; }
.crp-input-row { display: flex; flex-direction: column; gap: 6px; }
.crp-instruction { width: 100%; border: 1px solid rgba(128,128,128,0.35); border-radius: 6px; padding: 8px 10px; font-size: 13px; resize: vertical; font-family: inherit; background: transparent; color: inherit; }
.crp-presets { display: flex; gap: 6px; flex-wrap: wrap; }
.crp-preset { border: 1px dashed rgba(128,128,128,0.4); background: transparent; border-radius: 10px; padding: 2px 10px; font-size: 11px; cursor: pointer; opacity: 0.8; }
.crp-preset:hover { opacity: 1; border-color: var(--color-brand, #35567e); }
.crp-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.crp-btn { border: 1px solid rgba(128,128,128,0.35); background: transparent; border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
.crp-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.crp-btn--primary { background: var(--color-brand, #35567e); border-color: var(--color-brand, #35567e); color: #fff; }
.crp-status { margin: 0; font-size: 12px; opacity: 0.8; }
.crp-status.is-error { color: #c2492e; opacity: 1; }
.crp-preview { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.crp-pane { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.crp-pane__head { display: flex; justify-content: space-between; font-size: 12px; opacity: 0.75; }
.crp-pane__body { height: 300px; border: 1px solid rgba(128,128,128,0.3); border-radius: 6px; padding: 8px 10px; font-size: 13px; line-height: 1.7; resize: vertical; font-family: inherit; background: transparent; color: inherit; }
.crp-pane--new .crp-pane__body { border-color: rgba(53,86,126,0.55); }
.crp-hint { margin: 0; font-size: 11px; opacity: 0.6; }
@media (max-width: 900px) { .crp-preview { grid-template-columns: 1fr; } }
</style>
