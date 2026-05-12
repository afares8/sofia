import { useEffect, useState, useCallback } from 'react'
import { getIssues, getOccurrences, resolveIssue, purgeEvents, Issue, Occurrence } from '../api/client'
import clsx from 'clsx'
import { Trash2, CheckCheck, ChevronDown, ChevronUp, Clock, Link, User, Zap, FileText, Search, History, AlertCircle } from 'lucide-react'
import { BarChart, Bar, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

function LevelBadge({ level }: { level: string }) {
  const cls: Record<string, string> = {
    CRITICAL: 'bg-red-900/60 text-red-300 border border-red-700/50',
    ERROR:    'bg-orange-900/60 text-orange-300 border border-orange-700/50',
    WARNING:  'bg-yellow-900/60 text-yellow-300 border border-yellow-700/50',
    INFO:     'bg-blue-900/60 text-blue-300 border border-blue-700/50',
  }
  return (
    <span className={clsx('px-2 py-0.5 rounded text-xs font-bold uppercase tracking-wide', cls[level] ?? 'bg-gray-700 text-gray-300')}>
      {level}
    </span>
  )
}

function timeAgo(iso: string) {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'ahora'
  if (m < 60) return `hace ${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `hace ${h}h`
  return `hace ${Math.floor(h / 24)}d`
}

function OccurrenceList({ issueId }: { issueId: number }) {
  const [occurrences, setOccurrences] = useState<Occurrence[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getOccurrences(issueId, 10).then(setOccurrences).finally(() => setLoading(false))
  }, [issueId])

  if (loading) return <div className="text-xs text-gray-500 py-2">Cargando ocurrencias...</div>

  return (
    <div className="space-y-1 mt-2">
      <div className="text-xs text-gray-400 font-medium mb-2">Últimas {occurrences.length} ocurrencias</div>
      {occurrences.map(o => {
        let breadcrumbs: Array<{ category?: string; message?: string; timestamp?: number; level?: string }> = []
        if (o.breadcrumbs) {
          try { breadcrumbs = JSON.parse(o.breadcrumbs) } catch { /* ignore */ }
        }
        return (
          <div key={o.id} className="text-xs text-gray-400 bg-gray-950 rounded px-2 py-1">
            <div className="flex items-center gap-3">
              <Clock size={10} className="shrink-0" />
              <span>{new Date(o.timestamp).toLocaleString('es')}</span>
              {o.url && <span className="truncate text-gray-500">{o.url}</span>}
              {o.user_info && <span className="text-sky-400">👤 {o.user_info}</span>}
            </div>
            {breadcrumbs.length > 0 && (
              <details className="mt-1">
                <summary className="cursor-pointer text-gray-500 hover:text-gray-300 select-none">
                  {breadcrumbs.length} breadcrumbs
                </summary>
                <ul className="mt-1 space-y-0.5 ml-3">
                  {breadcrumbs.map((c, i) => (
                    <li key={i} className="text-[11px] text-gray-500 truncate">
                      <span className="text-gray-600">[{c.category ?? '–'}]</span> {c.message}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )
      })}
    </div>
  )
}

function bucketIssuesHourly(issues: Issue[]) {
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

function IssueRow({ issue, onResolve }: { issue: Issue; onResolve: (id: number) => void }) {
  const [open, setOpen] = useState(false)
  const [resolving, setResolving] = useState(false)

  const handleResolve = async (e: React.MouseEvent) => {
    e.stopPropagation()
    setResolving(true)
    await resolveIssue(issue.id)
    onResolve(issue.id)
  }

  const leftBorder: Record<string, string> = {
    CRITICAL: 'border-l-red-500',
    ERROR:    'border-l-orange-500',
    WARNING:  'border-l-yellow-500',
    INFO:     'border-l-blue-500',
  }

  return (
    <div className={clsx(
      'bg-gray-900 border border-gray-800 border-l-2 rounded-lg overflow-hidden transition-all',
      leftBorder[issue.level] ?? 'border-l-gray-600',
    )}>
      {/* Header row */}
      <button
        className="w-full px-4 py-3 flex items-start gap-3 text-left hover:bg-gray-800/40 transition-colors"
        onClick={() => setOpen(o => !o)}
      >
        {/* Level + count */}
        <div className="flex flex-col items-center gap-1 shrink-0 pt-0.5">
          <LevelBadge level={issue.level} />
          <span className="text-xs font-bold text-gray-300 bg-gray-800 rounded px-1.5 py-0.5 min-w-[2rem] text-center">
            {issue.count}×
          </span>
        </div>

        {/* Message + meta */}
        <div className="flex-1 min-w-0">
          <div className="text-sm text-white font-medium truncate flex items-center gap-2">
            {/* Regression: resolved=false but first_seen is older than last_seen by a notable margin
               and count is >= 2 — heuristic until backend exposes a dedicated flag. */}
            {!issue.resolved && new Date(issue.last_seen).getTime() - new Date(issue.first_seen).getTime() > 60 * 60 * 1000 && issue.count >= 2 && (
              <span className="shrink-0 inline-flex items-center gap-1 text-[10px] uppercase tracking-wide font-bold px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300 border border-purple-700/40">
                <History size={10} /> Reaparece
              </span>
            )}
            <span className="truncate">{issue.message}</span>
          </div>
          <div className="flex flex-wrap items-center gap-2 mt-1 text-xs text-gray-500">
            <span className="text-gray-400 font-medium">{issue.service_name}</span>
            {issue.environment && (
              <>
                <span>·</span>
                <span className="text-sky-400 uppercase tracking-wide font-bold text-[10px]">
                  {issue.environment}
                </span>
              </>
            )}
            {issue.release && (
              <>
                <span>·</span>
                <span className="text-gray-400">v{issue.release}</span>
              </>
            )}
            <span>·</span>
            <span className={clsx('flex items-center gap-1',
              issue.source === 'active' ? 'text-sky-400' : 'text-gray-500')}>
              {issue.source === 'active' ? <Zap size={10} /> : <FileText size={10} />}
              {issue.source === 'active' ? 'SDK' : 'Log'}
            </span>
            <span>·</span>
            <span title={new Date(issue.first_seen).toLocaleString('es')}>
              Primero: {timeAgo(issue.first_seen)}
            </span>
            <span>·</span>
            <span title={new Date(issue.last_seen).toLocaleString('es')}>
              Último: {timeAgo(issue.last_seen)}
            </span>
            {issue.url && (
              <>
                <span>·</span>
                <span className="flex items-center gap-1 truncate max-w-[180px]">
                  <Link size={10} />{issue.url}
                </span>
              </>
            )}
            {issue.user_info && (
              <>
                <span>·</span>
                <span className="flex items-center gap-1 text-sky-400">
                  <User size={10} />{issue.user_info}
                </span>
              </>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2 shrink-0 self-center" onClick={e => e.stopPropagation()}>
          <button
            onClick={handleResolve}
            disabled={resolving}
            className="flex items-center gap-1 px-2 py-1 rounded bg-green-900/30 hover:bg-green-900/60 text-green-400 text-xs transition-colors"
            title="Marcar como resuelto"
          >
            <CheckCheck size={12} /> Resolver
          </button>
          {open ? <ChevronUp size={14} className="text-gray-500" /> : <ChevronDown size={14} className="text-gray-500" />}
        </div>
      </button>

      {/* Expanded detail */}
      {open && (
        <div className="px-4 pb-4 space-y-3 border-t border-gray-800 pt-3">
          {issue.detail && (
            <div>
              <div className="text-xs text-gray-400 mb-1 font-medium uppercase tracking-wide">Detalle</div>
              <pre className="text-xs text-gray-300 bg-gray-950 rounded p-3 whitespace-pre-wrap break-words">{issue.detail}</pre>
            </div>
          )}
          {issue.traceback && (
            <div>
              <div className="text-xs text-gray-400 mb-1 font-medium uppercase tracking-wide">Stack Trace</div>
              <pre className="text-xs text-red-300 bg-gray-950 rounded p-3 overflow-auto max-h-64 font-mono leading-5">{issue.traceback}</pre>
            </div>
          )}
          {issue.tags && (
            <div>
              <div className="text-xs text-gray-400 mb-1 font-medium uppercase tracking-wide">Tags</div>
              <pre className="text-xs text-gray-300 bg-gray-950 rounded p-3 whitespace-pre-wrap break-words">{issue.tags}</pre>
            </div>
          )}
          <OccurrenceList issueId={issue.id} />
        </div>
      )}
    </div>
  )
}

export default function EventsPage() {
  const [issues,     setIssues]     = useState<Issue[]>([])
  const [loading,    setLoading]    = useState(true)
  const [serviceId,  setServiceId]  = useState('')
  const [level,      setLevel]      = useState('')
  const [sinceHours, setSinceHours] = useState(24 * 7)
  const [showResolved, setShowResolved] = useState(false)
  const [search,      setSearch]      = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await getIssues({
        service_id: serviceId || undefined,
        level:      level || undefined,
        since_hours: sinceHours,
        resolved: showResolved,
        limit: 100,
      })
      setIssues(data)
    } finally { setLoading(false) }
  }, [serviceId, level, sinceHours, showResolved])

  useEffect(() => { load() }, [load])

  const handleResolve = (id: number) => {
    setIssues(prev => prev.filter(i => i.id !== id))
  }

  const handlePurge = async () => {
    if (!confirm('¿Eliminar issues resueltos y eventos antiguos?')) return
    await purgeEvents(7)
    load()
  }

  const filtered = issues.filter(i =>
    search === '' || i.message.toLowerCase().includes(search.toLowerCase())
  )

  // Stats
  const critical = filtered.filter(i => i.level === 'CRITICAL').length
  const errors   = filtered.filter(i => i.level === 'ERROR').length
  const warnings = filtered.filter(i => i.level === 'WARNING').length
  const totalOcc = filtered.reduce((a, i) => a + i.count, 0)

  return (
    <div className="p-6 space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Issues</h1>
          <p className="text-gray-400 text-sm mt-1">
            {filtered.length} issues · {totalOcc.toLocaleString()} ocurrencias
          </p>
        </div>
        <button onClick={handlePurge} className="flex items-center gap-2 px-3 py-2 bg-red-900/20 hover:bg-red-900/40 border border-red-700/30 rounded-lg text-sm text-red-400 transition-colors">
          <Trash2 size={14} /> Purgar resueltos
        </button>
      </div>

      {/* Summary counters */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { label: 'Critical', val: critical, cls: 'border-red-500/30 text-red-400' },
          { label: 'Errors',   val: errors,   cls: 'border-orange-500/30 text-orange-400' },
          { label: 'Warnings', val: warnings, cls: 'border-yellow-500/30 text-yellow-400' },
          { label: 'Ocurrencias', val: totalOcc, cls: 'border-gray-700 text-gray-300' },
        ].map(({ label, val, cls }) => (
          <div key={label} className={clsx('bg-gray-900 border rounded-xl p-3 text-center', cls)}>
            <div className="text-2xl font-bold">{val.toLocaleString()}</div>
            <div className="text-xs text-gray-500 mt-0.5">{label}</div>
          </div>
        ))}
      </div>

      {/* Hourly trend */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div className="text-xs uppercase tracking-wide text-gray-400 mb-2 flex items-center gap-1">
          <AlertCircle size={12} /> Distribución de errores · últimas 24h
        </div>
        <div style={{ width: '100%', height: 140 }}>
          <ResponsiveContainer>
            <BarChart data={bucketIssuesHourly(filtered)}>
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

      {/* Filters */}
      <div className="flex flex-wrap gap-2">
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            placeholder="Buscar en mensajes..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="bg-gray-900 border border-gray-700 rounded-lg pl-8 pr-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-sky-500 w-56"
          />
        </div>
        <input
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-sky-500"
          placeholder="Filtrar por servicio..."
          value={serviceId}
          onChange={e => setServiceId(e.target.value)}
        />
        <select className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={level} onChange={e => setLevel(e.target.value)}>
          <option value="">Todos los niveles</option>
          <option value="CRITICAL">CRITICAL</option>
          <option value="ERROR">ERROR</option>
          <option value="WARNING">WARNING</option>
          <option value="INFO">INFO</option>
        </select>
        <select className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={sinceHours} onChange={e => setSinceHours(Number(e.target.value))}>
          <option value={1}>Última hora</option>
          <option value={6}>Últimas 6h</option>
          <option value={24}>Últimas 24h</option>
          <option value={24*7}>Última semana</option>
          <option value={24*30}>Último mes</option>
        </select>
        <label className="flex items-center gap-2 px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-300 cursor-pointer">
          <input type="checkbox" className="accent-sky-500"
            checked={showResolved} onChange={e => setShowResolved(e.target.checked)} />
          Ver resueltos
        </label>
      </div>

      {/* List */}
      {loading ? (
        <div className="text-gray-500 text-center py-12">Cargando...</div>
      ) : filtered.length === 0 ? (
        <div className="text-gray-500 text-sm bg-gray-900 border border-gray-800 rounded-xl p-10 text-center">
          <CheckCheck size={32} className="text-green-500 mx-auto mb-3" />
          Sin issues en este período
        </div>
      ) : (
        <div className="space-y-2">
          {filtered.map(i => <IssueRow key={i.id} issue={i} onResolve={handleResolve} />)}
        </div>
      )}
    </div>
  )
}
