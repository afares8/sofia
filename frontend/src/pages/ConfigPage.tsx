import { useEffect, useState } from 'react'
import {
  getConfig, updateConfig, testAlert,
  MonitorConfig, ServiceConfig, AlertConfig, AlertRule,
} from '../api/client'
import { Save, Trash2, Plus, Send, CheckCircle, XCircle, Bell, AlertOctagon } from 'lucide-react'
import clsx from 'clsx'

const inputCls = 'w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-sky-500'
const labelCls = 'block text-xs text-gray-400 mb-1 font-medium'
const sectionCls = 'bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4'

const BLANK_SERVICE: ServiceConfig = {
  id: '', name: '', url: '', enabled: true,
  log_path: '', expected_status: 200, timeout_seconds: 5,
  failure_threshold: 3,
  restore_enabled: false,
  auto_restore: false,
}

const BLANK_RULE: AlertRule = {
  id: '', name: '', enabled: true,
  condition_type: 'error_count',
  threshold: 10,
  window_minutes: 60,
  service_id: null,
  cooldown_minutes: 30,
}

const CONDITION_LABEL: Record<string, string> = {
  error_count: 'Errores en ventana',
  response_ms: 'Response time (ms)',
  downtime_minutes: 'Downtime (min)',
  spike: 'Spike (x sobre promedio)',
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
      <div className="grid grid-cols-3 gap-3">
        <div>
          <label className={labelCls}>HTTP Status esperado</label>
          <input type="number" className={inputCls} value={svc.expected_status} onChange={e => onChange({ ...svc, expected_status: Number(e.target.value) })} />
        </div>
        <div>
          <label className={labelCls}>Timeout (segundos)</label>
          <input type="number" className={inputCls} value={svc.timeout_seconds} onChange={e => onChange({ ...svc, timeout_seconds: Number(e.target.value) })} />
        </div>
        <div>
          <label className={labelCls}>Failure threshold</label>
          <input type="number" className={inputCls} value={svc.failure_threshold} onChange={e => onChange({ ...svc, failure_threshold: Number(e.target.value) })} />
        </div>
      </div>

      {/* Restore */}
      <div className="border-t border-gray-700 pt-3 space-y-2">
        <label className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer">
          <input type="checkbox"
            checked={!!svc.restore_enabled}
            onChange={e => onChange({ ...svc, restore_enabled: e.target.checked,
              ...(e.target.checked ? {} : { auto_restore: false }) })}
            className="accent-sky-500" />
          <span className="font-medium">Restauración habilitada</span>
          <span className="text-gray-500">(permite restaurar este servicio cuando se cae)</span>
        </label>
        <label className={clsx('flex items-center gap-2 text-xs text-gray-300 cursor-pointer',
          !svc.restore_enabled && 'opacity-50 pointer-events-none')}>
          <input type="checkbox"
            checked={!!svc.auto_restore}
            disabled={!svc.restore_enabled}
            onChange={e => onChange({ ...svc, auto_restore: e.target.checked })}
            className="accent-yellow-500" />
          <span className="font-medium">Auto-restaurar (sin confirmación)</span>
        </label>
        {svc.auto_restore && (
          <div className="text-xs text-yellow-400 bg-yellow-950/40 border border-yellow-700/40 rounded px-2 py-1.5 flex items-start gap-1.5">
            <AlertOctagon size={13} className="mt-0.5 shrink-0" />
            <span>Si activas auto-restaurar, Sofia restaurará este servicio sin preguntarte por WhatsApp.</span>
          </div>
        )}
      </div>
    </div>
  )
}

function RuleForm({
  rule, onChange, onDelete, isNew = false, services,
}: {
  rule: AlertRule
  onChange: (r: AlertRule) => void
  onDelete?: () => void
  isNew?: boolean
  services: ServiceConfig[]
}) {
  return (
    <div className={clsx('border rounded-lg p-4 space-y-3', isNew ? 'border-sky-600/40 bg-sky-950/20' : 'border-gray-700 bg-gray-800/40')}>
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-white">{isNew ? 'Nueva regla' : rule.name || rule.id}</span>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer">
            <input type="checkbox" checked={rule.enabled}
              onChange={e => onChange({ ...rule, enabled: e.target.checked })} className="accent-sky-500" />
            Activa
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
          <label className={labelCls}>ID</label>
          <input className={inputCls} value={rule.id} onChange={e => onChange({ ...rule, id: e.target.value })} placeholder="ej: mi_regla" />
        </div>
        <div>
          <label className={labelCls}>Nombre</label>
          <input className={inputCls} value={rule.name} onChange={e => onChange({ ...rule, name: e.target.value })} placeholder="Descripción visible" />
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div>
          <label className={labelCls}>Condición</label>
          <select className={inputCls} value={rule.condition_type}
            onChange={e => onChange({ ...rule, condition_type: e.target.value })}>
            {Object.entries(CONDITION_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div>
          <label className={labelCls}>Umbral</label>
          <input type="number" className={inputCls} value={rule.threshold}
            onChange={e => onChange({ ...rule, threshold: Number(e.target.value) })} />
        </div>
        <div>
          <label className={labelCls}>Ventana (min)</label>
          <input type="number" className={inputCls} value={rule.window_minutes}
            onChange={e => onChange({ ...rule, window_minutes: Number(e.target.value) })} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>Servicio (vacío = todos)</label>
          <select className={inputCls} value={rule.service_id ?? ''}
            onChange={e => onChange({ ...rule, service_id: e.target.value || null })}>
            <option value="">Todos los servicios</option>
            {services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </div>
        <div>
          <label className={labelCls}>Cooldown (min)</label>
          <input type="number" className={inputCls} value={rule.cooldown_minutes}
            onChange={e => onChange({ ...rule, cooldown_minutes: Number(e.target.value) })} />
        </div>
      </div>
    </div>
  )
}

export default function ConfigPage() {
  const [cfg,       setCfg]       = useState<MonitorConfig | null>(null)
  const [saved,     setSaved]     = useState(false)
  const [newSvc,    setNewSvc]    = useState<ServiceConfig | null>(null)
  const [newRule,   setNewRule]   = useState<AlertRule | null>(null)
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
  const updateRule   = (i: number, r: AlertRule) =>
    setCfg(c => c ? { ...c, alert_rules: c.alert_rules.map((x, idx) => idx === i ? r : x) } : c)
  const removeRule   = (i: number) =>
    setCfg(c => c ? { ...c, alert_rules: c.alert_rules.filter((_, idx) => idx !== i) } : c)
  const addRule      = () => {
    if (!newRule || !newRule.id || !newRule.name) return
    setCfg(c => c ? { ...c, alert_rules: [...c.alert_rules, newRule] } : c)
    setNewRule(null)
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

        {/* Escalation */}
        <div className="border-t border-gray-800 pt-3 space-y-2">
          <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
            <input type="checkbox" checked={!!cfg.alerts.escalation_enabled}
              onChange={e => updateAlerts({ ...cfg.alerts, escalation_enabled: e.target.checked })}
              className="accent-sky-500" />
            Habilitar escalación si nadie responde
          </label>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className={labelCls}>Minutos antes de escalar</label>
              <input type="number" className={inputCls}
                value={cfg.alerts.escalation_minutes ?? 15}
                onChange={e => updateAlerts({ ...cfg.alerts, escalation_minutes: Number(e.target.value) })} />
            </div>
            <div>
              <label className={labelCls}>Números adicionales (separados por coma)</label>
              <input className={inputCls}
                value={(cfg.alerts.escalation_numbers ?? []).join(',')}
                onChange={e => updateAlerts({ ...cfg.alerts, escalation_numbers: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })}
                placeholder="50711112222, 50733334444" />
            </div>
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

      {/* Alert rules */}
      <div className={sectionCls}>
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-white flex items-center gap-2">
            <Bell size={14} /> Reglas de alerta
          </h2>
          <button
            onClick={() => setNewRule(BLANK_RULE)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-sky-600 hover:bg-sky-500 rounded-lg text-xs text-white font-medium transition-colors"
          >
            <Plus size={13} /> Agregar regla
          </button>
        </div>
        <p className="text-xs text-gray-500">
          Las reglas se evalúan cada 2 minutos. Cuando una condición se cumple, Sofia envía una
          alerta por WhatsApp (respetando el cooldown por regla).
        </p>

        {newRule && (
          <div className="space-y-2">
            <RuleForm rule={newRule} onChange={setNewRule} isNew services={cfg.services} />
            <div className="flex gap-2 justify-end">
              <button onClick={() => setNewRule(null)} className="px-3 py-1.5 text-xs text-gray-400 hover:text-white">Cancelar</button>
              <button onClick={addRule} className="px-3 py-1.5 bg-sky-600 hover:bg-sky-500 rounded-lg text-xs text-white font-medium">Agregar</button>
            </div>
          </div>
        )}

        <div className="space-y-3">
          {(cfg.alert_rules ?? []).map((r, i) => (
            <RuleForm
              key={r.id}
              rule={r}
              onChange={updated => updateRule(i, updated)}
              onDelete={() => removeRule(i)}
              services={cfg.services}
            />
          ))}
          {(!cfg.alert_rules || cfg.alert_rules.length === 0) && (
            <div className="text-xs text-gray-500 px-3 py-4 text-center bg-gray-800/40 border border-gray-800 rounded-lg">
              Sin reglas configuradas
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
