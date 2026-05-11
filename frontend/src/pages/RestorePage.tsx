import { useEffect, useState, useCallback } from 'react'
import { RefreshCw, CheckCircle, XCircle, Clock, Loader2, Ban, AlertTriangle } from 'lucide-react'
import clsx from 'clsx'

interface RestoreEntry {
  service_id: string
  service_name: string
  status: 'pending' | 'confirmed' | 'running' | 'success' | 'failed' | 'rejected' | 'expired'
  requested_at: string | null
  confirmed_at: string | null
  finished_at: string | null
  result_message: string | null
}

function timeAgo(iso: string | null) {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'ahora'
  if (m < 60) return `hace ${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `hace ${h}h`
  return `hace ${Math.floor(h / 24)}d`
}

const STATUS_META: Record<string, { label: string; color: string; icon: React.ReactNode }> = {
  pending:   { label: 'Esperando confirmación', color: 'text-yellow-400 border-yellow-500/40 bg-yellow-950/20', icon: <Clock size={16} /> },
  confirmed: { label: 'Confirmado',             color: 'text-sky-400 border-sky-500/40 bg-sky-950/20',         icon: <CheckCircle size={16} /> },
  running:   { label: 'Restaurando…',           color: 'text-blue-400 border-blue-500/40 bg-blue-950/20',      icon: <Loader2 size={16} className="animate-spin" /> },
  success:   { label: 'Restaurado',             color: 'text-green-400 border-green-500/40 bg-green-950/20',   icon: <CheckCircle size={16} /> },
  failed:    { label: 'Falló',                  color: 'text-red-400 border-red-500/40 bg-red-950/20',         icon: <XCircle size={16} /> },
  rejected:  { label: 'Cancelado',              color: 'text-gray-400 border-gray-700 bg-gray-900',            icon: <Ban size={16} /> },
  expired:   { label: 'Expirado',               color: 'text-gray-500 border-gray-700 bg-gray-900',            icon: <AlertTriangle size={16} /> },
}

export default function RestorePage() {
  const [entries,  setEntries]  = useState<RestoreEntry[]>([])
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/restore/')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: RestoreEntry[] = await res.json()
      // Sort: active first (pending/running), then by requested_at desc
      data.sort((a, b) => {
        const active = ['pending', 'confirmed', 'running']
        const aActive = active.includes(a.status) ? 1 : 0
        const bActive = active.includes(b.status) ? 1 : 0
        if (aActive !== bActive) return bActive - aActive
        return new Date(b.requested_at ?? 0).getTime() - new Date(a.requested_at ?? 0).getTime()
      })
      setEntries(data)
      setLastUpdated(new Date())
      setError(null)
    } catch (e: any) {
      setError(`No se pudo cargar: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    // Auto-refresh every 5s when there are active restores
    const id = setInterval(() => {
      load()
    }, 5000)
    return () => clearInterval(id)
  }, [load])

  const active = entries.filter(e => ['pending', 'confirmed', 'running'].includes(e.status))
  const finished = entries.filter(e => !['pending', 'confirmed', 'running'].includes(e.status))

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Restauraciones</h1>
          <p className="text-gray-400 text-sm mt-1">
            Restauraciones automáticas vía WhatsApp
            {lastUpdated && (
              <span className="ml-2 text-gray-600">· {lastUpdated.toLocaleTimeString('es')}</span>
            )}
          </p>
        </div>
        <button
          onClick={load}
          className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors"
        >
          <RefreshCw size={14} /> Actualizar
        </button>
      </div>

      {error && (
        <div className="bg-red-950/40 border border-red-700/40 rounded-lg px-4 py-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* How it works */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 text-sm text-gray-400 space-y-1">
        <div className="text-white font-medium mb-2">¿Cómo funciona?</div>
        <div>1. Sofia detecta que un servicio lleva 3 chequeos fallidos consecutivos (≈90s)</div>
        <div>2. Envía WhatsApp: <span className="text-yellow-400 font-mono">"🔴 Mayor caído. Responde SI MAYOR para restaurar."</span></div>
        <div>3. Tú respondes <span className="text-green-400 font-mono">SI MAYOR</span> (o <span className="text-green-400 font-mono">SI PACKING</span> / <span className="text-green-400 font-mono">SI PANTALLA</span>)</div>
        <div>4. Sofia mata huérfanos, lanza el proceso y espera hasta 3 minutos que levante</div>
        <div>5. Para Mayor y Packing también verifica que el middleware SAP DIAPI responda</div>
        <div>6. Te confirma el resultado por WhatsApp</div>
        <div className="text-gray-500 text-xs mt-2">
          Responde <span className="font-mono">NO</span> para cancelar · Tienes 5 minutos para confirmar
        </div>
      </div>

      {/* Active restores */}
      {loading ? (
        <div className="text-gray-500 text-center py-12">Cargando...</div>
      ) : (
        <>
          {active.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">En progreso</h2>
              {active.map(e => <RestoreCard key={e.service_id} entry={e} />)}
            </div>
          )}

          {finished.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Historial</h2>
              {finished.map(e => <RestoreCard key={`${e.service_id}-${e.requested_at}`} entry={e} />)}
            </div>
          )}

          {entries.length === 0 && (
            <div className="text-gray-500 text-sm bg-gray-900 border border-gray-800 rounded-xl p-10 text-center">
              <CheckCircle size={32} className="text-green-500 mx-auto mb-3" />
              Sin restauraciones registradas
            </div>
          )}
        </>
      )}
    </div>
  )
}

function RestoreCard({ entry }: { entry: RestoreEntry }) {
  const meta = STATUS_META[entry.status] ?? STATUS_META.expired

  return (
    <div className={clsx('border rounded-xl p-4 space-y-2', meta.color)}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 font-semibold text-white">
          {meta.icon}
          {entry.service_name}
        </div>
        <span className={clsx('text-xs font-bold uppercase tracking-wide px-2 py-0.5 rounded border', meta.color)}>
          {meta.label}
        </span>
      </div>

      <div className="flex flex-wrap gap-4 text-xs text-gray-400">
        {entry.requested_at && (
          <span title={new Date(entry.requested_at).toLocaleString('es')}>
            Solicitado: {timeAgo(entry.requested_at)}
          </span>
        )}
        {entry.confirmed_at && (
          <span title={new Date(entry.confirmed_at).toLocaleString('es')}>
            Confirmado: {timeAgo(entry.confirmed_at)}
          </span>
        )}
        {entry.finished_at && (
          <span title={new Date(entry.finished_at).toLocaleString('es')}>
            Finalizado: {timeAgo(entry.finished_at)}
          </span>
        )}
      </div>

      {entry.result_message && (
        <div className="text-xs text-gray-300 bg-gray-950/50 rounded px-3 py-2">
          {entry.result_message}
        </div>
      )}

      {entry.status === 'pending' && (
        <div className="text-xs text-yellow-500 animate-pulse">
          ⏳ Esperando respuesta por WhatsApp — responde <strong>SI {entry.service_id.toUpperCase()}</strong> o <strong>NO</strong>
        </div>
      )}
      {entry.status === 'running' && (
        <div className="text-xs text-blue-400 animate-pulse">
          ⚙️ Script en ejecución — esperando health check…
        </div>
      )}
    </div>
  )
}
