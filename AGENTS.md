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


## Triaje: warnings recurrentes de las apps monitoreadas (2026-10-15)

Familias que se repiten y son esperadas o artefactos de parsing, no fallos de las apps:
- **Mayor `hikvision_push` "DEVICE SECURITY: Peticion sin secret valido"**: chequeo de shared
  secret en modo suave por diseno (no bloquea; la marcacion se procesa igual). Detalle en
  `apps/mayor/backend/AGENTS.md`.
- **Mayor `app.security_service_improved` "PROBLEMA DETECTADO: IP privada sin proxy"**: se emite
  cuando un cliente LAN conecta directo sin proxy confiable (`security_service_improved.py`); es
  por diseno, no indica intrusion.
- **"Slow request — ..." (fuente active)**: umbral 5 s (`SOFIA_SLOW_MS`, default 5000) en el
  `sofia_sdk.py` de Mayor y Packing; endpoints pesados lo superan de forma recurrente.
- **Packing `[DATOS] ApTaxCode column not available`**: `OARG.ApTaxCode` no existe en esta BD SAP;
  el codigo cae a la consulta basica y responde OK (warning esperado).
- **Packing `[SAP-DI] Could not set U_*`**: faltan UDFs en la BD SAP (confirmado por
  `GET http://localhost:8001/health/ValidateUDF` -> falta `U_SecBltoNum` en RDR1). El codigo
  tolera la ausencia; es un hueco de configuracion SAP, no de codigo.
- **Packing WMS "reference ... indeterminate ... automatic release prohibited"**: guarda de
  seguridad de inventario; no libera automaticamente referencias ambiguas.
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

### Falso positivo del clasificador pasivo (verificado 2026-10-15)
`_classify_level` en `backend/app/services/log_service.py` clasifica lineas SIN prefijo de nivel
buscando la palabra ERROR/WARNING/WARN en cualquier parte de la linea (`_LEVEL_RE`). Por eso una
linea de continuacion de una excepcion multi-linea ya manejada puede crear un issue ERROR propio.
Caso real: `(Background on this error at: https://sqlalche.me/e/20/f405)` (cola de un
`logger.warning(str(exc))` de SQLAlchemy/pyodbc en Packing) quedo como issue ERROR 74128 sin que
Packing fallara; `normalize_reported_level` (event_classification.py) no cubre ese patron. Antes de
tratar un issue ERROR de una sola linea tecnica como incidente, ubicar la linea en el log y ver si
es continuacion (`[SQL: ...]`, `(Background on this error...)`, `Traceback`, lineas de codigo) de
un evento ya registrado.
