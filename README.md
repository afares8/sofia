# Sofia Monitor

Sistema de monitoreo centralizado para todas tus aplicaciones. Inspirado en Sentry pero 100% local, tuyo y con alertas por WhatsApp.

---

## Qué hace

| Feature | Descripción |
|---|---|
| **Health checks** | Verifica cada N segundos si tus servicios (Mayor, Packing, Pantalla, Cortana, WppConnect) están vivos |
| **Monitor pasivo** | Lee los archivos de log de cada app y detecta líneas `ERROR` / `CRITICAL` automáticamente |
| **Monitor activo (SDK)** | Un middleware de una línea que puedes agregar a cualquier app FastAPI para reportar errores en tiempo real |
| **Dashboard** | Vista en tiempo real del estado de cada servicio con tiempo de respuesta y último error |
| **Visor de logs** | Lee las últimas N líneas de cualquier log directamente en el browser |
| **Historial de errores** | Todos los errores quedan guardados en SQLite, filtrables por servicio, nivel y tiempo |
| **Alertas WhatsApp** | Cuando un servicio cae o hay un error crítico te llega un mensaje de WhatsApp vía WppConnect |
| **Panel de config** | **Todo** es configurable desde la UI — servicios, número de WhatsApp, tokens, intervalos, retención |

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
│   │   │   ├── health.py            # GET /api/health/
│   │   │   ├── events.py            # GET /api/events/
│   │   │   ├── ingest.py            # POST /api/ingest/event  (SDK activo)
│   │   │   ├── logs.py              # GET /api/logs/{service_id}
│   │   │   └── config.py            # GET/PUT /api/config/
│   │   └── services/
│   │       ├── health_service.py    # Poll loop, check_service()
│   │       ├── log_service.py       # Tail log files, detectar errores
│   │       ├── whatsapp_service.py  # Enviar alertas via WppConnect
│   │       ├── db_service.py        # SQLite async (aiosqlite)
│   │       └── config_service.py    # Leer/guardar config.json
│   ├── data/                        # Auto-creado: config.json + sofia.db
│   ├── requirements.txt
│   ├── run.py
│   └── .env.example
├── frontend/         # React + Vite + Tailwind
│   └── src/
│       ├── pages/
│       │   ├── DashboardPage.tsx    # Estado en tiempo real
│       │   ├── EventsPage.tsx       # Historial de errores con filtros
│       │   ├── LogsPage.tsx         # Visor de logs en vivo
│       │   └── ConfigPage.tsx       # Panel de configuración completo
│       └── api/client.ts            # Todas las llamadas al backend
└── sdk/
    └── sofia_sdk.py                 # Middleware para inyectar en otras apps
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
- **Pantalla** → `http://localhost:8000/health`
- **Cortana** → `http://localhost:8200/health`
- **WppConnect** → `http://localhost:21465/api/default/status-session`

Todo se puede modificar desde la UI en **Configuración**.

---

## SDK Activo — instalar en tus apps FastAPI

Copia `sdk/sofia_sdk.py` a tu proyecto y agrega **una línea** a tu `main.py`:

```python
# En main.py de mayor / packing / pantalla
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
| GET | `/api/events/` | Listar errores (filtrable) |
| DELETE | `/api/events/purge` | Purgar errores antiguos |
| POST | `/api/ingest/event` | Recibir error desde SDK |
| GET | `/api/logs/{service_id}` | Últimas líneas de log |
| GET | `/api/config/` | Obtener config completa |
| PUT | `/api/config/` | Guardar config completa |
| POST | `/api/config/alerts/test` | Enviar alerta de prueba |

---

## Stack

- **Backend:** Python 3.11+, FastAPI, aiosqlite, httpx, uvicorn
- **Frontend:** React 18, TypeScript, Vite, Tailwind CSS, Lucide icons
- **DB:** SQLite (sin setup, archivo local)
- **Alertas:** WppConnect (tu servidor local de WhatsApp)
