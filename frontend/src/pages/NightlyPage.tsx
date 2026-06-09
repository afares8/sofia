import { useEffect, useState, useCallback, useRef } from 'react'
import {
  getNightlyReports, getNightlyReport, getProposalRuns,
  triggerNightlyRun, approveNightlyReport, rejectNightlyReport,
  approveAndApplyProposal, applyBatchProposal,
  NightlyReport, Proposal, ProposalRun,
} from '../api/client'
import clsx from 'clsx'
import {
  Moon, Play, X, RefreshCw, AlertTriangle, Zap, Shield,
  Clock, CheckCircle, XCircle, Loader, ChevronDown, ChevronUp,
  FileText, Terminal, Timer, BarChart2,
} from 'lucide-react'

// ─────────────────────────────────────────────────────────────────────────────
// Tiny helpers
// ─────────────────────────────────────────────────────────────────────────────

function fmt(iso: string | null, full = false) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (full) return d.toLocaleString('es', { dateStyle: 'short', timeStyle: 'short' })
  const diff = Date.now() - d.getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'ahora'
  if (m < 60) return `hace ${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `hace ${h}h`
  return `hace ${Math.floor(h / 24)}d`
}

function dur(s: number | null) {
  if (s == null) return '—'
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

// ─────────────────────────────────────────────────────────────────────────────
// Badge components
// ─────────────────────────────────────────────────────────────────────────────

function ReportStatusBadge({ status }: { status: string }) {
  const cfg: Record<string, { cls: string; label: string }> = {
    pending:      { cls: 'bg-yellow-900/50 text-yellow-300 border-yellow-700/40', label: 'Pendiente' },
    approved:     { cls: 'bg-sky-900/50 text-sky-300 border-sky-700/40',          label: 'Aprobado' },
    rejected:     { cls: 'bg-gray-800 text-gray-400 border-gray-700',             label: 'Rechazado' },
    applied:      { cls: 'bg-green-900/50 text-green-300 border-green-700/40',    label: 'Aplicado' },
    apply_failed: { cls: 'bg-red-900/50 text-red-300 border-red-700/40',          label: 'Error al aplicar' },
  }
  const c = cfg[status] ?? { cls: 'bg-gray-800 text-gray-400', label: status }
  return (
    <span className={clsx('px-2 py-0.5 rounded text-xs font-bold uppercase tracking-wide border', c.cls)}>
      {c.label}
    </span>
  )
}

function RunStatusIcon({ status }: { status: string }) {
  if (status === 'running') return <Loader size={14} className="text-sky-400 animate-spin" />
  if (status === 'success') return <CheckCircle size={14} className="text-green-400" />
  return <XCircle size={14} className="text-red-400" />
}

function ConfidencePip({ v }: { v: string }) {
  const map: Record<string, string> = {
    high: 'bg-green-400', medium: 'bg-yellow-400', low: 'bg-gray-600',
  }
  const labels: Record<string, string> = { high: 'Alta', medium: 'Media', low: 'Baja' }
  return (
    <span className="flex items-center gap-1 text-xs text-gray-400">
      <span className={clsx('w-2 h-2 rounded-full', map[v] ?? 'bg-gray-600')} />
      {labels[v] ?? v}
    </span>
  )
}

function RiskChip({ v }: { v: string }) {
  const map: Record<string, string> = {
    low:    'bg-green-900/30 text-green-400 border-green-700/30',
    medium: 'bg-yellow-900/30 text-yellow-400 border-yellow-700/30',
    high:   'bg-red-900/30 text-red-400 border-red-700/30',
  }
  return (
    <span className={clsx('px-1.5 py-0.5 rounded text-xs border', map[v] ?? 'bg-gray-700 text-gray-400')}>
      Riesgo {v}
    </span>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// ProposalRow — one row in the proposals table with inline apply
// ─────────────────────────────────────────────────────────────────────────────

function ProposalRow({
  proposal, index, reportId, reportStatus, run, onApplied,
}: {
  proposal: Proposal
  index: number
  reportId: number
  reportStatus: string
  run: ProposalRun | undefined   // last run for this proposal index
  onApplied: () => void
}) {
  const [expanded, setExpanded]   = useState(false)
  const [outputOpen, setOutputOpen] = useState(false)
  const [applying, setApplying]   = useState(false)
  const [errMsg, setErrMsg]       = useState<string | null>(null)

  const canApply = reportStatus !== 'rejected' && run?.status !== 'running'
  const isApplying = run?.status === 'running' || applying

  const handleApply = async () => {
    if (!confirm(`Aplicar fix individual: "${proposal.title}"?\n\nDevin editará SOLO este archivo.`)) return
    setApplying(true)
    setErrMsg(null)
    try {
      await approveAndApplyProposal(reportId, index)
      onApplied()
    } catch (e: any) {
      setErrMsg(e.message)
    } finally {
      setApplying(false)
    }
  }

  const handleBatchApply = async () => {
    if (!confirm(`Aplicar BATCH para "${proposal.service_id}"?\n\nEsto puede agrupar hasta 3 fixes del mismo servicio en una sola sesión de Devin.`)) return
    setApplying(true)
    setErrMsg(null)
    try {
      await applyBatchProposal(reportId, index)
      onApplied()
    } catch (e: any) {
      setErrMsg(e.message)
    } finally {
      setApplying(false)
    }
  }

  return (
    <>
      {/* Main row */}
      <tr
        className={clsx(
          'border-t border-gray-800/60 cursor-pointer transition-colors',
          expanded ? 'bg-gray-800/20' : 'hover:bg-gray-800/10',
        )}
        onClick={() => setExpanded(o => !o)}
      >
        {/* # */}
        <td className="px-3 py-3 text-xs font-mono text-gray-600 w-8">#{index + 1}</td>

        {/* Title + service */}
        <td className="px-3 py-3">
          <div className="text-sm text-white font-medium leading-snug">{proposal.title}</div>
          <div className="text-xs text-gray-500 mt-0.5">{proposal.service_id}</div>
        </td>

        {/* File */}
        <td className="px-3 py-3 hidden md:table-cell">
          {proposal.file_path ? (
            <span className="font-mono text-xs text-gray-500 truncate max-w-[220px] block">
              {proposal.file_path.split(/[\\/]/).slice(-2).join('/')}
              {proposal.line_hint ? `:${proposal.line_hint}` : ''}
            </span>
          ) : (
            <span className="text-gray-700 text-xs">—</span>
          )}
        </td>

        {/* Confidence */}
        <td className="px-3 py-3 text-center"><ConfidencePip v={proposal.confidence} /></td>

        {/* Risk */}
        <td className="px-3 py-3 text-center hidden sm:table-cell">
          <RiskChip v={proposal.risk} />
        </td>

        {/* Run status */}
        <td className="px-3 py-3 text-center">
          {run ? (
            <div className="flex flex-col items-center gap-0.5">
              <RunStatusIcon status={run.status} />
              {run.duration_s != null && (
                <span className="text-xs text-gray-600">{dur(run.duration_s)}</span>
              )}
            </div>
          ) : (
            <span className="text-gray-700 text-xs">—</span>
          )}
        </td>

        {/* Apply button */}
        <td className="px-3 py-3 text-right" onClick={e => e.stopPropagation()}>
          {run?.status === 'success' ? (
            <span className="flex items-center gap-1 justify-end text-xs text-green-400">
              <CheckCircle size={12} /> Aplicado
            </span>
          ) : run?.status === 'failed' ? (
            <div className="flex flex-col items-end gap-1">
              <span className="flex items-center gap-1 text-xs text-red-400">
                <XCircle size={12} /> Falló
              </span>
              {canApply && (
                <button
                  onClick={handleApply}
                  disabled={isApplying}
                  className="flex items-center gap-1 px-2 py-1 rounded bg-red-900/30 hover:bg-red-900/50 border border-red-700/30 text-red-300 text-xs transition-colors disabled:opacity-40"
                >
                  <RefreshCw size={10} /> Reintentar
                </button>
              )}
            </div>
          ) : canApply ? (
            <div className="flex items-center gap-1.5">
              <button
                onClick={handleApply}
                disabled={isApplying}
                className="flex items-center gap-1 px-2.5 py-1.5 rounded bg-sky-900/40 hover:bg-sky-800/60 border border-sky-700/40 text-sky-300 text-xs transition-colors disabled:opacity-40 whitespace-nowrap"
              >
                {isApplying
                  ? <><Loader size={11} className="animate-spin" /> Aplicando…</>
                  : <><Zap size={11} /> Aplicar</>}
              </button>
              <button
                onClick={handleBatchApply}
                disabled={isApplying}
                title="Aplicar en batch (hasta 3 fixes del mismo servicio)"
                className="flex items-center gap-1 px-2 py-1.5 rounded bg-yellow-900/30 hover:bg-yellow-900/50 border border-yellow-700/30 text-yellow-300 text-xs transition-colors disabled:opacity-40 whitespace-nowrap"
              >
                <><Zap size={10} /> x3</>
              </button>
            </div>
          ) : null}
          {errMsg && (
            <div className="text-xs text-red-400 mt-1 max-w-[160px] text-right">{errMsg}</div>
          )}
        </td>

        {/* Expand chevron */}
        <td className="px-2 py-3 text-gray-600">
          {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        </td>
      </tr>

      {/* Expanded detail row */}
      {expanded && (
        <tr className="bg-gray-950/60">
          <td colSpan={8} className="px-6 py-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-5xl">
              <div>
                <div className="text-xs text-gray-500 uppercase tracking-wide font-medium mb-1">Causa raíz</div>
                <div className="text-sm text-gray-300 bg-gray-900 rounded-lg p-3 border border-gray-800">
                  {proposal.root_cause}
                </div>
              </div>
              <div>
                <div className="text-xs text-gray-500 uppercase tracking-wide font-medium mb-1">Propuesta de fix</div>
                <div className="text-sm text-gray-200 bg-gray-900 rounded-lg p-3 border border-gray-800 whitespace-pre-wrap">
                  {proposal.proposal}
                </div>
              </div>
              {proposal.file_path && (
                <div className="md:col-span-2">
                  <div className="flex items-center gap-2 text-xs font-mono text-gray-500 bg-gray-900 border border-gray-800 rounded-lg px-3 py-2">
                    <FileText size={12} />
                    {proposal.file_path}{proposal.line_hint ? `:${proposal.line_hint}` : ''}
                  </div>
                </div>
              )}

              {/* Run output if exists */}
              {run && (
                <div className="md:col-span-2">
                  <button
                    className="flex items-center gap-2 text-xs text-gray-400 hover:text-white mb-2 transition-colors"
                    onClick={() => setOutputOpen(o => !o)}
                  >
                    <Terminal size={12} />
                    Output de Devin
                    {run.duration_s != null && (
                      <span className="text-gray-600 flex items-center gap-1">
                        <Timer size={11} /> {dur(run.duration_s)}
                      </span>
                    )}
                    {outputOpen ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                  </button>
                  {outputOpen && run.devin_output && (
                    <pre className="text-xs text-gray-300 bg-gray-900 border border-gray-800 rounded-lg p-3 overflow-auto max-h-80 whitespace-pre-wrap">
                      {run.devin_output}
                    </pre>
                  )}
                  {outputOpen && !run.devin_output && (
                    <div className="text-xs text-gray-600 italic">Sin output guardado.</div>
                  )}
                  {run.error_msg && (
                    <div className="text-xs text-red-400 mt-1 flex items-center gap-1">
                      <XCircle size={11} /> {run.error_msg}
                    </div>
                  )}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// ReportDetail — full single-report view
// ─────────────────────────────────────────────────────────────────────────────

function ReportDetail({ reportId, onBack }: { reportId: number; onBack: () => void }) {
  const [report,  setReport]  = useState<NightlyReport | null>(null)
  const [runs,    setRuns]    = useState<ProposalRun[]>([])
  const [loading, setLoading] = useState(true)
  const [acting,  setActing]  = useState(false)
  const [msg,     setMsg]     = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    try {
      const [r, rs] = await Promise.all([
        getNightlyReport(reportId),
        getProposalRuns(reportId),
      ])
      setReport(r)
      setRuns(rs)
    } finally {
      setLoading(false)
    }
  }, [reportId])

  useEffect(() => {
    load()
    // Poll while any run is 'running'
    pollRef.current = setInterval(async () => {
      const rs = await getProposalRuns(reportId)
      setRuns(rs)
      const anyRunning = rs.some(r => r.status === 'running')
      if (!anyRunning && pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
        load() // reload report status too
      }
    }, 3000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [load, reportId])

  const handleReject = async () => {
    const reason = prompt('Motivo del rechazo (opcional):') ?? ''
    setActing(true)
    try {
      await rejectNightlyReport(reportId, reason)
      await load()
      setMsg('Reporte rechazado.')
    } catch (e: any) { setMsg(`Error: ${e.message}`) }
    finally { setActing(false) }
  }

  if (loading) return <div className="text-gray-500 py-16 text-center">Cargando reporte…</div>
  if (!report)  return <div className="text-gray-500 py-16 text-center">Reporte no encontrado.</div>

  const proposals: Proposal[] = Array.isArray(report.proposals) ? report.proposals : []

  // Derived stats
  const byConf   = { high: 0, medium: 0, low: 0 } as Record<string, number>
  proposals.forEach(p => { byConf[p.confidence] = (byConf[p.confidence] ?? 0) + 1 })
  const runMap   = new Map<number, ProposalRun>()
  runs.forEach(r => {
    const prev = runMap.get(r.proposal_index)
    if (!prev || r.id > prev.id) runMap.set(r.proposal_index, r)
  })
  const applied  = [...runMap.values()].filter(r => r.status === 'success').length
  const failed   = [...runMap.values()].filter(r => r.status === 'failed').length
  const running  = [...runMap.values()].filter(r => r.status === 'running').length
  const totalDur = runs.reduce((s, r) => s + (r.duration_s ?? 0), 0)

  const isPending  = report.status === 'pending'
  const isApproved = report.status === 'approved'
  const isRejected = report.status === 'rejected'

  return (
    <div className="space-y-5">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-sm">
        <button onClick={onBack} className="text-gray-400 hover:text-white transition-colors">
          Revisión Nocturna
        </button>
        <span className="text-gray-700">/</span>
        <span className="text-white font-medium">Reporte #{report.id}</span>
        <ReportStatusBadge status={report.status} />
      </div>

      {/* KPI bar */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
        {[
          { icon: <BarChart2 size={14} />, label: 'Issues analizados', val: report.issues_analyzed.toString(), dim: false },
          { icon: <Zap size={14} />,       label: 'Propuestas',         val: proposals.length.toString(), dim: false },
          { icon: null,                    label: 'Alta confianza',      val: byConf.high?.toString() ?? '0', dim: false },
          { icon: <CheckCircle size={14} />, label: 'Aplicados',         val: applied.toString(), dim: applied === 0 },
          { icon: <XCircle size={14} />,   label: 'Fallidos',            val: failed.toString(), dim: failed === 0 },
          { icon: <Timer size={14} />,     label: 'Tiempo total Devin',  val: totalDur > 0 ? dur(totalDur) : '—', dim: totalDur === 0 },
        ].map(({ icon, label, val, dim }) => (
          <div key={label} className="bg-gray-900 border border-gray-800 rounded-xl p-3">
            <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-1">
              {icon} {label}
            </div>
            <div className={clsx('text-xl font-bold', dim ? 'text-gray-600' : 'text-white')}>{val}</div>
          </div>
        ))}
      </div>

      {/* Period + timestamps */}
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500">
        <span className="flex items-center gap-1">
          <Clock size={11} />
          Período: {fmt(report.period_start, true)} → {fmt(report.period_end, true)}
        </span>
        {report.approved_at && (
          <span>Aprobado: {fmt(report.approved_at, true)}</span>
        )}
        {report.applied_at && (
          <span>Aplicado: {fmt(report.applied_at, true)}</span>
        )}
        {report.rejected_at && (
          <span>Rechazado: {fmt(report.rejected_at, true)}</span>
        )}
        {running > 0 && (
          <span className="flex items-center gap-1 text-sky-400 animate-pulse">
            <Loader size={11} className="animate-spin" /> {running} fix aplicándose…
          </span>
        )}
      </div>

      {/* Action bar — only when actionable */}
      {(isPending || isApproved) && !isRejected && (
        <div className="flex gap-2 flex-wrap items-center">
          <div className="text-xs text-gray-500 mr-2">
            Aplicá cada fix individualmente con el botón "Aplicar" en la tabla.
          </div>
          <button
            onClick={handleReject}
            disabled={acting}
            className="flex items-center gap-2 px-3 py-1.5 bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 text-xs rounded-lg transition-colors disabled:opacity-50"
          >
            <X size={12} /> Rechazar reporte
          </button>
        </div>
      )}

      {msg && (
        <div className="text-sm text-sky-300 bg-sky-900/20 border border-sky-700/30 rounded-lg px-4 py-2">{msg}</div>
      )}

      {/* Proposals table */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Zap size={14} className="text-sky-400" />
            <h3 className="text-sm font-semibold text-white">Propuestas de fix</h3>
          </div>
          <div className="flex items-center gap-3 text-xs">
            <span className="flex items-center gap-1 text-green-400">
              <span className="w-2 h-2 rounded-full bg-green-400" /> Alta: {byConf.high ?? 0}
            </span>
            <span className="flex items-center gap-1 text-yellow-400">
              <span className="w-2 h-2 rounded-full bg-yellow-400" /> Media: {byConf.medium ?? 0}
            </span>
            <span className="flex items-center gap-1 text-gray-500">
              <span className="w-2 h-2 rounded-full bg-gray-600" /> Baja: {byConf.low ?? 0}
            </span>
          </div>
        </div>

        {proposals.length === 0 ? (
          <div className="py-12 text-center text-gray-600 text-sm">Sin propuestas en este reporte.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-950/50">
                <tr className="text-xs text-gray-500 uppercase tracking-wide">
                  <th className="px-3 py-2 text-left w-8">#</th>
                  <th className="px-3 py-2 text-left">Fix</th>
                  <th className="px-3 py-2 text-left hidden md:table-cell">Archivo</th>
                  <th className="px-3 py-2 text-center">Confianza</th>
                  <th className="px-3 py-2 text-center hidden sm:table-cell">Riesgo</th>
                  <th className="px-3 py-2 text-center">Estado</th>
                  <th className="px-3 py-2 text-right">Acción</th>
                  <th className="px-2 py-2 w-6" />
                </tr>
              </thead>
              <tbody>
                {proposals.map((p, i) => (
                  <ProposalRow
                    key={i}
                    proposal={p}
                    index={i}
                    reportId={report.id}
                    reportStatus={report.status}
                    run={runMap.get(i)}
                    onApplied={load}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Notes */}
      {report.notes && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">Notas</div>
          <div className="text-sm text-gray-300">{report.notes}</div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// ReportListRow — one row in the reports list
// ─────────────────────────────────────────────────────────────────────────────

function ReportListRow({ report, onClick }: { report: NightlyReport; onClick: () => void }) {
  const proposals: Proposal[] = Array.isArray(report.proposals) ? report.proposals : []
  const highConf = proposals.filter(p => p.confidence === 'high').length
  const medConf  = proposals.filter(p => p.confidence === 'medium').length

  return (
    <tr
      onClick={onClick}
      className="border-t border-gray-800 cursor-pointer hover:bg-gray-800/30 transition-colors group"
    >
      <td className="px-4 py-3">
        <span className="font-mono text-gray-400 text-sm">#{report.id}</span>
      </td>
      <td className="px-4 py-3">
        <div className="text-sm text-white">
          {new Date(report.created_at).toLocaleDateString('es', { day: '2-digit', month: 'short', year: 'numeric' })}
        </div>
        <div className="text-xs text-gray-500">
          {new Date(report.created_at).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })}
        </div>
      </td>
      <td className="px-4 py-3 text-center">
        <span className="text-white font-semibold">{report.issues_analyzed}</span>
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-white font-semibold">{proposals.length}</span>
          <div className="flex gap-1">
            {highConf > 0 && (
              <span className="flex items-center gap-0.5 text-xs text-green-400">
                <span className="w-1.5 h-1.5 rounded-full bg-green-400" />{highConf}
              </span>
            )}
            {medConf > 0 && (
              <span className="flex items-center gap-0.5 text-xs text-yellow-400">
                <span className="w-1.5 h-1.5 rounded-full bg-yellow-400" />{medConf}
              </span>
            )}
          </div>
        </div>
      </td>
      <td className="px-4 py-3"><ReportStatusBadge status={report.status} /></td>
      <td className="px-4 py-3 text-xs text-gray-500">{fmt(report.created_at)}</td>
      <td className="px-3 py-3 text-gray-700 group-hover:text-gray-400 transition-colors">›</td>
    </tr>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Main page
// ─────────────────────────────────────────────────────────────────────────────

export default function NightlyPage() {
  const [reports,  setReports]  = useState<NightlyReport[]>([])
  const [loading,  setLoading]  = useState(true)
  const [selected, setSelected] = useState<number | null>(null)
  const [running,  setRunning]  = useState(false)
  const [runMsg,   setRunMsg]   = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try { setReports(await getNightlyReports()) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const handleRun = async () => {
    if (!confirm('Ejecutar la revisión nocturna ahora?\n\nDevin analizará los errores (solo lectura). Tarda ~2–5 min.')) return
    setRunning(true)
    setRunMsg(null)
    try {
      const res = await triggerNightlyRun()
      setRunMsg(res.message ?? 'Iniciado. Recargá en 2–5 minutos.')
      setTimeout(load, 8000)
    } catch (e: any) { setRunMsg(`Error: ${e.message}`) }
    finally { setRunning(false) }
  }

  if (selected !== null) {
    return (
      <div className="p-6">
        <ReportDetail reportId={selected} onBack={() => { setSelected(null); load() }} />
      </div>
    )
  }

  const pending = reports.filter(r => r.status === 'pending').length

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">

      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <Moon size={22} className="text-sky-400" /> Revisión Nocturna
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Devin analiza los errores del día, identifica causas raíz y propone fixes — vos aprobás cada uno individualmente
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={load}
            className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors"
          >
            <RefreshCw size={13} /> Actualizar
          </button>
          <button
            onClick={handleRun}
            disabled={running}
            className="flex items-center gap-2 px-3 py-2 bg-sky-700 hover:bg-sky-600 rounded-lg text-sm text-white transition-colors disabled:opacity-50"
          >
            {running ? <Loader size={13} className="animate-spin" /> : <Play size={13} />}
            Ejecutar ahora
          </button>
        </div>
      </div>

      {/* Run feedback */}
      {runMsg && (
        <div className="text-sm text-sky-300 bg-sky-900/20 border border-sky-700/30 rounded-lg px-4 py-3 flex items-center gap-2">
          <Loader size={13} className="animate-spin shrink-0" /> {runMsg}
        </div>
      )}

      {/* Pending alert */}
      {pending > 0 && (
        <div className="bg-yellow-900/20 border border-yellow-700/30 rounded-xl px-4 py-3 flex items-center gap-3">
          <AlertTriangle size={15} className="text-yellow-400 shrink-0" />
          <span className="text-yellow-200 text-sm">
            Tenés <strong>{pending}</strong> reporte{pending > 1 ? 's' : ''} pendiente{pending > 1 ? 's' : ''} de revisión — abrilo y aplicá los fixes que querés.
          </span>
        </div>
      )}

      {/* How it works */}
      <details className="group bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <summary className="px-4 py-3 flex items-center gap-2 cursor-pointer text-sm text-gray-400 hover:text-white transition-colors list-none">
          <Shield size={13} className="text-sky-400 shrink-0" />
          <span className="font-medium">¿Cómo funciona?</span>
          <ChevronDown size={13} className="ml-auto group-open:rotate-180 transition-transform" />
        </summary>
        <div className="px-4 pb-4 space-y-1.5 text-sm text-gray-400 border-t border-gray-800">
          <p className="pt-3">
            <strong className="text-gray-200">1. Análisis (solo lectura)</strong> — cada medianoche Sofia recolecta errores y lanza Devin. Devin lee el código, identifica causas raíz y genera propuestas en JSON sin tocar nada.
          </p>
          <p>
            <strong className="text-gray-200">2. Revisión</strong> — ves cada propuesta con: causa raíz, cambio sugerido, archivo, confianza y riesgo. Las de baja confianza son informativas.
          </p>
          <p>
            <strong className="text-gray-200">3. Aplicación individual</strong> — al presionar "Aplicar" en una propuesta, Devin abre una sesión con permisos de escritura y hace exactamente ese cambio. Podés ver el output completo y la duración.
          </p>
          <p>
            <strong className="text-gray-200">4. Resolución automática</strong> — si el fix se aplica con éxito, el issue vinculado se marca como resuelto automáticamente.
          </p>
        </div>
      </details>

      {/* Reports table */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
          <Moon size={13} className="text-sky-400" />
          <h2 className="text-sm font-semibold text-white">Historial de reportes</h2>
          {!loading && (
            <span className="ml-auto text-xs text-gray-600">{reports.length} total</span>
          )}
        </div>

        {loading ? (
          <div className="py-10 text-center text-sm text-gray-500 flex items-center justify-center gap-2">
            <Loader size={14} className="animate-spin" /> Cargando…
          </div>
        ) : reports.length === 0 ? (
          <div className="py-14 text-center text-sm text-gray-500">
            <Moon size={36} className="text-gray-700 mx-auto mb-3" />
            <p>Aún no hay reportes nocturnos.</p>
            <p className="text-xs mt-1 text-gray-600">Se genera automáticamente a medianoche, o presioná "Ejecutar ahora".</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-950/40">
                <tr className="text-xs text-gray-500 uppercase tracking-wide">
                  <th className="px-4 py-2 text-left">ID</th>
                  <th className="px-4 py-2 text-left">Fecha</th>
                  <th className="px-4 py-2 text-center">Issues</th>
                  <th className="px-4 py-2 text-left">Propuestas</th>
                  <th className="px-4 py-2 text-left">Estado</th>
                  <th className="px-4 py-2 text-left">Hace</th>
                  <th className="px-3 py-2 w-6" />
                </tr>
              </thead>
              <tbody>
                {reports.map(r => (
                  <ReportListRow key={r.id} report={r} onClick={() => setSelected(r.id)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
