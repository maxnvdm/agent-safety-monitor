export interface Session {
  id: string
  log_path: string
  cwd: string | null
  git_branch: string | null
  started_at: string | null
  ran_at: string
  total_failures: number
  scorer_count?: number
}

export interface ScoreResult {
  session_id: string
  scorer_name: string
  passed: boolean
  explanation: string | null
  match_metadata: Record<string, unknown> | null
}

export interface SessionFilters {
  failed_only?: boolean
  scorer?: string
  branch?: string
}
