const BASE = '/api'

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(`${res.status}: ${err}`)
  }
  return res.json()
}

export interface ServiceStatus {
  id: string; name: string; status: 'up' | 'down' | 'restarting' | 'unknown'
  status_code?: number; response_ms?: number
  last_checked?: string; last_seen_up?: string; enabled: boolean
  consecutive_failures?: number
}

export interface Issue {
  id: number
  fingerprint: string
  service_id: string
  service_name: string
  level: string
  message: string
  detail?: string
  traceback?: string
  url?: string
  user_info?: string
  source: string
  count: number
  first_seen: string
  last_seen: string
  resolved: boolean
  notified: boolean
  tags?: string
  environment?: string
  release?: string
}

export interface Occurrence {
  id: number
  issue_id: number
  timestamp: string
  url?: string
  user_info?: string
  detail?: string
  traceback?: string
  breadcrumbs?: string
}

export interface ServiceConfig {
  id: string; name: string; url: string; enabled: boolean
  log_path?: string; expected_status: number; timeout_seconds: number
  failure_threshold: number
  restore_enabled?: boolean
  auto_restore?: boolean
}

export interface AlertConfig {
  whatsapp_enabled: boolean; whatsapp_number: string
  wppconnect_url: string; wppconnect_token: string
  wppconnect_session: string; cooldown_minutes: number
  escalation_enabled?: boolean
  escalation_minutes?: number
  escalation_numbers?: string[]
}

export interface AlertRule {
  id: string
  name: string
  enabled: boolean
  condition_type: string  // 'error_count' | 'response_ms' | 'downtime_minutes' | 'spike'
  threshold: number
  window_minutes: number
  service_id?: string | null
  cooldown_minutes: number
}

export interface AutonomyConfig {
  enabled: boolean
  kill_switch: boolean
  default_level: number
  sandbox_root: string
  auto_create_jobs_from_issues: boolean
  auto_fix_issue_min_count: number
  auto_fix_loop_minutes: number
  max_actions_per_hour: number
  max_devin_sessions_per_day: number
  max_autofix_jobs_per_day: number
  max_failed_jobs_before_pause: number
  require_verifier: boolean
  require_tests_for_code_fixes: boolean
  require_human_for_apply: boolean
  commit_in_sandbox: boolean
  run_smoke_checks: boolean
  max_files_changed: number
  max_lines_changed: number
  count_test_files_in_limit: boolean
  promotion_mode: string
  auto_promote_low_risk: boolean
  job_timeout_minutes: number
  allowed_paths: string[]
  blocked_paths: string[]
  forbidden_actions: string[]
}

export interface AppRepoConfig {
  id: string
  name: string
  path: string
  enabled: boolean
  branch: string
  autonomy_level: number
  autofix_enabled: boolean
  test_commands: string[]
  build_commands: string[]
  smoke_urls: string[]
  allowed_paths: string[]
  blocked_paths: string[]
}

export interface GithubSyncRepo {
  id: string
  path: string
  enabled: boolean
  branch: string
}

export interface GithubSyncConfig {
  enabled: boolean
  auto_push_at_midnight: boolean
  commit_message_prefix: string
  require_clean_secret_scan: boolean
  max_files_per_repo: number
  blocked_paths: string[]
  repos: GithubSyncRepo[]
}

export interface MonitorConfig {
  poll_interval_seconds: number; log_tail_lines: number
  error_retention_days: number
  services: ServiceConfig[]; alerts: AlertConfig
  alert_rules: AlertRule[]
  autonomy: AutonomyConfig
  app_repos: AppRepoConfig[]
  github_sync: GithubSyncConfig
}

export interface MetricPoint {
  timestamp: string
  response_ms: number | null
  status_code: number | null
  is_up: 0 | 1
}

export interface ServiceStats {
  avg: number | null
  p50: number | null
  p95: number | null
  p99: number | null
  min: number | null
  max: number | null
  total_checks: number
  uptime_percent: number
}

export interface HealthSummaryRow {
  service_id: string
  service_name: string
  uptime_24h: number
  uptime_7d: number
  avg_response_ms: number | null
  p95_response_ms: number | null
  current_status: 'up' | 'down' | 'restarting' | 'unknown'
  enabled: boolean
}

export interface SofiaSelfHealth {
  ok: boolean
  uptime_seconds: number
  db_ok: boolean
  db_error?: string | null
  memory_mb?: number | null
  wpp_ok: boolean
  wpp_error?: string | null
  last_poll?: string | null
  now: string
}

export interface RestoreEntry {
  service_id: string
  service_name: string
  status: 'pending' | 'confirmed' | 'running' | 'success' | 'failed' | 'rejected' | 'expired'
  requested_at: string | null
  confirmed_at: string | null
  finished_at: string | null
  result_message: string | null
  devin_output: string | null
  retry_count?: number
  trigger_mode?: 'auto' | 'manual'
  restore_method?: 'devin' | 'ps1_script' | null
}

// --- Health ---
export const getStatuses = () => req<ServiceStatus[]>('/health/')
export const forceCheck  = (id: string) => req<ServiceStatus>(`/health/check/${id}`, { method: 'POST' })
export const getSofiaHealth   = () => req<SofiaSelfHealth>('/health/sofia')
export const getServiceMetrics = (id: string, since_hours = 24) =>
  req<MetricPoint[]>(`/health/${id}/metrics?since_hours=${since_hours}`)
export const getServiceStats   = (id: string, since_hours = 24) =>
  req<ServiceStats>(`/health/${id}/stats?since_hours=${since_hours}`)
export const getHealthSummary  = () => req<HealthSummaryRow[]>('/health/summary')

// --- Issues ---
export const getIssues = (params?: {
  service_id?: string; level?: string; resolved?: boolean
  limit?: number; since_hours?: number
}) => {
  const q = new URLSearchParams()
  if (params?.service_id)  q.set('service_id', params.service_id)
  if (params?.level)       q.set('level', params.level)
  if (params?.resolved != null) q.set('resolved', String(params.resolved))
  if (params?.limit)       q.set('limit', String(params.limit))
  if (params?.since_hours) q.set('since_hours', String(params.since_hours))
  return req<Issue[]>(`/events/?${q}`)
}
export const getOccurrences = (issueId: number, limit = 50) =>
  req<Occurrence[]>(`/events/${issueId}/occurrences?limit=${limit}`)
export const resolveIssue = (issueId: number) =>
  req(`/events/${issueId}/resolve`, { method: 'POST' })
export const purgeEvents = (days: number) =>
  req(`/events/purge?retention_days=${days}`, { method: 'DELETE' })

// --- Logs ---
export const getLogTail = (serviceId: string, lines = 100) =>
  req<string[]>(`/logs/${serviceId}?lines=${lines}`)

// --- Config ---
export const getConfig    = () => req<MonitorConfig>('/config/')
export const updateConfig = (cfg: MonitorConfig) =>
  req<MonitorConfig>('/config/', { method: 'PUT', body: JSON.stringify(cfg) })
export const getServices    = () => req<ServiceConfig[]>('/config/services')
export const addService     = (s: ServiceConfig) =>
  req<ServiceConfig>('/config/services', { method: 'POST', body: JSON.stringify(s) })
export const updateService  = (id: string, s: ServiceConfig) =>
  req<ServiceConfig>(`/config/services/${id}`, { method: 'PUT', body: JSON.stringify(s) })
export const deleteService  = (id: string) =>
  req(`/config/services/${id}`, { method: 'DELETE' })
export const getAlerts    = () => req<AlertConfig>('/config/alerts')
export const updateAlerts = (a: AlertConfig) =>
  req<AlertConfig>('/config/alerts', { method: 'PUT', body: JSON.stringify(a) })
export const testAlert    = () => req('/config/alerts/test', { method: 'POST' })

// --- Alert rules ---
export const getAlertRules    = () => req<AlertRule[]>('/config/rules')
export const addAlertRule     = (r: AlertRule) =>
  req<AlertRule>('/config/rules', { method: 'POST', body: JSON.stringify(r) })
export const updateAlertRule  = (id: string, r: AlertRule) =>
  req<AlertRule>(`/config/rules/${id}`, { method: 'PUT', body: JSON.stringify(r) })
export const deleteAlertRule  = (id: string) =>
  req(`/config/rules/${id}`, { method: 'DELETE' })

// --- Restore ---
export const getRestores       = () => req<RestoreEntry[]>('/restore/')
export const getRestoreHistory = (limit = 50) =>
  req<RestoreEntry[]>(`/restore/history?limit=${limit}`)
export const triggerRestore    = (serviceId: string) =>
  req<RestoreEntry>(`/restore/trigger/${serviceId}`, { method: 'POST' })

// --- Nightly review ---
export interface Proposal {
  issue_id: number | null
  service_id: string
  title: string
  root_cause: string
  proposal: string
  file_path: string | null
  line_hint: number | null
  confidence: 'high' | 'medium' | 'low'
  risk: 'low' | 'medium' | 'high'
  source?: string
}

export interface NightlyReport {
  id: number
  created_at: string
  period_start: string
  period_end: string
  status: 'pending' | 'approved' | 'rejected' | 'applied' | 'apply_failed'
  issues_analyzed: number
  proposals: Proposal[]
  approved_at: string | null
  rejected_at: string | null
  applied_at: string | null
  apply_output: string | null
  notes: string | null
}

/** One apply-run per proposal attempt */
export interface ProposalRun {
  id: number
  report_id: number
  proposal_index: number
  issue_id: number | null
  service_id: string | null
  title: string | null
  status: 'running' | 'success' | 'failed'
  started_at: string | null
  finished_at: string | null
  duration_s: number | null
  devin_output: string | null
  error_msg: string | null
}

export const getNightlyReports  = (limit = 30) =>
  req<NightlyReport[]>(`/nightly/?limit=${limit}`)
export const getNightlyReport   = (id: number) =>
  req<NightlyReport>(`/nightly/${id}`)
export const getProposalRuns    = (reportId: number) =>
  req<ProposalRun[]>(`/nightly/${reportId}/runs`)
export const triggerNightlyRun  = (params = { force: true, since_hours: 24 }) =>
  req<{ ok: boolean; message: string }>('/nightly/run', { method: 'POST', body: JSON.stringify(params) })
export const approveNightlyReport = (id: number, notes = '') =>
  req<{ ok: boolean }>(`/nightly/${id}/approve`, { method: 'POST', body: JSON.stringify({ notes }) })
export const rejectNightlyReport  = (id: number, notes = '') =>
  req<{ ok: boolean }>(`/nightly/${id}/reject`, { method: 'POST', body: JSON.stringify({ notes }) })
export const approveAndApplyProposal = (reportId: number, proposalIndex: number) =>
  req<{ ok: boolean; message: string }>(`/nightly/${reportId}/approve/${proposalIndex}`, { method: 'POST' })

export const applyBatchProposal = (reportId: number, proposalIndex: number) =>
  req<{ ok: boolean; message: string; batch: boolean; service_id: string }>(`/nightly/${reportId}/apply-batch/${proposalIndex}`, { method: 'POST' })

// --- Autonomy / AI Engineer ---
export interface AIJob {
  id: number
  created_at: string
  updated_at: string
  status: string
  service_id: string | null
  issue_id: number | null
  repo_id: string | null
  goal: string
  autonomy_level: number
  mode: string
  sandbox_path: string | null
  base_branch: string | null
  work_branch: string | null
  branch_name: string | null
  commit_sha: string | null
  devin_output: string | null
  diff_summary: string | null
  tests_output: string | null
  tests_status: string | null
  smoke_output: string | null
  smoke_status: string | null
  verifier_output: string | null
  verifier_status: string | null
  verifier_decision: string | null
  risk: string | null
  blocked_reason: string | null
  pr_url: string | null
  promoted_at: string | null
  result_message: string | null
}

export interface AuditEvent {
  id: number
  created_at: string
  entity_type: string
  entity_id: number | null
  event_type: string
  message: string | null
  data: string | null
}

export interface ActionRun {
  id: number
  created_at: string
  finished_at: string | null
  action_type: string
  service_id: string | null
  status: string
  autonomy_level: number
  trigger_source: string | null
  target: string | null
  output: string | null
  error_msg: string | null
}

export interface GithubSyncRun {
  id: number
  created_at: string
  finished_at: string | null
  repo_id: string
  repo_path: string
  status: string
  branch: string | null
  files_changed: number
  commit_sha: string | null
  pushed: 0 | 1
  output: string | null
  error_msg: string | null
}

export const getAutonomyConfig = () =>
  req<{ autonomy: AutonomyConfig; app_repos: AppRepoConfig[]; github_sync: GithubSyncConfig }>('/autonomy/config')
export const updateAutonomyConfig = (cfg: AutonomyConfig) =>
  req<AutonomyConfig>('/autonomy/config/autonomy', { method: 'PUT', body: JSON.stringify(cfg) })
export const updateAppRepos = (repos: AppRepoConfig[]) =>
  req<AppRepoConfig[]>('/autonomy/config/app-repos', { method: 'PUT', body: JSON.stringify(repos) })
export const updateGithubSyncConfig = (cfg: GithubSyncConfig) =>
  req<GithubSyncConfig>('/autonomy/config/github-sync', { method: 'PUT', body: JSON.stringify(cfg) })
export const setKillSwitch = (enabled: boolean) =>
  req<{ ok: boolean; kill_switch: boolean }>('/autonomy/kill-switch', { method: 'POST', body: JSON.stringify({ enabled }) })
export const getAIJobs = (limit = 50) => req<AIJob[]>(`/autonomy/jobs?limit=${limit}`)
export const createAIJob = (body: { goal: string; service_id?: string; issue_id?: number; repo_id?: string; autonomy_level?: number; mode: 'plan' | 'fix' }) =>
  req<{ ok: boolean; job_id: number }>('/autonomy/jobs', { method: 'POST', body: JSON.stringify(body) })
export const promoteAIJob = (jobId: number) =>
  req<{ ok: boolean; job_id: number; pr_url: string | null; message: string }>(`/autonomy/jobs/${jobId}/promote`, { method: 'POST' })
export const getActionRuns = (limit = 50) => req<ActionRun[]>(`/autonomy/actions?limit=${limit}`)
export const getAuditEvents = (limit = 100) => req<AuditEvent[]>(`/autonomy/audit?limit=${limit}`)
export const getGithubSyncRuns = (limit = 50) => req<GithubSyncRun[]>(`/autonomy/github-sync/runs?limit=${limit}`)
export const triggerGithubSync = (background = true) =>
  req<{ ok: boolean; message?: string; results?: unknown[] }>(`/autonomy/github-sync/run?background=${background}`, { method: 'POST' })
export const runPolicyScan = () => req('/autonomy/policy-scan')
