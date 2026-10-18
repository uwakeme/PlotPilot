<template>
  <div class="voice-drift-indicator">
    <div class="indicator-header">
      <span class="indicator-title">🎭 文风警报器</span>
      <n-button v-if="isDanger" size="tiny" type="error" @click="showDetail">
        查看详情
      </n-button>
    </div>

    <div class="indicator-body">
      <!-- 圆形进度指示器 -->
      <div class="progress-circle">
        <n-progress
          type="circle"
          :percentage="driftPercentage"
          :color="driftColor"
          :rail-color="railColor"
          :stroke-width="8"
          :show-indicator="false"
          :style="{ width: '76px', height: '76px' }"
        />
        <div class="progress-center">
          <div class="drift-icon">{{ driftIcon }}</div>
          <div class="drift-score">
            <template v-if="driftScore !== null">{{ driftScore.toFixed(1) }}</template>
            <template v-else>--</template>
          </div>
        </div>
      </div>

      <!-- 状态信息 -->
      <div class="status-info">
        <n-text :type="driftTextType" class="status-label">
          {{ driftLabel }}
        </n-text>
        <n-text depth="3" class="status-desc">
          {{ driftDescription }}
        </n-text>

        <!-- 最近检测时间 -->
        <n-text v-if="lastCheckTime" depth="3" class="last-check">
          最近检测: {{ formatTime(lastCheckTime) }}
        </n-text>
      </div>
    </div>

    <!-- 详情弹窗 -->
    <n-modal
      v-model:show="showDetailModal"
      preset="card"
      title="文风偏移详情"
      style="width: 600px"
    >
      <n-space vertical :size="12">
        <n-descriptions :column="2" bordered size="small">
          <n-descriptions-item label="当前偏移值">
            <n-text :type="driftTextType">
              <template v-if="driftScore !== null">{{ driftScore.toFixed(2) }}</template>
              <template v-else>未评估</template>
            </n-text>
          </n-descriptions-item>
          <n-descriptions-item label="告警阈值">
            <n-text depth="3">
              警告 ≥ {{ warningThreshold }} / 严重 ≥ {{ criticalThreshold }}
            </n-text>
          </n-descriptions-item>
          <n-descriptions-item label="检测章节">
            <template v-if="lastCheckChapter > 0">第 {{ lastCheckChapter }} 章</template>
            <template v-else>--</template>
          </n-descriptions-item>
          <n-descriptions-item label="基准样本">
            {{ driftSampleCount }} 章
          </n-descriptions-item>
          <n-descriptions-item label="状态" :span="2">
            <n-tag :type="driftTextType" size="small">{{ driftLabel }}</n-tag>
          </n-descriptions-item>
          <n-descriptions-item v-if="driftReason" label="未评估原因" :span="2">
            <n-text depth="3">{{ driftReason }}</n-text>
          </n-descriptions-item>
        </n-descriptions>

        <n-card v-if="driftDetails" title="偏移分析" size="small">
          <n-space vertical :size="8">
            <div v-for="(item, index) in driftDetails" :key="index" class="drift-item">
              <n-text depth="2">{{ item.dimension }}:</n-text>
              <n-text :type="item.severity">{{ item.description }}</n-text>
            </div>
          </n-space>
        </n-card>

        <n-alert v-if="isDanger" type="error" :show-icon="true">
          <template #header>建议操作</template>
          <n-ul>
            <n-li>考虑回滚到最近的语义快照</n-li>
            <n-li>检查并调整 AI Prompt 参数</n-li>
            <n-li>手动审阅最近生成的章节</n-li>
          </n-ul>
        </n-alert>
      </n-space>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { monitorApi } from '@/api/monitor'
import { usePolling } from '@/composables/usePolling'
import { runtimePerformance } from '@/config/performance'

interface VoiceDriftData {
  /** 漂移度 0-10（前端刻度），**越高越糟**；未评估时为 null */
  drift_score: number | null
  evaluated: boolean
  /** 未评估原因；已评估时为空 */
  reason: string
  /** 真实的基准样本数 */
  sample_count: number
  last_check_chapter: number
  /** 本次评分的真实计算时刻；null = 未评估（**不得**用拉取时刻顶替） */
  last_check_time: string | null
  /** 告警阈值（0-1 刻度），由后端下发 —— 前端不再另定一套 */
  warning_threshold: number
  critical_threshold: number
  /** 后端判定的状态，是唯一事实源 */
  status: string
  details?: Array<{
    dimension: string
    description: string
    severity: 'default' | 'warning' | 'error'
  }>
}

const props = defineProps<{
  novelId: string
  refreshKey?: number  // 🔥 刷新信号，变化时重新拉数据
}>()

const emit = defineEmits<{
  'drift-alert': [score: number, status: string]
}>()

const driftData = ref<VoiceDriftData | null>(null)
const showDetailModal = ref(false)
const loading = ref(false)

// 告警阈值 —— **由后端下发**，不再由前端另定一套。
// 后端判定用 0-1 刻度（0.3/0.5）；前端此前用 0-10 刻度（3.0/6.0），
// drift ∈ [0.5, 0.6) 时两边结论不同，且展示给用户的「安全阈值 3.0」是假信息。
const warningThreshold = computed(() => driftData.value?.warning_threshold ?? 0.3)
const criticalThreshold = computed(() => driftData.value?.critical_threshold ?? 0.5)

/** 是否完成过真实评估 —— 未评估时**不得**把 drift_score 当 0 处理 */
const evaluated = computed(() => driftData.value?.evaluated === true)

// 偏移分数：未评估时保持 null，绝不回填 0
const driftScore = computed<number | null>(() => {
  if (!evaluated.value) return null
  return driftData.value?.drift_score ?? null
})

// 偏移百分比（用于圆形进度条，最大值为 10）；未评估时环为空
const driftPercentage = computed(() => {
  const s = driftScore.value
  return s === null ? 0 : Math.min((s / 10) * 100, 100)
})

// 状态 —— **后端 status 是唯一事实源**。
// 此前前端拿自己的 3.0/6.0 阈值（0-10 刻度，因为 drift_score 被乘了 10）
// 重算，而后端用的是 0.3/0.5（0-1 刻度）。drift ∈ [0.5, 0.6) 时
// 后端判 critical、前端显示「轻微偏离」—— 两边对同一个数给出不同结论。
const driftStatus = computed(() => {
  if (driftScore.value === null) return 'unevaluated'
  switch (driftData.value?.status) {
    case 'critical': return 'danger'
    case 'warning': return 'warning'
    case 'normal': return 'safe'
    default: return 'unevaluated'
  }
})

const isDanger = computed(() => driftStatus.value === 'danger')
const isWarning = computed(() => driftStatus.value === 'warning')
const isSafe = computed(() => driftStatus.value === 'safe')
const isUnevaluated = computed(() => driftStatus.value === 'unevaluated')

// 颜色
const driftColor = computed(() => {
  if (isDanger.value) return '#d03050'
  if (isWarning.value) return '#f0a020'
  if (isUnevaluated.value) return '#8a8f99'
  return '#18a058'
})

const railColor = computed(() => {
  return 'rgba(255, 255, 255, 0.1)'
})

// 图标
const driftIcon = computed(() => {
  if (isDanger.value) return '!'
  if (isWarning.value) return '~'
  if (isUnevaluated.value) return '?'
  return '✓'
})

// 标签
const driftLabel = computed(() => {
  if (isDanger.value) return '严重偏离'
  if (isWarning.value) return '轻微偏离'
  if (isUnevaluated.value) return '尚未检测'
  return '文风稳定'
})

// 描述
const driftDescription = computed(() => {
  if (isDanger.value) return '文风与基准差异过大，建议立即处理'
  if (isWarning.value) return '检测到文风波动，请注意观察'
  if (isUnevaluated.value) return driftData.value?.reason || '尚未积累足够的文风基准样本'
  return '文风保持一致，无需干预'
})

// 文本类型 —— 未评估用 default（灰），不冒充 success（绿）
const driftTextType = computed(() => {
  if (isDanger.value) return 'error'
  if (isWarning.value) return 'warning'
  if (isUnevaluated.value) return 'default'
  return 'success'
})

// 最近检测时间
const lastCheckTime = computed(() => driftData.value?.last_check_time)
const lastCheckChapter = computed(() => driftData.value?.last_check_chapter ?? 0)

// 偏移详情
const driftDetails = computed(() => driftData.value?.details ?? [])

// 真实的基准样本数（后端给，不是编的 10）
const driftSampleCount = computed(() => driftData.value?.sample_count ?? 0)

// 未评估原因
const driftReason = computed(() => (evaluated.value ? '' : driftData.value?.reason ?? ''))

// 加载文风偏移数据
async function loadDriftData() {
  loading.value = true
  try {
    const res = await monitorApi.getVoiceDrift(props.novelId)
    if (res && typeof res.evaluated === 'boolean') {
      // 🔥 后端返回的是单对象（整本书一个漂移度），不是数组 ——
      // 旧实现按「每个角色一条」构造，但 Novel 实体根本没有 characters 属性，
      // 那个数组恒为空，这里只是把「无数据」伪装成「文风稳定 0.0」。
      driftData.value = {
        // 后端 drift 是 0-1（越高越糟），前端刻度是 0-10
        drift_score: res.evaluated && typeof res.drift_score === 'number'
          ? res.drift_score * 10
          : null,
        evaluated: res.evaluated,
        reason: res.reason ?? '',
        sample_count: res.sample_count ?? 0,
        last_check_chapter: res.last_scored_chapter ?? 0,
        // 后端给的**真实**评分时刻。此前填 `new Date().toISOString()` ——
        // 那是「前端拉取的时刻」，被当成「检测时刻」展示；
        // 未评估时更会显示「最近检测=现在」，等于凭空造一个检测记录。
        last_check_time: res.computed_at ?? null,
        warning_threshold: res.warning_threshold ?? 0.3,
        critical_threshold: res.critical_threshold ?? 0.5,
        status: res.status ?? 'unevaluated',
        details: []
      }

      // 触发警报 —— 只有真正评估过才可能触发
      if (isDanger.value || isWarning.value) {
        const s = driftScore.value
        if (s !== null) emit('drift-alert', s, driftStatus.value)
      }
    }
  } catch (err) {
    console.error('Failed to load voice drift data:', err)
  } finally {
    loading.value = false
  }
}

// 显示详情
function showDetail() {
  showDetailModal.value = true
}

// 格式化时间
function formatTime(timestamp: string): string {
  try {
    const date = new Date(timestamp)
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    })
  } catch {
    return '--'
  }
}

const polling = usePolling(loadDriftData, runtimePerformance.autopilotMetrics.voiceDriftPollMs)

// 监听
watch(() => props.novelId, () => {
  polling.restart({ immediate: true })
})

// 🔥 刷新信号变化时重新加载（由 Dashboard 的 SSE 事件驱动）
watch(() => props.refreshKey, (newKey) => {
  if (newKey && newKey > 0) void loadDriftData()
})

// 生命周期
onMounted(() => {
  polling.start({ immediate: true })
})
</script>

<style scoped>
.voice-drift-indicator {
  background: var(--card-color);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  padding: 14px 16px;
}

.indicator-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.indicator-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-color-1);
}

.indicator-body {
  display: flex;
  align-items: flex-start;
  gap: 18px;
}

.progress-circle {
  position: relative;
  flex-shrink: 0;
}

.progress-center {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
}

.drift-icon {
  font-size: 18px;
  margin-bottom: 2px;
}

.drift-score {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-color-1);
  font-variant-numeric: tabular-nums;
}

.status-info {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
  min-width: 0;
  padding-top: 2px;
}

.status-label {
  font-size: 14px;
  font-weight: 600;
}

.status-desc {
  font-size: 12px;
  line-height: 1.55;
}

.last-check {
  font-size: 11px;
  margin-top: 4px;
}

.drift-item {
  display: flex;
  gap: 8px;
  font-size: 13px;
  line-height: 1.6;
}
</style>
