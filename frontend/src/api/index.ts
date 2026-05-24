import axios from 'axios'
import type { Session, ScoreResult } from './types'

const http = axios.create({ baseURL: '/api' })

export async function fetchSessions(): Promise<Session[]> {
  const { data } = await http.get<Session[]>('/sessions/')
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
