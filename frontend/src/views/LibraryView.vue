<script setup lang="ts">
/** LibraryView — 书库网格。只读：不提供删除、编辑、改设置。 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NEmpty, NIcon, NInput, NSelect, NSpin } from 'naive-ui'
import { SearchOutline } from '@vicons/ionicons5'
import { libraryApi, type LibraryBookItem, type LibrarySort } from '@/api/library'
import LibraryBookCard from '@/components/library/LibraryBookCard.vue'
import { formatApiError } from '@/utils/apiError'

const router = useRouter()
const books = ref<LibraryBookItem[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const search = ref('')
const sort = ref<LibrarySort | 'title'>('recent')

const sortOptions = [
  { label: '最近阅读', value: 'recent' },
  { label: '最近更新', value: 'updated' },
  { label: '书名', value: 'title' },
]

/** 书名与作者在前端过滤——几十本书的量级不值得走后端检索。 */
const visible = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return books.value
  return books.value.filter(
    (b) =>
      b.title.toLowerCase().includes(q) ||
      b.author.toLowerCase().includes(q) ||
      b.genre.toLowerCase().includes(q),
  )
})

/** 书名排序纯前端做，不重新请求；recent/updated 交给后端排。 */
const displayed = computed(() =>
  sort.value === 'title'
    ? [...visible.value].sort((a, b) => a.title.localeCompare(b.title, 'zh-CN'))
    : visible.value,
)

const load = async () => {
  loading.value = true
  error.value = null
  try {
    books.value = await libraryApi.listBooks(sort.value === 'title' ? 'recent' : sort.value)
  } catch (e) {
    error.value = formatApiError(e, '书库加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

// 「最近阅读 / 最近更新」是后端排序，切换即需重新取数；
// 「书名」是纯前端排序（见 displayed），切过去不该发请求。
watch(
  () => sort.value,
  (next) => {
    if (next !== 'title') void load()
  },
)

/** 直接带上「继续读到的那一章」进阅读器。 */
const openBook = (book: LibraryBookItem) => {
  if (book.chapterCount <= 0) return
  // 尽力钳制：章节被删后 lastRead.chapterNumber 可能越过 chapterCount。
  // 但 chapterCount 只数有正文的章、章号未必连续，所以这只是兜底——
  // 真正的权威判断在阅读器：它拿目录校验章号存在，不存在则走 notFound 态。
  const target = Math.max(1, Math.min(book.chapterCount, book.lastRead?.chapterNumber ?? 1))
  router.push({
    name: 'Read',
    params: { novelId: book.novelId, chapter: String(target) },
  })
}
</script>

<template>
  <div class="library">
    <header class="lib-header">
      <div>
        <h1>书库</h1>
        <p class="lib-sub">{{ books.length }} 本</p>
      </div>
      <div class="lib-tools">
        <NInput v-model:value="search" placeholder="搜索书名、作者或类型…" clearable>
          <template #prefix><NIcon><SearchOutline /></NIcon></template>
        </NInput>
        <NSelect v-model:value="sort" :options="sortOptions" style="width: 140px" />
        <NButton quaternary @click="load">刷新</NButton>
      </div>
    </header>

    <NSpin :show="loading">
      <p v-if="error" class="lib-error">{{ error }}</p>

      <NEmpty v-else-if="!loading && displayed.length === 0" description="书库里还没有书">
        <template #extra>
          <NButton type="primary" @click="router.push('/')">去首页建一本</NButton>
        </template>
      </NEmpty>

      <div v-else class="lib-grid">
        <LibraryBookCard
          v-for="book in displayed"
          :key="book.novelId"
          :book="book"
          @open="openBook(book)"
        />
      </div>
    </NSpin>
  </div>
</template>

<style scoped>
.library { max-width: 1180px; margin: 0 auto; padding: 24px 20px 64px; }
.lib-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.lib-header h1 { margin: 0; font-size: 22px; }
.lib-sub { margin: 4px 0 0; font-size: 12px; opacity: 0.6; }
.lib-tools { display: flex; align-items: center; gap: 10px; }
.lib-error { color: #d03050; font-size: 13px; }
.lib-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
</style>