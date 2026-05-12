import { useEffect, useState, useCallback } from 'react'
import { Link } from 'react-router-dom'
import {
  getStatuses, forceCheck, getIssues,
  getHealthSummary, getServiceMetrics,
  ServiceStatus, Issue, HealthSummaryRow, MetricPoint,
} from '../api/client'
import { RefreshCw, CheckCircle, XCircle, Clock, Wifi, WifiOff, RotateCw, TrendingUp } from 'lucide-react'
import {
  LineChart, Line, ResponsiveContainer, BarChart, Bar, Tooltip, XAxis, YAxis,
} from 'recharts'
import clsx from 'clsx'

function Sparkline({ data }: { data: MetricPoint[] }) {
  const points = data
    .filter(d => d.response_ms != null)
    .slice(-30)
    .map((d, i) => ({ i, ms: d.response_ms ?? 0 }))
  if (points.length < 2) {
    return <div className="text-[10px] text-gray-600">Sin muestras</div>
  }
  return (
    <div style={{ width: '100%', height: 36 }}>
      <ResponsiveContainer>
        <LineChart data={points}>
          <Line dataKey="ms" stroke="#38bdf8" strokeWidth={1.5} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function UptimeBadge({ pct }: { pct: number }) {
  const tone = pct >= 99 ? 'bg-green-900/40 text-green-400 border-green-500/30'
            : pct >= 95 ? 'bg-yellow-900/40 text-yellow-400 border-yellow-500/30'
                        : 'bg-red-900/40 text-red-400 border-red-500/30'
  return (
    <span className={clsx('text-[10px] px-1.5 py-0.5 rounded border font-medium', tone)}>
      {pct.toFixed(1)}% uptime
    </span>
  )
}

function StatusCard({
  s, summary, history, onRefresh,
}: {
  s: ServiceStatus
  summary?: HealthSummaryRow
  history?: MetricPoint[]
  onRefresh: (id: string) => void
}) {
  const isUp         = s.status === 'up'
  const isDown       = s.status === 'down'
  const isRestarting = s.status === 'restarting'
  return (
    <div className={clsx(
      'rounded-xl border p-4 flex flex-col gap-3 transition-all',
      isUp         && 'bg-gray-900 border-green-500/30',
      isDown       && 'bg-gray-900 border-red-500/40 shadow-red-900/20 shadow-lg',
      isRestarting && 'bg-gray-900 border-yellow-500/40 shadow-yellow-900/10 shadow-md',
      !isUp && !isDown && !isRestarting && 'bg-gray-900 border-gray-700',
    )}>
      <div className="flex items-center justify-between">
        <span className="font-semibold text-white text-sm">{s.name}</span>
        <button
          onClick={() => onRefresh(s.id)}
          className="text-gray-500 hover:text-white transition-colors"
          title="Verificar ahora"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        {isUp         && <CheckCircle size={20} className="text-green-400" />}
        {isDown       && <XCircle     size={20} className="text-red-400" />}
        {isRestarting && <RotateCw    size={20} className="text-yellow-400 animate-spin" />}
        {!isUp && !isDown && !isRestarting && <Clock size={20} className="text-gray-500" />}
        <span className={clsx(
          'font-bold uppercase text-sm tracking-wide',
          isUp         && 'text-green-400',
          isDown       && 'text-red-400',
          isRestarting && 'text-yellow-400',
          !isUp && !isDown && !isRestarting && 'text-gray-500',
        )}>
          {isRestarting ? 'Reiniciando…' : s.status}
        </span>
        {summary && <UptimeBadge pct={summary.uptime_24h} />}
        {s.status_code && (
          <span className="ml-auto text-xs text-gray-500">HTTP {s.status_code}</span>
        )}
      </div>

      {isRestarting && s.consecutive_failures != null && (
        <div className="text-xs text-yellow-600 bg-yellow-950/40 rounded px-2 py-1">
          Intento {s.consecutive_failures} — esperando que levante antes de alertar
        </div>
      )}

      <Sparkline data={history ?? []} />

      <div className="text-xs text-gray-500 flex gap-3 flex-wrap">
        {s.response_ms != null && <span>{s.response_ms} ms</span>}
        {summary?.avg_response_ms != null && <span>avg {Math.round(summary.avg_response_ms)}ms</span>}
        {summary?.p95_response_ms != null && <span>p95 {Math.round(summary.p95_response_ms)}ms</span>}
        {s.last_checked && (
          <span>Revisado: {new Date(s.last_checked).toLocaleTimeString('es')}</span>
        )}
      </div>
    </div>
  )
}

function LevelBadge({ level }: { level: string }) {
  const cls: Record<string, string> = {
    CRITICAL: 'bg-red-900 text-red-300',
    ERROR:    'bg-orange-900 text-orange-300',
    WARNING:  'bg-yellow-900 text-yellow-300',
    INFO:     'bg-blue-900 text-blue-300',
  }
  return (
    <span className={clsx('px-2 py-0.5 rounded text-xs font-bold uppercase', cls[level] ?? 'bg-gray-700 text-gray-300')}>
      {level}
    </span>
  )
}

function bucketHourly(issues: Issue[]) {
  const buckets: Record<string, number> = {}
  const now = new Date()
  for (let i = 23; i >= 0; i--) {
    const d = new Date(now.getTime() - i * 3600_000)
    const key = `${d.getHours().toString().padStart(2, '0')}h`
    buckets[key] = 0
  }
  issues.forEach(iss => {
    const t = new Date(iss.last_seen)
    if (isNaN(t.getTime())) return
    const ageHours = (now.getTime() - t.getTime()) / 3600_000
    if (ageHours > 24) return
    const key = `${t.getHours().toString().padStart(2, '0')}h`
    buckets[key] = (buckets[key] ?? 0) + iss.count
  })
  return Object.entries(buckets).map(([hour, count]) => ({ hour, count }))
}

export default function DashboardPage() {
  const [statuses, setStatuses] = useState<ServiceStatus[]>([])
  const [events,   setEvents]   = useState<Issue[]>([])
  const [summary,  setSummary]  = useState<Record<string, HealthSummaryRow>>({})
  const [history,  setHistory]  = useState<Record<string, MetricPoint[]>>({})
  const [loading,  setLoading]  = useState(true)

  const load = useCallback(async () => {
    try {
      const [s, e, sum] = await Promise.all([
        getStatuses(),
        getIssues({ limit: 20, since_hours: 24 }),
        getHealthSummary().catch(() => [] as HealthSummaryRow[]),
      ])
      setStatuses(s)
      setEvents(e)
      const summaryMap: Record<string, HealthSummaryRow> = {}
      sum.forEach(row => { summaryMap[row.service_id] = row })
      setSummary(summaryMap)
      // Sparkline data — best-effort; failures are silent
      const metrics = await Promise.allSettled(
        s.map(svc => getServiceMetrics(svc.id, 6).then(m => [svc.id, m] as const))
      )
      const histMap: Record<string, MetricPoint[]> = {}
      metrics.forEach(r => {
        if (r.status === 'fulfilled') {
          const [id, data] = r.value
          histMap[id] = data
        }
      })
      setHistory(histMap)
    } catch { /* backend might not be up yet */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    load()
    const interval = setInterval(load, 15_000)
    return () => clearInterval(interval)
  }, [load])

  const handleRefresh = async (id: string) => {
    const updated = await forceCheck(id)
    setStatuses(prev => prev.map(s => s.id === id ? updated : s))
  }

  const upCount          = statuses.filter(s => s.status === 'up').length
  const downCount        = statuses.filter(s => s.status === 'down').length
  const restartingCount  = statuses.filter(s => s.status === 'restarting').length

  const hourlyData = bucketHourly(events)

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="text-gray-400 text-sm mt-1">Estado en tiempo real de tus servicios</p>
        </div>
        <button onClick={load} className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors">
          <RefreshCw size={14} /> Actualizar
        </button>
      </div>

      {/* Summary */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 text-center">
          <div className="text-3xl font-bold text-white">{statuses.length}</div>
          <div className="text-xs text-gray-400 mt-1">Servicios</div>
        </div>
        <div className="bg-gray-900 border border-green-500/20 rounded-xl p-4 text-center">
          <div className="text-3xl font-bold text-green-400 flex items-center justify-center gap-2">
            <Wifi size={24} /> {upCount}
          </div>
          <div className="text-xs text-gray-400 mt-1">En línea</div>
        </div>
        <div className={clsx(
          'rounded-xl p-4 text-center border',
          restartingCount > 0 ? 'bg-yellow-950/30 border-yellow-500/30' : 'bg-gray-900 border-gray-800',
        )}>
          <div className={clsx('text-3xl font-bold flex items-center justify-center gap-2',
            restartingCount > 0 ? 'text-yellow-400' : 'text-gray-500')}>
            <RotateCw size={24} /> {restartingCount}
          </div>
          <div className="text-xs text-gray-400 mt-1">Reiniciando</div>
        </div>
        <div className={clsx(
          'rounded-xl p-4 text-center border',
          downCount > 0 ? 'bg-red-950 border-red-500/30' : 'bg-gray-900 border-gray-800',
        )}>
          <div className={clsx('text-3xl font-bold flex items-center justify-center gap-2',
            downCount > 0 ? 'text-red-400' : 'text-gray-500')}>
            <WifiOff size={24} /> {downCount}
          </div>
          <div className="text-xs text-gray-400 mt-1">Caídos</div>
        </div>
      </div>

      {/* Service grid */}
      {loading ? (
        <div className="text-gray-500 text-center py-12">Cargando...</div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {statuses.map(s => (
            <StatusCard
              key={s.id}
              s={s}
              summary={summary[s.id]}
              history={history[s.id]}
              onRefresh={handleRefresh}
            />
          ))}
        </div>
      )}

      {/* Error rate chart */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <TrendingUp size={14} className="text-sky-400" />
            Errores por hora (últimas 24h)
          </h2>
          <Link to="/performance" className="text-xs text-sky-400 hover:underline">Ver performance →</Link>
        </div>
        <div style={{ width: '100%', height: 160 }}>
          <ResponsiveContainer>
            <BarChart data={hourlyData}>
              <XAxis dataKey="hour" stroke="#6b7280" fontSize={10} />
              <YAxis stroke="#6b7280" fontSize={10} allowDecimals={false} />
              <Tooltip
                contentStyle={{ background: '#0b1220', border: '1px solid #1f2937', fontSize: 12 }}
                labelStyle={{ color: '#94a3b8' }}
              />
              <Bar dataKey="count" fill="#f97316" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Recent errors */}
      <div>
        <h2 className="text-lg font-semibold text-white mb-3">Errores recientes (24h)</h2>
        {events.length === 0 ? (
          <div className="text-gray-500 text-sm bg-gray-900 border border-gray-800 rounded-xl p-6 text-center">
            Sin errores en las últimas 24 horas
          </div>
        ) : (
          <div className="space-y-2">
            {events.map(e => (
              <div key={e.id} className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 flex items-start gap-3">
                <LevelBadge level={e.level} />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{e.message}</div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    {e.service_name} · {new Date(e.last_seen).toLocaleString('es')} · {e.count}× ocurrencias
                    {e.environment && <> · <span className="text-sky-400">{e.environment}</span></>}
                    {e.release && <> · <span className="text-gray-400">{e.release}</span></>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
