<template>
  <n-drawer :show="show" :width="Math.min(920, windowWidth - 80)" placement="right" :z-index="3600" @update:show="onShowChange">
    <n-drawer-content title="历史版本" closable>
      <n-spin :show="loading">
        <div v-if="!drafts.length" class="cvd-empty">本章还没有历史版本——手动保存、AI 优化或重新生成时会自动留痕。</div>

        <div v-else class="cvd-layout">
          <!-- 版本列表 -->
          <div class="cvd-list">
            <div
              v-for="draft in drafts"
              :key="draft.id"
              class="cvd-item"
              :class="{ 'is-selected': selectedId === draft.id }"
              @click="selectDraft(draft)"
            >
              <div class="cvd-item__top">
                <span class="cvd-source" :class="`is-${draft.source}`">{{ sourceLabel(draft.source) }}</span>
                <span class="cvd-time">{{ formatTime(draft.created_at) }}</span>
              </div>
              <div class="cvd-item__meta">{{ draft.word_count }} 字 · {{ draft.content.slice(0, 30).replace(/\n/g, ' ') }}…</div>
            </div>
          </div>

          <!-- 对比与恢复 -->
          <div v-if="selected" class="cvd-diff">
            <div class="cvd-diff__head">
              <span>{{ sourceLabel(selected.source) }} · {{ formatTime(selected.created_at) }} · {{ selected.word_count }} 字</span>
              <n-button size="tiny" :type="isSameAsCurrent ? 'default' : 'warning'" :disabled="isSameAsCurrent" @click="confirmRestore">
                {{ isSameAsCurrent ? '即当前版本' : '恢复此版本' }}
              </n-button>
            </div>
            <p v-if="isSameAsCurrent" class="cvd-hint">该版本与当前正文一致。</p>
            <div class="cvd-diff__body">
              <div
                v-for="(line, index) in diffLines"
                :key="index"
                class="cvd-line"
                :class="`is-${line.type}`"
              >
                <span class="cvd-line__mark">{{ line.type === 'removed' ? '−' : line.type === 'added' ? '+' : ' ' }}</span>
                <span class="cvd-line__text">{{ line.text || ' ' }}</span>
              </div>
            </div>
            <p class="cvd-legend">
              <span class="cvd-line__mark is-removed">−</span> 当前多出（恢复后移除）
              <span class="cvd-line__mark is-added" style="margin-left: 12px">+</span> 该版本多出（恢复后补回）
            </p>
          </div>
        </div>
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMessage, useDialog } from 'naive-ui'
import { listChapterDrafts, restoreChapterDraft, type ChapterDraftDTO } from '../../api/workflow'

const props = defineProps<{
  show: boolean
  novelId: string
  chapterNumber: number
  currentContent: string
}>()

const emit = defineEmits<{ 'update:show': [value: boolean]; restored: [] }>()

const message = useMessage()
const dialog = useDialog()

const drafts = ref<ChapterDraftDTO[]>([])
const selectedId = ref('')
const loading = ref(false)
const windowWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1280)

const selected = computed(() => drafts.value.find(d => d.id === selectedId.value) ?? null)
const isSameAsCurrent = computed(() => Boolean(selected.value && selected.value.content === props.currentContent))

interface DiffLine { type: 'same' | 'removed' | 'added'; text: string }

/** LCS 行级对比：removed=当前有而选中版本没有,added=选中版本有而当前没有 */
const diffLines = computed<DiffLine[]>(() => {
  if (!selected.value) return []
  const a = (props.currentContent || '').split('\n')  // 当前
  const b = (selected.value.content || '').split('\n') // 选中版本
  const n = a.length
  const m = b.length
  // DP 表
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const lines: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      lines.push({ type: 'same', text: a[i] })
      i++; j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      lines.push({ type: 'removed', text: a[i] }); i++
    } else {
      lines.push({ type: 'added', text: b[j] }); j++
    }
  }
  while (i < n) { lines.push({ type: 'removed', text: a[i] }); i++ }
  while (j < m) { lines.push({ type: 'added', text: b[j] }); j++ }
  return lines
})

async function load(): Promise<void> {
  loading.value = true
  try {
    drafts.value = await listChapterDrafts(props.novelId, props.chapterNumber)
    if (drafts.value.length && !drafts.value.some(d => d.id === selectedId.value)) {
      selectedId.value = drafts.value[0].id
    }
    if (!drafts.value.length) selectedId.value = ''
  } catch (err) {
    message.error(`读取历史版本失败：${extractError(err)}`)
  } finally {
    loading.value = false
  }
}

function selectDraft(draft: ChapterDraftDTO): void {
  selectedId.value = draft.id
}

function onShowChange(value: boolean): void {
  emit('update:show', value)
  if (value) void load()
}

function confirmRestore(): void {
  if (!selected.value) return
  const draft = selected.value
  dialog.warning({
    title: '恢复此版本？',
    content: `将把第 ${props.chapterNumber} 章正文恢复为「${sourceLabel(draft.source)}」（${draft.word_count} 字）。当前正文会先自动存为一 条「回退前」历史，可再次回退。`,
    positiveText: '恢复',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await restoreChapterDraft(props.novelId, props.chapterNumber, draft.id)
        message.success('已恢复到所选版本')
        emit('restored')
        await load()
      } catch (err) {
        message.error(`恢复失败：${extractError(err)}`)
      }
    },
  })
}

const SOURCE_LABELS: Record<string, string> = {
  manual_save: '手动保存前',
  pre_regen: '重新生成前',
  pre_prose: 'AI 生成前',
  pre_revise: 'AI 优化前',
  pre_restore: '回退前',
  pre_autopilot: '自动写作前',
  auto_gen: '首次生成',
}
const sourceLabel = (key: string) => SOURCE_LABELS[key] ?? key

function formatTime(iso: string): string {
  if (!iso) return ''
  const normalized = iso.includes('T') ? iso : iso.replace(' ', 'T')
  const date = new Date(normalized.endsWith('Z') ? normalized : `${normalized}Z`)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function extractError(err: unknown): string {
  const anyErr = err as { response?: { data?: { detail?: string } }; message?: string }
  return anyErr?.response?.data?.detail || anyErr?.message || String(err)
}

watch(
  () => [props.show, props.chapterNumber] as const,
  ([show]) => {
    if (show) {
      selectedId.value = ''
      drafts.value = []
      void load()
    }
  },
  { immediate: true },
)
</script>

<style scoped>
.cvd-empty { padding: 40px 0; text-align: center; font-size: 13px; opacity: 0.6; }
.cvd-layout { display: flex; gap: 14px; min-height: 60vh; }
.cvd-list { width: 260px; flex-shrink: 0; display: flex; flex-direction: column; gap: 6px; max-height: 74vh; overflow-y: auto; }
.cvd-item { border: 1px solid rgba(128,128,128,0.28); border-radius: 8px; padding: 8px 10px; cursor: pointer; font-size: 12px; }
.cvd-item:hover { border-color: var(--color-brand, #35567e); }
.cvd-item.is-selected { border-color: var(--color-brand, #35567e); background: rgba(53,86,126,0.06); }
.cvd-item__top { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
.cvd-source { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid rgba(128,128,128,0.4); white-space: nowrap; }
.cvd-source.is-critical { color: #b0432a; }
.cvd-time { opacity: 0.65; }
.cvd-item__meta { margin-top: 4px; opacity: 0.6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.cvd-diff { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.cvd-diff__head { display: flex; justify-content: space-between; align-items: center; font-size: 12px; opacity: 0.85; }
.cvd-hint { margin: 0; font-size: 12px; opacity: 0.6; }
.cvd-diff__body { flex: 1; max-height: 64vh; overflow-y: auto; border: 1px solid rgba(128,128,128,0.28); border-radius: 8px; font-family: inherit; font-size: 12px; line-height: 1.7; }
.cvd-line { display: flex; gap: 6px; padding: 0 8px; white-space: pre-wrap; word-break: break-all; }
.cvd-line__mark { width: 12px; flex-shrink: 0; opacity: 0.7; }
.cvd-line.is-removed { background: rgba(176,67,42,0.12); }
.cvd-line.is-added { background: rgba(53,134,90,0.14); }
.cvd-legend { margin: 0; font-size: 11px; opacity: 0.6; }
</style>
