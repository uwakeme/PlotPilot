<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AiCallItem from './AiCallItem.vue'
import { useAiStreamStore } from '@/stores/aiStreamStore'

const STORAGE_KEY = 'plotpilot:ai-stream-popup-pos'
const PANEL_W = 420
const PANEL_H = 560
/** 胶囊态宽度只是夹取用的估算值，多留一点余量免得贴边时算松。 */
const CAPSULE_W = 190
const CAPSULE_H = 40
const EDGE = 16
/** 位移小于这个值视为「点击」而不是「拖拽」。 */
const CLICK_SLOP = 4

const route = useRoute()
const store = useAiStreamStore()

const expanded = ref(false)

/**
 * pos 存的是「距视口右/下边缘的距离」，不是绝对坐标。
 * 面板始终以**右下角**为锚点向左上生长，因此展开永远不会溢出视口，
 * 收起时也真正贴在角上——之前用展开态左上角做锚点，胶囊会浮在屏幕右侧中间。
 * 用边距而非坐标，窗口缩放时不需要重算就能保持贴边。
 */
const pos = ref({ right: EDGE, bottom: EDGE })

const novelId = computed(() => String(route.params.slug ?? '').trim())

const boxW = computed(() => (expanded.value ? PANEL_W : CAPSULE_W))
const boxH = computed(() => (expanded.value ? PANEL_H : CAPSULE_H))

function clampPos(next: { right: number; bottom: number }): { right: number; bottom: number } {
  const maxRight = Math.max(EDGE, window.innerWidth - EDGE - boxW.value)
  const maxBottom = Math.max(EDGE, window.innerHeight - EDGE - boxH.value)
  return {
    right: Math.min(Math.max(next.right, EDGE), maxRight),
    bottom: Math.min(Math.max(next.bottom, EDGE), maxBottom),
  }
}

function loadPos(): void {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { right?: number; bottom?: number }
      if (typeof parsed.right === 'number' && typeof parsed.bottom === 'number') {
        pos.value = clampPos({ right: parsed.right, bottom: parsed.bottom })
        return
      }
    }
  } catch {
    // localStorage 不可用（隐私模式）或数据损坏，走默认贴边。
  }
  pos.value = { right: EDGE, bottom: EDGE }
}

function savePos(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pos.value))
  } catch {
    // 隐私模式下写不进去，忽略
  }
}

const idleLabel = computed(() => {
  if (store.runningCount > 0) return `AI 调用中 ${store.runningCount}`
  return 'AI 空闲'
})

const connLabel = computed(() => {
  if (store.connection === 'connected') return store.degraded ? '已连接（调用级）' : '已连接'
  if (store.connection === 'reconnecting') return '重连中…'
  if (store.connection === 'connecting') return '连接中…'
  return '未连接'
})

const connTone = computed(() => {
  if (store.connection === 'connected') return store.degraded ? 'warn' : 'ok'
  if (store.connection === 'connecting') return 'idle'
  if (store.connection === 'reconnecting') return 'warn'
  return 'bad'
})

const doneCount = computed(() => store.calls.filter((c) => c.status === 'done').length)
const errorCount = computed(() => store.calls.filter((c) => c.status === 'error').length)
const tokenLabel = computed(() => {
  const total = store.calls.reduce((sum, c) => sum + (c.tokenIn ?? 0) + (c.tokenOut ?? 0), 0)
  if (!total) return '—'
  return total >= 1000 ? `${(total / 1000).toFixed(1)}k` : String(total)
})

const dragging = ref(false)
let dragOrigin: { x: number; y: number; right: number; bottom: number } | null = null
let moved = false

function onHeadPointerDown(e: PointerEvent): void {
  if (e.button !== 0) return
  dragging.value = true
  moved = false
  dragOrigin = { x: e.clientX, y: e.clientY, right: pos.value.right, bottom: pos.value.bottom }
  ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  e.preventDefault()
}

function onPointerMove(e: PointerEvent): void {
  if (!dragging.value || !dragOrigin) return
  const dx = e.clientX - dragOrigin.x
  const dy = e.clientY - dragOrigin.y
  if (Math.abs(dx) > CLICK_SLOP || Math.abs(dy) > CLICK_SLOP) moved = true
  // 向右拖 → 距右边缘变小，所以是减去位移。
  pos.value = clampPos({ right: dragOrigin.right - dx, bottom: dragOrigin.bottom - dy })
}

function onPointerUp(): void {
  if (!dragging.value) return
  dragging.value = false
  dragOrigin = null
  if (moved) savePos()
  else expanded.value = !expanded.value
}

function onResize(): void {
  pos.value = clampPos(pos.value)
}

let subscribedNovelId = ''
let subscribed = false

/** 保证始终只有一个 subscribe/unsubscribe 配对，避免切书时消费者计数漂移。 */
function ensureSubscription(id: string): void {
  if (!id) {
    if (subscribed) {
      store.unsubscribe()
      subscribed = false
      subscribedNovelId = ''
    }
    return
  }
  if (subscribed && subscribedNovelId === id) return
  if (subscribed) store.unsubscribe()
  store.subscribe(id)
  subscribed = true
  subscribedNovelId = id
}

watch(novelId, (id) => ensureSubscription(id))
// 展开时把之前收起态允许的「更靠左」位置收回来，保证整块面板在屏内。
watch(expanded, () => {
  pos.value = clampPos(pos.value)
})

onMounted(() => {
  loadPos()
  ensureSubscription(novelId.value)
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp)
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  if (subscribed) {
    store.unsubscribe()
    subscribed = false
    subscribedNovelId = ''
  }
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('resize', onResize)
})
</script>

<template>
  <div
    v-if="novelId"
    class="popup"
    :class="{ expanded, dragging }"
    :style="{ right: `${pos.right}px`, bottom: `${pos.bottom}px` }"
  >
    <button
      class="bar"
      type="button"
      :aria-expanded="expanded"
      title="拖动移动，点击展开或收起"
      @pointerdown="onHeadPointerDown"
    >
      <span class="dot" :class="{ live: store.runningCount > 0 }" />
      <span class="title">{{ expanded ? 'AI 调用流' : idleLabel }}</span>
      <span v-if="expanded" class="conn" :class="`tone-${connTone}`">{{ connLabel }}</span>
      <span class="caret" aria-hidden="true">{{ expanded ? '▾' : '▴' }}</span>
    </button>

    <div v-if="expanded" class="meter" aria-hidden="true">
      <span class="metric"><b>{{ store.runningCount }}</b>进行中</span>
      <span class="metric"><b>{{ doneCount }}</b>完成</span>
      <span class="metric" :class="{ bad: errorCount > 0 }"><b>{{ errorCount }}</b>失败</span>
      <span class="metric"><b class="mono">{{ tokenLabel }}</b>token</span>
    </div>

    <div v-if="expanded" class="list">
      <div v-if="store.calls.length === 0" class="empty">
        暂无调用<br /><span class="empty-hint">开始托管写作后，这里会实时滚动每一次 AI 调用的输入输出。</span>
      </div>
      <AiCallItem v-for="call in store.calls" :key="call.spanId" :call="call" />
    </div>
  </div>
</template>

<style scoped>
.popup {
  position: fixed;
  /* 压在应用框架之上、Naive 的 modal/drawer（2000+）之下 */
  z-index: 1900;
  display: flex;
  flex-direction: column;
  width: max-content;
  background: var(--app-surface-raised);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  box-shadow: var(--app-shadow-lg);
  color: var(--app-text-primary);
  overflow: hidden;
}
.popup.expanded { width: min(420px, calc(100vw - 32px)); }

.bar {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 12px;
  background: none;
  border: 0;
  cursor: grab;
  user-select: none;
  font: inherit;
  font-size: var(--font-size-sm);
  color: inherit;
  text-align: left;
}
.dragging .bar { cursor: grabbing; }
.bar:hover { background: var(--app-surface-subtle); }
.bar:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: -2px;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--app-text-muted);
  flex: none;
}
.dot.live { background: var(--color-success); animation: breathe 1.4s ease-in-out infinite; }
@keyframes breathe {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: .35; transform: scale(.82); }
}

.title { font-weight: 600; white-space: nowrap; }
.conn { font-size: var(--font-size-xs); margin-left: auto; }
.conn.tone-ok { color: var(--app-text-muted); }
.conn.tone-warn { color: var(--color-warning); }
.conn.tone-bad { color: var(--color-danger); }
.conn.tone-idle { color: var(--app-text-muted); }

.caret {
  font-size: 10px;
  color: var(--app-text-muted);
  line-height: 1;
  padding-left: 2px;
}

/* 仪表条：这是整个面板唯一「仪表化」的地方，用计数而不是装饰。 */
.meter {
  display: flex;
  gap: 14px;
  padding: 0 12px 10px;
  font-size: var(--font-size-xs);
  color: var(--app-text-muted);
  border-bottom: 1px solid var(--app-divider);
}
.metric { display: flex; align-items: baseline; gap: 4px; }
.metric b {
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--app-text-secondary);
}
.metric.bad b { color: var(--color-danger); }
.mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }

.list {
  height: min(500px, calc(100vh - 260px));
  overflow-y: auto;
  overscroll-behavior: contain;
}

.empty {
  padding: 28px 20px;
  text-align: center;
  font-size: var(--font-size-sm);
  color: var(--app-text-muted);
  line-height: 1.7;
}
.empty-hint { font-size: var(--font-size-xs); }

@media (prefers-reduced-motion: reduce) {
  .dot.live { animation: none; }
}
</style>
