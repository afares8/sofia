# Sofia Monitor - instrucciones del proyecto

## Alcance y fuente de verdad

Servidor productivo Windows SERVER-SOFIA. Este repo es Sofia Monitor, no Sofia AI/Executive de Mayor. Revisar primero `C:/Users/ahmed/AGENTS.md` sin imprimir secretos y las skills de este repo. No trasladar comandos historicos D:/ ni instrucciones de dev directamente a produccion.

Estado verificado 2026-10-11: SofiaBackend y SofiaFrontend corren mediante NSSM; backend en 5180, frontend en 5179, tunel SofiaTunnel. Confirmar siempre estado/rutas efectivos, no asumir que esta fecha sigue vigente. `SofiaBackend` corre como LocalSystem con `backend/.venv/Scripts/python.exe` y cwd `backend`. Logs NSSM en `C:/Users/ahmed/apps/service-logs/SofiaBackend.{out,err}.log`.

## Seguridad y auditoria

- Una solicitud de reporte NO autoriza reinicios, restores, auto-fix, deploy, migraciones, envio de mensajes ni cambios de datos. Solicitar autorizacion especifica para esos efectos.

- EXCEPCION: las sesiones del puente Sofia-Devin (`Sofia-Devin-AppsPC` y `Sofia-Devin-ElenaTec`) SI estan pre-autorizadas por el dueno a aplicar auto-fix sin pedir aprobacion, con el alcance de su prompt inicial en `.devin/sofia-cli/` (ver `C:/Users/ahmed/AGENTS.md`). Limite comun: nada destructivo/irreversible y nunca exponer secretos.
- No leer/imprimir `.env`, tokens, cookies, claves privadas ni config completa. Leer metadata de NSSM por allowlist; no mostrar AppEnvironmentExtra. Redactar queries de URLs, tokens de rutas, correos y telefonos en evidencia.
- Preservar cambios ajenos: `git status --short` antes de editar; no reset/stash/clean ni stage global. No commit/push sin instruccion.
- No importar `app.main`, ejecutar `init_db`, iniciar la app o invocar `load_config` para un reporte: el lifespan inicia workers y `load_config` puede guardar config.
- SQLite: URI `mode=ro`, `PRAGMA query_only=ON`, transaccion de lectura; cerrar pronto. No copiar un .db vivo ignorando WAL. Esquema en `backend/app/services/db_service.py`; verificar antes de consultar.
- `issues.count` es acumulado, `occurrences` son registros individuales retenidos, un issue no es un incidente unico. No usar SUM(count) como errores de las ultimas 24h. Usar occurrences.timestamp para ventanas; separar fuente/entorno/release.
- Retencion se define en config; purge elimina occurrences/metrics antiguos y solo issues antiguas resueltas. `resolved=0` no demuestra que siga fallando.
- Docker: first_seen/last_seen son ingesta; timestamp de origen en detail. NSSM usa reloj local. Confirmar zona antes de correlacionar; no inferir causalidad por proximidad.
- Guardar una captura redactada y derivar todo de ella. No reconsultar una DB viva para re-renderizar un reporte ya entregado. Reconciliar totales e inventario sin filtrar silenciosamente fuentes, niveles o servicios.

## Skills

- `.devin/skills/sofia-incident-audit/SKILL.md`: inventario completo, evidencia y reporte read-only.
- `.devin/skills/sofia-root-cause-fix/SKILL.md`: reparaciones futuras con causa demostrada, pruebas aisladas e invariantes.
- Para Mayor/Packing, leer sus AGENTS y skills de SAP/WhatsApp/WMS; no asumir una convencion comun. Packing usa DIAPI 8001 y Mayor 9000 en los registros revisados: confirmar contratos efectivos.

## Verificacion proporcional

Documentacion: discovery de skills, enlaces/paths, revision del diff y redaccion, sin arrancar aplicaciones. Para Python de auditoria: `ast.parse`/compilacion en memoria, sin ejecutar consultas ni importar apps.

Para un futuro cambio funcional de Sofia, pruebas acotadas desde `backend` con su `.venv/Scripts/python.exe -m pytest tests/test_<area>.py -q`. Dependencias de test en `requirements-dev.txt`; `pytest.ini` configura asyncio_mode=auto. Inspeccionar fixtures antes: DB/config temporales, HTTP/WhatsApp/SSH/subprocess mockeados, sin lifespan productivo. No ejecutar suites de Mayor/Packing a ciegas contra SAP.

Backend: FastAPI/aiosqlite/httpx; persistencia en `backend/data/sofia.db`, config en `backend/data/config.json` salvo overrides. Frontend: React/TypeScript/Vite; build declarado en package.json como `tsc && vite build`. No hacer build en la ruta servida por NSSM por una tarea documental; usar entorno aislado cuando un fix frontend lo requiera.

## Lecciones operativas verificadas

Log_path inexistente no implica servicio sin errores: el SDK puede seguir reportando mientras el tail retorna vacio. Comparar config con filesystem y AppStdout/AppStderr sin reemplazar fuentes ciegamente. Un 404 de sofiatunnel_e2e puede ser el expected_status correcto. HTTP 200 de WPPConnect no demuestra sesion conectada ni entrega del mensaje. Vision/vms-api y ambos DIAPI requieren cobertura explicita; eventos Windows genericos no son readiness de negocio.

No instalar asistentes para reparar una revision nocturna sin acordar CLI/cuenta/ruta. Monitor usa Codex en su servicio nocturno; el CLI disponible en ahmed no demuestra disponibilidad en LocalSystem. Cambios locales y HEAD del host remoto no prueban que la imagen desplegada los contenga.

No guardar hipotesis como reglas permanentes. Cerrar issues solo con evidencia de fix/release/flujo y observacion posterior; no purgar para aparentar salud. Reporte de referencia: `REPORTE_SOFIA_CAUSAS_RAIZ_2026-10-11.md` (datos historicos, no estado vivo).


## Correcciones verificadas de monitoreo (2026-10-11)

El tail informa fuentes ready/missing/error, configured_path/effective_path y backlog en log_monitor de /api/health/sofia. Para defaults ausentes de Mayor/Packing puede usar exclusivamente AppStderr de NSSM; un path custom no se sustituye silenciosamente. Cursor por bytes e identidad de archivo, lotes acotados, linea parcial diferida, descarte de linea excesiva y persistencia antes de avanzar. No drenar con sleep(0) cuando hay DB/read error o una cola formada solo por linea incompleta.

/health/sofia expone RSS en MiB usando psutil y automation.codex_available. No confundir disponibilidad del ejecutable con autenticacion/lista de propuestas. El motor nocturno quedo diferido por decision del usuario: no instalar ni migrar automaticamente. SOFIA_CODEX_BIN es un override opcional de ejecutable absoluto; invalido falla cerrado y no cambia PATH. El runner aplica deadline real sobre stdin/stdout/salida y limpia solo el arbol del proceso que creo.

Mayor DIAPI: http://localhost:9000/api/Health/Ping; Packing DIAPI: http://localhost:8001/Health/Index, requiere connected=true. Los probes reales confirmaron localhost; 127.0.0.1 devolvio HTTP400 por el listener HTTP.sys. No sustituir hostname por IP ni considerar cualquier status<500 como salud. VisionBackend/VisionMedia/vms-api tienen vigilancia de estado Windows, pero sus metadatos/probes de negocio siguen pendientes por permisos; una elevacion UAC cancelada no se repite ni se sustituye por cambios de ACL.

Pruebas especificas: tests/test_codex_cli_service.py, test_log_service.py, test_restore_health.py, test_self_health.py y test_local_host.py. Inspeccionar conftest/fixtures: DB y config temporales, HTTP/registry/subprocess mockeados. No usar servicios reales como fixture.

Dependencias SCM confirmadas: MayorBackend -> PackingBackend -> SofiaBackend; cada frontend depende de su backend y SofiaBackend tambien requiere MayorBackend. Restart-Service sin detener dependientes falla aunque la cuenta tenga START/STOP; no asumir un problema de ACL ni elevar a ciegas. Para un reinicio coordinado autorizado de las seis apps, parar SofiaFrontend/PackingFrontend/MayorFrontend y luego SofiaBackend/PackingBackend/MayorBackend; iniciar MayorBackend/PackingBackend/SofiaBackend y restaurar los frontends. No usar -Force ni parar dependientes adicionales sin aprobacion especifica. Registro de activacion: activacion_backends_autorizada.json. No tocar WPPConnect.


## Evidencia Windows y persistencia de eventos (2026-10-11)

Los eventos Windows se capturan de System y Application dentro de un unico @(...) antes de Select/ConvertTo-Json; sin agrupar, PowerShell mezcla objetos formateados y JSON y puede perder toda la captura. Una vez establecido cursor por log, usar EventRecordID > cursor y -Oldest con limite por lote para reintentar tras una falla de DB sin depender solo de una ventana de 3 minutos. Cursor y dedup se confirman despues de persistir; una notificacion fallida no reingesta el evento.

Conservar mensaje completo redactado y timestamp de origen en detail (limite 12000 caracteres, bandera de truncacion); la primera linea de Windows 1026 no contiene la causa. El resumen agrega Exception Info / codigo / modulo cuando existan. No registrar tokens, userinfo RTSP, correos ni telefonos. Un log borrado/reset y un cursor nunca establecido siguen requiriendo diagnostico explicito: no asumir cobertura de toda la historia.


## Puente de escritorio Sofia-Devin (2026-10-15)

`backend/app/scripts/sofia_devin_bridge.py` es un puente independiente (solo stdlib, no importa `app.*`): un watcher oculto lee `occurrences` nuevas de `sofia.db` en modo `mode=ro` + `PRAGMA query_only=ON` usando UNICAMENTE `.devin/sofia-cli/occurrences.sql` y `highwater.sql`; rutea por `policy.json` (local vs elenatec) y, solo con reportes elegibles, abre UNA consola visible por grupo/dia que habla con el Devin CLI oficial via `devin acp` (JSON-RPC por stdio). Consola custom sobre ACP real, no TUI nativo ni inyeccion de foco/clipboard.

- Estado compartido por `StateStore.transaction()`: lock cross-proceso por named mutex (`Local\SofiaDevinState-<hash>`) + lock in-process; TODA mutacion (cursor, pending, sessions, inflight, lease de spawn) va en transaccion corta; nunca se retiene el lock durante RPCs de modelo ni spawns. `watch` tambien toma mutex de vida `Local\SofiaDevinBridgeWatcher-<grupo>`; worker `Local\SofiaDevinBridgeWorker-<grupo>`.
- Activacion `from_now`: primer ciclo guarda highwater sin backlog; `max(id)=0` (purga de retencion) NO es reset; `0<max<cursor` marca `source_reset` y frena cerrado. Cada evento lleva `received_day`; el watcher abre el worker del dia del pendiente mas antiguo; el worker solo envia eventos de su dia, termina su turno y cierra al cambiar la fecha local si ya no tiene trabajo; un dia sin reportes no crea nada (worker manual sin eventos ni sesion guardada sale sin abrir ACP).
- `inflight` se persiste ANTES de `session/prompt` (con `use_initial` y `day`); muerte del worker con `prompt_sent` => `uncertain` (detectado por el watcher por vida/mutex, no solo al enviar). NUNCA reenvio automatico; `/reintentar` reconecta ACP y `session/load` del mismo ID antes de reenviar; `session/load` fallido => `manual_gap`, sin sesion de reemplazo. Solo `stopReason=="end_turn"` marca completado; cancelled/max_tokens/refusal => `turn_incomplete` con reportes retenidos. `use_initial` se recomputa desde la sesion persistida en cada lote.
- Consola amigable: encabezado "Sofia + Devin" con etiqueta de grupo y dia; el stderr del agente NUNCA va a consola — se guarda redactado en `devin-diagnostics.log` por grupo (rotativo 1MiB x2) y solo errores ERROR/FATAL/PANIC muestran un aviso breve una vez por mensaje. `/detalle` muestra el ultimo detalle tecnico local sin llamar al modelo; `/estado` resume en espanol; `/salir` (interrumpe turno via notificacion `session/cancel` sin id, con cierre acotado; EOF en turno tambien cancela), `/reintentar`, `/ayuda`. Estados de herramientas en espanol deduplicados por toolCallId+status; permisos ACP destacados con contenido/ruta del toolCall en texto legible redactado, opciones numeradas, default rechazar. Chunks del agente solo por lineas completas redactadas (bloques PEM hasta END). Observabilidad local `ui-events.jsonl` (rotativo) registra console_ready/session_ready(prompt)/prompt_started(event_ids)/agent_message(event_ids del lote activo)/prompt_completed solo con end_turn/permission_requested/resolved — sin prompts ni secretos.
- `devin acp` se lanza con argv `--permission-mode <policy.permission_mode>` + `DEVIN_PERMISSION_MODE` y cwd del grupo; leccion verificada: una sesion reanudada conserva su modo previo (`accept-edits`) aun con flag/env `dangerous` — el modo efectivo se fija SIEMPRE tras session/new/load via RPC `session/set_config_option` (`configId:'mode'`, `value:'bypass'` para dangerous/bypass/yolo; `smart`/`accept-edits` directos; desconocido => fallo cerrado) y se valida con el `currentValue` devuelto; sin ACK confirmado no se envia ningun prompt y el lote queda retenido (`permission_mode_error` cuenta en backoff). Comandos slash anunciados incluyen /bypass; verificado en vivo con ACK real (accept-edits -> bypass). el worker verifica elevacion (local admin / elenatec NO admin) y `devin auth status` antes de spawn. El watcher reclama un lease de spawn (`last_spawn`, `worker_ready=False`, `spawn_counted=False`) antes de Popen; un hijo que marca ready no se sobreescribe; el lease persiste tras reinicio del watcher; muerte de worker sano con pendientes => `paused` hasta un reporte nuevo; fallos de init/auth/elevacion/`manual_gap` cuentan en backoff exponencial con circuit breaker y no se resetean por eventos nuevos.
- Tareas programadas: `.devin/sofia-cli/install.ps1` DRY RUN por defecto (preflight de rutas, cwd, templates, SQL, `devin auth status` acotado, drift de tareas existentes por args/exe/principal); `-Install` exige consola elevada, auth `autenticado` y cuenta resoluble; `-Start` requiere `-Install`. Requiere autorizacion expresa; imprime comandos reversibles, no borra nada.
- Leccion de proceso: `pythonw.exe`/`python.exe` del venv arranca un hijo redirector en `Python311` real (padre->hijo PID); ese hijo NO es una instancia duplicada del puente ni de tareas. Verificar parent/child antes de reportar watchers duplicados.
- Pruebas: `backend/.venv/Scripts/python.exe -m pytest tests/test_sofia_devin_bridge.py -q` desde `backend` (agente ACP falso por pipes, DB/state temporales, subprocess reales para mutex/estado, sin red ni modelo). Evidencia en `backend/data/devin-desktop/evidence/` (gitignored). Handshake `initialize` verificado en vivo (Devin 3000.11.3, `loadSession=true`, `authMethods=[devin-browser]`, `devin auth status`=autenticado); durante las pruebas no se envio session/new ni prompts reales. Activacion posterior: ambas tareas quedaron Running y avisos naturales abrieron una sesion real por grupo; esto no acredita que el analisis termine ni autoriza reinicios adicionales.
- Consola minimizada (2026-10-18, pedido del dueno): `default_spawn_worker` pasa `STARTUPINFO` con `STARTF_USESHOWWINDOW` + `SW_SHOWMINNOACTIVE`; la consola del worker arranca minimizada en la barra de tareas y se restaura para interactuar. Verificado en vivo: con el flag el spawn usa conhost clasico (no Windows Terminal) y la ventana queda minimizada; sin el flag abria visible (WT). Aplica a workers nuevos: watchers reiniciados 2026-10-18 23:34:34 (Stop/Start) con el codigo nuevo; los workers abiertos siguen con el codigo previo. Test: `test_default_spawn_worker_starts_minimized_console`.


## Vision como servicio monitoreado (2026-10-16)

`vision` se agrego a `DEFAULT_SERVICES` (`app/models/config.py`) y a `backend/data/config.json`
(runtime, gitignored): health `https://127.0.0.1:8200/health` (TLS self-signed; el health service
usa `verify=False`) y `log_path C:/Users/ahmed/apps/vision/backend/service.log`. Sofia relee
`config.json` cada ciclo (~30s) via `load_config()`, asi que no hace falta reiniciar el backend
para agregar un servicio; `load_config()` tambien auto-agrega los `DEFAULT_SERVICES` faltantes al
reiniciar. `restore_enabled=false` (no hay `restore_vision.ps1`): solo notifica, no ofrece restore.
El puente Sofia-Devin rutea `vision` al grupo **local** (todo lo que no sea elenatec/droplet), asi
que sus avisos los atiende la sesion diaria de las apps de la PC. El log arranca `from_now`
(cursor al final), asi que no reingesta el historial previo.

Ruido esperado (2026-10-18): `marked N stale AI job(s) as error after restart` (bookkeeping de cada
reinicio de VisionBackend; los jobs en vuelo quedan "abandonado por reinicio del servicio" y visibles
en la UI de IA) quedo suprimido con el patron `vision-stale-ai-jobs-after-restart` en
`log_ignore_patterns`; Vision emite la linea como WARNING desde el fix de logging de `vision.ai.engine`
(antes salia sin nivel y Sofia la clasificaba ERROR por la palabra "error"). Los ERROR reales del motor
(p. ej. `ai worker iteration failed`) NO estan suprimidos.

## Reboot de Windows Update y revival de HikCentral (2026-10-18)

- 03:01-03:10 local: Windows Update (MoUsoCoreWorker/TrustedInstaller) reinicio el servidor (3
  arranques; la tarea `ReinicioProgramadoServidor` no corrio, proxima 10/25). Durante la ventana
  cayeron SofiaTunnel/Sofia-Devin (5185/5186)/Ingest E2E y los enlaces SAP (08S01 10053; issues
  12926/12927/81782/81783/400): todo se recupero solo. El host monitor ingesta los eventos del boot
  (OpenSSH 7034 x2, ThirdPluginServer 1000, nssm 1002, BitLocker 24641, BrFilter 7000, etc.) —
  transitorios, sin accion.
- El reboot revivio el stack HikCentral local (servicios Automatic: BeeAgent, 6 Nginx, SADPServer,
  STREAM...; nginx ocupa 80/443). El 17-oct se habia detenido por la crisis de memoria. Hoy: 0
  crashes de nginx desde el boot, commit disponible ~4.4 GB, HCP ~950 MB — sin presion aguda.
  PENDIENTE decision del dueno: dejarlo corriendo (trabajo bridge Tocumen) o detenerlo otra vez
  (y deshabilitar los start types para que no vuelva con cada reboot).
- Patron `packing-maersk-key` corregido a `MAERSK_CONSUMER_KEY( is)? not configured` (la variante
  con "is" no matcheaba; issue 29897). AbuseIPDB (71289, 48 ocurrencias) y los probes de API URL de
  Packing (29909/29910) siguen sin patron: son config/transitorios, decision del dueno.
- Schannel 36884 hacia `api.elenatec.com` (18-oct 15:15Z, desde curl): el host NO existe en DNS y el
  certificado del droplet (198.211.99.4) solo cubre elenatec.com/app/www. `api.elenatec.com` figura
  como API de produccion del movil (`tec-review/mobile/eas.json`) y como ejemplo de webhooks
  (`config.py`). Un solo evento, actor no identificado (curl efimero; no aparece en los exec
  loggeados de la sesion ElenaTec; sin tarea programada). Dominio ElenaTec — su sesion trabaja en
  tec-review; no hay dependencia local.
- Mayor: el reinicio de las 11:14:56 del 18-oct (despliegue del modulo `elenatec` por otra sesion)
  dejo el app SIN rol scheduler: los workers loguearon "Scheduler lock port 8175 already held" (un
  huerfano de la instancia anterior retenia 8175) -> sin schedulers ni middleware SAP (9000 caido;
  el issue 15465 "Error reading middleware stdout: [Errno 22]" es el artefacto benigno del reinicio).
  FIX aplicado: `taskkill /T /F` del arbol uvicorn -> NSSM respawneo a los 5 s (AppRestartDelay=5000);
  el worker nuevo adquirio el lock (11:16:57) y levanto el middleware (READY 11:17:16). Si se repite
  tras un reinicio: confirmar "Acquired scheduler lock" en el log y quien retiene el puerto 8175.
- WPPConnect (18-oct 11:27-11:36): el watchdog de Chromium "flapeo" en ciclos
  (unresponsive/recovered 1-2/3, sin llegar a 3/3) y `check-connection-session` se colgaba ->
  Sofia DOWN (issue 81, CRITICAL) y envios en riesgo. Criterio: si flapea >5 min sin escalar y
  `wpp_ok` sigue False, **reiniciar WPPConnect** (sin dependientes; preserva pairing: el log
  muestra `isLogged` + "Started Session" al minuto y medio, SIN QR, con WhatsApp Web fijado).
  Verificado 11:36: wpp_ok True y sesion CONNECTED. No esperar al 3/3 del watchdog si ya hay
  degradacion real; no usar logout/clear-session.
- Duplicados de ocurrencias tras reiniciar SofiaBackend (observado 3x el 18-oct: 08:13, 16:24,
  16:45): el log_monitor reanuda desde su cursor persistido y **re-ingesta el ultimo lote previo
  al reinicio** (at-least-once). Identificarlos por el timestamp de origen viejo en el detail
  (mismo texto ya visto); NO son eventos nuevos ni requieren accion — solo inflan counts.
  El reinicio de las 11:45:41 (tercero del dia) fue de la sesion que itera el despliegue.
  MECANISMO VERIFICADO (5a re-ingesta a las 14:25:44, 1 s despues de un reinicio de SofiaBackend
  a las 14:25:43): los cursores viven SOLO en memoria (`_file_positions = {}` en log_service.py);
  en la primera lectura tras un arranque `pos = size - _STARTUP_TAIL_BYTES (8192)` -> se relee la
  cola del archivo y se re-ingesta su ultimo lote. NO es un cursor atascado: es el diseno
  (at-least-once) y explica todos los episodios de duplicados del dia. Mitigacion en curso: la
  ventana de acumulacion + dedup del puente agrupa estas ocurrencias del mismo issue al enviarlas.
- Pico de memoria 18-oct ~17:30-17:32: crashearon procesos no relacionados (PackingSapDiService,
  RustDesk, ChatGPT, Codex) y fallaron jobs del motor de IA de Vision (llama CLI + cv2); Vision se
  recupero solo (5 jobs OK despues). El DIAPI de Packing quedo caido y se restauro con reinicio
  coordinado de PackingBackend (parar SofiaFrontend/SofiaBackend/PackingFrontend -> Restart ->
  levantar; healthy 17:34:40, connected WALIGMAG). Causa probable: presion de commit
  (WindowsTerminal ~28 GB commit + 6 procesos devin ~19 GB; disponible cayo a ~3 GB) — son
  procesos del dueno/sesiones, NO del stack: no tocarlos; flag al dueno.

## Salud del puente y recuperacion (2026-10-16)

El watcher expone `GET /health` en localhost (`policy.health_ports`: local 5185, elenatec 5186):
200 con `{ok, group, last_poll_age_seconds, status, worker_alive, pending}`; 503 si el ultimo ciclo
supera `HEALTH_STALE_SECONDS` (120 s). Sofia monitorea `sofia-devin-local` y `sofia-devin-elenatec`
contra esos endpoints, asi que si el puente se cae o se cuelga, Sofia alerta (antes era un
componente critico sin vigilancia). Si el puente local muere, el propio CLI local no puede
arreglarlo (es el que esta caido) → alerta al dueno; el de elenatec si lo puede reiniciar.

Recuperacion de servicios: `sc.exe failure` de los 8 servicios principales y de `VisionMedia`/
`vms-api` quedo en `restart/5000,restart/10000,restart/60000` con `reset=86400` — **sin `REBOOT`**
(la maquina ya no se reinicia sola por fallos). Crashes los levanta NSSM/SCM; cuelgues los detecta
Sofia (probe HTTP) y los reinicia el CLI.

## Alertas de reglas tambien al AI (2026-10-16)

`rules_engine.evaluate_rules` ahora, ademas de `send_alert` a WhatsApp, crea un issue
(`upsert_event`, level WARNING, `source="rules"`, mensaje estable `[REGLA] <nombre>` y el detalle
variable en `detail`) para que el CLI Sofia-Devin lo vea. Antes las alertas de reglas
(spike/error_count/slow_response) iban SOLO al WhatsApp y el AI nunca las veia. El mensaje estable
evita crear un issue nuevo por cada conteo distinto (el fingerprint incluye el mensaje).

## Tareas programadas deshabilitadas (2026-10-16)

El dueno pidio no tener scripts automaticos de arranque/reinicio. Deshabilitadas:
`ServerSofia-HealthWatchdog` (server_watchdog.ps1, reiniciaba servicios), `SofiaTunnelFix`
(fix-tunnel.ps1) y `ServerSofia-PlatformHealthObserver` (Observe-PlatformHealth.ps1, solo observa).
Nada en el codigo de Sofia lee `PlatformHealth.state.json` ni `watchdog.log`. Se mantienen los
backups (`ServerSofia-BackupReplica`, `ServerSofia-BackupRestoreTest`) y `ReinicioProgramadoServidor`.
La recuperacion de servicios sigue por NSSM (`sc.exe failure ... restart/...`) y por el restore
manual de Sofia; el auto-reinicio por script ya no corre.

## Detector de drift y cobertura ampliada (2026-10-16)

`drift_service.py` compara la fecha del codigo mas nuevo de cada backend con el arranque del
servicio NSSM (`/api/health/sofia` -> `deploy_drift`, cacheado 300 s): si el codigo es mas nuevo,
hay un cambio sin desplegar. Nacio del bug de Vision (servicio con codigo de 2 dias antes).
`drift=None` cuando no se pudo determinar (no se reporta como drift).

Cobertura agregada: `diapi` (Mayor, `http://localhost:9000/api/Health/Ping`) quedo `enabled` y
`diapi_packing` (`http://localhost:8001/Health/Index`) se sumo — ambos middlewares SAP. Usar
`localhost`, no `127.0.0.1` (HTTP.sys devuelve 400). `wppconnect` ahora tiene
`log_path C:/Users/ahmed/apps/service-logs/WPPConnect.out.log`; sus lineas traen codigos ANSI
(`\x1b[31merror\x1b[39m`), asi que `log_service` los limpia con `_ANSI_RE` antes de clasificar
(sin el strip, `\berror\b` no matchea "merror" y el error se perdia).

## Supresion de ruido: familias conocido-esperadas (2026-10-16)

`config.json` acepta `log_ignore_patterns`: lista de `{id, pattern (regex, case-insensitive,
busqueda parcial), service_id (opcional; vacio = todos), note}`. Se aplican en
`db_service.upsert_event` — el punto unico de creacion de issues — asi que cubren ingesta activa
(SDK) y pasiva (logs): un mensaje que matchea NO crea issue ni alerta y el cursor del log avanza
igual. `ingest.py` responde `ignored: true` e `issue_id: 0`; `log_service.tail_log` no agrega el
evento ni alerta. Observabilidad en `/api/health/sofia` -> `noise.counts` / `noise.last_skip_at`
(`noise_service.get_stats()`). Cache por mtime de config.json; regex invalida o config ausente
fallan abierto (no suprimen). Usar SOLO con evidencia de que la familia es esperada; nunca para
ocultar fallos reales (los patrones cargados son los del triaje documentado de Mayor/Packing).
NO se silencian "Slow request" (fue como se detecto la caida de produccion del 15/10).

## Triaje: warnings recurrentes de las apps monitoreadas (2026-10-15)

Familias que se repiten y son esperadas o artefactos de parsing, no fallos de las apps:
- **Mayor `hikvision_push` "DEVICE SECURITY: Peticion sin secret valido"**: chequeo de shared
  secret en modo suave por diseno (no bloquea; la marcacion se procesa igual). Detalle en
  `apps/mayor/backend/AGENTS.md`.
- **Mayor `app.security_service_improved` "PROBLEMA DETECTADO: IP privada sin proxy"**: se emite
  cuando un cliente LAN/VPN conecta directo sin proxy confiable (`security_service_improved.py:184-188`);
  es por diseno, no indica intrusion. **NO tiene rate-limit** (a diferencia de hikvision_push), asi que
  loguea en CADA request directo -> muchas ocurrencias por IP (una issue por IP porque el fingerprint
  incluye la IP; p. ej. 71323 con 36+). `192.168.0.123` es la propia IP del servidor (llamadas internas
  directas).
- **"Slow request — ..." (fuente active)**: umbral 5 s (`SOFIA_SLOW_MS`, default 5000) en el
  `sofia_sdk.py` de Mayor y Packing; endpoints pesados lo superan de forma recurrente. Caso extremo
  verificado 2026-10-16: `POST /api/v1/packing/logistica/crear-entrega` = **80.9 s** para el pedido
  8056 (creacion de lineas SAP + backfill del pedido complejo que genero la rafaga de warnings de
  deficit/lineas); responde 200. `GET /api/v1/packing/logistica/resumen-pedidos` ~5 s (reporte pesado).
- **Packing `[DATOS] ApTaxCode column not available`**: `OARG.ApTaxCode` no existe en esta BD SAP;
  el codigo cae a la consulta basica y responde OK (warning esperado).
- **Packing `[SAP-DI] Could not set U_*`**: faltan UDFs en la BD SAP (confirmado por
  `GET http://localhost:8001/health/ValidateUDF` -> falta `U_SecBltoNum` en RDR1). El codigo
  tolera la ausencia; es un hueco de configuracion SAP, no de codigo.
- **Packing WMS "reference ... indeterminate ... automatic release prohibited"**: guarda de
  seguridad de inventario; no libera automaticamente referencias ambiguas.
- **Packing `purchasing.providers.scraping` "Scraping failed for <contenedor>: No scraper available
  for carrier" (issues 67095/67098)**: `GenericScraper` (scraping.py) es el fallback para carriers
  sin scraper propio y devuelve `success=False` con ese mensaje; el README de Packing lo documenta
  como esperado. Es un hueco de cobertura de carriers (no un fallo de la app): ese contenedor no se
  rastrea automaticamente. No hay fix minimo de codigo salvo agregar un scraper para ese carrier.
- **Packing tracking Maersk/AI sin datos**: "Scraping failed for <cont>: MAERSK_CONSUMER_KEY not
  configured" (issues 29897/77344) y "[AI_TRACKING] BL fallback also returned no results for <cont>"
  (issue 67101). El proveedor Maersk falla rapido A PROPOSITO si falta `MAERSK_CONSUMER_KEY`
  (`scraping.py:372-378`, para no gastar retry budget con 401/403) y el scheduler avisa que Maersk
  "will be skipped and fall back to AI/manual tracking" (`scheduler.py:76-82`). Esperado: el
  tracking de esos contenedores cae a AI/manual. Habilitar Maersk requiere configurar la credencial
  (decision de credenciales, no un fix de codigo).
- **Mayor `app.modules.pantalla.dispatch.dispatch_note` "Dispatch counter ... was behind saved
  notes"**: el contador de notas de despacho vive en un dict en memoria por worker
  (`app/modules/pantalla/state.py`), asi que los 4 workers se desfasan entre si; la funcion lo
  auto-corrige con `max(counter, max_note)` y persiste. Benigno (multi-worker), no es perdida de
  numeracion.
- **Packing `sap_gateway` "SAP error 3821 (amount exceeds balance due)" (issue 30203) y "SAP
  reconciliation skipped (BP line not found in JDT1)" (issue 30213)**: el reconciliador AR detecta
  que el monto pedido excede el saldo del documento, o que el `TransId` no pertenece al BP, y
  **omite** la conciliacion a proposito para no entrar en bucle de reintentos. Recurrentes ~diario
  a las 20:32-20:33 local (scheduler nocturno). Manejado por codigo; NO es un fallo de Sofia. Si el
  negocio necesita que esa conciliacion se aplique, es una revision de datos AR/SAP, no un fix de
  codigo.
- **SERVER-SOFIA Host, evento Windows Application 1026 `[nssm]` "Failed to open process handle ...
  when terminating service WPPConnect" (issue 24315)**: se emite al **reiniciar WPPConnect** (NSSM no
  logra abrir los handles de los procesos Chrome hijos al terminar). Es pre-existente (desde
  2026-10-02) y benigno: el servicio detiene y arranca correctamente. Aparece en rafaga de 5 por
  reinicio; no confundir con un fallo de la sesion WhatsApp.
- **Packing `ap_scheduler` "Invalid AP timezone 'America/Panama'; using fixed Panama UTC-05:00"
  (issue 29898)**: el venv de Packing NO tiene el paquete `tzdata`, asi que
  `zoneinfo.ZoneInfo('America/Panama')` lanza `ZoneInfoNotFoundError` en Windows y el codigo cae a
  un fallback fijo UTC-05:00 (`timezone_fixed_panama`, `ap_scheduler.py:21-60`). El fallback es
  CORRECTO (Panama no tiene DST) y el mensaje es enganoso: la zona SI es valida, falta tzdata.
  Se emite ~diario al calcular el proximo run (cron 01:30 Panama) y en cada reinicio del backend.
  Benigno; no es un fallo. Silenciarlo requeriria instalar `tzdata` (cambio de dependencia) o
  emitir el aviso una sola vez / a DEBUG (cambio de codigo).
- **Mayor `collections_scheduler` "[COLLECTIONS] Omitido <cliente>: cliente sin numero de WhatsApp
  configurado" (issue 72577)**: el scheduler de cobranzas omite A PROPOSITO a los clientes sin
  telefono configurado (`collections_scheduler.py:789-797`): registra el skip en la DB
  (`notes="Omitido: cliente sin telefono configurado"`) y retorna `{"skipped": true}`. Recurrente
  ~diario (~09:2x local) para los mismos clientes (25003802, 25001782). NO es un fallo; cargarles
  el telefono es una decision de datos, no un fix de codigo.
- **Mayor `hikvision_device_service` "Could not resolve employee by name: '<nombre>'" (issue
  72075 y familia 71617/72998/73568)**: una marcacion Hikvision no se pudo atribuir: el clock ID
  no matcheo ningun empleado Y el nombre tampoco (`_resolve_by_name`, `hikvision_device_service.py:70-83`,
  solo matchea "nombre apellido" exacto o el primer nombre, contra empleados ACTIVOS). El evento
  crudo SI queda guardado con `processed=False` (`HRHikvisionEvent`) y hay un reproceso MANUAL
  (`POST /{device_id}/reprocess-failed`) que se corre tras corregir `biometric_employee_no`/nombre
  del empleado. No es un crash: es un hueco de datos/atribucion (empleado inactivo/ausente o con
  nombre distinto). NO relajar el matching por nombre (riesgo de atribuir a otra persona). Nombres
  vistos ~1/dia: Nazareth Drenald, JACQUELINE AGRELLI, Christopher Morris, ALEXANDER MARTINEZ.
- **Mayor `siecloud_service` "SIECloud: not configured, returning empty ..." (issue 71569)**: el
  servicio SIECloud (precios para Etiquetas, existencias, productos, price-search) NO esta
  configurado en este server: no existe `backend/siecloud_config.json` y no estan las env
  `SIECLOUD_API_URL/USER/PASSWORD/CONTEXT_ID` (main.py:1110-1122). Las funciones devuelven vacio
  con un WARNING (`siecloud_service.py`). Impacto: el cache offline de Etiquetas queda vacio
  (`frontend/src/lib/productCache.ts::refreshCache` -> `/api/v1/siecloud/all-products-for-cache`).
  Es un HUECO DE CONFIGURACION, no un bug: se arregla cargando credenciales por el panel Tecnico
  (`POST /api/v1/siecloud/config`) o por env vars (decision/credenciales del dueno). ~1-2/dia.
- **Mayor `sales_floor_orders.services` "Stock transfer failed for order PV-xxxxx: No items to
  transfer" (issue 71851)**: en `complete_order` (PICKING->COMPLETED) se crea una transferencia
  automatica con los items de `picked_quantity > 0` (`wms_integration.py:210-227`); si NINGUN item
  tiene picks (todos NOT_FOUND / 0), devuelve `error="No items to transfer"` y `services.py:332-337`
  loguea WARNING. No es un crash y NO bloquea el completado. Es una condicion legitima (nada
  recogido -> nada que transferir), pero el "failed" podria indicar pedidos completados sin picking:
  verificar con negocio si esos pedidos debian tener picks. ~1/dia (PV-10466/10476/10505). NO
  convertir a `skipped` a ciegas: este warning es la unica senal de ese caso.
- **Mayor "Slow request — POST /api/v1/email/send-generic" (issue 16863)**: endpoint de envio de
  correo generico usado por Packing WMS ("Diferencias Inventario - Pedido PV-xxxxx"), familia
  "Slow request" esperada. Es lento por diseno (SMTP sincrono, 5-13 s) Y ademas hace el `smtplib`
  BLOQUEANTE dentro del `async def` (`generic_email_routes.py:98-101`), lo que estanca el event
  loop del worker durante el envio. Uso raro (~3 envios en 2 semanas) => impacto bajo. Mejora
  posible (NO aplicada): mover el bloque SMTP a `asyncio.to_thread`; requiere reinicio coordinado
  de MayorBackend (dependencias SCM) y conviene fuera de horario laboral.
- **Packing `picking_service` "Salesfloor exception email not sent for task X (order PV-xxxxx):
  {'sent': False, 'reason': 'no_exceptions'}" (issue 54492)**: el correo de diferencias de Sala de
  Venta solo se envia si HAY excepciones (`salesfloor_email_service.py:190-192` devuelve
  `no_exceptions` y loguea INFO); el llamador (`picking_service.py:3426-3430`) loguea WARNING para
  CUALQUIER `sent=False`, incluido este caso normal. Benigno (nada que reportar -> no se envia
  correo), pero el nivel WARNING es enganoso. ~6 en 4 dias, todos `no_exceptions`. Mejora posible
  (NO aplicada): INFO para `no_exceptions` y WARNING solo para fallos reales
  (no_email_configured/no_mayor_url/excel_generation_failed); requiere reinicio de PackingBackend.
- **Packing `packing_service` guardas/fallbacks (issues 46519, 32190, 32191, 32984, 38523/47976/
  73526/73532) y "Slow request" en add-bulto/finalize-bodega (10723/10730)**: WARNINGs que el codigo
  registra y luego CONTINUA (no son crashes):
  * `Product X not found in SAP order Y after comprehensive search` -> rechaza empacar un producto
    que no esta en la orden SAP (`{"success": false, "error": "Product not found in SAP order"}`).
  * `Health check has warnings - proceeding with caution` + `UDF fields validation failed - delivery
    creation may fail` -> pre-flight con warnings (UDFs faltantes en SAP; ver ApTaxCode/U_SecBltoNum);
    el codigo sigue con cautela.
  * `available_sap_lines was empty, using all SAP lines as fallback reference` -> fallback al agregar
    linea; continua.
  * `Product X ordered (N) but not packed` / `under-packed: ordered N, packed M` -> resultados de
    validacion "incomplete" al finalizar (discrepancia de empaque; el sistema avisa, no bloquea).
  * `add-bulto`/`finalize-bodega` (6-19 s) caen en la familia "Slow request" (endpoints pesados).
- **Mayor Pantalla `invoice_service`/`dispatch_notifications` "No invoice found for delivery N"
  (issues 77437/77438/77440)**: al despachar una entrega sin factura asociada todavia; aviso esperado,
  no es un crash.
- **Packing `host_utils`/`sap_di_service_manager` "No working API URL found ..." / "No working SAP DI
  API URL found, using configured URL" (issues 29909/29910)**: warnings de ARRANQUE cuando el middleware
  SAP DI (8001) aun no responde mientras PackingBackend inicia (es hijo suyo); el codigo cae a la URL
  configurada y se recupera en ~1 min. Verificado 2026-10-16 tras un reinicio: `/Health/Index` ->
  `connected:true`, `connectionAge 0.7 min`. Transitorio y esperado en cada reinicio (esperar 30-60s
  antes de tratarlo como caida).
- **Packing `packing_service` backfill / entrega parcial (issues 77520-77531/77541-77550 "No matching
  SAP line found for X in order Y (bulto N)"; 32195 "Order has incomplete quantities - proceeding with
  partial delivery" + continuaciones "   - Product ...")**: la reconciliacion de cantidades detecta
  faltantes y el codigo PROCEDE con entrega PARCIAL (loguea el detalle de cada item con prefijo "   - ");
  el backfill que enlaza lineas empacadas a su LineNum SAP avisa "No matching SAP line found" y OMITE
  esa linea, pero continua ("Backfill completed! Updated N"). No son crashes; "No matching SAP line"
  puede indicar lineas SAP ya asignadas/agotadas para ese producto -> revision de datos si negocio lo
  requiere. Ocurren en rafaga por pedido al finalizar/backfill (p. ej. orden 8056). Variantes del
  mismo flujo de asignacion de lineas: "Could not assign full quantity (N) to existing lines.
  Remaining: N" (32982/32985) + "Will create new SAP line for deficit of N units" (32983/32986) ->
  el codigo detecta el deficit y CREA una linea SAP nueva para cubrirlo (diseno, continua); y
  "available_sap_lines was empty, using all SAP lines as fallback reference" (32984) -> fallback.
- **Packing `websockets` legacy "ERROR:    data transfer failed" + traceback terminando en `OSError:
  [WinError 121] The semaphore timeout period has expired` (issue 77645)**: NO es un error de la app.
  La libreria `websockets` (protocolo legacy) lo loguea cuando una conexion WebSocket
  (`/api/v1/ws/<usuario>`) se queda obsoleta y el TCP expira (cliente que dejo de responder o red caida);
  el cliente solo desconecta. El clasificador pasivo lo marca ERROR por el prefijo de la linea; no
  requiere accion.
- **Packing `pre_item_service` "Error resolving codes for target DB" (issue 77691)**: al crear un item
  en la BD destino, `_resolve_codes_for_target_db` consulta OARG y falla con `Invalid column name 'Code'`
  (mismo tipo de hueco de esquema SAP que ApTaxCode); el `except` (linea ~694) loguea WARNING y devuelve
  los codigos sin resolver (-1) -> fallback, el flujo CONTINUA. No es un crash. El ERROR
  "(Background on this error...)" que lo acompana (74128) es el falso positivo del clasificador pasivo.
- **Packing `sap_di_service_manager` ERROR "[SAP-DI] Item creation failed in ELMAGINCSA: Error -5002:
  [OITM.CstGrpCode] , 'Linked value 773 does not exist'" (issue 77826)**: la creacion de un item en la
  BD destino (compania ELMAGINCSA) falla porque el `CstGrpCode` del item origen (773) no existe alli.
  Se RELACIONA con "Error resolving codes for target DB" (77691): como la resolucion de codigos falla
  (`Invalid column 'Code'`), no se ajusta el CstGrpCode y SAP lo rechaza (-5002). Es un hueco de mapeo
  de datos multi-compania SAP (no un crash del backend); el endpoint create-item igual responde 200 y el
  error queda en el log/resultado. Revision de datos SAP si negocio lo requiere. Variante en el
  llamador `pre_item_service`: "Failed to create item in ELMAGINCSA: SAP Error -5002: [OITM.CstGrpCode]..."
  (issue 77831).
- **Vision vms-api (Kestrel) "warn: Microsoft.AspNetCore.Server.Kestrel[22]" (issue 77862)**: es el
  warning de Kestrel "the heartbeat has been running for X which is longer than 1s. This could be
  caused by thread pool starvation" -> el servicio .NET (vms-api) estuvo ocupado >1s en un latido.
  Transitorio/benigno, no es un error. Log en `C:/ProgramData/VmsSofia/Api/vms-api.log`. Ojo: la
  ocurrencia ingerida era del 2026-10-14 18:19:30Z (el monitor leyo el log desde el inicio, no
  from_now); revisar el cursor de ese servicio si se quiere evitar reingesta de historial.
- **Mayor `printnode_service` "[PrintNode] process_order_for_printing called ..." (71535) y
  "[Approver Resolution] ... Found approver ..." (71536)**: son DIAGNOSTICOS logueados a nivel WARNING
  (no errores): reportan que se disparo el flujo de impresion y que se resolvio el aprobador
  (`printnode_service.py:99-138,1642`). Benignos. Solo "[Approver Resolution] No approver found"
  seria un caso a revisar (sin aprobador resoluble).
- **Packing `routers.packing` "Skipping pallet label print for order X, pallet N: Cannot print
  pallet with only placeholder records ..." (issue 32637)**: SKIP intencional (`packing.py:2298-2305`):
  el palet tiene solo registros placeholder (sin contenido real) -> no se imprime etiqueta
  (`status="skipped"`, se registra en el log de impresion). Benigno; el WARNING podria bajarse a INFO.
- **Mayor `security_service_improved` "AbuseIPDB API key no configurada" (issue 71289)**: falta la
  credencial de AbuseIPDB, asi que se salta el chequeo de reputacion de IP
  (`check_abuseipdb_improved`); el resto del sistema de seguridad funciona. Se emite en
  `init_security_system` (arranque) una vez por worker (~4 por reinicio de Mayor). Hueco de
  configuracion, no un bug.
- **Packing `bulto_sequence_manager` "[STRICT_OWNERSHIP] ACCESS DENIED: Bulto N warehouse_id=X, access
  denied for warehouse Y" (78130) + "[SMART_BULTO] Ownership conflict for bulto N, falling back to
  regular logic" (78131)**: guarda multi-bodega: el bulto pertenece a otra bodega -> se DENIEGA el
  acceso y se cae a la logica regular (`_open_smart_next_bulto_atomic` -> `_open_next_bulto_atomic`,
  `bulto_sequence_manager.py:550-558`). Manejado; no es un crash ni perdida de datos.
- **SERVER-SOFIA Host, Windows System event 20 (WindowsUpdateClient) "Installation Failure ... error
  0x80073D02: <Store app>" (issues 78159/78160)**: Windows Update no pudo instalar/actualizar apps de
  Microsoft Store (IntelArcSoftware, YourPhone) -> error 0x80073D02. Es ruido del SO (no afecta a las
  apps del servidor ni a los servicios NSSM); no requiere accion.
- **Vision "Exception in ASGI application" (issue 78189)**: el traceback es HISTORICO. Referencia
  `scanner.py:157` = una version vieja; el `scanner.py` actual tiene esa sentencia (`pwd = password or
  dev.password or DEFAULT_DEVICE_PASSWORD`) en la linea **278**, y el modelo `Device` SI expone la
  property `password` (`models.py:40-44`, descifra `password_enc`/`password_legacy`). La linea se
  reingirio desde el INICIO del `service.log` (cuyo primer registro es un arranque con pid 26980,
  distinto del proceso actual 42780 del 2026-10-16 13:30). NO es un bug actual; el cursor del log de
  `vision` tambien esta reingiriendo historial.
- **Vision "marked N stale AI job(s) as error after restart" (issue 78193)**: es un `log.warning` de
  housekeeping al arrancar (`app/services/ai_engine.py:759`): los AI jobs interrumpidos por un reinicio
  se marcan como error. El monitor lo clasifica ERROR porque la palabra "error" aparece en el texto
  (nivel real: WARNING). Benigno, no es un fallo.
- **Vision `vision.perf` "slow request <METHOD> <path> -> 200 in N ms" (issue 78207)**: Vision tiene su
  propio logger de requests lentos (`vision.perf`); p. ej. `POST /vms-api/api/devices/discovery/sadp`
  (descubrimiento SADP = escaneo de red, inherentemente lento) en 4.1 s. Es la familia "slow request"
  de Vision: responde 200, no es un fallo. Mismo patron que el "Slow request" de Mayor/Packing.

### Falso positivo del clasificador pasivo (verificado 2026-10-15; CORREGIDO 2026-10-16)
`_classify_level` en `backend/app/services/log_service.py` clasificaba lineas SIN prefijo de nivel
buscando la palabra ERROR/WARNING/WARN en cualquier parte de la linea (`_LEVEL_RE`). Por eso una
linea de continuacion de una excepcion multi-linea ya manejada podia crear un issue ERROR propio.
Caso real: `(Background on this error at: https://sqlalche.me/e/20/f405)` (cola de un
`logger.warning(str(exc))` de SQLAlchemy/pyodbc en Packing) quedo como issue ERROR 74128 sin que
Packing fallara.

**Corregido**: `_CONTINUATION_RE` hace que `_classify_level` devuelva `None` para lineas de
continuacion (empiezan con espacio, `(`, `[SQL:`, `[parameters:`, `File "`, `Traceback`, `at `,
`...`) antes de buscar el nivel. Una linea sin prefijo que SI menciona ERROR/WARNING (con word
boundary) sigue clasificandose. Nota: `\bERROR\b` NO matchea dentro de `ValueError` (sin boundary),
asi que los tracebacks que solo dicen `ValueError:` no generan issue por si solos.


## SofiaTunnel: forward remoto zombie (corregido 2026-10-16)

Los probes `sofiatunnel` y `sofiatunnel_e2e` se caen juntos cuando el sshd del
droplet mantiene `127.0.0.1:5180` por una sesion SSH muerta (cliente que
desaparece en silencio); NSSM reintenta en bucle (exit 255) hasta ~17-18 min.
Corregido en el droplet con `ClientAliveInterval 15` + `ClientAliveCountMax 3`
(drop-in `/etc/ssh/sshd_config.d/10-client-alive.conf`, `systemctl reload ssh`).
Detalle, evidencia y rollback en `C:/Users/ahmed/AGENTS.md`. No hay bug en el
monitor; el auto-restore solo reintenta/espera.


### Slow request en /products/image (mecanismo verificado 2026-10-16)

El issue 10862 (`Slow request - GET /products/image/...`, WARNING, fuente active) es la
familia esperada, pero el endpoint es normalmente rapido (16-47 ms): se vuelve lento por el
indice de imagenes. En `mayor/backend/app/utils/images.py` el indice se construye con
`os.scandir` sobre el share SMB `\\192.168.0.243\Imagenes Sap\WM-M\Images` y se
RECONSTRUYE INLINE dentro de la request cuando expira el TTL (`PRODUCT_IMAGES_INDEX_TTL`,
default 300 s) o cuando el sentinel cross-worker marca invalidacion; las demas requests de
imagen concurrentes quedan bloqueadas en `_directory_index_lock` hasta que termina el scan. El
rebuild se dispara cada ~5 min y suelen hacerlo 3-4 workers en el mismo segundo (evidencia:
`Building image directory index` en el AppStderr de Mayor), asi que las requests de imagen se
encolan juntas; medido 2026-10-16 01:56 local, 3 rebuilds en 0,2 s y ~9 s hasta liberar.
Por eso aparecen rafagas con la MISMA duracion (p. ej. 2026-10-14 18:34:49-52: 6 requests de
22-28 s). `_fingerprint` (db_service.py) normaliza numeros largos a `N`, asi que todos los
`/products/image/*` caen en el mismo issue. Duraciones observadas 5-28 s. Medido 2026-10-16 con
el share sano: enumerar sus 9.849 archivos tarda ~245 ms (Test-Path 41 ms), asi que esos stalls
de 6-28 s son latencia SMB TRANSITORIA, no el costo base del scan (re-medir el share antes de
culpar al codigo). La request responde
200 (no es un fallo). Mejora posible (NO aplicada): reconstruir el indice en segundo plano o
servir el indice anterior mientras se refresca.


## WppConnect DOWN: frame de puppeteer colgado (issue 81, 2026-10-16)

Fallo real recurrente (~1-2/dia). Sintoma: el probe `wppconnect` de Sofia marca DOWN con
`status_code=None` (excepcion/timeout), pero `GET /api/default/status-session` responde
`CONNECTED` en <0.2s. La causa es que `GET /api/default/check-connection-session` CUELGA:
internamente llama `Whatsapp.isConnected()` -> `page.evaluate()` y el frame de
puppeteer/Chromium esta colgado (`Runtime.callFunctionOn timed out` en el log de WPPConnect).
El status en memoria queda `CONNECTED` stale (no llegan eventos statusFind con el frame muerto).

Remedio NO destructivo (preserva pairing; es lo que hace el propio codigo de Mayor):
1. `POST /api/default/{secret}/generate-token` -> token.
2. `POST /api/default/close-session` (Bearer).
3. `POST /api/default/start-session` (Bearer) con `{"waitQrCode":true,"createOptions":{"autoClose":0}}`
   y SIN la clave `webhook` (asi el server usa su `WEBHOOK_URL` y no se rompen los entrantes).
   Tarda ~30s y devuelve `{"status":"CONNECTED","qrcode":null}` (qrcode:null = pairing intacto).
4. Verificar `check-connection-session` -> 200 `{"status":true}`.
Verificado 2026-10-16 (12:36Z): recuperado en <1 min, sin QR, y el probe volvio a UP.

Alternativa nativa: Mayor ya tiene `_auto_recover_session` (misma secuencia close+start) que
dispara `_track_abnormal_and_maybe_recover` (grace `_STUCK_CONNECTED_GRACE_S=90s`) al final de
`get_full_status`. PERO solo corre cuando alguien pollea `/status`/`/health` de Mayor (el
frontend cada ~5s); fuera de horario no se dispara y el probe queda DOWN hasta que alguien
recupere. No hay job periodico que lo dispare. Mejora posible (NO aplicada): un trigger
periodico en Mayor (o un probe de Sofia a `/api/v1/wppconnect/status`).

No reiniciar el servicio NSSM como primera opcion: close+start es mas suave y suficiente para
el detached-frame comun. Reiniciar WPPConnect NO borra el pairing (pero es mas pesado).

La linea de log `error: ... Runtime.callFunctionOn timed out ... ProtocolError` (issue 77881) es la
MISMA firma de ese frame colgado; el monitor pasivo la ingesta como issue ERROR, y puede llegar
tarde (la linea ingerida era de ~6 h antes, cuando WPPConnect ya estaba UP). No es un error nuevo:
si el probe `wppconnect` esta UP (200) al momento, es historico. ADEMAS: las mismas lineas del
2026-10-16T19:32:37Z se reingirieron varias veces (issues 77959/77961 re-creados con nuevas
occurrences 78050/78051, 78166/78167) -> el CURSOR del log de WPPConnect no avanza / relee tramos;
revisar el cursor de `WPPConnect.out.log` (identidad de archivo / from_now).

## Vision: ola de 502 WHEP en MUCHAS camaras (2026-10-16 ~21:21-22:28 local)

Escalada respecto a la familia "cam_1702 sola": en la cola de `service.log` (logger vision.perf)
hay 158 lineas `-> 502` repartidas en 26 camaras distintas (`cam_*` / `_low` / `_sub`), no una sola.
Ambos servicios estan Running (VisionBackend, VisionMedia): NO es caida de Vision.

Evidencia (solo lectura, 2026-10-16):
- `GET http://127.0.0.1:9997/v3/paths/list`: 1225 paths registrados; solo 38 `ready`.
- 7 paths `ready` con `readers=0` y bytesReceived altos (cam_1225_sub 4.4 GB, cam_1544_sub 1.5 GB,
  cam_1011_low/cam_1291_low/cam_1035_low/cam_1596_low ~600 MB) -> sources on-demand que NO se
  cerraron pese a `sourceOnDemandCloseAfter: 15s` (posibles sesiones RTSP colgadas hacia la camara).
- Las camaras que dan 502 son las que NO estan `ready` (0 bytes): su source RTSP no arranca a tiempo
  (`sourceOnDemandStartTimeout: 15s`) y el proxy `/mtx/<path>/whep` responde 502 tras ~35 s.
- Los picos de 502 coinciden con rafagas de aperturas WHEP simultaneas (muro/grid abriendo decenas
  de camaras a la vez), p.ej. ~22:24-22:25 y ~22:27-22:28.

Interpretacion: problema de INFRAESTRUCTURA/UPSTREAM (camaras/red/MediaMTX), no de codigo de Vision.
Combinacion probable: camaras inalcanzables/inestables + timeout de arranque on-demand bajo rafaga +
paths colgados ocupando sesiones RTSP de la camara.

Accion tomada: NINGUNA (solo lectura). Items para el dueno:
- Revisar red/RTSP de las camaras que fallan (posible segmento/NVR comun).
- Evaluar limpiar paths colgados (reiniciar VisionMedia) -> CORTA todas las vistas en vivo; requiere OK.
- Evaluar subir `sourceOnDemandStartTimeout` o reducir aperturas simultaneas en el muro.

No confundir 201/204/404 (negociacion normal de WHEP) con 502 (fallo real). Solo el 502 es accionable.
## Vision /mtx WHEP: 502 = timeout del proxy (35 s); ERROR = ClientDisconnect no capturado (2026-10-16)

- `backend/app/routers/mtx_proxy.py` proxya `/mtx/<path>/whep` hacia MediaMTX `https://127.0.0.1:8889`.
- El `502 "video service unavailable"` lo genera el PROPIO proxy: `_SESSION.request(..., timeout=(5, 35))`.
  Si MediaMTX no contesta el WHEP en 35 s (source on-demand no lista), salta
  `requests.exceptions.RequestException` -> `HTTPException(502)`. NO implica MediaMTX caido.
  Por eso los 502 de la ola aparecen a ~35 s (y bajo carga extrema hasta 36-69 s por cola).
- `ERROR: Exception in ASGI application` (issue 78189) = `starlette.requests.ClientDisconnect` SIN capturar
  en `mtx_proxy.py:61` (`body = await request.body()`): el navegador cierra/cancela mientras el proxy
  espera hasta 35-70 s. Benigno (el cliente ya se fue) pero ensucia el log y el monitor lo sube a ERROR.
- Es VIVO, no historico: los ultimos 2 tracebacks referencian `mtx_proxy.py:61` y `main.py:162/88/119`
  actuales. Nota: issue 78189 agrupa por el TEXTO "Exception in ASGI application", asi que distintos
  tracebacks (p.ej. el viejo de Device.password) caen en el MISMO issue -> no fiarse del issue para el origen.
- Fix minimo propuesto (NO aplicado; requiere reiniciar VisionBackend -elevado- y corta las vistas en vivo):
  envolver `body = await request.body()` en `try/except starlette.requests.ClientDisconnect: return Response(status_code=499)`.

## Vision: el 502 masivo NO es de codigo (muro/grid sobrecarga MediaMTX) (2026-10-16)

Bajo un muro/grid que abre decenas de camaras a la vez, MediaMTX no arranca todas las sources on-demand
dentro del timeout y el proxy devuelve 502 en rafaga sobre MUCHAS camaras (26 distintas, 158 eventos en ~1 h).
Las latencias de negociacion suben de 2-7 s a 20-69 s. Es infraestructura/upstream (camaras/red/MediaMTX),
no un defecto por camara. Ver tambien la seccion de paths colgados mas arriba.
## Vision: la ola de 502 TERMINO; el cursor pasivo va ~10 h atrasado (2026-10-17)

Comprobado en vivo (solo lectura) el 2026-10-17 ~09:14:
- La ola de 502 se corto ~**07:09** (ultimo 502: `cam_1011_low` 07:09:50). Sin 502 despues.
- MediaMTX recuperado: **158 paths ready / 1302** (estaba en 38-44 durante la ola).
- Los reportes que llegan con `detail` de **2026-10-16 22:35** son BACKLOG: el log ya esta en
  ~09:14 del 2026-10-17 -> el cursor pasivo de Vision va ~10 h atrasado (mismo defecto de cursor
  ya documentado para WPPConnect). NO tratar esos reportes como estado actual.
- Regla practica: antes de concluir "sigue fallando", mirar la ULTIMA linea de `service.log` y el
  `ready` de MediaMTX (`:9997/v3/paths/list`), no la marca de tiempo del reporte.
## WPPConnect: auto-recuperacion por WATCHDOG y webhook 502 por reinicios de Mayor (2026-10-17)

- `start_wppconnect_nssm.ps1` ya trae un WATCHDOG (chequeo cada 60s): `show-all-sessions` (HTTP, sin
  Chromium) y `check-connection-session` (Chromium). Tras 3 fallos consecutivos de Chromium mata el node
  y el loop externo lo relanza. Log: `C:\Users\ahmed\apps\service-logs\watchdog.log`.
  -> El "frame colgado" de Puppeteer se auto-recupera; ya NO hay que hacer close+start a mano si el
  watchdog esta vivo. Verificado 2026-10-17: episodio de "Chromium unresponsive (1/3)-(2/3)-recovered"
  cada ~90 s entre ~00:48 y ~01:31 local, luego silencio (sano). El watchdog NO loguea cuando todo va bien.
- `WEBHOOK_URL` de WPPConnect se define en `start_wppconnect_nssm.ps1` (NO en el registro NSSM):
  `http://127.0.0.1:8075/api/v1/whatsapp/webhook/wpp` (Mayor). El secretKey del API NO es el placeholder
  por defecto (generate-token con `THISISMYSECURETOKEN` da 404); esta en `src/config.ts` (env SECRET_KEY).
- **`warn: Error calling Webhook ... status code 502`** = Mayor estaba reiniciando en ese instante.
  Correlacion verificado 2026-10-17: 502 a las 09:06:28/35 y 09:54:17/40 <-> arranques de Mayor a las
  09:06:53 y 09:54:55 (log `MayorBackend.out.log`, marca `SAP DI API Middleware is now running!`).
  El endpoint en si responde 200 (`POST /api/v1/whatsapp/webhook/wpp` 200 OK en el out de Mayor).
  -> Cada reinicio de Mayor = ventana en que se PIERDEN mensajes entrantes de WhatsApp.
- Mayor se reinicio 4 veces en ~1 h el 2026-10-17 (09:06, 09:54, 10:02, 10:09), sin traceback en
  `MayorBackend.err.log` (arranca limpio). Es un item a vigilar: si se repite, revisar por que muere.

## Vision: ERROR "El modelo local termino con error." (issues 79024/79025, 2026-10-16 23:52 local)

Familia NUEVA: `raise RuntimeError("El modelo local terminó con error.")` + `RuntimeError: El modelo
local terminó con error.` (2 lineas del MISMO evento; el monitor las crea como 2 issues porque no
reconoce el patron raise/mensaje). Es un fallo REAL del motor de IA local de Vision (no del monitor).
Ocurrio en backlog (23:52 local del 10-16). Revisar el modulo de IA local de Vision si reaparece.
## Vision: POST /api/playback/open -> 502 = fallo del GRABADOR (no de Vision) (2026-10-16 23:54 local)

- `routers/playback.py::open_playback` -> `_open_sync` -> `services/recordings.py` que lanza
  `PlaybackError(502, ...)` (lineas 210/259/335) cuando el grabador/camara NO sirve el track grabado.
- Patron observado: la latencia del endpoint ESCALA (1.9 -> 3.5 -> 4.4 -> 4.7 -> 7.0 -> 8.7 -> 9.7 s)
  y luego 502 a ~10.5 s. Es el grabador estresado/lento y luego fallando: mismo origen que los 502 de
  WHEP (lado dispositivo/upstream), NO codigo de Vision.
- No confundir con `/api/playback/media/*` ni con el WHEP `pb_<hash>` (400 = "no hay grabacion", normal).

## Sofia (monitor): el redactor SOBRE-redacta palabras normales en URLs

- Reportado 2026-10-16: `POST /api/[REDACTADO]/open` en el mensaje del issue, cuando la ruta real
  (log crudo de Vision) es `/api/playback/open`. El redactor sustituyo la palabra "playback".
- Efecto: se pierde informacion de diagnostico. Sugerencia (NO aplicada): redactar solo valores
  (numeros largos, tokens, UUID, emails, telefonos) y no segmentos de ruta con palabras del dominio.

## Fixes 2026-10-17 (noche): monitor docker, watcher y ruteo - autorizados por el dueno

Contexto: el dueno pidio "arreglar de raiz las dos salvedades [de la auditoria], que el CLI local las vea; todo lo de esta PC es area de mantenimiento".

### 1. Monitor docker del droplet caido ~8 h (46 fallos consecutivos desde ~11:10 local)
- Causa demostrada: `docker logs --since <ventana> tec-db-1` tarda 60-150 s porque su json-log mide ~2.2 GB en el droplet (domina el escaneo hacia atras del archivo, no el tamano de la ventana); revienta el timeout de canal de `_ssh_exec` (40 s) -> `TimeoutError()` con str vacio -> poll fallido; la ventana crece hasta el tope de 2 h y no se auto-recupera.
- Fix (`droplet_docker_service.py`): `LOG_TAIL_CAP = 50000` (`docker logs --since X --tail 50000 ...`; la consulta baja a ~3 s) + `_fetch_container_logs()` aisla fallas por contenedor (queda en `log_failures` de `/api/health/sofia`; una fuente lenta ya no tumba el poll completo).
- Verificado en vivo tras reiniciar SofiaBackend: primer poll OK y segundo poll estable (`consecutive_failures: 0`, `log_failures: {}`), sin nuevas ocurrencias `droplet-ssh`.
- Pendiente del dueno (NO autorizado aun): rotacion de logs docker en el droplet (`daemon.json` log-opts max-size/max-file; requiere recrear contenedores) y/o truncar el json-log de 2.2 GB. El droplet ademas mostro OOM reales esa noche (postgres/redis/systemd oom-killer 19:35-19:47 y 01:03 UTC) - presion de memoria a vigilar.

### 2. Errores WinError 5 del watcher al reemplazar state.json
- Evidencia: 5 lineas en `local/watcher.log` (mtime <=07:58 local) y 4 en `elenatec/watcher.log` (<=02:08); los state.json se escribian normal; la suite de tests usa dirs temporales (no toca el state real). Holder no identificado (locks transitorios; candidatos: AV/filtros). OJO: `occurrences.timestamp` de eventos pasivos = hora de INGESTA (los 9 eventos se ingirieron recien al activar el monitoreo, no a la hora del error original).
- Fix (`sofia_devin_bridge.py`): retry del rename con backoff exponencial topado: `STATE_REPLACE_ATTEMPTS=8`, `STATE_REPLACE_DELAY=0.2`, `STATE_REPLACE_MAX_DELAY=2.0` (~9 s de tolerancia total; antes ~2 s).
- Visibilidad: `log_path` de ambos watcher.log agregado a los servicios `sofia-devin-local`/`sofia-devin-elenatec` (config.json + DEFAULT_SERVICES); el clasificador los marca ERROR y rutean al grupo local. Los watcher.log previos quedaron como `watcher.log.bak-20261017` (evidencia).

### 3. Ruteo: `droplet-ssh` al grupo local
- `elenatec_route.exclude_service_ids = ["droplet-ssh"]` (policy.json) + soporte en `load_policy`/`route_event`: los fallos del propio monitor (fix Sofia-side) son area local, no de elenatec. Verificado en vivo: la ultima ocurrencia pre-fix (81581) se entrego a la sesion local.

### 4. Reloj del host y procesos Python
- `tzutil /g` = Eastern Standard Time; bias 300->240 el 2026-10-17 20:04:43 local => offset actual UTC-04:00. Confirmar con `Get-Date -Format K` antes de correlacionar.
- Los procesos Python escriben asctime con el tz CACHEADO al arrancar: SofiaBackend viejo (arrancado con UTC-5) logueaba UTC-5; el reiniciado hoy loguea UTC-4. Correlacionar por proceso, no por regla fija.

### 5. Efectos colaterales de la activacion (informativo)
- Al agregar los watcher.log como fuente se ingirieron UNA vez los 9 eventos historicos (2 alertas WhatsApp, issues 81570-81578); el archivo se roto despues para no repetir.
- El restart de SofiaBackend ingirio OOMs reales del droplet que el monitor roto nunca vio (alerta CRITICAL `droplet-host`).
- Tests: `backend/.venv/Scripts/python.exe -m pytest tests/ -q` -> 336 passed (incluye tests nuevos de `--tail`/aislamiento por contenedor y del backoff/ruteo).


## Ventana de acumulacion del puente y correo diferido de WPPConnect (2026-10-18)

Pedido del dueno: (a) que el puente espere ~1 min antes de mandar a Devin para
acumular rafagas y no repetir lo mismo; (b) que el correo de respaldo por
WPPConnect caido espere 3 min y se cancele si el servicio se reactiva (solo era
un reinicio).

### Ventana de acumulacion (`sofia_devin_bridge.py`, policy `batch_hold_seconds=60`)
- `hold_until` (clave nueva de estado, OPCIONAL en `validate_state`: los
  state.json previos cargan y se completan con 0.0; no se sube STATE_VERSION).
- El watcher arma la ventana cuando encola eventos y la cola estaba VACIA
  (`had_pending=False`): `hold_until = now + batch_hold_seconds`. No se extiende
  con cada aviso nuevo (latencia acotada); si ya habia pendientes (backlog,
  worker ocupado) no se re-arma.
- `Watcher._maybe_spawn` no abre consola y `ConsoleWorker._take_pending_events`
  no toma lote mientras `now < hold_until`. `/reintentar` manual NO se ve
  afectado. `batch_hold_seconds=0` desactiva la espera (asi corren los tests
  que no la prueban).
- Dedup en la cola: `find_pending_duplicate` agrupa por `issue_id`; las
  ocurrencias del MISMO issue mientras siga pendiente no se reenvian, solo
  incrementan `duplicates` y el reporte lo indica
  (`(+N ocurrencia(s) del mismo issue agrupadas en esta ventana)`). Un issue ya
  despachado que vuelve a fallar SI se encola de nuevo (no se oculta una
  recurrencia). `poll_once` devuelve `queued` y `deduped` para diagnostico.
- No se modifican los templates de prompt inicial; el marcador de duplicados es
  autoexplicativo y este AGENTS lo documenta.

### Correo diferido por WPPConnect caido (3 min)
- `health_service.WPPCONNECT_EMAIL_DEFER_SECONDS = 180`; al cruzar el umbral
  DOWN de `wppconnect`, `send_alert(..., defer_email_seconds=180)`. La alerta
  WhatsApp se intenta igual (queda encolada y sale cuando WPPConnect vuelve).
- `whatsapp_service.send_alert`: si el envio falla y hay defer, programa
  `email_alert_service.schedule_fallback_email(...)` con
  `cancel_check=lambda: _service_is_up(service_id)` (estado del health poll) y
  `key="deferred:<service_id>"`; sin defer el correo sigue siendo inmediato.
- `schedule_fallback_email` crea un task en el event loop del backend
  (`asyncio.get_running_loop()`; sin loop cae a envio inmediato). Un schedule
  nuevo con la misma clave reemplaza al anterior; al vencer, si el servicio
  esta `up` el correo NO se manda; si `cancel_check` falla, se manda igual
  (fail-open: no se pierde el aviso). Cooldown de correo (5 min) sigue
  aplicando al envio real.
- LIMITE CONOCIDO: la espera vive en memoria; un restart de SofiaBackend dentro
  de la ventana descarta ese correo (el issue queda en la DB y la alerta
  WhatsApp queda en `alert_queue`, que se drena al recuperar WPPConnect).
- NO se cambia el flujo de la cola WhatsApp: si WPPConnect se recupera rapido,
  el dueno puede recibir la alerta encolada + la recuperacion por WhatsApp.

### Verificacion
- Tests nuevos: `tests/test_sofia_devin_bridge.py` (ventana, no-extension,
  deshabilitada, dedup, reencolado post-despacho, migracion de estado, worker
  espera la ventana), `tests/test_email_alert_service.py` (diferido/cancelado/
  reemplazo/sin SMTP/sin loop), `tests/test_whatsapp_service.py` (defer +
  cancel_check + cadena completa E2E) y `tests/test_health_service.py`
  (wppconnect difiere, otros servicios no). Suite completa: 356 passed.
- DRY RUN de `.devin/sofia-cli/install.ps1`: policy.json parsea, tareas
  registradas sin drift, `devin auth status` autenticado.
- Los state.json reales (local/elenatec) migran sin tocar produccion (solo
  lectura): `hold_until=0.0`.

### Despliegue y monitoreo (2026-10-18, autorizado por el dueno)
- 14:23:42: tareas `Sofia-Devin-AppsPC`/`Sofia-Devin-ElenaTec` reiniciadas
  (Stop/Start). Ambos watchers Running con el codigo nuevo; state.json ya
  escribe `hold_until`; health 5185/5186 en 200; watcher.log sin errores.
- 14:25:37-14:25:51: `SofiaBackend` reiniciado con elevacion UAC aprobada por
  el dueno (script `backend/data/restart_sofia_backend_20261018.ps1`, log
  homonimo `.log`): SofiaFrontend parado -> SofiaBackend parado/arrancado ->
  SofiaFrontend arrancado; corte backend ~8 s, frontend ~13 s. Arranque limpio
  (una sola falla transitoria de SofiaFrontend mientras estaba parado, 1/3,
  sin alerta). El correo diferido quedo ACTIVO en el backend.
- En vivo se observo la ventana armandose correctamente (+60 s exactos) al
  primer evento nuevo; el worker abierto (codigo previo) tomo el evento antes
  de que venciera: **la espera efectiva de 1 min aplica cuando las consolas
  abiertas se reemplacen** (cierre del dueno o cambio de dia, la sesion se
  reanuda con session/load). La dedup del watcher SI esta activa ya.
- Senal del problema que motivaba la ventana (ui-events, 10-17/10-18):
  elenatec 237 lotes con 166 de tamano 1; local 48 lotes con 16 de tamano 1.
- Monitor de 15 min post-restart (data/monitor_fix_20261018.log): 45/45
  muestras 200/ok en ambos grupos, poll age < 15 s, 0 muestras no-ok, 0
  lineas de error nuevas en backend o watcher.log. En la ventana solo salieron
  2 alertas normales (wppconnect WARNING 14:33 y droplet ERROR 14:37) y 0
  correos de respaldo. La ventana se armo en vivo dos veces (+60 s exactos).
- PENDIENTE de observar en vivo: primer flap real de WPPConnect tras el
  restart para ver `[EMAIL] Correo diferido programado/descartado`; y la
  espera de 1 min efectiva cuando se reemplacen las consolas abiertas.

### Prueba: WhatsApp apagado 1 semana (2026-10-18, decision del dueno)
- `backend/data/config.json`: `alerts.whatsapp_enabled=false` (respaldo
  `config.json.bak-20261018-wa-off`). El backend relee config cada ~30 s;
  verificado en vivo sin reiniciar: `GET /api/config/alerts` -> false y
  `POST /api/config/alerts/test` -> `{"sent": false}` (no envia nada).
- Efecto: 0 mensajes de WhatsApp y 0 correos de respaldo (`send_alert`/
  `send_message` retornan False temprano); avisos de restauracion manual por
  WhatsApp silenciados (los auto-restore no dependen de esto); `alert_queue`
  queda congelada. El puente Sofia-Devin NO depende de WhatsApp.
- Revision acordada: ~2026-10-25. Revertir: poner `whatsapp_enabled=true` en
  config.json (o restaurar el .bak) — aplica en ~30 s sin reiniciar; confirmar
  con `POST /api/config/alerts/test`. Con el apagado, el correo diferido de
  3 min queda dormido hasta reactivar.
- Sin cola durante la prueba (pedido del dueno, 2026-10-18): con
  `whatsapp_enabled=false` no se encola, no se drena y no sale correo (los
  envios retornan temprano; `flush_queue` no toca la cola); test
  `test_disabled_whatsapp_never_sends_or_queues`. Verificado contra la DB:
  0 filas serian enviadas al reactivar (las 103 pendientes son
  `delivery_uncertain` o superan las 24 h de `get_pending_alerts`), asi que
  no habra rafaga al volver a activar WhatsApp.
