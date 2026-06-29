# Sofia Monitor

Sistema de monitoreo centralizado para todas tus aplicaciones. Inspirado en Sentry pero 100% local, tuyo y con alertas por WhatsApp.

---

## Qué hace

| Feature | Descripción |
|---|---|
| **Health checks** | Verifica cada N segundos si tus servicios (Mayor, Packing, WppConnect) están vivos |
| **Monitor pasivo** | Lee los archivos de log de cada app y detecta líneas `ERROR` / `CRITICAL` automáticamente |
| **Monitor activo (SDK)** | Un middleware de una línea que puedes agregar a cualquier app FastAPI para reportar errores en tiempo real |
| **Dashboard** | Vista en tiempo real del estado de cada servicio con tiempo de respuesta y último error |
| **Performance** | Métricas históricas: response times (P50/P95/P99), uptime, heatmap de disponibilidad, distribución de errores por hora |
| **Visor de logs** | Lee las últimas N líneas de cualquier log directamente en el browser |
| **Historial de errores** | Todos los errores quedan guardados en SQLite, filtrables por servicio, nivel y tiempo |
| **Restauración** | Restauración autónoma o por WhatsApp con fallback a scripts PowerShell y reintentos exponenciales |
| **Alertas WhatsApp** | Cuando un servicio cae o hay un error crítico te llega un mensaje de WhatsApp vía WppConnect (con queue offline) |
| **Reglas de alerta** | Motor de reglas configurable: error rate, response time, downtime, spike detection |
| **Breadcrumbs / tags** | El SDK adjunta los últimos 20 eventos (clicks, fetch, navegación) antes de cada error, más tags / environment / release |
| **Auto-monitoreo** | Watchdog externo (PS1 + tarea programada) y endpoint `/api/health/sofia` para auto-diagnóstico |
| **Panel de config** | **Todo** es configurable desde la UI — servicios, restauración, reglas de alerta, tokens, intervalos, retención |

---

## Arquitectura

```
sofia/
├── backend/          # FastAPI - API REST + tareas en background
│   ├── app/
│   │   ├── main.py                  # Entry point, lifespan, rutas
│   │   ├── models/
│   │   │   ├── config.py            # Pydantic models de configuración
│   │   │   └── event.py             # ErrorEvent, ServiceStatus
│   │   ├── routers/
│   │   │   ├── health.py            # /api/health, /api/health/sofia, /api/health/{id}/metrics, .../stats, .../summary
│   │   │   ├── events.py            # GET /api/events/
│   │   │   ├── ingest.py            # POST /api/ingest/event  (SDK activo)
│   │   │   ├── logs.py              # GET /api/logs/{service_id}
│   │   │   ├── restore.py           # /api/restore + /api/restore/history + /api/restore/trigger/{id}
│   │   │   └── config.py            # /api/config + /api/config/rules (CRUD)
│   │   ├── scripts/
│   │   │   └── sofia_watchdog.ps1   # Tarea programada que reinicia Sofia si /api/ping falla 2x
│   │   └── services/
│   │       ├── health_service.py    # Poll loop + record_metric en cada chequeo
│   │       ├── log_service.py       # Tail log files, detectar errores
│   │       ├── whatsapp_service.py  # Enviar alertas via WppConnect (con queue offline)
│   │       ├── alert_queue.py       # Background loop que reintenta enviar alertas en cola
│   │       ├── analytics_service.py # Error rate y spike detection
│   │       ├── rules_engine.py      # Evalúa AlertRule cada 2 min y dispara alertas
│   │       ├── restore_service.py   # Auto-restaurar o pedir confirmación por WhatsApp
│   │       ├── db_service.py        # SQLite async (aiosqlite) + tabla metrics/restores/alert_queue
│   │       └── config_service.py    # Leer/guardar config.json
│   ├── data/                        # Auto-creado: config.json + sofia.db
│   ├── requirements.txt
│   ├── run.py
│   └── .env.example
├── frontend/         # React + Vite + Tailwind
│   └── src/
│       ├── pages/
│       │   ├── DashboardPage.tsx    # Estado en tiempo real + sparklines + error trend
│       │   ├── EventsPage.tsx       # Historial de errores con filtros + breadcrumbs + tags
│       │   ├── LogsPage.tsx         # Visor de logs en vivo
│       │   ├── PerformancePage.tsx  # P50/P95/P99, uptime 24h/7d, heatmap por hora
│       │   ├── RestorePage.tsx      # Restauraciones (auto/manual, Codex/PS1, historial)
│       │   └── ConfigPage.tsx       # Servicios + auto_restore + reglas de alerta + escalation
│       └── api/client.ts            # Todas las llamadas al backend
└── sdk/
    ├── sofia_sdk.py                 # Middleware Python con breadcrumbs + env/release/tags
    └── sofia-browser.js             # SDK navegador con breadcrumbs de clicks / fetch / navigation
```

---

## Instalación

### Backend

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
python run.py
```

El servidor corre en `http://localhost:9000` por defecto.

### Frontend (desarrollo)

```bash
cd frontend
npm install
npm run dev
```

### Frontend (producción — sirve desde el backend)

```bash
cd frontend
npm run build
# Los archivos quedan en frontend/dist/
# El backend los sirve automáticamente en /
```

---

## Configuración inicial

La primera vez que arranca, Sofia crea `backend/data/config.json` con los servicios por defecto de tu ecosistema:

- **Mayor** → `http://localhost:8075/health`
- **Packing** → `http://localhost:8100/health`
- **WppConnect** → `http://localhost:21465/api/default/status-session`

Todo se puede modificar desde la UI en **Configuración**.

---

## SDK Activo — instalar en tus apps FastAPI

Copia `sdk/sofia_sdk.py` a tu proyecto y agrega **una línea** a tu `main.py`:

```python
# En main.py de mayor / packing
from sofia_sdk import SofiaMiddleware

app.add_middleware(
    SofiaMiddleware,
    service_id="mayor",       # ID único del servicio
    service_name="Mayor",     # Nombre legible
    sofia_url="http://localhost:9000",  # URL de Sofia (default)
)
```

Desde ese momento, cualquier error 500 o excepción no manejada se reporta automáticamente a Sofia y te llega un WhatsApp.

También puedes reportar errores manualmente:

```python
from sofia_sdk import report_error

await report_error(
    service_id="mayor",
    service_name="Mayor",
    level="ERROR",
    message="Falló conexión a SAP",
    detail=str(exception),
)
```

---

## Variables de entorno (backend/.env)

| Variable | Default | Descripción |
|---|---|---|
| `SOFIA_HOST` | `0.0.0.0` | IP donde escucha el servidor |
| `SOFIA_PORT` | `9000` | Puerto |
| `SOFIA_CONFIG_PATH` | `data/config.json` | Ruta del archivo de config |
| `SOFIA_DB_PATH` | `data/sofia.db` | Ruta de la base de datos SQLite |
| `SOFIA_HOST_IP` | `192.168.0.123` | IP del host usado en las URLs por defecto de los servicios monitoreados |
| `SOFIA_EXTERNAL_URL` | `http://localhost:5180` | URL externa de Sofia para registro de webhooks de WPPConnect y endpoint del SDK |
| `SOFIA_API_KEY` | _(no set)_ | Si está definido, las rutas `/api/*` requieren `Authorization: Bearer <key>` (excepto `/api/ping`, `/api/ingest/event` y `/api/webhook/wppconnect`) |

---

## Alertas WhatsApp

Sofia usa **WppConnect** (ya instalado en esta PC en `localhost:21465`) para enviar mensajes.

Formato de alerta:
```
🔴 Sofia Monitor
Servicio: Mayor
Nivel: CRITICAL
Mensaje: Mayor no responde (DOWN)
Detalle: URL: http://localhost:8075/health
🕐 2026-05-10 20:35:00
```

Configuración desde la UI → **Configuración → Alertas WhatsApp**:
- Número destino
- Token de WppConnect
- Sesión activa
- Cooldown entre alertas (evita spam)

---

## API Reference

| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/api/health/` | Estado de todos los servicios |
| POST | `/api/health/check/{id}` | Forzar check inmediato |
| GET | `/api/health/sofia` | Self-health: DB, memoria, uptime, WPP, último poll |
| GET | `/api/health/{id}/metrics?since_hours=N` | Datapoints de response time / status / is_up |
| GET | `/api/health/{id}/stats?since_hours=N` | Estadísticas: avg, P50, P95, P99, min, max, uptime % |
| GET | `/api/health/summary` | Resumen por servicio: uptime 24h y 7d, P95, status actual |
| GET | `/api/events/` | Listar errores (filtrable) |
| DELETE | `/api/events/purge` | Purgar errores antiguos |
| POST | `/api/ingest/event` | Recibir error desde SDK (con `breadcrumbs`, `tags`, `environment`, `release`) |
| GET | `/api/logs/{service_id}` | Últimas líneas de log |
| GET | `/api/config/` | Obtener config completa |
| PUT | `/api/config/` | Guardar config completa |
| GET/POST | `/api/config/rules` | Listar / crear reglas de alerta |
| PUT/DELETE | `/api/config/rules/{id}` | Editar / eliminar regla |
| POST | `/api/config/alerts/test` | Enviar alerta de prueba |
| GET | `/api/restore/` | Pendientes + últimas restauraciones |
| GET | `/api/restore/history?limit=50` | Historial completo desde DB |
| POST | `/api/restore/trigger/{service_id}` | Lanzar restore manual desde la UI (bypass WhatsApp) |

---

## Stack

- **Backend:** Python 3.11+, FastAPI, aiosqlite, httpx, uvicorn
- **Frontend:** React 18, TypeScript, Vite, Tailwind CSS, Lucide icons, Recharts
- **DB:** SQLite (sin setup, archivo local)
- **Alertas:** WppConnect (tu servidor local de WhatsApp) con queue persistente para tolerar caídas
- **Restauración:** Codex CLI como método primario, fallback a scripts PowerShell por servicio

---

## Restauración autónoma (toggle por servicio)

Cada servicio tiene dos checkboxes en **Configuración**:

1. **Restauración habilitada** — permite que Sofia restaure este servicio cuando lo detecta caído.
2. **Auto-restaurar (sin confirmación)** — si está activo, Sofia restaura sin pedir confirmación por WhatsApp.

Por defecto **ambos están OFF**. Active primero solo `Restauración habilitada` y use la restauración manual (botón en la página **Restauraciones** o comando `SI {SERVICIO}` por WhatsApp). Cuando confíe en el sistema, active el toggle de auto-restore por servicio.

El restore intenta primero la integración con el CLI de Codex. Si Codex no está instalado, busca un script PowerShell en `backend/app/scripts/restore_{service_id}.ps1`. Si falla, reintenta hasta 3 veces con backoff exponencial (30s, 60s, 120s).

## Watchdog externo

`backend/app/scripts/sofia_watchdog.ps1` es un script PowerShell pensado para correr como tarea programada de Windows cada 2 minutos. Hace `GET /api/ping` a Sofia, y si falla 2 veces consecutivas relanza `start.bat`. Variables: `SOFIA_URL`, `SOFIA_START_BAT`, `SOFIA_WATCHDOG_STATE`.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest
```
