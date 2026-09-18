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
 *   <script>SofiaSDK.init({ serviceId: 'mayor', serviceName: 'Mayor' })</script>
 */

const DEFAULT_URL = 'http://localhost:5180/api/ingest/event'
const BREADCRUMBS_MAX = 20
const QUEUE_KEY = 'sofia:queue'
const QUEUE_MAX = 50
const QUEUE_FLUSH_MS = 15000

let _cfg = {
  serviceId: 'frontend',
  serviceName: 'Frontend',
  sofiaUrl: DEFAULT_URL,
  environment: null,
  release: null,
  tags: null,
  enabled: true,
  slowMs: 8000,
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

function _queueRead() {
  try {
    const raw = localStorage.getItem(QUEUE_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

function _queueWrite(arr) {
  try {
    if (arr.length === 0) localStorage.removeItem(QUEUE_KEY)
    else localStorage.setItem(QUEUE_KEY, JSON.stringify(arr.slice(-QUEUE_MAX)))
  } catch {}
}

function _enqueue(payload) {
  const arr = _queueRead()
  arr.push(payload)
  _queueWrite(arr)
}

function _postPayload(payload, useBeacon = false) {
  const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' })
  // fetch is the primary path: its promise reports failure so the caller can
  // queue for retry. sendBeacon has no delivery confirmation — use it only for
  // the unload flush, or as a last resort when fetch is unavailable.
  if (!useBeacon && typeof fetch !== 'undefined') {
    return fetch(_cfg.sofiaUrl, { method: 'POST', body: blob, keepalive: true })
      .then(() => true)
      .catch(() => false)
  }
  if (typeof navigator !== 'undefined' && navigator.sendBeacon) {
    return navigator.sendBeacon(_cfg.sofiaUrl, blob)
  }
  return false
}

function _beaconFlush() {
  // pagehide/beforeunload: no time for promises — fire-and-forget via beacon.
  const arr = _queueRead()
  _queueWrite([])
  for (const payload of arr) _postPayload(payload, true)
}

function _flushQueue() {
  const arr = _queueRead()
  if (arr.length === 0) return
  _queueWrite([])
  const survivors = []
  let pending = 0
  const done = () => { if (--pending === 0 && survivors.length) _queueWrite(_queueRead().concat(survivors)) }
  for (const payload of arr) {
    pending++
    Promise.resolve(_postPayload(payload)).then((ok) => { if (!ok) survivors.push(payload); done() })
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
  Promise.resolve(_postPayload(payload)).then((ok) => { if (!ok) _enqueue(payload) })
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
      const duration_ms = Date.now() - t0
      addBreadcrumb('fetch', `${method} ${url} ${resp.status}`, {
        method, url, status: resp.status, duration_ms,
      }, resp.ok ? 'info' : 'warning')
      // Message omits the duration so repeated slow calls to the same URL
      // group into one issue (fingerprint = message hash).
      if (duration_ms > _cfg.slowMs) {
        _send('WARNING', `Slow fetch — ${method} ${url}`,
          `Duration: ${duration_ms}ms\nStatus: ${resp.status}`, '', window.location.href)
      }
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
  const origPush = window.history.pushState
  window.history.pushState = function (...args) {
    addBreadcrumb('navigation', `pushState ${args[2]}`, { url: args[2] })
    return origPush.apply(window.history, args)
  }
  const origReplace = window.history.replaceState
  window.history.replaceState = function (...args) {
    addBreadcrumb('navigation', `replaceState ${args[2]}`, { url: args[2] })
    return origReplace.apply(window.history, args)
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

  // 5. Offline queue: replay events captured while Sofia was unreachable
  _flushQueue()
  setInterval(_flushQueue, QUEUE_FLUSH_MS)
  window.addEventListener('online', _flushQueue)
  window.addEventListener('pagehide', _beaconFlush)

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
