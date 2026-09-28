<script setup lang="ts">
/**
 * BookEditModal — 编辑书目基本信息（书名/作者/目标章数/梗概）。
 * 首页书卡「编辑」入口挂载；打开时拉取书目详情预填，保存走 PUT /novels/{id} 增量字段。
 */
import { computed, h, onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { novelApi, type NovelDTO } from '@/api/novel'
import { formatApiError } from '@/utils/apiError'

const props = defineProps<{
  slug: string
  show: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'saved', novel: NovelDTO): void
}>()

const message = useMessage()

const IconSparkle = () =>
  h('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', { fill: 'currentColor', d: 'M12 2l2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4z' }))

const loading = ref(false)
const saving = ref(false)
const suggesting = ref(false)

/** 书目已锁定的题材上下文，AI 起名时随梗概一起传给后端 */
const lockedGenre = ref('')
const lockedWorldPreset = ref('')
const lockedStoryStructure = ref('')

const form = reactive({
  title: '',
  author: '',
  targetChapters: null as number | null,
  premise: '',
})

/** 打开时的原始值，用于只在字段真的变化时提交 */
const original = ref<{ title: string; author: string; targetChapters: number | null; premise: string }>({
  title: '',
  author: '',
  targetChapters: null,
  premise: '',
})

const titleValid = computed(() => form.title.trim().length > 0)
const hasChanges = computed(
  () =>
    form.title.trim() !== original.value.title ||
    form.author !== original.value.author ||
    (form.targetChapters != null && form.targetChapters !== original.value.targetChapters) ||
    form.premise !== original.value.premise,
)
const canSave = computed(() => titleValid.value && hasChanges.value && !saving.value)

const loadNovel = async () => {
  loading.value = true
  try {
    const n = await novelApi.getNovel(props.slug)
    applyNovel(n)
  } catch (error: unknown) {
    message.error(formatApiError(error, '加载书目信息失败'))
    emit('update:show', false)
  } finally {
    loading.value = false
  }
}

function applyNovel(n: NovelDTO) {
  form.title = n.title ?? ''
  form.author = n.author ?? ''
  // 建书走体量档时 target_chapters 为 0（由服务端推导），此处留空不固定章数
  form.targetChapters = typeof n.target_chapters === 'number' && n.target_chapters > 0 ? n.target_chapters : null
  form.premise = n.premise ?? ''
  lockedGenre.value = n.locked_genre?.trim() ?? ''
  lockedWorldPreset.value = n.locked_world_preset?.trim() ?? ''
  lockedStoryStructure.value = n.locked_story_structure?.trim() ?? ''
  original.value = {
    title: form.title.trim(),
    author: form.author,
    targetChapters: form.targetChapters,
    premise: form.premise,
  }
}

/** AI 起名：按当前梗概生成书名填入输入框，用户仍可手改后保存（与建书留空标题同一接口） */
const suggestTitle = async () => {
  const premise = form.premise.trim()
  if (!premise || suggesting.value) return
  suggesting.value = true
  try {
    const r = await novelApi.suggestTitle({
      premise,
      genre: lockedGenre.value,
      world_preset: lockedWorldPreset.value,
      story_structure: lockedStoryStructure.value,
    })
    form.title = r.title
    if (r.source === 'ai') {
      message.success('已生成书名，可手动修改后再保存')
    } else {
      message.warning('AI 起名未成功，已用梗概截取兜底，可手动修改')
    }
  } catch (error: unknown) {
    message.error(formatApiError(error, 'AI 起名失败'))
  } finally {
    suggesting.value = false
  }
}

const save = async () => {
  if (!canSave.value) return
  saving.value = true
  try {
    const payload: {
      title?: string
      author?: string
      target_chapters?: number
      premise?: string
    } = {}
    if (form.title.trim() !== original.value.title) payload.title = form.title.trim()
    if (form.author !== original.value.author) payload.author = form.author
    if (form.targetChapters != null && form.targetChapters !== original.value.targetChapters) {
      payload.target_chapters = form.targetChapters
    }
    if (form.premise !== original.value.premise) payload.premise = form.premise

    const updated = await novelApi.updateNovel(props.slug, payload)
    message.success('书目信息已更新')
    emit('saved', updated)
    emit('update:show', false)
  } catch (error: unknown) {
    message.error(formatApiError(error, '保存失败'))
  } finally {
    saving.value = false
  }
}

const close = () => {
  if (saving.value) return
  emit('update:show', false)
}

onMounted(loadNovel)
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="编辑书目信息"
    class="book-edit-modal"
    :style="{ width: 'min(560px, 94vw)' }"
    :mask-closable="!saving"
    :close-on-esc="!saving"
    :segmented="{ content: 'soft', footer: 'soft' }"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-spin :show="loading">
      <n-form
        label-placement="top"
        :show-feedback="false"
        class="edit-form"
      >
        <n-grid :cols="2" :x-gap="16" responsive="screen">
          <n-gi>
            <n-form-item label="书名" required>
              <div class="title-row">
                <n-input
                  v-model:value="form.title"
                  placeholder="书名"
                  :maxlength="60"
                  show-count
                  :disabled="saving"
                />
                <n-button
                  size="small"
                  :loading="suggesting"
                  :disabled="saving || !form.premise.trim()"
                  title="按核心梗概生成书名，生成后可手动修改"
                  @click="suggestTitle"
                >
                  <template #icon>
                    <n-icon><IconSparkle /></n-icon>
                  </template>
                  AI 起名
                </n-button>
              </div>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item label="作者">
              <n-input
                v-model:value="form.author"
                placeholder="作者"
                :maxlength="40"
                :disabled="saving"
              />
            </n-form-item>
          </n-gi>
        </n-grid>

        <n-form-item label="目标章节数">
          <n-input-number
            v-model:value="form.targetChapters"
            :min="1"
            :max="9999"
            :disabled="saving"
            class="chapters-input"
            placeholder="留空则按体量档推导"
          >
            <template #suffix>章</template>
          </n-input-number>
        </n-form-item>

        <n-form-item label="核心梗概">
          <n-input
            v-model:value="form.premise"
            type="textarea"
            :rows="6"
            show-count
            :maxlength="2000"
            placeholder="用一段话写清主线与爽点预期…"
            :disabled="saving"
          />
        </n-form-item>

        <div class="edit-hint">
          书名可点「AI 起名」按梗概自动生成，生成后仍可手动修改；赛道 / 世界观基调等标签由梗概解析，修改梗概后书卡分类会随之更新；已生成的大纲与章节不受影响。
        </div>
      </n-form>
    </n-spin>

    <template #footer>
      <n-space justify="end">
        <n-button :disabled="saving" @click="close">取消</n-button>
        <n-button type="primary" :loading="saving" :disabled="!canSave" @click="save">
          保存
        </n-button>
      </n-space>
    </template>
  </n-modal>
</template>

<style scoped>
.edit-form {
  padding-top: 4px;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
}

.title-row .n-input {
  flex: 1;
  min-width: 0;
}

.chapters-input {
  width: 11rem;
}

.edit-hint {
  margin-top: 4px;
  padding: 0.625rem 0.875rem;
  border-radius: 0.625rem;
  border: 1px dashed var(--app-border-soft, rgba(148, 163, 184, 0.45));
  background: var(--app-surface-subtle, rgba(248, 250, 252, 0.6));
  font-size: var(--font-size-xs);
  line-height: 1.6;
  color: var(--app-text-muted);
}
</style>
