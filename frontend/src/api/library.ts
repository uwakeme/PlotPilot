import { apiClient } from './config'
import { apiRoutes } from './endpoints'

export type LibrarySort = 'recent' | 'updated'

export interface LibraryLastRead {
  chapterNumber: number
  chapterTitle: string
  readAt: string | null
}

export interface LibraryBookItem {
  novelId: string
  title: string
  author: string
  genre: string
  worldPreset: string
  stage: string
  chapterCount: number
  targetChapters: number
  totalWordCount: number
  lastRead: LibraryLastRead | null
}

export interface LibraryTocEntry {
  number: number
  title: string
  wordCount: number
  status: string
  /** 面向读者的中文状态标签；未知取值回落到 status 原文。 */
  statusLabel: string
}

export interface LibraryToc {
  novelId: string
  title: string
  chapters: LibraryTocEntry[]
}

export interface LibraryChapter {
  novelId: string
  number: number
  title: string
  content: string
  wordCount: number
  prevNumber: number | null
  nextNumber: number | null
}

/**
 * 书库路由统一返回 `{ success, data }` 信封，而 apiClient 只做 `response.data`
 * （axios 层）拆包，**不再**拆这一层信封。漏拆会让 r.books 直接是 undefined。
 * 这里显式取 data；若某天后端改成裸返回，则原样透传，两种形态都吃得下。
 */
function unwrap<T>(body: unknown): T {
  if (body && typeof body === 'object' && 'data' in (body as Record<string, unknown>)) {
    return (body as { data: T }).data
  }
  return body as T
}

/** 后端 snake_case → 前端 camelCase。书库专属，不动全局转换逻辑。 */
function toLastRead(raw: any): LibraryLastRead | null {
  // 注意字段名：服务层 _to_book_item 把它改名成 chapter_number 了
  // （last_chapter_number 只是 SQL 里的列别名，不是对外字段）。
  if (!raw || raw.chapter_number == null) return null
  return {
    chapterNumber: Number(raw.chapter_number),
    chapterTitle: String(raw.chapter_title ?? ''),
    readAt: raw.read_at ?? null,
  }
}

function toBookItem(raw: any): LibraryBookItem {
  return {
    novelId: raw.novel_id,
    title: raw.title ?? '',
    author: raw.author ?? '',
    genre: raw.genre ?? '',
    worldPreset: raw.world_preset ?? '',
    stage: raw.stage ?? 'planning',
    chapterCount: Number(raw.chapter_count ?? 0),
    targetChapters: Number(raw.target_chapters ?? 0),
    totalWordCount: Number(raw.total_word_count ?? 0),
    lastRead: toLastRead(raw.last_read),
  }
}

/**
 * 章节状态标签。
 *
 * `chapters.status` 有两条写入路径，取值域是**并集**，缺任何一项都会让真实数据漏出英文原值：
 * - 流水线 / 域枚举（domain/novel/entities/chapter.py:10-14）：draft / reviewing / completed
 * - 审读保存（Chapter.vue:240-247 的 statusToNew，由 pending/ok/revise 转换）：draft / reviewed / approved
 *
 * 措辞沿用仓库既有中文：approved 取 Chapter.vue:98 的「已定稿」(ok)，
 * reviewed 对应 Chapter.vue:99 的「需修订」(revise)。
 */
const CHAPTER_STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  reviewing: '审读中',
  reviewed: '待修订',
  approved: '已定稿',
  completed: '已完稿',
}

function toTocEntry(raw: any): LibraryTocEntry {
  const status = String(raw.status ?? 'draft')
  return {
    number: Number(raw.number),
    title: raw.title ?? '',
    wordCount: Number(raw.word_count ?? 0),
    status,
    statusLabel: CHAPTER_STATUS_LABEL[status] ?? status,
  }
}

function toChapter(raw: any): LibraryChapter {
  return {
    novelId: raw.novel_id,
    number: Number(raw.number),
    title: raw.title ?? '',
    content: raw.content ?? '',
    wordCount: Number(raw.word_count ?? 0),
    prevNumber: raw.prev_number == null ? null : Number(raw.prev_number),
    nextNumber: raw.next_number == null ? null : Number(raw.next_number),
  }
}

export const libraryApi = {
  listBooks: async (sort: LibrarySort = 'recent'): Promise<LibraryBookItem[]> => {
    const data = unwrap<{ books?: any[] }>(await apiClient.get<unknown>(apiRoutes.library.books({ sort })))
    return (data.books ?? []).map(toBookItem)
  },

  getToc: async (novelId: string): Promise<LibraryToc> => {
    const data = unwrap<any>(await apiClient.get<unknown>(apiRoutes.library.toc(novelId)))
    return {
      novelId: data.novel_id,
      title: data.title ?? '',
      chapters: (data.chapters ?? []).map(toTocEntry),
    }
  },

  getChapter: async (novelId: string, number: number): Promise<LibraryChapter> =>
    toChapter(unwrap<any>(await apiClient.get<unknown>(apiRoutes.library.chapter(novelId, number)))),

  recordProgress: async (novelId: string, chapterNumber: number): Promise<void> => {
    await apiClient.put(apiRoutes.library.progress(novelId), { chapter_number: chapterNumber })
  },
}