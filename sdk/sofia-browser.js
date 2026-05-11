/**
 * Sofia Browser SDK
 * Captures JS errors, unhandled promise rejections and React errors.
 *
 * Usage (paste in your app's main entry, e.g. main.tsx):
 *   import { initSofia } from './sofia-browser'
 *   initSofia({ serviceId: 'mayor', serviceName: 'Mayor' })
 *
 * Or as a plain <script> tag (no bundler):
 *   <script src="/sofia-browser.js"></script>
 *   <script>SofiaSDK.init({ serviceId: 'pantalla', serviceName: 'Pantalla' })</script>
 */

const DEFAULT_URL = 'http://localhost:5180/api/ingest/event'

let _cfg = {
  serviceId: 'frontend',
  serviceName: 'Frontend',
  sofiaUrl: DEFAULT_URL,
  enabled: true,
}

function _send(level, message, detail, stack, url) {
  if (!_cfg.enabled) return
  const payload = {
    service_id: _cfg.serviceId,
    service_name: _cfg.serviceName,
    level,
    message: String(message).slice(0, 500),
    detail: (detail || '').slice(0, 2000),
    traceback: (stack || '').slice(0, 6000),
    url: url || window.location.href,
    user_info: null,
  }
  // Use sendBeacon if available (works even during page unload)
  const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' })
  if (navigator.sendBeacon) {
    navigator.sendBeacon(_cfg.sofiaUrl, blob)
  } else {
    fetch(_cfg.sofiaUrl, { method: 'POST', body: blob, keepalive: true }).catch(() => {})
  }
}

export function initSofia(options = {}) {
  _cfg = { ..._cfg, ...options }

  // 1. Global JS errors
  window.addEventListener('error', (event) => {
    const msg = event.message || 'Unknown error'
    const stack = event.error?.stack || ''
    const src = event.filename ? `${event.filename}:${event.lineno}:${event.colno}` : ''
    _send('ERROR', msg, src, stack, window.location.href)
  })

  // 2. Unhandled promise rejections
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason
    const msg = reason?.message || String(reason) || 'Unhandled promise rejection'
    const stack = reason?.stack || ''
    _send('ERROR', msg, 'Unhandled Promise Rejection', stack, window.location.href)
  })

  // 3. Console.error override (optional — captures frontend error logs)
  const _origConsoleError = console.error.bind(console)
  console.error = (...args) => {
    _origConsoleError(...args)
    const msg = args.map(a => (typeof a === 'object' ? JSON.stringify(a) : String(a))).join(' ')
    // Only report if it looks like an actual error (has Error object)
    const errObj = args.find(a => a instanceof Error)
    if (errObj) {
      _send('WARNING', msg.slice(0, 400), 'console.error', errObj.stack || '', window.location.href)
    }
  }

  console.info('[Sofia] Browser SDK initialized for', _cfg.serviceName)
}

// React ErrorBoundary helper
export function reportReactError(error, errorInfo, componentStack) {
  _send(
    'CRITICAL',
    `React render error: ${error?.message || error}`,
    errorInfo || '',
    (error?.stack || '') + '\n\nComponent stack:\n' + (componentStack || ''),
    window.location.href,
  )
}

// Manual report
export function captureError(message, detail, level = 'ERROR') {
  _send(level, message, detail || '', '', window.location.href)
}

// For plain <script> usage
if (typeof window !== 'undefined') {
  window.SofiaSDK = { init: initSofia, captureError, reportReactError }
}
