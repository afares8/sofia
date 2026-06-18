import { useEffect, useState, useCallback } from 'react'
import {
  RefreshCw, CheckCircle, XCircle, Clock, Loader2, Ban, AlertTriangle,
  Bot, User, Hand,
} from 'lucide-react'
import clsx from 'clsx'
import {
  getRestores, triggerRestore, getConfig,
  RestoreEntry, ServiceConfig,
} from '../api/client'

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
  const [services, setServices] = useState<ServiceConfig[]>([])
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState<string | null>(null)
  const [triggering, setTriggering] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const load = useCallback(async () => {
    try {
      const [data, cfg] = await Promise.all([
        getRestores(),
        getConfig().catch(() => null),
      ])
      data.sort((a, b) => {
        const active = ['pending', 'confirmed', 'running']
        const aActive = active.includes(a.status) ? 1 : 0
        const bActive = active.includes(b.status) ? 1 : 0
        if (aActive !== bActive) return bActive - aActive
        return new Date(b.requested_at ?? 0).getTime() - new Date(a.requested_at ?? 0).getTime()
      })
      setEntries(data)
      if (cfg) setServices(cfg.services)
      setLastUpdated(new Date())
      setError(null)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(`No se pudo cargar: ${msg}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(() => { load() }, 5000)
    return () => clearInterval(id)
  }, [load])

  const onTrigger = async (id: string) => {
    setTriggering(id)
    try {
      await triggerRestore(id)
      await load()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(`No se pudo lanzar: ${msg}`)
    } finally {
      setTriggering(null)
    }
  }

  const active   = entries.filter(e => ['pending', 'confirmed', 'running'].includes(e.status))
  const finished = entries.filter(e => !['pending', 'confirmed', 'running'].includes(e.status))
  const restorableServices = services.filter(s => s.restore_enabled)

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Restauraciones</h1>
          <p className="text-gray-400 text-sm mt-1">
            Restauraciones manuales, vía WhatsApp o automáticas
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

      {/* Manual trigger panel */}
      {restorableServices.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-3">
            <Hand size={14} className="text-sky-400" />
            <h2 className="text-sm font-semibold text-white uppercase tracking-wide">Restaurar manualmente</h2>
          </div>
          <div className="flex flex-wrap gap-2">
            {restorableServices.map(s => (
              <button
                key={s.id}
                onClick={() => onTrigger(s.id)}
                disabled={triggering === s.id}
                className={clsx(
                  'flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                  triggering === s.id
                    ? 'bg-gray-700 text-gray-400 cursor-wait'
                    : 'bg-sky-600 hover:bg-sky-500 text-white',
                )}
              >
                {triggering === s.id ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
                {s.name}
              </button>
            ))}
          </div>
          <p className="text-xs text-gray-500 mt-3">
            El restore se lanza sin pasar por WhatsApp. Solo se muestran los servicios con
            <span className="text-gray-300 font-medium"> "Restauración habilitada"</span> en config.
          </p>
        </div>
      )}

      {/* How it works */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 text-sm text-gray-400 space-y-1">
        <div className="text-white font-medium mb-2">¿Cómo funciona?</div>
        <div>1. Sofia detecta que un servicio lleva 3 chequeos fallidos consecutivos (≈90s)</div>
        <div>2. Si <strong className="text-white">auto-restore</strong> está activo: restaura sin preguntar.</div>
        <div>3. Si está OFF: envía WhatsApp <span className="text-yellow-400 font-mono">"🔴 Mayor caído. Responde SI MAYOR..."</span></div>
        <div>4. Sofia ejecuta Codex (si está instalado) o cae a un script PowerShell de fallback.</div>
        <div>5. Si falla, reintenta hasta 3 veces con backoff exponencial.</div>
        <div>6. Para Mayor/Packing también verifica que el middleware SAP DIAPI responda.</div>
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
              {active.map(e => <RestoreCard key={`a-${e.service_id}-${e.requested_at}`} entry={e} />)}
            </div>
          )}

          {finished.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Historial</h2>
              {finished.map(e => <RestoreCard key={`h-${e.service_id}-${e.requested_at}`} entry={e} />)}
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

function Badge({ children, tone }: { children: React.ReactNode; tone: 'green' | 'blue' | 'purple' | 'gray' }) {
  const toneCls =
    tone === 'green'  ? 'bg-green-900/40 text-green-400 border-green-500/30' :
    tone === 'blue'   ? 'bg-sky-900/40 text-sky-400 border-sky-500/30' :
    tone === 'purple' ? 'bg-purple-900/40 text-purple-400 border-purple-500/30' :
                        'bg-gray-800 text-gray-400 border-gray-700'
  return (
    <span className={clsx('text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border font-bold inline-flex items-center gap-1', toneCls)}>
      {children}
    </span>
  )
}

function RestoreCard({ entry }: { entry: RestoreEntry }) {
  const meta = STATUS_META[entry.status] ?? STATUS_META.expired

  return (
    <div className={clsx('border rounded-xl p-4 space-y-2', meta.color)}>
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2 font-semibold text-white">
          {meta.icon}
          {entry.service_name}
        </div>
        <div className="flex items-center gap-1.5">
          {entry.trigger_mode === 'auto' && <Badge tone="green"><Bot size={10} /> AUTO</Badge>}
          {entry.trigger_mode === 'manual' && <Badge tone="blue"><User size={10} /> MANUAL</Badge>}
          {entry.restore_method === 'codex'      && <Badge tone="purple">CODEX</Badge>}
          {entry.restore_method === 'devin'      && <Badge tone="purple">AI LEGACY</Badge>}
          {entry.restore_method === 'ps1_script' && <Badge tone="gray">PS1</Badge>}
          {entry.retry_count != null && entry.retry_count > 0 && (
            <Badge tone="gray">retry {entry.retry_count}</Badge>
          )}
          <span className={clsx('text-xs font-bold uppercase tracking-wide px-2 py-0.5 rounded border', meta.color)}>
            {meta.label}
          </span>
        </div>
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

      {entry.devin_output && (
        <details className="text-xs">
          <summary className="cursor-pointer text-gray-500 hover:text-gray-300 select-none py-1">
            🤖 Ver output del restore
          </summary>
          <pre className="mt-1 bg-gray-950 rounded p-3 overflow-auto max-h-64 text-gray-400 whitespace-pre-wrap break-words leading-4">
            {entry.devin_output}
          </pre>
        </details>
      )}

      {entry.status === 'pending' && (
        <div className="text-xs text-yellow-500 animate-pulse">
          ⏳ Esperando respuesta por WhatsApp — responde <strong>SI {entry.service_id.toUpperCase()}</strong> o <strong>NO</strong>
        </div>
      )}
      {entry.status === 'running' && (
        <div className="text-xs text-blue-400 animate-pulse">
          ⚙️ Restore en ejecución — esperando health check…
        </div>
      )}
    </div>
  )
}
