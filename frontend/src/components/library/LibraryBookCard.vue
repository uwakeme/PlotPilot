<script setup lang="ts">
/**
 * LibraryBookCard — 书库卡片。
 *
 * 与首页 BookCard 刻意不复用：首页卡片服务管理（多选/删除/编辑），
 * 这里服务阅读（封面/进度/继续阅读）。
 */
import { computed } from 'vue'
import { NButton, NTag } from 'naive-ui'
import type { LibraryBookItem } from '@/api/library'

const props = defineProps<{ book: LibraryBookItem }>()
const emit = defineEmits<{ (e: 'open'): void }>()

/** 色相由 novelId 派生：同一本书每次渲染颜色稳定。 */
const hue = computed(() => {
  let h = 0
  for (let i = 0; i < props.book.novelId.length; i += 1) {
    h = (h * 31 + props.book.novelId.charCodeAt(i)) % 360
  }
  return h
})

const coverStyle = computed(() => ({
  background: `linear-gradient(160deg, hsl(${hue.value} 32% 34%), hsl(${(hue.value + 40) % 360} 28% 20%))`,
}))

/** 进度比例钳制在 [0, 1]：章节被删后 lastRead 可能超出 chapterCount。 */
const progress = computed(() => {
  const total = props.book.chapterCount
  if (total <= 0) return 0
  const current = props.book.lastRead?.chapterNumber ?? 0
  return Math.min(1, Math.max(0, current / total))
})

const readable = computed(() => props.book.chapterCount > 0)

const continueLabel = computed(() =>
  props.book.lastRead ? `继续阅读 · 第${props.book.lastRead.chapterNumber}章` : '开始阅读',
)
</script>

<template>
  <article class="lib-card" :class="{ 'is-empty': !readable }" @click="emit('open')">
    <div class="lib-cover" :style="coverStyle">
      <span class="lib-cover-title">{{ book.title }}</span>
    </div>

    <div class="lib-body">
      <div class="lib-tags">
        <NTag v-if="book.genre" size="small" :bordered="false">{{ book.genre }}</NTag>
        <NTag v-if="book.worldPreset" size="small" :bordered="false" type="info">
          {{ book.worldPreset }}
        </NTag>
      </div>

      <h3 class="lib-title">{{ book.title }}</h3>
      <p class="lib-meta">
        {{ readable ? `第${book.lastRead?.chapterNumber ?? 0}章 · 共${book.chapterCount}章` : '尚未开写' }}
        · {{ (book.totalWordCount / 10000).toFixed(1) }}万字
      </p>

      <div class="lib-progress" role="progressbar" :aria-valuenow="Math.round(progress * 100)">
        <div class="lib-progress-fill" :style="{ width: `${progress * 100}%` }" />
      </div>

      <NButton
        type="primary"
        round
        block
        :disabled="!readable"
        @click.stop="emit('open')"
      >
        {{ continueLabel }}
      </NButton>
    </div>
  </article>
</template>

<style scoped>
.lib-card {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 14px;
  padding: 12px;
  border-radius: 10px;
  cursor: pointer;
  transition: transform 0.16s ease, box-shadow 0.16s ease;
}
.lib-card:hover { transform: translateY(-2px); }
.lib-card.is-empty { opacity: 0.55; cursor: default; }
.lib-cover {
  aspect-ratio: 3 / 4;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 8px;
}
.lib-cover-title {
  writing-mode: vertical-rl;
  color: rgba(255, 255, 255, 0.92);
  font-size: 14px;
  letter-spacing: 2px;
}
.lib-body { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.lib-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.lib-title { margin: 0; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lib-meta { margin: 0; font-size: 12px; opacity: 0.65; }
.lib-progress { height: 3px; border-radius: 2px; background: rgba(128, 128, 128, 0.22); }
.lib-progress-fill { height: 100%; border-radius: 2px; background: currentColor; opacity: 0.7; }
</style>