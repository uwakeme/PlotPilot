<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AiCallItem from './AiCallItem.vue'
import { useAiStreamStore } from '@/stores/aiStreamStore'

const STORAGE_KEY = 'plotpilot:ai-stream-popup-pos'
const PANEL_W = 420
const PANEL_H = 560

const route = useRoute()
const store = useAiStreamStore()

const expanded = ref(false)
const pos = ref({ x: 0, y: 0 })

const novelId = computed(() => String(route.params.slug ?? '').trim())

const connLabel = computed(() => {
  if (store.connection === 'connected') return store.degraded ? '已连接（调用级）' : '已连接'
  if (store.connection === 'reconnecting') return '重连中…'
  if (store.connection === 'connecting') return '连接中…'
  return '未连接'
})

const idleLabel = computed(() => {
  if (!novelId.value) return '未打开书籍'
  if (store.runningCount > 0) return `● AI 调用中 ${store.runningCount}`
  return 'AI 空闲'
})

function loadPos(): void {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) {
      pos.value = { x: window.innerWidth - PANEL_W - 24, y: window.innerHeight - PANEL_H - 24 }
      return
    }
    const parsed = JSON.parse(raw) as { x?: number; y?: number }
    pos.value = {
      x: typeof parsed.x === 'number' ? parsed.x : window.innerWidth - PANEL_W - 24,
      y: typeof parsed.y === 'number' ? parsed.y : window.innerHeight - PANEL_H - 24,
    }
  } catch {
    pos.value = { x: window.innerWidth - PANEL_W - 24, y: window.innerHeight - PANEL_H - 24 }
  }
}

function savePos(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pos.value))
  } catch {
    // 隐私模式下写不进去，忽略
  }
}

const dragging = ref(false)
let dragOffset = { x: 0, y: 0 }

function onHeadPointerDown(e: PointerEvent): void {
  dragging.value = true
  dragOffset = { x: e.clientX - pos.value.x, y: e.clientY - pos.value.y }
  ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent): void {
  if (!dragging.value) return
  // 夹取一律按**展开态**的尺寸算：胶囊收起时也用同一个锚点，展开就不会顶出视口。
  // 代价是胶囊拖不到最右下角，只能拖到「展开后仍在屏内」的范围——这个取舍
  // 写在这里，别哪天有人为了「能拖到角上」把夹取改回收起态尺寸，结果展开即溢出。
  pos.value = {
    x: Math.max(8, Math.min(window.innerWidth - PANEL_W - 8, e.clientX - dragOffset.x)),
    y: Math.max(8, Math.min(window.innerHeight - PANEL_H - 8, e.clientY - dragOffset.y)),
  }
}

function onPointerUp(): void {
  if (!dragging.value) return
  dragging.value = false
  savePos()
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

onMounted(() => {
  loadPos()
  ensureSubscription(novelId.value)
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp)
})

onBeforeUnmount(() => {
  if (subscribed) {
    store.unsubscribe()
    subscribed = false
    subscribedNovelId = ''
  }
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
})
</script>

<template>
  <div
    v-if="novelId"
    class="popup"
    :class="{ expanded }"
    :style="{
      left: `${pos.x}px`,
      top: `${pos.y}px`,
      width: expanded ? `${PANEL_W}px` : undefined,
    }"
  >
    <div class="bar" @pointerdown="onHeadPointerDown">
      <span class="dot" :class="{ live: store.runningCount > 0 }" />
      <span class="title">{{ expanded ? 'AI 调用流' : idleLabel }}</span>
      <span class="conn">{{ expanded ? connLabel : '' }}</span>
      <button class="toggle" type="button" @click.stop="expanded = !expanded">
        {{ expanded ? '收起' : '展开' }}
      </button>
    </div>

    <div v-if="expanded" class="list">
      <div v-if="store.calls.length === 0" class="empty">暂无调用</div>
      <AiCallItem v-for="call in store.calls" :key="call.spanId" :call="call" />
    </div>
  </div>
</template>

<style scoped>
.popup {
  position: fixed;
  z-index: 2000;
  background: var(--n-color, #fff);
  border: 1px solid rgba(26,29,33,.12);
  border-radius: 10px;
  box-shadow: 0 6px 24px rgba(0,0,0,.14);
  overflow: hidden;
}
.bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; cursor: grab; user-select: none;
  border-bottom: 1px solid rgba(26,29,33,.06);
}
.bar:active { cursor: grabbing; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #c9cdd4; flex: none; }
.dot.live { background: #52c41a; animation: pulse 1.1s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .25; } }
.title { font-size: 12px; font-weight: 600; flex: 1; }
.conn { font-size: 11px; color: #909399; }
.toggle {
  background: none; border: 1px solid rgba(26,29,33,.14); border-radius: 6px;
  font-size: 11px; padding: 2px 8px; cursor: pointer; color: inherit;
}
.list { height: 500px; overflow-y: auto; }
.empty { padding: 24px; text-align: center; font-size: 12px; color: #909399; }
</style>