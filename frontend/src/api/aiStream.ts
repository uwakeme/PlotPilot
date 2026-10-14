import { apiRoutes } from './endpoints'
import { fetchUrl } from './http'
import type { AiStreamEvent, AiStreamEventType } from '@/stores/aiStreamReduce'

export type { AiStreamEvent, AiStreamEventType } from '@/stores/aiStreamReduce'

export const aiStreamApi = {
  url(novelId: string): string {
    return fetchUrl(apiRoutes.aiStream.stream(novelId))
  },
}