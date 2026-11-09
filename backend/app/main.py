"""
Sofia Monitor - Main FastAPI application.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.routers import health, events, ingest, logs, config, webhook, restore, nightly, autonomy, incident_team, bridge
from app.services.alert_queue import alert_queue_loop
from app.services.analytics_service import spike_detection_loop
from app.services.config_service import load_config
from app.services.db_service import init_db, purge_old_events, purge_failed_alerts
from app.services.droplet_docker_service import droplet_docker_loop
from app.services.health_service import poll_loop
from app.services.local_host_service import local_host_loop
from app.services.log_service import log_poll_loop
from app.services.nightly_review_service import nightly_loop
from app.services.rules_engine import rules_loop
from app.services.autonomy_service import autofix_loop, job_watchdog_loop
from app.services.incident_team_service import incident_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sofia")


def install_proactor_accept_guard() -> bool:
    """Evita que un reset de cliente en AcceptEx (WinError 64/10054/etc.)
    cierre el socket de escucha en BaseProactorEventLoop (CPython Windows)."""
    try:
        from asyncio import exceptions as _aio_exc
        from asyncio import proactor_events as _pe
    except ImportError:
        return False
    base_cls = getattr(_pe, "BaseProactorEventLoop", None)
    if base_cls is None:
        return False
    if getattr(base_cls, "_sofia_accept_guard_installed", False):
        return True

    def _guarded_start_serving(
        self, protocol_factory, sock,
        sslcontext=None, server=None, backlog=100,
        ssl_handshake_timeout=None, ssl_shutdown_timeout=None,
    ):
        def loop(f=None):
            try:
                if f is not None:
                    conn = None
                    try:
                        conn, addr = f.result()
                        if self._debug:
                            _pe.logger.debug(
                                "%r got a new connection from %r: %r",
                                server, addr, conn)
                        protocol = protocol_factory()
                        if sslcontext is not None:
                            self._make_ssl_transport(
                                conn, protocol, sslcontext, server_side=True,
                                extra={"peername": addr}, server=server,
                                ssl_handshake_timeout=ssl_handshake_timeout,
                                ssl_shutdown_timeout=ssl_shutdown_timeout)
                        else:
                            self._make_socket_transport(
                                conn, protocol,
                                extra={"peername": addr}, server=server)
                    except _aio_exc.CancelledError:
                        if conn is not None:
                            conn.close()
                        sock.close()
                        return
                    except OSError as exc:
                        if conn is not None:
                            try:
                                conn.close()
                            except OSError:
                                pass
                        if self._debug:
                            _pe.logger.debug(
                                "Transient accept error on %r ignored: %r",
                                sock, exc, exc_info=True)
                if self.is_closed() or sock.fileno() == -1:
                    return
                f = self._proactor.accept(sock)
            except _aio_exc.CancelledError:
                sock.close()
            except OSError as exc:
                if sock.fileno() != -1 and not self.is_closed():
                    if getattr(exc, "winerror", None) == 10038:
                        sock.close()
                        return
                    _pe.logger.warning(
                        "Accept arm failed on %r (%r); retrying in 0.1s",
                        sock, exc)
                    self.call_later(0.1, loop)
            else:
                self._accept_futures[sock.fileno()] = f
                f.add_done_callback(loop)

        self.call_soon(loop)

    base_cls._start_serving = _guarded_start_serving
    base_cls._sofia_accept_guard_installed = True

    try:
        import struct as _struct
        import socket as _socket
        from asyncio import tasks as _tasks
        from asyncio import windows_events as _we
        iocp_cls = getattr(_we, "IocpProactor", None)
        _overlapped = getattr(_we, "_overlapped", None)
        _null = getattr(_we, "NULL", 0)
        if (iocp_cls is not None and _overlapped is not None
                and not getattr(iocp_cls, "_sofia_accept_guard_installed", False)):
            def _guarded_iocp_accept(self, listener):
                self._register_with_iocp(listener)
                conn = self._get_accept_socket(listener.family)
                ov = _overlapped.Overlapped(_null)
                try:
                    ov.AcceptEx(listener.fileno(), conn.fileno())
                except BaseException:
                    conn.close()
                    raise

                def finish_accept(trans, key, ov):
                    ov.getresult()
                    buf = _struct.pack("@P", listener.fileno())
                    conn.setsockopt(_socket.SOL_SOCKET,
                                    _overlapped.SO_UPDATE_ACCEPT_CONTEXT, buf)
                    conn.settimeout(listener.gettimeout())
                    return conn, conn.getpeername()

                async def accept_coro(future, conn):
                    try:
                        await future
                    except _aio_exc.CancelledError:
                        conn.close()
                        raise
                    except BaseException:
                        conn.close()

                future = self._register(ov, listener, finish_accept)
                coro = accept_coro(future, conn)
                _tasks.ensure_future(coro, loop=self._loop)
                return future

            iocp_cls.accept = _guarded_iocp_accept
            iocp_cls._sofia_accept_guard_installed = True
    except Exception:
        pass
    return True


install_proactor_accept_guard()


# Paths exempt from API-key auth (SDKs and WPPConnect can't send a header).
AUTH_EXEMPT_PREFIXES = (
    "/api/ping",
    "/api/ingest/event",
    "/api/webhook/wppconnect",
)


async def _register_wppconnect_webhook():
    """Register Sofia's webhook URL in WPPConnect so it receives incoming messages."""
    import httpx
    from app.services.whatsapp_service import get_auth_headers
    await asyncio.sleep(5)  # Wait for WPPConnect to be ready
    cfg = load_config()
    sofia_external_url = os.getenv("SOFIA_EXTERNAL_URL", "http://localhost:5180").rstrip("/")
    webhook_url = f"{sofia_external_url}/api/webhook/wppconnect"
    url = f"{cfg.alerts.wppconnect_url}/api/{cfg.alerts.wppconnect_session}/webhook"
    payload = {"webhook": webhook_url, "events": ["onMessage", "onAnyMessage"]}
    try:
        # Use get_auth_headers which generates a fresh token via generate-token,
        # instead of the static (often empty) wppconnect_token config field.
        # The old code built "Bearer " with an empty token, producing an illegal
        # header value error and a 401/404 from WPPConnect that was logged as
        # "registered" — masking the failure.
        headers = await get_auth_headers(cfg.alerts)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code < 300:
                logger.info(f"[WEBHOOK] WPPConnect webhook registered: {resp.status_code} — {webhook_url}")
            else:
                logger.warning(
                    f"[WEBHOOK] WPPConnect webhook registration FAILED: "
                    f"status={resp.status_code} body={resp.text[:200]} — {webhook_url}"
                )
    except Exception as exc:
        logger.warning(f"[WEBHOOK] Could not register WPPConnect webhook (WPPConnect may not be running): {exc}")


async def _auto_purge_loop():
    """Delete old occurrences/metrics/issues and failed alert_queue rows once a day."""
    PURGE_INTERVAL_SECONDS = 24 * 60 * 60
    # Wait 5 minutes after startup before first purge
    await asyncio.sleep(5 * 60)
    while True:
        try:
            cfg = load_config()
            await purge_old_events(cfg.error_retention_days)
            logger.info(f"[PURGE] Old events purged (retention={cfg.error_retention_days}d).")
        except Exception as exc:
            logger.error(f"[PURGE] failed: {exc}")
        try:
            deleted = await purge_failed_alerts()
            if deleted:
                logger.info(f"[PURGE] Purged {deleted} failed alert_queue rows (attempts>=10).")
        except Exception as exc:
            logger.error(f"[PURGE] alert_queue purge failed: {exc}")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Start background polling tasks
    asyncio.create_task(poll_loop())
    asyncio.create_task(log_poll_loop())
    asyncio.create_task(alert_queue_loop())
    asyncio.create_task(spike_detection_loop())
    asyncio.create_task(rules_loop())
    asyncio.create_task(nightly_loop())
    asyncio.create_task(autofix_loop())
    asyncio.create_task(job_watchdog_loop())
    asyncio.create_task(incident_loop())
    asyncio.create_task(droplet_docker_loop())
    asyncio.create_task(local_host_loop())
    asyncio.create_task(_auto_purge_loop())
    logger.info("Sofia Monitor started.")
    yield
    logger.info("Sofia Monitor shutting down.")


app = FastAPI(
    title="Sofia Monitor",
    description="Sistema de monitoreo centralizado para tus apps",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    """
    Optional API-key auth. Only enforced if SOFIA_API_KEY env var is set.
    Allows /api/ping, /api/ingest/event and /api/webhook/wppconnect without auth.
    """
    api_key = os.getenv("SOFIA_API_KEY")
    if not api_key:
        return await call_next(request)

    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {api_key}"
    # Also accept X-API-Key for legacy clients
    x_key = request.headers.get("X-API-Key", "")
    if auth_header == expected or x_key == api_key:
        return await call_next(request)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


# API routes
app.include_router(health.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(ingest.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(webhook.router, prefix="/api")
app.include_router(restore.router, prefix="/api")
app.include_router(nightly.router, prefix="/api")
app.include_router(autonomy.router, prefix="/api")
app.include_router(incident_team.router, prefix="/api")
app.include_router(bridge.router, prefix="/api")


@app.get("/api/ping")
async def ping():
    return {"status": "ok", "service": "sofia-monitor"}


# Serve frontend build (React SPA)
FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        # Never intercept API routes — return 404 JSON instead of the SPA
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index = FRONTEND_DIST / "index.html"
        return FileResponse(str(index))
