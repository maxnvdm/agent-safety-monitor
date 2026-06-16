import axios from 'axios'
import type { Session, ScoreResult, SessionFilters } from './types'

const http = axios.create({ baseURL: '/api' })

export async function fetchSessions(filters: SessionFilters = {}): Promise<Session[]> {
  const params: Record<string, string> = {}
  if (filters.failed_only) params.failed_only = 'true'
  if (filters.scorer) params.scorer = filters.scorer
  if (filters.branch) params.branch = filters.branch
  if (filters.cwd) params.cwd = filters.cwd
  const { data } = await http.get<Session[]>('/sessions/', { params })
  return data
}

export async function fetchSession(id: string): Promise<Session> {
  const { data } = await http.get<Session>(`/sessions/${id}`)
  return data
}

export async function fetchResults(sessionId: string): Promise<ScoreResult[]> {
  const { data } = await http.get<ScoreResult[]>(`/results/${sessionId}`)
  return data
}

export async function fetchTranscript(sessionId: string): Promise<string> {
  const { data } = await http.get<{ transcript: string }>(`/sessions/${sessionId}/transcript`)
  return data.transcript
}

export async function markResultSafe(
  sessionId: string,
  scorerName: string,
  safe: boolean,
): Promise<void> {
  await http.patch(`/sessions/${sessionId}/results/${scorerName}`, { marked_safe: safe })
}
