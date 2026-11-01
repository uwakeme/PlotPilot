<!--
  守护进程监督者徽标（第 77 批）：让「自动驾驶静默停摆」在**顶栏**也看得见。

  ⚠ 为什么不放在自动驾驶面板里就够了：
    面板只有打开自动驾驶视图时才在 ⇒ 用户在**写书**（工作台）时看不到
    ⇒ 守护进程被外部杀掉又自动拉起这件事「界面完全无感」（那正是它要解决的事）。

  ⚠ **不新增轮询通道**：读的是既有的 ``autopilotStatusStore``
    （``/autopilot/status`` 的**单一**轮询链，本 store 就是它的所有者）
    ⇒ 顶栏只是**又一个订阅者** ⇒ 同一条链、同一份快照。

  ⚠ 平时**不占位**：只有「重启过 / 拉起失败 / 状态陈旧」时才出现。
-->
<template>
  <n-tooltip v-if="notice" trigger="hover">
    <template #trigger>
      <span class="ds-badge" :class="toneClass" role="status" :aria-label="notice">
        {{ notice }}
      </span>
    </template>
    <div class="ds-detail">
      <div>{{ detailLine }}</div>
      <div v-if="publishedAt">状态发布时间：{{ publishedAt }}</div>
    </div>
  </n-tooltip>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { NTooltip } from 'naive-ui'
import { useAutopilotStatusStore } from '@/stores/autopilotStatusStore'

const props = defineProps<{ novelId: string }>()

const store = useAutopilotStatusStore()

/** 订阅既有轮询链（不自己发请求）。 */
let unsubscribe: (() => void) | null = null
function subscribe() {
  if (unsubscribe) unsubscribe()
  unsubscribe = props.novelId ? store.subscribe(props.novelId, () => {}) : null
}
onMounted(subscribe)
onBeforeUnmount(() => {
  if (unsubscribe) unsubscribe()
  unsubscribe = null
})
watch(() => props.novelId, subscribe)

const supervisor = computed(() => store.statusByNovel[props.novelId]?.daemon_supervisor)

const notice = computed(() => {
  const s = supervisor.value
  if (!s) return ''
  const lastError = String(s.last_error ?? '')
  if (lastError) return '自动拉起失败'
  const attempts = Number(s.restart_attempts ?? 0)
  if (attempts > 0) return `已自动重启 ${attempts} 次`
  return ''
})

const toneClass = computed(() =>
  String(supervisor.value?.last_error ?? '') ? 'is-error' : 'is-warn',
)

const publishedAt = computed(() => {
  const raw = Number(supervisor.value?.published_at ?? 0)
  if (!raw) return ''
  return new Date(raw * 1000).toLocaleTimeString()
})

const detailLine = computed(() => {
  const s = supervisor.value
  if (!s) return ''
  const lastError = String(s.last_error ?? '')
  if (lastError) return `最近一次拉起失败：${lastError}`
  return `守护进程已被自动拉起 ${Number(s.restart_attempts ?? 0)} 次`
})
</script>

<style scoped>
.ds-badge {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 11px;
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
  cursor: default;
}

.ds-badge.is-warn {
  color: var(--color-warning);
  background: var(--color-warning-soft, rgba(230, 160, 0, 0.12));
}

.ds-badge.is-error {
  color: var(--color-danger);
  background: var(--color-danger-soft, rgba(220, 38, 38, 0.12));
}

.ds-detail {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}
</style>
