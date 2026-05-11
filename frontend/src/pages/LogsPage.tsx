import { useEffect, useState, useRef, useCallback } from 'react'
import { getLogTail, getConfig, ServiceConfig } from '../api/client'
import clsx from 'clsx'
import { RefreshCw, Play, Pause, Search } from 'lucide-react'

function colorLine(line: string): string {
  if (/CRITICAL/i.test(line)) return 'text-red-400 bg-red-950/20'
  if (/ERROR/i.test(line))    return 'text-orange-400 bg-orange-950/10'
  if (/WARNING|WARN/i.test(line)) return 'text-yellow-400'
  if (/INFO/i.test(line))     return 'text-green-400'
  return 'text-gray-400'
}

function levelMatch(line: string, filter: string): boolean {
  if (filter === 'ALL') return true
  if (filter === 'ERROR')   return /\bERROR\b|\bCRITICAL\b/i.test(line)
  if (filter === 'WARNING') return /\bWARN(ING)?\b/i.test(line)
  if (filter === 'INFO')    return /\bINFO\b/i.test(line)
  return true
}

export default function LogsPage() {
  const [services,    setServices]    = useState<ServiceConfig[]>([])
  const [selected,    setSelected]    = useState<string>('')
  const [lines,       setLines]       = useState<string[]>([])
  const [lineCount,   setLineCount]   = useState(200)
  const [loading,     setLoading]     = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [autoScroll,  setAutoScroll]  = useState(true)
  const [search,      setSearch]      = useState('')
  const [levelFilter, setLevelFilter] = useState('ALL')
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [error,       setError]       = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    getConfig()
      .then(cfg => {
        const withLogs = cfg.services.filter(s => s.log_path && s.enabled)
        setServices(withLogs)
        if (withLogs.length > 0) setSelected(withLogs[0].id)
      })
      .catch(() => setError('No se pudo conectar con el backend'))
  }, [])

  const load = useCallback(async () => {
    if (!selected) return
    setLoading(true)
    setError(null)
    try {
      const data = await getLogTail(selected, lineCount)
      setLines(data)
      setLastUpdated(new Date())
    } catch (e: any) {
      setError(`Error al cargar logs: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }, [selected, lineCount])

  // Initial load + when selection changes
  useEffect(() => { load() }, [load])

  // Auto-refresh interval
  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current)
    if (autoRefresh && selected) {
      intervalRef.current = setInterval(load, 5000)
    }
    return () => { if (intervalRef.current) clearInterval(intervalRef.current) }
  }, [autoRefresh, selected, load])

  // Auto-scroll to bottom
  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [lines, autoScroll])

  const filtered = lines.filter(l =>
    levelMatch(l, levelFilter) &&
    (search === '' || l.toLowerCase().includes(search.toLowerCase()))
  )

  return (
    <div className="p-6 space-y-4 h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Logs en Vivo</h1>
          <p className="text-gray-400 text-sm mt-1">
            {filtered.length} líneas
            {lastUpdated && (
              <span className="ml-2 text-gray-600">
                · Actualizado: {lastUpdated.toLocaleTimeString('es')}
              </span>
            )}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setAutoScroll(a => !a)}
            className={clsx(
              'px-3 py-2 rounded-lg text-sm transition-colors',
              autoScroll
                ? 'bg-sky-900/40 border border-sky-600/40 text-sky-400'
                : 'bg-gray-800 text-gray-400 hover:text-white'
            )}
            title="Auto-scroll al final"
          >
            ↓ Scroll
          </button>
          <button
            onClick={() => setAutoRefresh(a => !a)}
            className={clsx(
              'flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors',
              autoRefresh
                ? 'bg-green-900/40 border border-green-600/40 text-green-400'
                : 'bg-gray-800 text-gray-400 hover:text-white'
            )}
          >
            {autoRefresh ? <Pause size={14} /> : <Play size={14} />}
            {autoRefresh ? 'En vivo' : 'Pausado'}
          </button>
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors disabled:opacity-50"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Actualizar
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-950/40 border border-red-700/40 rounded-lg px-4 py-2 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Controls */}
      <div className="flex gap-2 flex-wrap">
        <select
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={selected}
          onChange={e => setSelected(e.target.value)}
        >
          {services.length === 0
            ? <option value="">Sin servicios con log configurado</option>
            : services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)
          }
        </select>
        <select
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={lineCount}
          onChange={e => setLineCount(Number(e.target.value))}
        >
          <option value={100}>100 líneas</option>
          <option value={200}>200 líneas</option>
          <option value={500}>500 líneas</option>
        </select>
        <select
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={levelFilter}
          onChange={e => setLevelFilter(e.target.value)}
        >
          <option value="ALL">Todos los niveles</option>
          <option value="ERROR">Solo ERROR/CRITICAL</option>
          <option value="WARNING">Solo WARNING</option>
          <option value="INFO">Solo INFO</option>
        </select>
        <div className="relative flex-1 min-w-[180px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            placeholder="Buscar en logs..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg pl-8 pr-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-sky-500"
          />
        </div>
      </div>

      {/* Log output */}
      <div
        className="flex-1 bg-gray-950 border border-gray-800 rounded-xl p-4 overflow-auto font-mono text-xs leading-5 min-h-0"
        style={{ maxHeight: 'calc(100vh - 280px)' }}
      >
        {services.length === 0 ? (
          <div className="text-gray-500 text-center py-8">
            No hay servicios con <code className="bg-gray-800 px-1 rounded">log_path</code> configurado.<br />
            <span className="text-xs mt-1 block">Configura la ruta del log en Panel → Configuración.</span>
          </div>
        ) : filtered.length === 0 && !loading ? (
          <div className="text-gray-500 text-center py-8">
            {lines.length === 0
              ? 'Sin líneas de log — ¿existe el archivo?'
              : 'Ninguna línea coincide con el filtro actual'}
          </div>
        ) : (
          <>
            {filtered.map((line, i) => (
              <div
                key={i}
                className={clsx('whitespace-pre-wrap break-all px-1 rounded-sm', colorLine(line))}
              >
                {line}
              </div>
            ))}
            <div ref={bottomRef} />
          </>
        )}
      </div>
    </div>
  )
}