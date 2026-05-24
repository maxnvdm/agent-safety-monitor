export interface Session {
  id: string
  log_path: string
  cwd: string | null
  git_branch: string | null
  started_at: string | null
  ran_at: string
  total_failures: number
}

export interface ScoreResult {
  session_id: string
  scorer_name: string
  passed: boolean
  explanation: string | null
}
