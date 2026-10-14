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
 * `chapters.status` 的取值域**不被写入路径强制**：DDL 层是
 * `status TEXT DEFAULT 'draft'`，无 NOT NULL、无 CHECK。
 *
 * 所有**已知调用方**产出的都是三值（draft / reviewing / completed，域枚举见
 * domain/novel/entities/chapter.py:10-14）：审读路径的 pending/ok/revise 会给出
 * reviewed/approved，但 chapter_service.py:236-245 在落库前就归一化
 * （approved→COMPLETED、reviewed→REVIEWING、draft→DRAFT），原始审定值另存进
 * chapter_reviews 表，而 TOC 端点只读 chapters（library_service.py:134）。
 *
 * 但归一化是**约定**、不是结构性保证，仍有绕过它的写入路径：
 * - chapter_generation_workspace.py:195-198：`except ValueError` 分支把
 *   **原始字符串**直接赋给 existing.status
 * - persistence_queue.py:607-610：一条裸的 `UPDATE chapters SET status = ?`
 *
 * 这就是 reviewed/approved 两键要留的理由——它们当前不可达，但上面任一路径被
 * 触达时，它们是唯一挡住英文原值漏进 UI 的东西。要删它们，先给
 * chapters.status 加 CHECK 约束，而不是只改前端注释。
 *
 * 用 Map 而非对象字面量：对象字面量会继承 Object.prototype，`M['toString']` 命中原型链，
 * `??` 不触发，标签会变成函数。
 */
const CHAPTER_STATUS_LABEL = new Map<string, string>([
  ['draft', '草稿'],
  ['reviewing', '审读中'],
  ['reviewed', '待修订'],
  ['approved', '已定稿'],
  ['completed', '已完稿'],
])

function toTocEntry(raw: any): LibraryTocEntry {
  const status = String(raw.status ?? 'draft')
  return {
    number: Number(raw.number),
    title: raw.title ?? '',
    wordCount: Number(raw.word_count ?? 0),
    status,
    statusLabel: CHAPTER_STATUS_LABEL.get(status) ?? status,
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