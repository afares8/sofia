import { Routes, Route, NavLink } from 'react-router-dom'
import { Activity, AlertTriangle, FileText, Settings, Zap, RotateCcw, BarChart3 } from 'lucide-react'
import DashboardPage from './pages/DashboardPage'
import EventsPage from './pages/EventsPage'
import LogsPage from './pages/LogsPage'
import ConfigPage from './pages/ConfigPage'
import RestorePage from './pages/RestorePage'
import PerformancePage from './pages/PerformancePage'
import clsx from 'clsx'
import { useState, useEffect } from 'react'

const nav = [
  { to: '/',            label: 'Dashboard',     icon: Activity },
  { to: '/performance', label: 'Performance',   icon: BarChart3 },
  { to: '/events',      label: 'Errores',       icon: AlertTriangle },
  { to: '/logs',        label: 'Logs',          icon: FileText },
  { to: '/restore',     label: 'Restauraciones',icon: RotateCcw },
  { to: '/config',      label: 'Configuración', icon: Settings },
]

export default function App() {
  const [connected, setConnected] = useState(true)
  
  useEffect(() => {
    const check = () => fetch('/api/ping').then(() => setConnected(true)).catch(() => setConnected(false))
    check()
    const id = setInterval(check, 10000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col">
        <div className="px-5 py-5 border-b border-gray-800">
          <div className="flex items-center gap-2">
            <Zap className="text-sky-400" size={22} />
            <span className="text-lg font-bold text-white">Sofia</span>
            <span className={clsx('w-2 h-2 rounded-full', connected ? 'bg-green-400' : 'bg-red-500 animate-pulse')} title={connected ? 'Backend conectado' : 'Backend desconectado'} />
            <span className="text-xs text-gray-400 mt-0.5">Monitor</span>
          </div>
        </div>
        <nav className="flex-1 py-4 space-y-1 px-2">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-sky-500/20 text-sky-400'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800',
                )
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="px-5 py-3 border-t border-gray-800 text-xs text-gray-600">
          v1.1.0
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto bg-gray-950">
        <Routes>
          <Route path="/"            element={<DashboardPage />} />
          <Route path="/performance" element={<PerformancePage />} />
          <Route path="/events"      element={<EventsPage />} />
          <Route path="/logs"        element={<LogsPage />} />
          <Route path="/restore"     element={<RestorePage />} />
          <Route path="/config"      element={<ConfigPage />} />
        </Routes>
      </main>
    </div>
  )
}
