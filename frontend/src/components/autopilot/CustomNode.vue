<template>
  <div
    class="dag-custom-node"
    :class="[statusClass, { 'node-selected': data.isSelected }]"
    @contextmenu.prevent="$emit('contextmenu', $event)"
  >
    <!-- 头部：图标 + 名称 + 状态徽章 -->
    <div class="node-header" :style="{ borderColor: categoryColor }">
      <span class="node-icon">{{ meta?.icon || '📦' }}</span>
      <span class="node-label">{{ data.label || meta?.display_name || data.id }}</span>
      <n-tooltip v-if="registryMissing" trigger="hover">
        <template #trigger>
          <span class="reg-miss" aria-label="类型未注册">!</span>
        </template>
        该节点类型未在已加载的注册表中找到，元数据与提示词广场可能不可用。
      </n-tooltip>
      <n-tag size="tiny" :type="statusTagType" round>{{ statusLabel }}</n-tag>
      <n-tag v-if="!data.enabled" size="tiny" type="default" round>禁用</n-tag>
    </div>

    <!-- 主体：节点类型特化渲染 -->
    <div class="node-body">
      <!-- 运行中：进度指示 -->
      <div v-if="isRunning" class="node-running-indicator">
        <n-spin :size="14" />
        <span class="running-text">执行中...</span>
        <div v-if="runState && runState.progress > 0" class="progress-bar">
          <div class="progress-fill" :style="{ width: `${(runState?.progress ?? 0) * 100}%` }" />
        </div>
      </div>

      <!-- 指标展示 -->
      <div v-else-if="displayMetrics.length > 0" class="node-metrics">
        <div v-for="m in displayMetrics" :key="m.key" class="metric-item">
          <span class="metric-key">{{ m.label }}</span>
          <span class="metric-value" :style="{ color: m.color }">{{ m.value }}</span>
        </div>
      </div>

      <!-- 默认：类型描述 -->
      <div v-else class="node-desc">
        <n-text depth="3" style="font-size: 11px">{{ meta?.display_name || data.type }}</n-text>
        <div v-if="meta?.description" class="node-description">{{ meta.description }}</div>
      </div>
    </div>

    <!-- 输入/输出端口 -->
    <div class="node-ports">
      <Handle
        v-for="port in meta?.input_ports"
        :key="`in-${port.name}`"
        type="target"
        :position="Position.Left"
        :id="port.name"
        :style="portStyle(port.data_type)"
      />
      <Handle
        v-for="port in meta?.output_ports"
        :key="`out-${port.name}`"
        type="source"
        :position="Position.Right"
        :id="port.name"
        :style="portStyle(port.data_type)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import type { NodeProps } from '@vue-flow/core'
import { useDAGStore } from '@/stores/dagStore'
import type { NodeMeta, NodeStatus, PortDataType } from '@/types/dag'
import { STATUS_LABELS, CATEGORY_COLORS } from '@/types/dag'

const props = defineProps<NodeProps>()

defineEmits<{
  contextmenu: [event: MouseEvent]
}>()

const dagStore = useDAGStore()

// ─── 计算属性 ───

const data = computed(() => props.data as {
  id: string
  type: string
  label: string
  enabled: boolean
  registryMissing?: boolean
  runState?: { status: NodeStatus; metrics: Record<string, number>; progress: number; duration_ms: number }
  isSelected: boolean
  [key: string]: unknown
})

const registryMissing = computed(() => Boolean(data.value.registryMissing))

const meta = computed((): NodeMeta | null => {
  const nodeType = data.value.type
  return dagStore.nodeTypeRegistry[nodeType] || null
})

const runState = computed(() => data.value.runState)

const status = computed((): NodeStatus => {
  if (!data.value.enabled) return 'disabled'
  return runState.value?.status || 'idle'
})

const isRunning = computed(() => status.value === 'running')

const statusClass = computed(() => `node-${status.value}`)

const statusLabel = computed(() => STATUS_LABELS[status.value] || status.value)

const statusTagType = computed(() => {
  const map: Record<string, 'default' | 'info' | 'success' | 'warning' | 'error'> = {
    idle: 'default',
    pending: 'default',
    running: 'info',
    success: 'success',
    warning: 'warning',
    error: 'error',
    bypassed: 'default',
    disabled: 'default',
    completed: 'success',
  }
  return map[status.value] || 'default'
})

const categoryColor = computed(() => {
  const cat = meta.value?.category
  return cat ? CATEGORY_COLORS[cat] : 'var(--color-brand)'
})

// ─── 指标展示 ───

const displayMetrics = computed(() => {
  if (!runState.value?.metrics) return []
  const metrics = runState.value.metrics
  const items: { key: string; label: string; value: string; color: string }[] = []

  const type = data.value.type
  // ⚠ 第 76 批（U-1'）：这里**曾经**有 val_style / val_tension / val_anti_ai
  //   三个指标分支（含 0.5 / 30 两个前端阈值）⇒ 已删。
  //   理由：它们的数据源是 **DAG 运行时的 NodeResult.metrics**，而
  //     ① `POST /dag/{novel_id}/run` 是 **501（未实现）** ⇒ 运行时根本不跑；
  //     ② 唯一的推送出口 `publish_sse_event` **没有任何调用者**；
  //     ③ 画布 SSE 推的是「共享状态投影」⇒ 只有 status/enabled，**没有 metrics**。
  //   ⇒ 三个分支**永远不会被填充**（界面看着有阈值，其实永远不显示）⇒ 死代码。
  //   ⇒ 真实的文风/张力/反 AI 指标在**仪表盘**里（走 /workbench/monitor/* 与
  //     /anti-ai/* 独立接口）⇒ 不在这里重复造一份。
  if (type === 'exec_writer') {
    // ⚠ 第 76 批（U-2'）：metrics 里可能有**布尔**（反 AI 的 severe）
    //   ⇒ 取数值前先判类型（不许把布尔硬转成数字）。
    const wordCount = metrics.word_count
    if (typeof wordCount === 'number') {
      items.push({
        key: 'word_count',
        label: '字数',
        value: String(Math.round(wordCount)),
        color: 'var(--color-info)',
      })
    }
  }

  // 通用：显示耗时
  if (runState.value.duration_ms > 0) {
    items.push({
      key: 'duration',
      label: '耗时',
      value: runState.value.duration_ms > 1000
        ? `${(runState.value.duration_ms / 1000).toFixed(1)}s`
        : `${runState.value.duration_ms}ms`,
      color: 'var(--app-text-muted)',
    })
  }

  return items.slice(0, 3)
})

// ─── 端口样式（数据类型 → CSS 变量映射）───

function portStyle(dataType: PortDataType) {
  const varMap: Record<string, string> = {
    text:    'var(--dag-port-text)',
    json:    'var(--dag-port-json)',
    score:   'var(--dag-port-score)',
    boolean: 'var(--dag-port-boolean)',
    list:    'var(--dag-port-list)',
    prompt:  'var(--dag-port-prompt)',
  }
  return {
    background: varMap[dataType] || 'var(--app-text-muted)',
    width: '8px',
    height: '8px',
    border: '2px solid var(--dag-node-bg)',
  }
}
</script>

<style scoped>
.dag-custom-node {
  min-width: 160px;
  max-width: 220px;
  border-radius: var(--app-radius-sm);
  border: 2px solid var(--dag-node-border);
  background: var(--dag-node-bg);
  font-size: var(--font-size-xs);
  transition: border-color var(--app-transition), box-shadow var(--app-transition), background var(--app-transition);
  position: relative;
  box-shadow: var(--dag-node-shadow);
  cursor: pointer;
}

/* ── 选中态 ── */
.dag-custom-node.node-selected {
  box-shadow: 0 0 0 2px var(--dag-node-selected-ring), var(--dag-node-shadow);
}

/* ── 运行态 ── */
.dag-custom-node.node-running {
  border-color: var(--color-brand);
  background: var(--color-brand-light);
  animation: dag-pulse-border 2s ease-in-out infinite;
}

/* ── 成功态 ── */
.dag-custom-node.node-success {
  border-color: var(--color-success);
  background: var(--color-success-dim);
}

/* ── 警告态 ── */
.dag-custom-node.node-warning {
  border-color: var(--color-warning);
  background: var(--color-warning-dim);
}

/* ── 错误态 ── */
.dag-custom-node.node-error {
  border-color: var(--color-danger);
  background: var(--color-danger-dim);
  animation: dag-blink-border 1s ease-in-out infinite;
}

/* ── 旁路态 ── */
.dag-custom-node.node-bypassed {
  border-color: var(--app-text-muted);
  border-style: dashed;
  background: var(--app-divider);
}

/* ── 禁用态 ── */
.dag-custom-node.node-disabled {
  border-color: var(--app-border-strong);
  background: var(--app-divider);
  opacity: 0.6;
}

/* ── 节点头部 ── */
.node-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--app-divider);
  border-top: 3px solid;
}

.node-icon {
  font-size: 14px;
}

.node-label {
  flex: 1;
  font-weight: 600;
  font-size: var(--font-size-xs);
  color: var(--app-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.reg-miss {
  cursor: help;
  font-size: 12px;
  line-height: 1;
  flex-shrink: 0;
}

/* ── 节点主体 ── */
.node-body {
  padding: 6px 10px;
  min-height: 24px;
}

.node-running-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
}

.running-text {
  font-size: 11px;
  color: var(--color-brand);
}

.progress-bar {
  flex: 1;
  height: 3px;
  background: var(--color-brand-light);
  border-radius: 2px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-brand);
  border-radius: 2px;
  transition: width 0.3s;
}

/* ── 指标区域 ── */
.node-metrics {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.metric-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
}

.metric-key {
  color: var(--app-text-muted);
}

.metric-value {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.node-desc {
  padding: 2px 0;
}

/* ── 节点描述 ── */
.node-description {
  font-size: 10px;
  color: var(--app-text-muted);
  margin-top: 2px;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
}

.node-ports {
  position: relative;
}

/* ── 动画关键帧（使用 CSS 变量） ── */
@keyframes dag-pulse-border {
  0%, 100% { border-color: var(--color-brand); }
  50%      { border-color: var(--color-brand-light); }
}

@keyframes dag-blink-border {
  0%, 100% { border-color: var(--color-danger); }
  50%      { border-color: var(--color-danger-dim); }
}
</style>
