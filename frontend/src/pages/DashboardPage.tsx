import { useEffect, useState, useCallback } from 'react'
import { getStatuses, forceCheck, getIssues, ServiceStatus, Issue } from '../api/client'
import { RefreshCw, CheckCircle, XCircle, Clock, Wifi, WifiOff } from 'lucide-react'
import clsx from 'clsx'

function StatusCard({ s, onRefresh }: { s: ServiceStatus; onRefresh: (id: string) => void }) {
  const isUp = s.status === 'up'
  const isDown = s.status === 'down'
  return (
    <div className={clsx(
      'rounded-xl border p-4 flex flex-col gap-3 transition-all',
      isUp   && 'bg-gray-900 border-green-500/30',
      isDown && 'bg-gray-900 border-red-500/40 shadow-red-900/20 shadow-lg',
      !isUp && !isDown && 'bg-gray-900 border-gray-700',
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

      <div className="flex items-center gap-2">
        {isUp   && <CheckCircle size={20} className="text-green-400" />}
        {isDown && <XCircle     size={20} className="text-red-400" />}
        {!isUp && !isDown && <Clock size={20} className="text-gray-500" />}
        <span className={clsx(
          'font-bold uppercase text-sm tracking-wide',
          isUp && 'text-green-400', isDown && 'text-red-400', !isUp && !isDown && 'text-gray-500',
        )}>
          {s.status}
        </span>
        {s.status_code && (
          <span className="ml-auto text-xs text-gray-500">HTTP {s.status_code}</span>
        )}
      </div>

      <div className="text-xs text-gray-500 flex gap-3">
        {s.response_ms != null && <span>{s.response_ms} ms</span>}
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

export default function DashboardPage() {
  const [statuses, setStatuses] = useState<ServiceStatus[]>([])
  const [events,   setEvents]   = useState<Issue[]>([])
  const [loading,  setLoading]  = useState(true)

  const load = useCallback(async () => {
    try {
      const [s, e] = await Promise.all([
        getStatuses(),
        getIssues({ limit: 20, since_hours: 24 }),
      ])
      setStatuses(s)
      setEvents(e)
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

  const upCount   = statuses.filter(s => s.status === 'up').length
  const downCount = statuses.filter(s => s.status === 'down').length

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
      <div className="grid grid-cols-3 gap-4">
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
            <StatusCard key={s.id} s={s} onRefresh={handleRefresh} />
          ))}
        </div>
      )}

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
