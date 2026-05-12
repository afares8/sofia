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

export interface MonitorConfig {
  poll_interval_seconds: number; log_tail_lines: number
  error_retention_days: number
  services: ServiceConfig[]; alerts: AlertConfig
  alert_rules: AlertRule[]
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
