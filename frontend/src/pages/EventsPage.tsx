import { useEffect, useState } from 'react'
import { getEvents, purgeEvents, ErrorEvent } from '../api/client'
import clsx from 'clsx'
import { Trash2, ChevronDown, ChevronUp } from 'lucide-react'

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

function EventRow({ e }: { e: ErrorEvent }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <button
        className="w-full px-4 py-3 flex items-start gap-3 text-left hover:bg-gray-800/50 transition-colors"
        onClick={() => setOpen(o => !o)}
      >
        <LevelBadge level={e.level} />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-white truncate">{e.message}</div>
          <div className="text-xs text-gray-500 mt-0.5">
            {e.service_name} · {new Date(e.timestamp).toLocaleString('es')} · {e.source === 'active' ? '⚡ SDK' : '📄 Log'}
            {e.notified && <span className="ml-2 text-green-500">✓ Notificado</span>}
          </div>
        </div>
        {open ? <ChevronUp size={14} className="text-gray-500 mt-1 shrink-0" /> : <ChevronDown size={14} className="text-gray-500 mt-1 shrink-0" />}
      </button>
      {open && (
        <div className="px-4 pb-4 space-y-2 border-t border-gray-800">
          {e.detail && (
            <div>
              <div className="text-xs text-gray-400 mb-1 font-medium">Detalle</div>
              <div className="text-xs text-gray-300 bg-gray-950 rounded p-2">{e.detail}</div>
            </div>
          )}
          {e.traceback && (
            <div>
              <div className="text-xs text-gray-400 mb-1 font-medium">Traceback</div>
              <pre className="text-xs text-red-300 bg-gray-950 rounded p-2 overflow-auto max-h-48">{e.traceback}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function EventsPage() {
  const [events,     setEvents]     = useState<ErrorEvent[]>([])
  const [loading,    setLoading]    = useState(true)
  const [serviceId,  setServiceId]  = useState('')
  const [level,      setLevel]      = useState('')
  const [sinceHours, setSinceHours] = useState(24)

  const load = async () => {
    setLoading(true)
    try {
      const data = await getEvents({
        service_id: serviceId || undefined,
        level:      level || undefined,
        since_hours: sinceHours,
        limit: 500,
      })
      setEvents(data)
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [serviceId, level, sinceHours])

  const handlePurge = async () => {
    if (!confirm('¿Eliminar todos los eventos antiguos?')) return
    await purgeEvents(7)
    load()
  }

  return (
    <div className="p-6 space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Errores & Eventos</h1>
          <p className="text-gray-400 text-sm mt-1">{events.length} eventos</p>
        </div>
        <button onClick={handlePurge} className="flex items-center gap-2 px-3 py-2 bg-red-900/30 hover:bg-red-900/50 border border-red-700/30 rounded-lg text-sm text-red-400 transition-colors">
          <Trash2 size={14} /> Purgar +7 días
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3">
        <input
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-sky-500"
          placeholder="Filtrar por servicio ID..."
          value={serviceId}
          onChange={e => setServiceId(e.target.value)}
        />
        <select
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={level}
          onChange={e => setLevel(e.target.value)}
        >
          <option value="">Todos los niveles</option>
          <option value="CRITICAL">CRITICAL</option>
          <option value="ERROR">ERROR</option>
          <option value="WARNING">WARNING</option>
          <option value="INFO">INFO</option>
        </select>
        <select
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={sinceHours}
          onChange={e => setSinceHours(Number(e.target.value))}
        >
          <option value={1}>Última hora</option>
          <option value={6}>Últimas 6h</option>
          <option value={24}>Últimas 24h</option>
          <option value={72}>Últimos 3 días</option>
          <option value={168}>Última semana</option>
        </select>
      </div>

      {/* List */}
      {loading ? (
        <div className="text-gray-500 text-center py-12">Cargando...</div>
      ) : events.length === 0 ? (
        <div className="text-gray-500 text-sm bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
          Sin eventos en este período
        </div>
      ) : (
        <div className="space-y-2">
          {events.map(e => <EventRow key={e.id} e={e} />)}
        </div>
      )}
    </div>
  )
}
