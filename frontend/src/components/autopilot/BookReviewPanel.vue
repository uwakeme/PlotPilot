<template>
  <section class="brp" aria-label="全书终审">
    <header class="brp-topbar">
      <div>
        <p class="brp-eyebrow">全书终审</p>
        <h2>完稿复查 · 分幕精审</h2>
      </div>
      <button class="brp-icon-btn" type="button" title="刷新" @click="loadReport">
        <span class="brp-refresh">⟳</span>
      </button>
    </header>

    <!-- 进度与运行控制 -->
    <div class="brp-progress-card">
      <div class="brp-progress-head">
        <span>精审进度</span>
        <strong>{{ report?.reviewed_act_count ?? 0 }} / {{ report?.total_act_count ?? 0 }} 幕</strong>
      </div>
      <div class="brp-bar">
        <div
          class="brp-bar__fill"
          :style="{ width: progressPct + '%' }"
          :class="{ 'is-running': running }"
        />
      </div>
      <div class="brp-actions">
        <button class="brp-btn brp-btn--primary" type="button" :disabled="running" @click="startReview">
          {{ startLabel }}
        </button>
        <button v-if="running" class="brp-btn" type="button" @click="stopReview">暂停</button>
        <button
          class="brp-btn"
          type="button"
          :disabled="running || !report || report.reviewed_act_count === 0"
          @click="runSynthesis"
        >
          生成汇总报告
        </button>
      </div>
      <p v-if="statusLine" class="brp-status">{{ statusLine }}</p>
      <p v-if="errorLine" class="brp-status brp-status--error">{{ errorLine }}</p>
    </div>

    <!-- 各幕列表 -->
    <div v-if="report && report.acts.length" class="brp-act-grid">
      <button
        v-for="act in report.acts"
        :key="act.act_number"
        type="button"
        class="brp-act"
        :class="{ 'is-reviewed': act.reviewed }"
        :title="act.reviewed ? '重审本幕' : '精审本幕'"
        :disabled="running"
        @click="reviewSingleAct(act.act_number)"
      >
        <span class="brp-act__no">第{{ act.act_number }}幕</span>
        <span class="brp-act__range">{{ act.chapter_start }}-{{ act.chapter_end }}章</span>
        <span class="brp-act__score" v-if="act.reviewed">{{ act.review?.quality_score ?? '-' }}</span>
        <span class="brp-act__badge" v-else>未审</span>
      </button>
    </div>

    <!-- 汇总报告 -->
    <div v-if="final" class="brp-report">
      <article class="brp-panel">
        <h3>总评</h3>
        <p class="brp-headline">{{ final.headline || '（暂无）' }}</p>
        <div class="brp-scores">
          <div class="brp-score brp-score--overall">
            <span>综合</span>
            <strong>{{ final.overall_score ?? '-' }}</strong>
          </div>
          <div v-for="(value, key) in final.scores" :key="key" class="brp-score">
            <span>{{ scoreLabel(String(key)) }}</span>
            <strong>{{ value }}</strong>
          </div>
        </div>
      </article>

      <article class="brp-panel">
        <h3>问题清单（按优先级）</h3>
        <p v-if="!final.top_issues?.length" class="brp-empty">未发现需要修复的问题。</p>
        <div
          v-for="issue in final.top_issues"
          :key="issue.priority"
          class="brp-issue"
          :class="`is-${issue.severity}`"
        >
          <div class="brp-issue__head">
            <span class="brp-sev" :class="`is-${issue.severity}`">{{ severityLabel(String(issue.severity)) }}</span>
            <span class="brp-cat">{{ categoryLabel(String(issue.category)) }}</span>
            <strong class="brp-issue__title">{{ issue.title }}</strong>
          </div>
          <p class="brp-issue__detail">{{ issue.detail }}</p>
          <p v-if="issue.quote" class="brp-issue__quote">“{{ issue.quote }}”</p>
          <p class="brp-issue__suggestion">修复方向：{{ issue.suggestion }}</p>
          <div class="brp-issue__foot">
            <span class="brp-chapters">
              涉及章节：
              <button
                v-for="num in issue.chapter_numbers"
                :key="num"
                type="button"
                class="brp-chip"
                @click="goRevise(num, issue.suggested_instruction)"
              >
                第{{ num }}章
              </button>
            </span>
            <button
              v-if="issue.chapter_numbers?.length"
              class="brp-btn brp-btn--small"
              type="button"
              @click="goRevise(issue.chapter_numbers[0], issue.suggested_instruction)"
            >
              去修改此章 →
            </button>
          </div>
        </div>
      </article>

      <article class="brp-panel" v-if="final.promise_verdicts?.length">
        <h3>未兑承诺判定</h3>
        <div v-for="(pv, index) in final.promise_verdicts" :key="index" class="brp-promise">
          <span class="brp-verdict" :class="`is-${pv.verdict}`">{{ verdictLabel(pv.verdict) }}</span>
          <span class="brp-promise__desc">ch{{ pv.planted_chapter }} {{ pv.description }}</span>
          <small class="brp-promise__reason">{{ pv.reason }}</small>
        </div>
      </article>
    </div>

    <p v-else-if="report && report.total_act_count > 0" class="brp-empty brp-empty--page">
      尚无汇总报告——完成全部（或部分）幕的精审后点击「生成汇总报告」。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  bookReviewApi,
  type BookReviewReport,
  type FinalReport,
} from '../../api/bookReview'

const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ goRevise: [chapterNumber: number, instruction: string] }>()

const report = ref<BookReviewReport | null>(null)
const final = ref<FinalReport | null>(null)
const running = ref(false)
const statusLine = ref('')
const errorLine = ref('')
let abortFlag = false

const progressPct = computed(() => {
  if (!report.value || report.value.total_act_count === 0) return 0
  return Math.round((report.value.reviewed_act_count / report.value.total_act_count) * 100)
})

const startLabel = computed(() => {
  const reviewed = report.value?.reviewed_act_count ?? 0
  const total = report.value?.total_act_count ?? 0
  if (reviewed === 0) return '开始全书精审'
  if (reviewed < total) return '继续精审'
  return '重新精审全书'
})

async function loadReport(): Promise<void> {
  try {
    report.value = await bookReviewApi.getReport(props.novelId)
    if (report.value.final_report && Object.keys(report.value.final_report).length) {
      final.value = report.value.final_report as FinalReport
    }
  } catch (err) {
    errorLine.value = `读取终审进度失败：${String(err)}`
  }
}

function stopReview(): void {
  abortFlag = true
  running.value = false
  statusLine.value = '已暂停——再次点击「继续精审」可从断点续跑。'
}

async function startReview(): Promise<void> {
  if (!report.value) await loadReport()
  const acts = (report.value?.acts ?? []).filter(act => !act.reviewed)
  if (!acts.length) {
    statusLine.value = '所有幕均已精审，可直接生成汇总报告，或点幕卡片重审。'
    return
  }
  running.value = true
  abortFlag = false
  errorLine.value = ''
  for (const act of acts) {
    if (abortFlag) return
    statusLine.value = `正在精审第 ${act.act_number} 幕（${act.chapter_start}-${act.chapter_end}章）…`
    try {
      const result = await bookReviewApi.reviewAct(props.novelId, act.act_number)
      applyActResult(result.act)
    } catch (err) {
      errorLine.value = `第 ${act.act_number} 幕精审失败：${extractError(err)}`
      running.value = false
      return
    }
  }
  running.value = false
  statusLine.value = '全部幕精审完成，可生成汇总报告。'
}

async function reviewSingleAct(actNumber: number): Promise<void> {
  running.value = true
  errorLine.value = ''
  statusLine.value = `正在精审第 ${actNumber} 幕…`
  try {
    const result = await bookReviewApi.reviewAct(props.novelId, actNumber)
    applyActResult(result.act)
    statusLine.value = `第 ${actNumber} 幕精审完成。`
  } catch (err) {
    errorLine.value = `精审失败：${extractError(err)}`
  } finally {
    running.value = false
  }
}

function applyActResult(act: { act_number: number; quality_score: number; summary: string; findings: unknown[] }): void {
  const target = report.value?.acts.find(a => a.act_number === act.act_number)
  if (target && report.value) {
    target.reviewed = true
    target.review = {
      act_number: act.act_number,
      quality_score: act.quality_score,
      summary: act.summary,
      finding_count: act.findings.length,
      updated_at: new Date().toISOString(),
    }
    report.value.reviewed_act_count = report.value.acts.filter(a => a.reviewed).length
  }
}

async function runSynthesis(): Promise<void> {
  running.value = true
  errorLine.value = ''
  statusLine.value = '正在汇总全书终审报告…'
  try {
    const result = await bookReviewApi.synthesize(props.novelId)
    final.value = result.report
    statusLine.value = '汇总报告已生成。'
  } catch (err) {
    errorLine.value = `汇总失败：${extractError(err)}`
  } finally {
    running.value = false
  }
}

function goRevise(chapterNumber: number, instruction: string): void {
  emit('goRevise', chapterNumber, instruction || '')
}

function extractError(err: unknown): string {
  const anyErr = err as { response?: { data?: { detail?: string } }; message?: string }
  return anyErr?.response?.data?.detail || anyErr?.message || String(err)
}

const SEVERITY_LABELS: Record<string, string> = { critical: '严重', major: '重要', minor: '轻微' }
const CATEGORY_LABELS: Record<string, string> = {
  logic: '逻辑', character: '人设', foreshadow: '伏笔', pacing: '节奏', style: '文笔',
}
const VERDICT_LABELS: Record<string, string> = {
  resolved_in_text: '文中已回收', open_ending: '刻意留白', dangling: '悬空待处理',
}
const SCORE_LABELS: Record<string, string> = {
  plot_logic: '逻辑', character: '人设', pacing: '节奏', foreshadow: '伏笔', prose: '文笔',
}
const severityLabel = (key: string) => SEVERITY_LABELS[key] ?? key
const categoryLabel = (key: string) => CATEGORY_LABELS[key] ?? key
const verdictLabel = (key: string) => VERDICT_LABELS[key] ?? key
const scoreLabel = (key: string) => SCORE_LABELS[key] ?? key

onMounted(() => {
  void loadReport()
})
</script>

<style scoped>
.brp { display: flex; flex-direction: column; gap: 16px; padding: 20px 24px 32px; }
.brp-topbar { display: flex; justify-content: space-between; align-items: flex-start; }
.brp-eyebrow { margin: 0; font-size: 12px; opacity: 0.6; letter-spacing: 2px; }
.brp-topbar h2 { margin: 2px 0 0; font-size: 18px; }
.brp-icon-btn { border: none; background: transparent; font-size: 18px; cursor: pointer; opacity: 0.7; }
.brp-icon-btn:hover { opacity: 1; }
.brp-refresh { display: inline-block; }

.brp-progress-card { border: 1px solid rgba(128, 128, 128, 0.2); border-radius: 10px; padding: 14px 16px; }
.brp-progress-head { display: flex; justify-content: space-between; font-size: 13px; opacity: 0.85; margin-bottom: 8px; }
.brp-bar { height: 8px; border-radius: 4px; background: rgba(128, 128, 128, 0.18); overflow: hidden; }
.brp-bar__fill { height: 100%; background: var(--color-brand, #35567e); transition: width 0.4s ease; }
.brp-bar__fill.is-running { background-image: linear-gradient(45deg, rgba(255,255,255,0.2) 25%, transparent 25%, transparent 50%, rgba(255,255,255,0.2) 50%, rgba(255,255,255,0.2) 75%, transparent 75%); background-size: 16px 16px; animation: brp-slide 1s linear infinite; }
@keyframes brp-slide { from { background-position: 0 0; } to { background-position: 16px 0; } }
.brp-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.brp-btn { border: 1px solid rgba(128,128,128,0.35); background: transparent; border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
.brp-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.brp-btn--primary { background: var(--color-brand, #35567e); border-color: var(--color-brand, #35567e); color: #fff; }
.brp-btn--small { padding: 3px 10px; font-size: 12px; }
.brp-status { margin: 10px 0 0; font-size: 12px; opacity: 0.8; }
.brp-status--error { color: #c2492e; opacity: 1; }

.brp-act-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
.brp-act { display: flex; flex-direction: column; gap: 2px; align-items: flex-start; border: 1px solid rgba(128,128,128,0.25); background: transparent; border-radius: 8px; padding: 8px 10px; cursor: pointer; font-size: 12px; }
.brp-act:hover { border-color: var(--color-brand, #35567e); }
.brp-act.is-reviewed { border-color: rgba(53, 132, 86, 0.5); }
.brp-act__no { font-weight: 600; }
.brp-act__range { opacity: 0.6; }
.brp-act__score { color: #35865a; font-weight: 600; }
.brp-act__badge { opacity: 0.5; }

.brp-report { display: flex; flex-direction: column; gap: 14px; }
.brp-panel { border: 1px solid rgba(128,128,128,0.2); border-radius: 10px; padding: 14px 16px; }
.brp-panel h3 { margin: 0 0 10px; font-size: 14px; }
.brp-headline { margin: 0; font-size: 14px; }
.brp-scores { display: flex; gap: 18px; margin-top: 12px; flex-wrap: wrap; }
.brp-score { display: flex; flex-direction: column; font-size: 12px; opacity: 0.8; }
.brp-score strong { font-size: 18px; opacity: 1; }
.brp-score--overall strong { color: var(--color-brand, #35567e); }

.brp-empty { font-size: 13px; opacity: 0.6; margin: 4px 0; }
.brp-empty--page { text-align: center; padding: 30px 0; }

.brp-issue { border-top: 1px dashed rgba(128,128,128,0.25); padding: 10px 0; }
.brp-issue__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.brp-sev { font-size: 11px; padding: 1px 8px; border-radius: 10px; color: #fff; }
.brp-sev.is-critical { background: #b0432a; }
.brp-sev.is-major { background: #b07d2a; }
.brp-sev.is-minor { background: #6b7a8c; }
.brp-cat { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid rgba(128,128,128,0.4); }
.brp-issue__title { font-size: 13px; }
.brp-issue__detail { margin: 6px 0 2px; font-size: 13px; line-height: 1.6; }
.brp-issue__quote { margin: 2px 0; font-size: 12px; opacity: 0.65; font-style: italic; }
.brp-issue__suggestion { margin: 4px 0; font-size: 12px; opacity: 0.85; }
.brp-issue__foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.brp-chapters { font-size: 12px; opacity: 0.8; display: flex; gap: 4px; align-items: center; flex-wrap: wrap; }
.brp-chip { border: 1px solid rgba(128,128,128,0.35); background: transparent; border-radius: 10px; padding: 1px 8px; font-size: 11px; cursor: pointer; }
.brp-chip:hover { border-color: var(--color-brand, #35567e); }

.brp-promise { display: flex; align-items: baseline; gap: 8px; padding: 6px 0; border-top: 1px dashed rgba(128,128,128,0.2); font-size: 12px; flex-wrap: wrap; }
.brp-verdict { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid rgba(128,128,128,0.4); white-space: nowrap; }
.brp-verdict.is-resolved_in_text { color: #35865a; border-color: rgba(53,134,90,0.5); }
.brp-verdict.is-open_ending { color: #b07d2a; border-color: rgba(176,125,42,0.5); }
.brp-verdict.is-dangling { color: #b0432a; border-color: rgba(176,67,42,0.5); }
.brp-promise__desc { flex: 1; min-width: 200px; }
.brp-promise__reason { opacity: 0.6; }
</style>
