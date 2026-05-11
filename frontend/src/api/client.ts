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
  id: string; name: string; status: 'up' | 'down' | 'unknown'
  status_code?: number; response_ms?: number
  last_checked?: string; last_seen_up?: string; enabled: boolean
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
}

export interface Occurrence {
  id: number
  issue_id: number
  timestamp: string
  url?: string
  user_info?: string
  detail?: string
  traceback?: string
}

export interface ServiceConfig {
  id: string; name: string; url: string; enabled: boolean
  log_path?: string; expected_status: number; timeout_seconds: number
}

export interface AlertConfig {
  whatsapp_enabled: boolean; whatsapp_number: string
  wppconnect_url: string; wppconnect_token: string
  wppconnect_session: string; cooldown_minutes: number
}

export interface MonitorConfig {
  poll_interval_seconds: number; log_tail_lines: number
  error_retention_days: number
  services: ServiceConfig[]; alerts: AlertConfig
}

// --- Health ---
export const getStatuses = () => req<ServiceStatus[]>('/health/')
export const forceCheck  = (id: string) => req<ServiceStatus>(`/health/check/${id}`, { method: 'POST' })

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
