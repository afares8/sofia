import { useEffect, useState } from 'react'
import { getLogTail, getConfig, ServiceConfig } from '../api/client'
import clsx from 'clsx'
import { RefreshCw } from 'lucide-react'

function colorLine(line: string) {
  if (/CRITICAL/i.test(line)) return 'text-red-400'
  if (/ERROR/i.test(line))    return 'text-orange-400'
  if (/WARNING|WARN/i.test(line)) return 'text-yellow-400'
  if (/INFO/i.test(line))     return 'text-green-400'
  return 'text-gray-400'
}

export default function LogsPage() {
  const [services,   setServices]   = useState<ServiceConfig[]>([])
  const [selected,   setSelected]   = useState<string>('')
  const [lines,      setLines]      = useState<string[]>([])
  const [lineCount,  setLineCount]  = useState(100)
  const [loading,    setLoading]    = useState(false)

  useEffect(() => {
    getConfig().then(cfg => {
      const withLogs = cfg.services.filter(s => s.log_path && s.enabled)
      setServices(withLogs)
      if (withLogs.length > 0) setSelected(withLogs[0].id)
    })
  }, [])

  const load = async () => {
    if (!selected) return
    setLoading(true)
    try {
      const data = await getLogTail(selected, lineCount)
      setLines(data)
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [selected, lineCount])

  return (
    <div className="p-6 space-y-4 h-full flex flex-col">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Logs en Vivo</h1>
          <p className="text-gray-400 text-sm mt-1">Últimas líneas de los archivos de log</p>
        </div>
        <button onClick={load} className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors">
          <RefreshCw size={14} /> Actualizar
        </button>
      </div>

      <div className="flex gap-3 flex-wrap">
        <select
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={selected}
          onChange={e => setSelected(e.target.value)}
        >
          {services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <select
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
          value={lineCount}
          onChange={e => setLineCount(Number(e.target.value))}
        >
          <option value={50}>50 líneas</option>
          <option value={100}>100 líneas</option>
          <option value={200}>200 líneas</option>
          <option value={500}>500 líneas</option>
        </select>
      </div>

      <div className="flex-1 bg-gray-950 border border-gray-800 rounded-xl p-4 overflow-auto font-mono text-xs leading-5 min-h-0" style={{ maxHeight: 'calc(100vh - 220px)' }}>
        {loading ? (
          <div className="text-gray-500 text-center py-8">Cargando...</div>
        ) : lines.length === 0 ? (
          <div className="text-gray-500 text-center py-8">Sin líneas de log</div>
        ) : (
          lines.map((line, i) => (
            <div key={i} className={clsx('whitespace-pre-wrap break-all', colorLine(line))}>
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
