<template>
  <section class="macro-refactor-panel">
    <header class="macro-refactor-header">
      <div class="macro-refactor-title">
        <n-icon size="14"><PulseOutline /></n-icon>
        <span>宏观诊断</span>
        <n-tag v-if="pending.length" size="small" round :bordered="false" type="warning">
          {{ pending.length }} 条待确认
        </n-tag>
      </div>
      <n-button size="tiny" secondary :loading="running" @click="runDiagnosis">
        立即诊断
      </n-button>
    </header>

    <div v-if="loading" class="macro-refactor-empty">读取中…</div>

    <div v-else-if="!pending.length" class="macro-refactor-empty">
      暂无待确认的诊断。诊断产出的叙事校准需要你批准后才会进入生成上下文。
    </div>

    <article v-for="item in pending" :key="item.id" class="macro-refactor-item">
      <div class="macro-refactor-item-head">
        <span class="macro-refactor-reason">{{ item.trigger_reason }}</span>
        <n-tag size="tiny" round :bordered="false">{{ item.trait }}</n-tag>
        <n-tag size="tiny" round :bordered="false" type="info">
          断点 {{ item.breakpoint_count }}
        </n-tag>
      </div>

      <div v-if="item.context_patch" class="macro-refactor-patch">
        <div class="macro-refactor-label">注入的叙事校准（批准后生效）</div>
        <pre class="macro-refactor-patch-text">{{ item.context_patch }}</pre>
      </div>
      <div v-else class="macro-refactor-empty">
        本次诊断没有产出校准文本，批准也不会改变生成上下文。
      </div>

      <ul v-if="item.breakpoints?.length" class="macro-refactor-breakpoints">
        <li v-for="(bp, index) in item.breakpoints.slice(0, 5)" :key="index">
          第{{ bp.chapter }}章 · {{ bp.reason }}
        </li>
      </ul>

      <div class="macro-refactor-actions">
        <n-button
          size="tiny"
          type="primary"
          :loading="busyId === item.id"
          @click="decide(item.id, 'approved')"
        >
          批准注入
        </n-button>
        <n-button
          size="tiny"
          secondary
          :loading="busyId === item.id"
          @click="decide(item.id, 'rejected')"
        >
          否决
        </n-button>
        <n-button size="tiny" quaternary @click="markResolved(item.id)">标记已处理</n-button>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
/**
 * H-06 子项 B：宏观诊断的「系统叙事校准」注入**经作者确认**。
 *
 * 此前诊断跑完就把校准指令静默注入生成上下文 —— 作者既看不到、也没机会拒绝。
 * 现在这里列出待确认的诊断：先看见校准文本，再决定批准（进上下文）还是否决。
 */
import { onMounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { PulseOutline } from '@vicons/ionicons5'
import { macroRefactorApi, type MacroDiagnosisResult } from '@/api/tools'
import { formatApiError } from '@/utils/apiError'

const props = withDefaults(defineProps<{ slug: string }>(), {})

const message = useMessage()

const pending = ref<MacroDiagnosisResult[]>([])
const loading = ref(false)
const running = ref(false)
const busyId = ref('')

async function load() {
  if (!props.slug) return
  loading.value = true
  try {
    pending.value = await macroRefactorApi.listPendingDiagnoses(props.slug)
  } catch (error) {
    pending.value = []
    message.warning(`读取待确认诊断失败：${formatApiError(error)}`)
  } finally {
    loading.value = false
  }
}

async function decide(id: string, decision: 'approved' | 'rejected') {
  busyId.value = id
  try {
    const result = await macroRefactorApi.decideDiagnosis(props.slug, id, decision)
    if (result.success) {
      message.success(decision === 'approved' ? '已批准：这条校准会进入生成上下文' : '已否决：不注入这条校准')
      await load()
    } else {
      message.error(result.message || '决定被拒收')
    }
  } catch (error) {
    message.error(`记录决定失败：${formatApiError(error)}`)
  } finally {
    busyId.value = ''
  }
}

async function markResolved(id: string) {
  busyId.value = id
  try {
    await macroRefactorApi.resolveDiagnosis(props.slug, id)
    message.success('已标记为已处理')
    await load()
  } catch (error) {
    message.error(`标记失败：${formatApiError(error)}`)
  } finally {
    busyId.value = ''
  }
}

async function runDiagnosis() {
  running.value = true
  try {
    await macroRefactorApi.runDiagnosis(props.slug)
    message.success('诊断完成')
    await load()
  } catch (error) {
    message.error(`诊断失败：${formatApiError(error)}`)
  } finally {
    running.value = false
  }
}

onMounted(load)
watch(() => props.slug, load)
</script>

<style scoped>
.macro-refactor-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--card-color);
}

.macro-refactor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.macro-refactor-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 700;
  color: var(--app-text-primary);
}

.macro-refactor-empty {
  font-size: 11px;
  color: var(--app-text-secondary);
  line-height: 1.6;
}

.macro-refactor-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
}

.macro-refactor-item-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.macro-refactor-reason {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-primary);
}

.macro-refactor-label {
  font-size: 10px;
  color: var(--app-text-secondary);
  margin-bottom: 4px;
}

.macro-refactor-patch-text {
  margin: 0;
  padding: 8px;
  border-radius: 6px;
  background: var(--app-surface);
  font-size: 11px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--app-text-secondary);
}

.macro-refactor-breakpoints {
  margin: 0;
  padding-left: 18px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--app-text-secondary);
}

.macro-refactor-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
</style>
