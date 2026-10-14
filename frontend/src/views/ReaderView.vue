<script setup lang="ts">
/**
 * ReaderView — 纯只读阅读器。
 *
 * 刻意不含任何写正文的控件（Global Constraint 6）：
 * 「读」与「改」是两种心智，要改正文请去 Chapter.vue。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { NButton, NDrawer, NDrawerContent, NEmpty, NSpin } from 'naive-ui'
import { libraryApi, type LibraryChapter, type LibraryTocEntry } from '@/api/library'
import { formatApiError } from '@/utils/apiError'

const route = useRoute()
const router = useRouter()

const novelId = computed(() => String(route.params.novelId ?? ''))
const routeChapter = computed(() => {
  const raw = Number(route.params.chapter)
  return Number.isFinite(raw) && raw > 0 ? raw : null
})

const loading = ref(false)
const error = ref<string | null>(null)
const notFound = ref(false)
const missingChapter = ref<number | null>(null)
const chapter = ref<LibraryChapter | null>(null)
const toc = ref<LibraryTocEntry[]>([])
const tocOpen = ref(false)

/** 同一章号不重复上报。 */
let lastReported = 0

/** 字号三档、主题三档；持久化到 localStorage。 */
const FONT_KEY = 'library.reader.fontSize'
const THEME_KEY = 'library.reader.theme'
const fontSize = ref(Number(localStorage.getItem(FONT_KEY)) || 17)
const theme = ref(localStorage.getItem(THEME_KEY) || 'paper')
const FONT_STEPS = [15, 17, 20]
const THEMES = ['paper', 'plain', 'night']

watch(fontSize, (v) => localStorage.setItem(FONT_KEY, String(v)))
watch(theme, (v) => localStorage.setItem(THEME_KEY, v))

/** marked + DOMPurify：与 Chapter.vue 同一管线，不新增依赖。 */
const rendered = computed(() => {
  const raw = (chapter.value?.content || '').replace(/\r\n/g, '\n')
  if (!raw.trim()) return ''
  return DOMPurify.sanitize(marked.parse(raw, { breaks: true, async: false }) as string)
})

const hasContent = computed(() => (chapter.value?.content || '').trim().length > 0)

const reportProgress = async (number: number) => {
  if (number === lastReported) return
  lastReported = number
  try {
    await libraryApi.recordProgress(novelId.value, number)
  } catch (e) {
    // 进度不是关键路径：静默降级，不打断阅读
    console.warn('[library] progress report failed', e)
  }
}

/**
 * 章号由路由携带，但**不能全信**。
 *
 * 书库的 `openBook` 做过一次钳制（`Math.min(chapterCount, lastRead.chapterNumber)`），
 * 但那只数有正文的章、章号未必连续——例如 chapterCount=5 而章号是 1,2,3,7,9 时，
 * 过期进度 7 会被钳到 5，而第 5 章并不存在。**所以这里的目录校验才是权威判断**，
 * 覆盖「进度指向已删章节」「书库钳制落到不存在的章」「手输越界 URL」三种情况。
 */
const loadChapter = async (number: number) => {
  loading.value = true
  error.value = null
  notFound.value = false
  missingChapter.value = null
  chapter.value = null
  try {
    toc.value = (await libraryApi.getToc(novelId.value)).chapters
    if (!toc.value.some((c) => c.number === number)) {
      // 两种「找不到」要分开：整本书没正文 vs 目标章不存在，用户的下一步不同。
      notFound.value = toc.value.length === 0
      missingChapter.value = toc.value.length === 0 ? null : number
      return
    }
    const data = await libraryApi.getChapter(novelId.value, number)
    chapter.value = data
    void reportProgress(number)
  } catch (e) {
    error.value = formatApiError(e, '章节加载失败')
  } finally {
    loading.value = false
  }
}

/** 目标章不存在时的出路：跳到目录里真实存在的第一章。 */
const startFromFirstAvailable = () => {
  const first = toc.value[0]
  if (first) goTo(first.number)
}

const goTo = (number: number | null) => {
  if (number == null) return
  tocOpen.value = false
  router.push({ name: 'Read', params: { novelId: novelId.value, chapter: String(number) } })
}

const backToLibrary = () => router.push({ name: 'Library' })

watch(routeChapter, (n) => {
  if (n != null) void loadChapter(n)
})

onMounted(() => {
  // 路由没带章号（或非法）时没有可加载的目标，与上面的 watch 同样跳过：
  // 简报里的 loadChapter(routeChapter.value) 传不进 number|null（vue-tsc 拒绝）。
  const n = routeChapter.value
  if (n != null) void loadChapter(n)
})
</script>
<template>
  <div class="reader" :class="`theme-${theme}`">
    <header class="rd-top">
      <NButton quaternary size="small" @click="backToLibrary">← 书库</NButton>
      <span class="rd-book">{{ chapter?.title ?? '书库' }}</span>
      <div class="rd-actions">
        <NButton quaternary size="small" @click="fontSize = FONT_STEPS[(FONT_STEPS.indexOf(fontSize) + 1) % FONT_STEPS.length]">
          字号
        </NButton>
        <NButton quaternary size="small" @click="theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]">
          主题
        </NButton>
        <NButton quaternary size="small" @click="tocOpen = true">目录</NButton>
      </div>
    </header>

    <NSpin :show="loading">
      <p v-if="error" class="rd-error">{{ error }}</p>

      <NEmpty
        v-else-if="missingChapter != null"
        :description="`第 ${missingChapter} 章已不存在（可能已被删除）`"
      >
        <template #extra>
          <NButton type="primary" @click="startFromFirstAvailable">从第一章开始读</NButton>
          <NButton @click="backToLibrary">返回书库</NButton>
        </template>
      </NEmpty>

      <NEmpty v-else-if="notFound" description="这本书还没有正文">
        <template #extra>
          <NButton @click="backToLibrary">返回书库</NButton>
        </template>
      </NEmpty>

      <article v-else-if="chapter" class="rd-body" :style="{ fontSize: `${fontSize}px` }">
        <h1 class="rd-chapter-title">{{ chapter.title }}</h1>
        <p v-if="!hasContent" class="rd-blank">本章尚无正文</p>
        <div v-else class="rd-prose markdown-body md-body" v-html="rendered" />

        <footer class="rd-foot">
          <NButton :disabled="chapter.prevNumber == null" @click="goTo(chapter.prevNumber)">
            上一章
          </NButton>
          <NButton @click="tocOpen = true">目录</NButton>
          <NButton :disabled="chapter.nextNumber == null" @click="goTo(chapter.nextNumber)">
            下一章
          </NButton>
        </footer>
      </article>
    </NSpin>

    <NDrawer v-model:show="tocOpen" :width="320" placement="right">
      <NDrawerContent title="目录" closable>
        <ol class="rd-toc">
          <li
            v-for="entry in toc"
            :key="entry.number"
            :class="{ 'is-current': entry.number === chapter?.number }"
            @click="goTo(entry.number)"
          >
            <span class="rd-toc-title">{{ entry.title }}</span>
            <span class="rd-toc-meta">{{ entry.wordCount }}字</span>
          </li>
        </ol>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>
<style scoped>
.reader { min-height: 100vh; }
.theme-paper { background: #f6f1e7; color: #2b2620; }
.theme-plain { background: #ffffff; color: #1f1f1f; }
.theme-night { background: #17181c; color: #d6d3cd; }
.rd-top {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 18px; position: sticky; top: 0; z-index: 2;
  backdrop-filter: blur(8px);
}
.rd-book { flex: 1; font-size: 13px; opacity: 0.7; text-align: center; }
.rd-actions { display: flex; gap: 4px; }
.rd-body { max-width: 680px; margin: 0 auto; padding: 24px 20px 96px; }
.rd-chapter-title { font-size: 20px; margin: 0 0 28px; }
.rd-prose { line-height: 1.9; }
.rd-blank { opacity: 0.6; }
.rd-foot { display: flex; justify-content: space-between; margin-top: 56px; }
.rd-error { color: #d03050; padding: 24px; }
.rd-toc { list-style: none; margin: 0; padding: 0; }
.rd-toc li {
  display: flex; justify-content: space-between; gap: 12px;
  padding: 9px 10px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.rd-toc li:hover { background: rgba(128, 128, 128, 0.14); }
.rd-toc li.is-current { background: rgba(128, 128, 128, 0.22); font-weight: 600; }
.rd-toc-meta { opacity: 0.55; flex-shrink: 0; }
</style>
