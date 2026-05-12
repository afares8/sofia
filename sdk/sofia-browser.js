/**
 * Sofia Browser SDK
 * Captures JS errors, unhandled promise rejections and React errors.
 * Keeps a circular buffer of breadcrumbs (clicks, navigations, fetch calls)
 * so each error report includes "what happened just before".
 *
 * Usage (paste in your app's main entry, e.g. main.tsx):
 *   import { initSofia } from './sofia-browser'
 *   initSofia({
 *     serviceId: 'mayor', serviceName: 'Mayor',
 *     environment: 'prod', release: '1.2.0',
 *   })
 *
 * Or as a plain <script> tag (no bundler):
 *   <script src="/sofia-browser.js"></script>
 *   <script>SofiaSDK.init({ serviceId: 'pantalla', serviceName: 'Pantalla' })</script>
 */

const DEFAULT_URL = 'http://localhost:5180/api/ingest/event'
const BREADCRUMBS_MAX = 20

let _cfg = {
  serviceId: 'frontend',
  serviceName: 'Frontend',
  sofiaUrl: DEFAULT_URL,
  environment: null,
  release: null,
  tags: null,
  enabled: true,
}

const _breadcrumbs = []

function addBreadcrumb(category, message, data, level = 'info') {
  _breadcrumbs.push({
    timestamp: Date.now() / 1000,
    category,
    message: String(message || '').slice(0, 300),
    level,
    data: data || {},
  })
  if (_breadcrumbs.length > BREADCRUMBS_MAX) {
    _breadcrumbs.splice(0, _breadcrumbs.length - BREADCRUMBS_MAX)
  }
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
    url: url || (typeof window !== 'undefined' ? window.location.href : null),
    user_info: null,
    breadcrumbs: _breadcrumbs.slice(),
  }
  if (_cfg.environment) payload.environment = _cfg.environment
  if (_cfg.release)     payload.release = _cfg.release
  if (_cfg.tags)        payload.tags = _cfg.tags
  // Use sendBeacon if available (works even during page unload)
  const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' })
  if (typeof navigator !== 'undefined' && navigator.sendBeacon) {
    navigator.sendBeacon(_cfg.sofiaUrl, blob)
  } else if (typeof fetch !== 'undefined') {
    fetch(_cfg.sofiaUrl, { method: 'POST', body: blob, keepalive: true }).catch(() => {})
  }
}

function _hookFetch() {
  if (typeof window === 'undefined' || !window.fetch) return
  const orig = window.fetch.bind(window)
  window.fetch = async (...args) => {
    const [resource, init] = args
    const method = (init?.method || 'GET').toUpperCase()
    const url = typeof resource === 'string' ? resource : (resource?.url || '')
    const t0 = Date.now()
    try {
      const resp = await orig(...args)
      addBreadcrumb('fetch', `${method} ${url} ${resp.status}`, {
        method, url, status: resp.status, duration_ms: Date.now() - t0,
      }, resp.ok ? 'info' : 'warning')
      return resp
    } catch (err) {
      addBreadcrumb('fetch', `${method} ${url} FAILED`, {
        method, url, error: String(err), duration_ms: Date.now() - t0,
      }, 'error')
      throw err
    }
  }
}

function _hookNavigation() {
  if (typeof window === 'undefined') return
  // Initial URL
  addBreadcrumb('navigation', `load ${window.location.pathname}`, {
    url: window.location.href,
  })
  window.addEventListener('popstate', () => {
    addBreadcrumb('navigation', `popstate ${window.location.pathname}`, {
      url: window.location.href,
    })
  })
  // History API (pushState / replaceState) wrapper for SPAs
  const origPush = history.pushState
  history.pushState = function (...args) {
    addBreadcrumb('navigation', `pushState ${args[2]}`, { url: args[2] })
    return origPush.apply(history, args)
  }
  const origReplace = history.replaceState
  history.replaceState = function (...args) {
    addBreadcrumb('navigation', `replaceState ${args[2]}`, { url: args[2] })
    return origReplace.apply(history, args)
  }
}

function _hookClicks() {
  if (typeof document === 'undefined') return
  document.addEventListener('click', (event) => {
    const t = event.target
    if (!t || !(t instanceof Element)) return
    // Build a short selector for the clicked element
    let sel = t.tagName.toLowerCase()
    if (t.id) sel += `#${t.id}`
    const cls = typeof t.className === 'string' ? t.className : ''
    if (cls) sel += `.${cls.split(/\s+/).slice(0, 2).join('.')}`
    const text = (t.textContent || '').trim().slice(0, 60)
    addBreadcrumb('ui.click', text || sel, { selector: sel })
  }, true)
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

  // 4. Breadcrumb hooks (silent — don't send, just collect)
  _hookFetch()
  _hookNavigation()
  _hookClicks()

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

// Manual breadcrumb (exposed)
export { addBreadcrumb }

// For plain <script> usage
if (typeof window !== 'undefined') {
  window.SofiaSDK = { init: initSofia, captureError, reportReactError, addBreadcrumb }
}
