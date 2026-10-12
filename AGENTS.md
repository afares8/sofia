# Sofia Monitor - instrucciones del proyecto

## Alcance y fuente de verdad

Servidor productivo Windows SERVER-SOFIA. Este repo es Sofia Monitor, no Sofia AI/Executive de Mayor. Revisar primero `C:/Users/ahmed/AGENTS.md` sin imprimir secretos y las skills de este repo. No trasladar comandos historicos D:/ ni instrucciones de dev directamente a produccion.

Estado verificado 2026-10-11: SofiaBackend y SofiaFrontend corren mediante NSSM; backend en 5180, frontend en 5179, tunel SofiaTunnel. Confirmar siempre estado/rutas efectivos, no asumir que esta fecha sigue vigente. `SofiaBackend` corre como LocalSystem con `backend/.venv/Scripts/python.exe` y cwd `backend`. Logs NSSM en `C:/Users/ahmed/apps/service-logs/SofiaBackend.{out,err}.log`.

## Seguridad y auditoria

- Una solicitud de reporte NO autoriza reinicios, restores, auto-fix, deploy, migraciones, envio de mensajes ni cambios de datos. Solicitar autorizacion especifica para esos efectos.
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
