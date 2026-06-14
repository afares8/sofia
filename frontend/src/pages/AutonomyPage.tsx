import { useCallback, useEffect, useState } from 'react'
import clsx from 'clsx'
import {
  Bot, Shield, Power, RefreshCw, Play, GitBranch, AlertTriangle,
  CheckCircle, XCircle, Loader2,
} from 'lucide-react'
import {
  AIJob, GithubSyncRun, getAIJobs, getAutonomyConfig, getGithubSyncRuns,
  setKillSwitch, createAIJob, promoteAIJob, triggerGithubSync, AutonomyConfig, GithubSyncConfig,
  updateAutonomyConfig, updateGithubSyncConfig, AuditEvent, getAuditEvents, AppRepoConfig,
} from '../api/client'

function badge(status: string) {
  const cls =
    ['success', 'completed', 'verified', 'promoted'].includes(status) ? 'text-green-300 bg-green-900/30 border-green-700/30' :
    ['blocked', 'failed', 'apply_failed'].includes(status) ? 'text-red-300 bg-red-900/30 border-red-700/30' :
    ['running', 'pending'].includes(status) ? 'text-sky-300 bg-sky-900/30 border-sky-700/30' :
    'text-gray-300 bg-gray-800 border-gray-700'
  return <span className={clsx('px-2 py-0.5 rounded border text-xs font-bold uppercase', cls)}>{status}</span>
}

export default function AutonomyPage() {
  const [autonomy, setAutonomyState] = useState<AutonomyConfig | null>(null)
  const [syncCfg, setSyncCfg] = useState<GithubSyncConfig | null>(null)
  const [repos, setRepos] = useState<AppRepoConfig[]>([])
  const [jobs, setJobs] = useState<AIJob[]>([])
  const [syncRuns, setSyncRuns] = useState<GithubSyncRun[]>([])
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [goal, setGoal] = useState('')
  const [mode, setMode] = useState<'plan' | 'fix'>('plan')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    const [cfg, js, sr, ae] = await Promise.all([
      getAutonomyConfig(),
      getAIJobs(),
      getGithubSyncRuns(),
      getAuditEvents(),
    ])
    setAutonomyState(cfg.autonomy)
    setRepos(cfg.app_repos)
    setSyncCfg(cfg.github_sync)
    setJobs(js)
    setSyncRuns(sr)
    setAudit(ae)
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [load])

  const saveAutonomy = async (patch: Partial<AutonomyConfig>) => {
    if (!autonomy) return
    const next = { ...autonomy, ...patch }
    setAutonomyState(next)
    await updateAutonomyConfig(next)
    await load()
  }

  const saveSync = async (patch: Partial<GithubSyncConfig>) => {
    if (!syncCfg) return
    const next = { ...syncCfg, ...patch }
    setSyncCfg(next)
    await updateGithubSyncConfig(next)
    await load()
  }

  const startJob = async () => {
    if (!goal.trim()) return
    setBusy(true)
    setMsg(null)
    try {
      const r = await createAIJob({ goal, mode, autonomy_level: mode === 'fix' ? 3 : 1 })
      setGoal('')
      setMsg(`Job #${r.job_id} iniciado.`)
      await load()
    } catch (e: any) {
      setMsg(`Error: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const toggleKill = async () => {
    if (!autonomy) return
    await setKillSwitch(!autonomy.kill_switch)
    await load()
  }

  const runSync = async () => {
    setBusy(true)
    try {
      await triggerGithubSync(true)
      setMsg('GitHub sync iniciado en background.')
      await load()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <Bot className="text-sky-400" /> AI Engineer
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Devin ejecuta, Sofia verifica con policy + AI verifier antes de confiar.
          </p>
        </div>
        <button onClick={load} className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300">
          <RefreshCw size={14} /> Actualizar
        </button>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-3">
        <div className="flex items-center gap-2 text-white font-semibold">
          <Shield size={16} className="text-purple-400" /> Repos controlados
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {repos.map(r => (
            <div key={r.id} className="bg-gray-950/60 border border-gray-800 rounded-lg p-3 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-white">{r.name}</span>
                {badge(`L${r.autonomy_level}`)}
              </div>
              <div className="text-xs text-gray-500 font-mono truncate mt-1">{r.path}</div>
              <div className="flex gap-2 mt-2 text-xs">
                {r.autofix_enabled ? badge('autofix:on') : badge('autofix:off')}
                {r.enabled ? badge('enabled') : badge('disabled')}
              </div>
            </div>
          ))}
        </div>
      </div>

      {msg && <div className="text-sm text-sky-300 bg-sky-900/20 border border-sky-700/30 rounded-lg px-4 py-2">{msg}</div>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-3">
          <div className="flex items-center gap-2 text-white font-semibold">
            <Shield size={16} className="text-green-400" /> Guardrails
          </div>
          {autonomy && (
            <>
              <Toggle label="Autonomía habilitada" value={autonomy.enabled} onChange={v => saveAutonomy({ enabled: v })} />
              <Toggle label="Verifier obligatorio" value={autonomy.require_verifier} onChange={v => saveAutonomy({ require_verifier: v })} />
              <Toggle label="Tests requeridos" value={autonomy.require_tests_for_code_fixes} onChange={v => saveAutonomy({ require_tests_for_code_fixes: v })} />
              <Toggle label="Solo aplicar con humano" value={autonomy.require_human_for_apply} onChange={v => saveAutonomy({ require_human_for_apply: v })} />
              <Toggle label="Autofix desde issues" value={autonomy.auto_create_jobs_from_issues} onChange={v => saveAutonomy({ auto_create_jobs_from_issues: v })} />
              <Toggle label="Smoke checks" value={autonomy.run_smoke_checks} onChange={v => saveAutonomy({ run_smoke_checks: v })} />
              <Toggle label="Auto-promover riesgo bajo" value={autonomy.auto_promote_low_risk} onChange={v => saveAutonomy({ auto_promote_low_risk: v })} />
              <label className="flex items-center justify-between gap-3 bg-gray-950/60 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-300">
                <span>Modo de promoción</span>
                <select
                  value={autonomy.promotion_mode}
                  onChange={e => saveAutonomy({ promotion_mode: e.target.value })}
                  className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200"
                >
                  <option value="pr">PR (GitHub)</option>
                  <option value="branch">Solo rama</option>
                  <option value="manual">Manual</option>
                </select>
              </label>
              <div className="text-xs text-gray-500">
                Límite: {autonomy.max_files_changed} archivos / {autonomy.max_lines_changed} líneas (tests no cuentan).
              </div>
              <div className="text-xs text-gray-500">
                Watchdog: jobs colgados &gt; {autonomy.job_timeout_minutes} min se marcan failed.
              </div>
              <div className="text-xs text-gray-500">
                Sandbox: <span className="font-mono">{autonomy.sandbox_root}</span>
              </div>
              <button
                onClick={toggleKill}
                className={clsx(
                  'w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-bold border',
                  autonomy.kill_switch
                    ? 'bg-red-900/30 border-red-700 text-red-300'
                    : 'bg-green-900/20 border-green-700/30 text-green-300',
                )}
              >
                <Power size={14} /> {autonomy.kill_switch ? 'KILL SWITCH ACTIVO' : 'Kill switch apagado'}
              </button>
            </>
          )}
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-3 lg:col-span-2">
          <div className="flex items-center gap-2 text-white font-semibold">
            <Play size={16} className="text-sky-400" /> Nueva sesión Devin
          </div>
          <textarea
            value={goal}
            onChange={e => setGoal(e.target.value)}
            placeholder="Ej: Investiga el issue 242 de Packing, identifica causa raíz y propone fix seguro..."
            className="w-full min-h-28 bg-gray-950 border border-gray-800 rounded-lg p-3 text-sm text-gray-200 outline-none focus:border-sky-700"
          />
          <div className="flex items-center gap-2">
            <select
              value={mode}
              onChange={e => setMode(e.target.value as 'plan' | 'fix')}
              className="bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-300"
            >
              <option value="plan">Plan / diagnóstico</option>
              <option value="fix">Fix en repo local</option>
            </select>
            <button
              onClick={startJob}
              disabled={busy || !goal.trim()}
              className="flex items-center gap-2 px-3 py-2 bg-sky-600 hover:bg-sky-500 disabled:opacity-40 rounded-lg text-sm text-white"
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Bot size={14} />} Lanzar
            </button>
          </div>
          <div className="text-xs text-gray-500 flex items-center gap-1">
            <AlertTriangle size={12} /> Modo fix requiere autonomía ON, sin kill switch y level ≥ 3.
          </div>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-white font-semibold">
            <GitBranch size={16} className="text-purple-400" /> GitHub nightly sync
          </div>
          <button onClick={runSync} disabled={busy} className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300">
            <RefreshCw size={14} /> Ejecutar sync
          </button>
        </div>
        {syncCfg && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
            <Toggle label="Sync habilitado" value={syncCfg.enabled} onChange={v => saveSync({ enabled: v })} />
            <Toggle label="Push a medianoche" value={syncCfg.auto_push_at_midnight} onChange={v => saveSync({ auto_push_at_midnight: v })} />
            <Toggle label="Secret scan obligatorio" value={syncCfg.require_clean_secret_scan} onChange={v => saveSync({ require_clean_secret_scan: v })} />
          </div>
        )}
        <div className="text-xs text-gray-500">
          No sube .env, data, logs, node_modules ni dist. Si detecta secretos o demasiados archivos, bloquea el push.
        </div>
      </div>

      <Section title="Jobs AI Engineer">
        {jobs.length === 0 ? <Empty text="Sin jobs todavía." /> : jobs.map(j => <JobCard key={j.id} job={j} onChange={load} />)}
      </Section>

      <Section title="Auditoría">
        {audit.length === 0 ? <Empty text="Sin eventos de auditoría." /> : audit.map(e => <AuditCard key={e.id} event={e} />)}
      </Section>

      <Section title="GitHub sync runs">
        {syncRuns.length === 0 ? <Empty text="Sin sincronizaciones." /> : syncRuns.map(r => <SyncCard key={r.id} run={r} />)}
      </Section>
    </div>
  )
}

function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center justify-between gap-3 bg-gray-950/60 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-300">
      <span>{label}</span>
      <input type="checkbox" checked={value} onChange={e => onChange(e.target.checked)} />
    </label>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">{title}</h2>
      {children}
    </div>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center text-sm text-gray-600">{text}</div>
}

function JobCard({ job, onChange }: { job: AIJob; onChange: () => void }) {
  const [promoting, setPromoting] = useState(false)
  const [promoteMsg, setPromoteMsg] = useState<string | null>(null)

  const promote = async () => {
    setPromoting(true)
    setPromoteMsg(null)
    try {
      const r = await promoteAIJob(job.id)
      setPromoteMsg(r.message)
      await onChange()
    } catch (e: any) {
      setPromoteMsg(`Error: ${e.message}`)
    } finally {
      setPromoting(false)
    }
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="text-white font-medium">Job #{job.id} · {job.mode} · {job.repo_id ?? 'repo?'}</div>
        <div className="flex items-center gap-2">
          {job.status === 'verified' && (
            <button
              onClick={promote}
              disabled={promoting}
              className="flex items-center gap-1 px-2 py-1 bg-green-700 hover:bg-green-600 disabled:opacity-40 rounded text-xs font-bold text-white"
            >
              {promoting ? <Loader2 size={12} className="animate-spin" /> : <GitBranch size={12} />} Promover
            </button>
          )}
          {job.pr_url && (
            <a href={job.pr_url} target="_blank" rel="noreferrer" className="text-xs text-sky-400 hover:underline">Ver PR</a>
          )}
          {job.risk && badge(job.risk)} {badge(job.status)}
        </div>
      </div>
      {promoteMsg && <div className="text-xs text-sky-300 bg-sky-900/20 border border-sky-700/30 rounded px-2 py-1">{promoteMsg}</div>}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2 text-xs text-gray-500">
        <span>Sandbox: <span className="font-mono">{job.sandbox_path ? job.sandbox_path.split(/[\\/]/).slice(-2).join('/') : '—'}</span></span>
        <span>Branch: <span className="font-mono">{job.work_branch ?? job.branch_name ?? '—'}</span></span>
        <span>Commit: <span className="font-mono">{job.commit_sha ? job.commit_sha.slice(0, 8) : '—'}</span></span>
      </div>
      <div className="flex flex-wrap gap-2">
        {job.tests_status && badge(`tests:${job.tests_status}`)}
        {job.smoke_status && badge(`smoke:${job.smoke_status}`)}
        {job.verifier_status && badge(`verifier:${job.verifier_status}`)}
      </div>
      <div className="text-sm text-gray-300 whitespace-pre-wrap">{job.goal}</div>
      {job.result_message && <div className="text-xs text-gray-400">{job.result_message}</div>}
      {job.blocked_reason && <div className="text-xs text-red-300">{job.blocked_reason}</div>}
      {(job.devin_output || job.verifier_output || job.diff_summary) && (
        <details className="text-xs">
          <summary className="cursor-pointer text-gray-500 hover:text-gray-300">Ver detalles</summary>
          {job.diff_summary && <pre className="mt-2 bg-gray-950 p-3 rounded overflow-auto max-h-48 text-gray-400">{job.diff_summary}</pre>}
          {job.tests_output && <pre className="mt-2 bg-gray-950 p-3 rounded overflow-auto max-h-48 text-gray-400 whitespace-pre-wrap">{job.tests_output}</pre>}
          {job.smoke_output && <pre className="mt-2 bg-gray-950 p-3 rounded overflow-auto max-h-48 text-gray-400 whitespace-pre-wrap">{job.smoke_output}</pre>}
          {job.verifier_output && <pre className="mt-2 bg-gray-950 p-3 rounded overflow-auto max-h-48 text-gray-400">{job.verifier_output}</pre>}
          {job.devin_output && <pre className="mt-2 bg-gray-950 p-3 rounded overflow-auto max-h-72 text-gray-400 whitespace-pre-wrap">{job.devin_output}</pre>}
        </details>
      )}
    </div>
  )
}

function AuditCard({ event }: { event: AuditEvent }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm text-white">
          {event.event_type} <span className="text-gray-500">· {event.entity_type}{event.entity_id ? ` #${event.entity_id}` : ''}</span>
        </div>
        <span className="text-xs text-gray-600">{new Date(event.created_at).toLocaleString('es')}</span>
      </div>
      {event.message && <div className="text-xs text-gray-400 mt-1">{event.message}</div>}
      {event.data && <pre className="text-xs text-gray-500 mt-2 bg-gray-950 rounded p-2 overflow-auto max-h-32">{event.data}</pre>}
    </div>
  )
}

function SyncCard({ run }: { run: GithubSyncRun }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="text-white font-medium">{run.repo_id} · {run.branch ?? 'branch?'}</div>
        {badge(run.status)}
      </div>
      <div className="text-xs text-gray-500 font-mono">{run.repo_path}</div>
      <div className="flex gap-4 text-xs text-gray-400">
        <span>{run.files_changed} archivos</span>
        <span>{run.pushed ? <CheckCircle size={12} className="inline text-green-400" /> : <XCircle size={12} className="inline text-gray-600" />} pushed</span>
        {run.commit_sha && <span className="font-mono">{run.commit_sha.slice(0, 8)}</span>}
      </div>
      {run.error_msg && <div className="text-xs text-red-300">{run.error_msg}</div>}
    </div>
  )
}
