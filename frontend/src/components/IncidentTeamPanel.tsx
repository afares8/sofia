import { useState } from 'react'

interface Incident {
  id: number; status: string; service_id: string; tenant_id: string
  environment: string; release: string; occurrences: number
  candidate_sha: string | null; reason: string | null; deployment_plan: string | null
  snapshot: string; snapshot_sha: string
}

const labels: Record<string, string> = {
  pending: 'En cola', analyzing: 'Analizando', preparing: 'Preparando corrección',
  reviewing: 'Revisión independiente', awaiting_ci: 'Esperando CI del candidato',
  awaiting_approval: 'Listo para tu decisión', deployment_requested: 'Plan aprobado · ejecutor desconectado',
  blocked: 'Bloqueado', uncertain: 'Requiere reconciliación', cancelled: 'Cancelado',
}

export default function IncidentTeamPanel() {
  // Operator key remains in component memory; never persisted in browser storage.
  const [key, setKey] = useState('')
  const [runs, setRuns] = useState<Incident[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<number | null>(null)
  const [rollback, setRollback] = useState('')
  const [artifact, setArtifact] = useState('')
  const [worker, setWorker] = useState<{ enabled: boolean; execution_enabled: boolean; scope_gaps: number; error_code: string | null } | null>(null)
  const [evidence, setEvidence] = useState<Record<number, string>>({})

  async function request(path = '', body?: object) {
    const response = await fetch(`/api/autonomy/incidents${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    if (!response.ok) throw new Error((await response.json()).detail || 'No se pudo completar la operación')
    return response.json()
  }

  async function act(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try {
      await action()
      const [list, status] = await Promise.all([request(), request('/status')])
      setRuns(list); setWorker(status)
    }
    catch (e) { setError(e instanceof Error ? e.message : 'Error al consultar incidentes') }
    finally { setBusy(false) }
  }

  return <section className="bg-gray-900 border border-sky-900 rounded-xl p-4 space-y-3">
    <h2 className="text-lg text-white font-semibold">Equipo de incidentes</h2>
    <p className="text-sm text-gray-400">Una tarjeta por empresa, entorno y versión. Análisis, corrección y revisión separados; CI verifica el commit. El despliegue requiere tu decisión y un ejecutor autorizado.</p>
    <div className="flex flex-wrap gap-2">
      <input type="password" autoComplete="off" aria-label="Clave de operador" placeholder="Clave de operador (solo esta sesión)"
        value={key} onChange={e => { setKey(e.target.value); setRuns([]); setWorker(null); setEvidence({}); setSelected(null) }}
        className="bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-white w-80" />
      <button disabled={!key || busy} onClick={() => act(async () => {})}
        className="bg-sky-800 rounded px-3 py-2 text-sm text-white disabled:opacity-40">Consultar incidentes</button>
    </div>
    {error && <p role="alert" className="text-red-300 text-sm">{error}</p>}
    {worker && <p className="text-sm text-gray-400">Detección: {worker.enabled ? 'habilitada' : 'apagada'} · Ejecución: {worker.execution_enabled ? 'sujeta a controles' : 'apagada'} · Ocurrencias sin contexto autorizado: {worker.scope_gaps}{worker.error_code ? ` · Atención: ${worker.error_code}` : ''}</p>}
    {runs.map(run => <article key={run.id} className="border border-gray-700 rounded-lg p-3 space-y-2 text-sm text-gray-300">
      <div className="flex flex-wrap justify-between gap-2"><strong>#{run.id} · {run.service_id} · {run.tenant_id}</strong><span>{labels[run.status] || run.status}</span></div>
      <p>{run.environment} · {run.release} · {run.occurrences} ocurrencia(s) agrupadas</p>
      {run.reason && <p className="text-amber-300">{run.reason}</p>}
      {run.candidate_sha && <p className="font-mono text-xs break-all">Candidato: {run.candidate_sha}</p>}
      <details><summary className="cursor-pointer">Evidencia del incidente</summary><pre className="whitespace-pre-wrap text-xs mt-2">{run.snapshot}</pre></details>
      <button disabled={busy} className="text-sky-300" onClick={() => act(async () => {
        const detail = await request(`/${run.id}`)
        setEvidence(previous => ({ ...previous, [run.id]: JSON.stringify(detail.evidence, null, 2) }))
      })}>Ver revisión y controles</button>
      {evidence[run.id] && <details open><summary>Revisión y controles registrados</summary><pre className="whitespace-pre-wrap text-xs mt-2 max-h-64 overflow-auto">{evidence[run.id]}</pre></details>}
      {['awaiting_ci', 'awaiting_approval'].includes(run.status) && <button disabled={busy} onClick={() => act(() => request(`/${run.id}/ci`, {}))}
        className="bg-gray-700 rounded px-3 py-2">Verificar CI</button>}
      {run.status === 'awaiting_approval' && <button disabled={busy} onClick={() => { setSelected(run.id); setRollback(''); setArtifact('') }}
        className="bg-sky-800 rounded px-3 py-2 ml-2">Revisar plan de despliegue</button>}
      {selected === run.id && run.status === 'awaiting_approval' && <form className="space-y-2" onSubmit={e => {
        e.preventDefault(); act(async () => { await request(`/${run.id}/approve-deployment`, {
          candidate_sha: run.candidate_sha, rollback_sha: rollback, rollback_artifact: artifact,
        }); setSelected(null) })
      }}>
        <p>Confirma la versión actualmente desplegada y su artefacto restaurable. La CI se verifica de nuevo al aprobar.</p>
        <input required pattern="[0-9a-f]{40}" aria-label="SHA de rollback" placeholder="SHA de la versión desplegada (40 caracteres)" value={rollback}
          onChange={e => setRollback(e.target.value)} className="block w-full bg-gray-950 border border-gray-700 rounded p-2" />
        <input required pattern="sha256:[0-9a-f]{64}" aria-label="Artefacto de rollback" placeholder="Artefacto de rollback: sha256:…" value={artifact}
          onChange={e => setArtifact(e.target.value)} className="block w-full bg-gray-950 border border-gray-700 rounded p-2" />
        <button disabled={busy} className="bg-sky-800 rounded px-3 py-2">Aprobar plan · sin ejecutar despliegue</button>
      </form>}
      {run.deployment_plan && <details><summary>Plan y condiciones de rollback</summary><pre className="whitespace-pre-wrap text-xs mt-2">{run.deployment_plan}</pre></details>}
    </article>)}
  </section>
}
