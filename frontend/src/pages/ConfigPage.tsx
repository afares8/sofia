import { useEffect, useState } from 'react'
import {
  getConfig, updateConfig, deleteService, testAlert,
  MonitorConfig, ServiceConfig, AlertConfig
} from '../api/client'
import { Save, Trash2, Plus, Send, CheckCircle, XCircle } from 'lucide-react'
import clsx from 'clsx'

const inputCls = 'w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-sky-500'
const labelCls = 'block text-xs text-gray-400 mb-1 font-medium'
const sectionCls = 'bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4'

const BLANK_SERVICE: ServiceConfig = {
  id: '', name: '', url: '', enabled: true,
  log_path: '', expected_status: 200, timeout_seconds: 5,
}

function ServiceForm({
  svc, onChange, onDelete, isNew = false,
}: {
  svc: ServiceConfig
  onChange: (s: ServiceConfig) => void
  onDelete?: () => void
  isNew?: boolean
}) {
  return (
    <div className={clsx('border rounded-lg p-4 space-y-3', isNew ? 'border-sky-600/40 bg-sky-950/20' : 'border-gray-700 bg-gray-800/40')}>
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-white">{isNew ? 'Nuevo servicio' : svc.name || svc.id}</span>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer">
            <input type="checkbox" checked={svc.enabled} onChange={e => onChange({ ...svc, enabled: e.target.checked })} className="accent-sky-500" />
            Activo
          </label>
          {onDelete && (
            <button onClick={onDelete} className="text-red-500 hover:text-red-400 transition-colors" title="Eliminar">
              <Trash2 size={14} />
            </button>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>ID (único, sin espacios)</label>
          <input className={inputCls} value={svc.id} onChange={e => onChange({ ...svc, id: e.target.value })} placeholder="ej: mi-app" />
        </div>
        <div>
          <label className={labelCls}>Nombre</label>
          <input className={inputCls} value={svc.name} onChange={e => onChange({ ...svc, name: e.target.value })} placeholder="Mi App" />
        </div>
      </div>
      <div>
        <label className={labelCls}>URL de Health Check</label>
        <input className={inputCls} value={svc.url} onChange={e => onChange({ ...svc, url: e.target.value })} placeholder="http://localhost:8000/health" />
      </div>
      <div>
        <label className={labelCls}>Ruta del archivo de log (opcional)</label>
        <input className={inputCls} value={svc.log_path ?? ''} onChange={e => onChange({ ...svc, log_path: e.target.value })} placeholder="D:/mi-app/backend/logs/app.log" />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>HTTP Status esperado</label>
          <input type="number" className={inputCls} value={svc.expected_status} onChange={e => onChange({ ...svc, expected_status: Number(e.target.value) })} />
        </div>
        <div>
          <label className={labelCls}>Timeout (segundos)</label>
          <input type="number" className={inputCls} value={svc.timeout_seconds} onChange={e => onChange({ ...svc, timeout_seconds: Number(e.target.value) })} />
        </div>
      </div>
    </div>
  )
}

export default function ConfigPage() {
  const [cfg,       setCfg]       = useState<MonitorConfig | null>(null)
  const [saved,     setSaved]     = useState(false)
  const [newSvc,    setNewSvc]    = useState<ServiceConfig | null>(null)
  const [testState, setTestState] = useState<'idle' | 'sending' | 'ok' | 'fail'>('idle')

  useEffect(() => { getConfig().then(setCfg) }, [])

  const save = async () => {
    if (!cfg) return
    await updateConfig(cfg)
    setSaved(true)
    setTimeout(() => setSaved(false), 2500)
  }

  const handleTest = async () => {
    setTestState('sending')
    try {
      await testAlert()
      setTestState('ok')
    } catch {
      setTestState('fail')
    }
    setTimeout(() => setTestState('idle'), 4000)
  }

  const updateAlerts = (a: AlertConfig) => setCfg(c => c ? { ...c, alerts: a } : c)
  const updateSvc    = (i: number, s: ServiceConfig) =>
    setCfg(c => c ? { ...c, services: c.services.map((x, idx) => idx === i ? s : x) } : c)
  const removeSvc    = (i: number) =>
    setCfg(c => c ? { ...c, services: c.services.filter((_, idx) => idx !== i) } : c)
  const addSvc       = () => {
    if (!newSvc || !newSvc.id || !newSvc.name || !newSvc.url) return
    setCfg(c => c ? { ...c, services: [...c.services, newSvc] } : c)
    setNewSvc(null)
  }

  if (!cfg) return <div className="p-6 text-gray-500">Cargando configuración...</div>

  return (
    <div className="p-6 space-y-6 max-w-3xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Configuración</h1>
          <p className="text-gray-400 text-sm mt-1">Todos los cambios se guardan en config.json y aplican al instante</p>
        </div>
        <button
          onClick={save}
          className={clsx(
            'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors',
            saved ? 'bg-green-600 text-white' : 'bg-sky-600 hover:bg-sky-500 text-white',
          )}
        >
          {saved ? <><CheckCircle size={14} /> Guardado</> : <><Save size={14} /> Guardar todo</>}
        </button>
      </div>

      {/* General */}
      <div className={sectionCls}>
        <h2 className="text-base font-semibold text-white">General</h2>
        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className={labelCls}>Intervalo de polling (seg)</label>
            <input type="number" className={inputCls} value={cfg.poll_interval_seconds}
              onChange={e => setCfg({ ...cfg, poll_interval_seconds: Number(e.target.value) })} />
          </div>
          <div>
            <label className={labelCls}>Líneas de log a leer</label>
            <input type="number" className={inputCls} value={cfg.log_tail_lines}
              onChange={e => setCfg({ ...cfg, log_tail_lines: Number(e.target.value) })} />
          </div>
          <div>
            <label className={labelCls}>Retención de errores (días)</label>
            <input type="number" className={inputCls} value={cfg.error_retention_days}
              onChange={e => setCfg({ ...cfg, error_retention_days: Number(e.target.value) })} />
          </div>
        </div>
      </div>

      {/* Alerts / WhatsApp */}
      <div className={sectionCls}>
        <h2 className="text-base font-semibold text-white">Alertas WhatsApp</h2>
        <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
          <input type="checkbox" checked={cfg.alerts.whatsapp_enabled}
            onChange={e => updateAlerts({ ...cfg.alerts, whatsapp_enabled: e.target.checked })}
            className="accent-sky-500" />
          Habilitar alertas de WhatsApp
        </label>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className={labelCls}>Número de WhatsApp (con código de país)</label>
            <input className={inputCls} value={cfg.alerts.whatsapp_number}
              onChange={e => updateAlerts({ ...cfg.alerts, whatsapp_number: e.target.value })}
              placeholder="50766662916" />
          </div>
          <div>
            <label className={labelCls}>Cooldown entre alertas (minutos)</label>
            <input type="number" className={inputCls} value={cfg.alerts.cooldown_minutes}
              onChange={e => updateAlerts({ ...cfg.alerts, cooldown_minutes: Number(e.target.value) })} />
          </div>
        </div>
        <div>
          <label className={labelCls}>URL de WppConnect</label>
          <input className={inputCls} value={cfg.alerts.wppconnect_url}
            onChange={e => updateAlerts({ ...cfg.alerts, wppconnect_url: e.target.value })}
            placeholder="http://localhost:21465" />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className={labelCls}>Token de WppConnect</label>
            <input className={inputCls} value={cfg.alerts.wppconnect_token}
              onChange={e => updateAlerts({ ...cfg.alerts, wppconnect_token: e.target.value })}
              placeholder="THISISMYSECURETOKEN" />
          </div>
          <div>
            <label className={labelCls}>Sesión de WppConnect</label>
            <input className={inputCls} value={cfg.alerts.wppconnect_session}
              onChange={e => updateAlerts({ ...cfg.alerts, wppconnect_session: e.target.value })}
              placeholder="default" />
          </div>
        </div>
        <button
          onClick={handleTest}
          disabled={testState === 'sending'}
          className={clsx(
            'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors',
            testState === 'ok'   && 'bg-green-600 text-white',
            testState === 'fail' && 'bg-red-700 text-white',
            testState === 'idle' || testState === 'sending' ? 'bg-gray-700 hover:bg-gray-600 text-white' : '',
          )}
        >
          {testState === 'ok'      && <><CheckCircle size={14} /> Mensaje enviado</>}
          {testState === 'fail'    && <><XCircle size={14} /> Error al enviar</>}
          {testState === 'sending' && 'Enviando...'}
          {testState === 'idle'    && <><Send size={14} /> Enviar alerta de prueba</>}
        </button>
      </div>

      {/* Services */}
      <div className={sectionCls}>
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-white">Servicios monitoreados</h2>
          <button
            onClick={() => setNewSvc(BLANK_SERVICE)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-sky-600 hover:bg-sky-500 rounded-lg text-xs text-white font-medium transition-colors"
          >
            <Plus size={13} /> Agregar servicio
          </button>
        </div>

        {newSvc && (
          <div className="space-y-2">
            <ServiceForm svc={newSvc} onChange={setNewSvc} isNew />
            <div className="flex gap-2 justify-end">
              <button onClick={() => setNewSvc(null)} className="px-3 py-1.5 text-xs text-gray-400 hover:text-white">Cancelar</button>
              <button onClick={addSvc} className="px-3 py-1.5 bg-sky-600 hover:bg-sky-500 rounded-lg text-xs text-white font-medium">Agregar</button>
            </div>
          </div>
        )}

        <div className="space-y-3">
          {cfg.services.map((s, i) => (
            <ServiceForm
              key={s.id}
              svc={s}
              onChange={updated => updateSvc(i, updated)}
              onDelete={() => removeSvc(i)}
            />
          ))}
        </div>
      </div>
    </div>
  )
}
