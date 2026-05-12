import { useEffect, useMemo, useState } from 'react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend, ReferenceLine,
} from 'recharts'
import { BarChart3, Activity, Clock, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
import {
  getHealthSummary, getServiceMetrics, getServiceStats,
  HealthSummaryRow, MetricPoint, ServiceStats,
} from '../api/client'

const WINDOW_OPTIONS = [
  { hours: 1, label: '1h' },
  { hours: 6, label: '6h' },
  { hours: 24, label: '24h' },
  { hours: 24 * 7, label: '7d' },
]

function pct(n: number | null | undefined) {
  if (n == null) return '—'
  return `${n.toFixed(2)}%`
}

function ms(n: number | null | undefined) {
  if (n == null) return '—'
  return `${Math.round(n)}ms`
}

function hourBucket(iso: string) {
  return iso.slice(0, 13) + ':00'
}

export default function PerformancePage() {
  const [summary, setSummary] = useState<HealthSummaryRow[]>([])
  const [loading, setLoading] = useState(true)
  const [windowHours, setWindowHours] = useState(24)
  const [selected, setSelected] = useState<string | null>(null)
  const [metrics, setMetrics] = useState<MetricPoint[]>([])
  const [stats, setStats] = useState<ServiceStats | null>(null)

  const load = async () => {
    try {
      const s = await getHealthSummary()
      setSummary(s)
      if (!selected && s.length > 0) setSelected(s[0].service_id)
    } catch (e) {
      console.error('summary load failed', e)
    } finally {
      setLoading(false)
    }
  }

  const loadServiceData = async (id: string, hrs: number) => {
    try {
      const [m, st] = await Promise.all([
        getServiceMetrics(id, hrs),
        getServiceStats(id, hrs),
      ])
      setMetrics(m)
      setStats(st)
    } catch (e) {
      console.error('service data load failed', e)
    }
  }

  useEffect(() => { load() }, [])
  useEffect(() => {
    if (selected) loadServiceData(selected, windowHours)
  }, [selected, windowHours])

  // Sample data for the response-time chart
  const chartData = useMemo(() => metrics
    .filter(m => m.response_ms != null)
    .map(m => ({
      ts: new Date(m.timestamp).getTime(),
      label: new Date(m.timestamp).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' }),
      response: m.response_ms ?? 0,
      up: m.is_up ? 1 : 0,
    })), [metrics])

  // Build hourly availability buckets for heatmap
  const heatmap = useMemo(() => {
    const buckets: Record<string, { total: number; ups: number }> = {}
    metrics.forEach(m => {
      const k = hourBucket(m.timestamp)
      if (!buckets[k]) buckets[k] = { total: 0, ups: 0 }
      buckets[k].total += 1
      buckets[k].ups += m.is_up ? 1 : 0
    })
    return Object.entries(buckets)
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(-48) // last 48 hours
      .map(([hour, v]) => ({
        hour,
        pct: v.total ? Math.round((v.ups / v.total) * 100) : 100,
        total: v.total,
        ups: v.ups,
      }))
  }, [metrics])

  const downtimeMin = useMemo(() => {
    const downs = metrics.filter(m => !m.is_up).length
    // Use 30s as average poll interval if we have no idea — close enough for an estimate
    return Math.round(downs * 30 / 60)
  }, [metrics])

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <BarChart3 size={22} /> Performance
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Métricas de respuesta, uptime y disponibilidad por servicio
          </p>
        </div>
        <button
          onClick={() => { load(); if (selected) loadServiceData(selected, windowHours) }}
          className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors"
        >
          <RefreshCw size={14} /> Actualizar
        </button>
      </div>

      {/* Uptime table */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
          <Activity size={14} className="text-sky-400" />
          <h2 className="text-sm font-semibold text-white uppercase tracking-wide">Resumen</h2>
        </div>
        {loading ? (
          <div className="px-4 py-6 text-sm text-gray-500">Cargando…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-950/40 text-gray-500 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-2 font-medium">Servicio</th>
                <th className="text-left px-4 py-2 font-medium">Estado</th>
                <th className="text-right px-4 py-2 font-medium">Uptime 24h</th>
                <th className="text-right px-4 py-2 font-medium">Uptime 7d</th>
                <th className="text-right px-4 py-2 font-medium">Avg ms</th>
                <th className="text-right px-4 py-2 font-medium">P95 ms</th>
              </tr>
            </thead>
            <tbody>
              {summary.map(row => (
                <tr
                  key={row.service_id}
                  onClick={() => setSelected(row.service_id)}
                  className={clsx(
                    'border-t border-gray-800 cursor-pointer hover:bg-gray-800/40 transition-colors',
                    selected === row.service_id && 'bg-sky-900/20',
                  )}
                >
                  <td className="px-4 py-2 font-medium text-white">{row.service_name}</td>
                  <td className="px-4 py-2">
                    <span className={clsx(
                      'text-xs font-bold uppercase tracking-wide',
                      row.current_status === 'up' && 'text-green-400',
                      row.current_status === 'down' && 'text-red-400',
                      row.current_status === 'restarting' && 'text-yellow-400',
                      row.current_status === 'unknown' && 'text-gray-500',
                    )}>
                      {row.current_status}
                    </span>
                  </td>
                  <td className={clsx('px-4 py-2 text-right tabular-nums',
                    row.uptime_24h >= 99 ? 'text-green-400' : row.uptime_24h >= 95 ? 'text-yellow-400' : 'text-red-400')}>
                    {pct(row.uptime_24h)}
                  </td>
                  <td className={clsx('px-4 py-2 text-right tabular-nums',
                    row.uptime_7d >= 99 ? 'text-green-400' : row.uptime_7d >= 95 ? 'text-yellow-400' : 'text-red-400')}>
                    {pct(row.uptime_7d)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-gray-300">{ms(row.avg_response_ms)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-gray-300">{ms(row.p95_response_ms)}</td>
                </tr>
              ))}
              {!summary.length && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-gray-500">
                    Sin datos todavía
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {/* Detail charts for selected service */}
      {selected && (
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <h2 className="text-base font-semibold text-white">
              Detalle: {summary.find(s => s.service_id === selected)?.service_name ?? selected}
            </h2>
            <div className="flex gap-1">
              {WINDOW_OPTIONS.map(o => (
                <button
                  key={o.hours}
                  onClick={() => setWindowHours(o.hours)}
                  className={clsx(
                    'px-2.5 py-1 rounded text-xs font-medium transition-colors',
                    windowHours === o.hours
                      ? 'bg-sky-600 text-white'
                      : 'bg-gray-800 text-gray-400 hover:bg-gray-700',
                  )}
                >
                  {o.label}
                </button>
              ))}
            </div>
          </div>

          {/* Stats badges */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatTile icon={<Clock size={14} />} label="Avg" value={ms(stats?.avg)} />
            <StatTile label="P50" value={ms(stats?.p50)} />
            <StatTile label="P95" value={ms(stats?.p95)} />
            <StatTile label="P99" value={ms(stats?.p99)} />
            <StatTile label="Checks" value={stats?.total_checks?.toLocaleString() ?? '—'} />
            <StatTile label="Uptime" value={pct(stats?.uptime_percent)}
              tone={stats && stats.uptime_percent >= 99 ? 'green' : stats && stats.uptime_percent >= 95 ? 'yellow' : 'red'} />
            <StatTile label="Min" value={ms(stats?.min)} />
            <StatTile label="Downtime aprox" value={`${downtimeMin}m`} tone={downtimeMin > 0 ? 'red' : undefined} />
          </div>

          {/* Response time chart */}
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <div className="text-xs uppercase tracking-wide text-gray-400 mb-3">Response time (ms)</div>
            <div style={{ width: '100%', height: 260 }}>
              <ResponsiveContainer>
                <LineChart data={chartData}>
                  <CartesianGrid stroke="#1f2937" strokeDasharray="3 3" />
                  <XAxis dataKey="label" stroke="#6b7280" fontSize={11} />
                  <YAxis stroke="#6b7280" fontSize={11} />
                  <Tooltip
                    contentStyle={{ background: '#0b1220', border: '1px solid #1f2937', fontSize: 12 }}
                    labelStyle={{ color: '#94a3b8' }}
                  />
                  <Legend wrapperStyle={{ color: '#94a3b8', fontSize: 12 }} />
                  <Line dataKey="response" stroke="#38bdf8" strokeWidth={1.5} dot={false} name="Response ms" />
                  {stats?.p50 != null && <ReferenceLine y={stats.p50} stroke="#22c55e" strokeDasharray="4 4" label={{ value: 'P50', fill: '#22c55e', fontSize: 10 }} />}
                  {stats?.p95 != null && <ReferenceLine y={stats.p95} stroke="#eab308" strokeDasharray="4 4" label={{ value: 'P95', fill: '#eab308', fontSize: 10 }} />}
                  {stats?.p99 != null && <ReferenceLine y={stats.p99} stroke="#ef4444" strokeDasharray="4 4" label={{ value: 'P99', fill: '#ef4444', fontSize: 10 }} />}
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Heatmap */}
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <div className="text-xs uppercase tracking-wide text-gray-400 mb-3">
              Disponibilidad por hora (últimas {heatmap.length}h)
            </div>
            <div className="flex flex-wrap gap-1">
              {heatmap.map(h => (
                <div
                  key={h.hour}
                  title={`${h.hour} · ${h.pct}% (${h.ups}/${h.total})`}
                  className={clsx(
                    'w-3 h-6 rounded-sm',
                    h.pct >= 99 ? 'bg-green-500' :
                    h.pct >= 90 ? 'bg-green-700' :
                    h.pct >= 50 ? 'bg-yellow-500' :
                    h.pct > 0   ? 'bg-orange-600' :
                                  'bg-red-600',
                  )}
                />
              ))}
              {!heatmap.length && <div className="text-xs text-gray-500">Sin muestras</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function StatTile({
  icon, label, value, tone,
}: {
  icon?: React.ReactNode
  label: string
  value: React.ReactNode
  tone?: 'green' | 'yellow' | 'red'
}) {
  const toneCls = tone === 'green' ? 'text-green-400' : tone === 'yellow' ? 'text-yellow-400' : tone === 'red' ? 'text-red-400' : 'text-white'
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
      <div className="text-xs text-gray-500 uppercase tracking-wide flex items-center gap-1">
        {icon}{label}
      </div>
      <div className={clsx('text-xl font-bold mt-1 tabular-nums', toneCls)}>{value}</div>
    </div>
  )
}
