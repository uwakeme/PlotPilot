const API_V1_ROOT = '/api/v1'

type QueryValue = string | number | boolean | null | undefined
type QueryParams = Record<string, QueryValue | QueryValue[]>

function encodeSegment(value: string | number | boolean): string {
  return encodeURIComponent(String(value))
}

function joinSegments(segments: Array<string | number | boolean>): string {
  return segments
    .filter(segment => String(segment).length > 0)
    .map(encodeSegment)
    .join('/')
}

function normalizeClientPath(path: string): string {
  return path.startsWith('/') ? path : `/${path}`
}

export function apiClientPath(...segments: Array<string | number | boolean>): string {
  return normalizeClientPath(joinSegments(segments))
}

export function apiRootPath(...segments: Array<string | number | boolean>): string {
  return `${API_V1_ROOT}${apiClientPath(...segments)}`
}

export function withQuery(path: string, params: QueryParams = {}): string {
  const query = new URLSearchParams()
  for (const [key, raw] of Object.entries(params)) {
    const values = Array.isArray(raw) ? raw : [raw]
    for (const value of values) {
      if (value === null || value === undefined || value === '') continue
      query.append(key, String(value))
    }
  }
  const qs = query.toString()
  return qs ? `${path}?${qs}` : path
}

export const apiRoutes = {
  novels: {
    // 后端把书目集合挂在 `@router.get("/")` / `@router.post("/")` 上，
    // 前缀拼接后是 `/api/v1/novels/`（**带尾斜杠**）。
    // 少写尾斜杠时 GET 靠 FastAPI 的 307 重定向能兜住，但 POST 不会被重定向，
    // 直接 404 —— 建书功能因此完全不可用。
    root: () => `${apiClientPath('novels')}/`,
    suggestTitle: () => apiClientPath('novels', 'suggest-title'),
    detail: (novelId: string) => apiClientPath('novels', novelId),
    stage: (novelId: string) => apiClientPath('novels', novelId, 'stage'),
    statistics: (novelId: string) => apiClientPath('novels', novelId, 'statistics'),
    autoApproveModeClient: (novelId: string) => apiClientPath('novels', novelId, 'auto-approve-mode'),
    autoApproveMode: (novelId: string) => apiRootPath('novels', novelId, 'auto-approve-mode'),
    chapters: (novelId: string, params?: QueryParams) =>
      withQuery(apiRootPath('novels', novelId, 'chapters'), params),
    chaptersClient: (novelId: string) => apiClientPath('novels', novelId, 'chapters'),
    setupSuggestTitleOptions: (novelId: string) =>
      apiClientPath('novels', novelId, 'setup', 'suggest-title-options'),
    chapterStream: (novelId: string) => apiRootPath('autopilot', novelId, 'chapter-stream'),
    exportNovel: (novelId: string) => apiClientPath('export', 'novel', novelId),
    exportChapter: (chapterId: string) => apiClientPath('export', 'chapter', chapterId),
  },
  autopilot: {
    root: (novelId: string) => apiRootPath('autopilot', novelId),
    status: (novelId: string) => apiRootPath('autopilot', novelId, 'status'),
    start: (novelId: string) => apiRootPath('autopilot', novelId, 'start'),
    stop: (novelId: string) => apiRootPath('autopilot', novelId, 'stop'),
    resume: (novelId: string) => apiRootPath('autopilot', novelId, 'resume'),
    stream: (novelId: string, params?: QueryParams) =>
      withQuery(apiRootPath('autopilot', novelId, 'stream'), params),
    logStream: (novelId: string) => apiRootPath('autopilot', novelId, 'log-stream'),
    circuitBreaker: (novelId: string) => apiRootPath('autopilot', novelId, 'circuit-breaker'),
    circuitBreakerReset: (novelId: string) => apiRootPath('autopilot', novelId, 'circuit-breaker', 'reset'),
  },
  aiStream: {
    stream: (novelId: string) => apiRootPath('novels', novelId, 'ai-stream'),
  },
  dag: {
    events: (novelId: string) => withQuery(apiRootPath('dag', 'events'), { novel_id: novelId }),
  },
  monitor: {
    voiceDrift: (novelId: string) => apiRootPath('novels', novelId, 'monitor', 'voice-drift'),
    tensionCurve: (novelId: string) => apiClientPath('novels', novelId, 'monitor', 'tension-curve'),
  },
  library: {
    books: (params?: QueryParams) => withQuery(apiClientPath('library', 'books'), params),
    toc: (novelId: string) => apiClientPath('library', 'books', novelId, 'toc'),
    chapter: (novelId: string, number: number) =>
      apiClientPath('library', 'books', novelId, 'chapters', number),
    progress: (novelId: string) => apiClientPath('library', 'books', novelId, 'progress'),
  },
}
