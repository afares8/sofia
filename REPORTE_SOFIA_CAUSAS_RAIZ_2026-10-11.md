# Auditoría de Sofía y aplicaciones monitoreadas — 11 de octubre de 2026

## 1. Dictamen ejecutivo

Sofía está ejecutándose como **LocalSystem mediante NSSM**, en `C:/Users/ahmed/apps/sofia/backend`, con Python de su `.venv`. Respondió `GET http://localhost:5180/api/ping`; sus **11 checks habilitados estaban UP** al consultar el estado cacheado a las 18:11 UTC. El check DIAPI está deshabilitado. Esto acredita disponibilidad de los probes, **no la corrección de ventas, inventario, SAP ni entrega de mensajes**.

El inventario incluye TODOS los issues presentes en la captura, sin limitarse a la UI ni a errores no resueltos recientes. Hay fallos recientes de negocio que requieren corrección; también hay históricos, avisos operativos, pruebas y registros duplicados del mismo incidente en SDK/Docker/DB.

**Prioridad de reparación:** (1) transacciones/idempotencia fiscal de ElenaTec; (2) disponibilidad y entrega verificable de WhatsApp; (3) identidad y consistencia de reservas WMS Mayor/Packing; (4) cobertura y revisión nocturna de Sofía; (5) recursos y pruebas aisladas. Reiniciar todo, borrar reservas o quitar constraints NO corrige estas causas.

Esta entrega es un reporte y actualización de instrucciones. **No se aplicaron fixes de aplicación, reinicios, despliegues, cambios de config runtime, migraciones ni modificaciones de datos.** No se enviaron mensajes de prueba.

## 2. Alcance, método y límites

- Captura consistente de `backend/data/sofia.db` con SQLite `mode=ro`, `PRAGMA query_only=ON` y una transacción de lectura. No se importó `app.main`, `init_db` ni `load_config`, que pueden activar escrituras/servicios.
- Fuente congelada: `auditoria_sofia_2026-10-11_datos.json`. Todas las tablas y anexos se derivan de ESA captura, no de nuevas consultas que mezclen cortes.
- Corte: **2026-10-11T18:11:09.349953+00:00**. Issues: primera detección conservada 2026-08-21; última actividad en el corte 2026-10-11. Retención configurada: **7 días** para occurrences/metrics. Las issues no resueltas antiguas sobreviven: `db_service.py:408-414` solo elimina issues antiguas RESUELTAS.
- `issues.count` es acumulado de ingestas del issue, NO ocurrencias de una ventana reciente, ni incidentes únicos, ni usuarios afectados. El total incluye WARNING/INFO. El mismo fallo fiscal aparece en SDK, backend y Postgres: no sumar esas capas como fallos independientes.
- `first_seen/last_seen` Docker reflejan INGESTA: `droplet_docker_service.py:399-418` guarda el timestamp de origen en `detail` y usa `datetime.utcnow()` para el evento. Para cronologías causales usar el `ts=...Z` de origen. Los logs NSSM muestran reloj local; no mezclarlo con UTC sin conversión. Los timestamps sin offset de clientes deben confirmarse antes de convertirlos.
- `resolved=0` significa que nadie cerró el issue en Sofía, NO que el fallo sigue sucediendo ahora. En esta captura los 5.982 issues están sin cerrar, incluso los INFO de backups exitosos.
- Se revisaron fuentes de Sofía, Mayor y Packing y el checkout local de ElenaTec. El HEAD del repo remoto `/root/tec` fue `f1f4386cd0227b7c9d8361803c707f59b45cc80f`; el checkout local `tec-review` fue `42449e5ac28889ed18598a333e452026ecfe2e78`. El SHA remoto no está en los objetos locales. **No son versiones equivalentes demostradas, y el HEAD del host no acredita por sí solo la imagen desplegada**. Antes de editar ElenaTec comprobar código/imagen exactos; no asumir que un fix del checkout local está publicado.
- SSH de lectura confirmó backend/frontend/db/redis saludables, backup activo y `tec-sofia-regression-20261002` todavía activo. No se detuvo ningún contenedor.
- Cobertura completa del inventario retenido NO equivale a causa raíz probada para cada registro. Se distinguen **mecanismo demostrado**, **síntoma confirmado con causa pendiente** e **hipótesis**. Historial purgado, traces truncados y ausencia de un release fiable impiden cerrar todas las causas hoy.

## 3. Magnitudes reconciliadas

| Nivel almacenado | Issues |
|---|---:|
| CRITICAL | 59 |
| ERROR | 2,897 |
| WARNING | 2,937 |
| INFO | 89 |

**Total:** 5.982 issues; **29.693 ingestas acumuladas**; **4.841 occurrences individuales retenidas** (2026-10-04T05:30:17.701490+00:00 → 2026-10-11T17:41:25.359300). Hay 44 service_id históricos y 12 probes configurados, 11 habilitados. Las familias normalizadas del anexo son agrupaciones de trabajo, NO 2.192 causas raíz demostradas.

### Checks de las últimas 24 horas del corte

Ventana UTC desde 2026-10-10T18:11:09.349953. Cada servicio tiene 2.753 muestras retenidas. Un check fallido aislado no implica una caída notificada: la gracia depende de `failure_threshold` y del polling. No se calcula duración de downtime multiplicando estos checks.

| Servicio | Muestras | Checks fallidos | Última muestra is_up | Último HTTP | Máxima respuesta medida ms |
|---|---:|---:|---:|---:|---:|
| mayor | 2753 | 1 | 1 | 200 | 1812.0 |
| mayor_frontend | 2753 | 1 | 1 | 200 | 2093.0 |
| ngrok | 2753 | 2 | 1 | 200 | 3828.0 |
| packing | 2753 | 16 | 1 | 200 | 5047.0 |
| packing_frontend | 2753 | 0 | 1 | 200 | 2515.0 |
| sofia_frontend | 2753 | 1 | 1 | 200 | 1281.0 |
| sofiatunnel | 2753 | 0 | 1 | 200 | 4250.0 |
| sofiatunnel_e2e | 2753 | 1 | 1 | 404 | 2532.0 |
| tec | 2753 | 2 | 1 | 200 | 5546.0 |
| tec-frontend | 2753 | 0 | 1 | 200 | 2266.0 |
| wppconnect | 2753 | 63 | 1 | 200 | 4954.0 |

WppConnect: **63 checks fallidos**; Packing: **16**. Son prioridades de investigación aunque el último estado sea UP. El probe `sofiatunnel_e2e` espera **404 por contrato**; no clasificar ese 404 como caída.

## 4. Hallazgos prioritarios y reparación desde la raíz

### P0-1. ElenaTec: doble intento de factura y sesión SQLAlchemy invalidada (R01)

**Evidencia:** #24813 (`CRITICAL`, acumulado 10, 8 occurrences retenidas), #24816, #24818, #24821; última recurrencia el 11-oct. #24813 muestra `create_sale` → `handle_sale_completed` → `issue_fiscal_document_for_sale` → `db.flush()` → `UniqueViolation uq_einvoice_tenant_sale` → `PendingRollbackError`. Parámetros: tenant 49, sale 11958. Históricamente #18968/#18969/#18971 repiten 539 veces cada uno el conflicto `uq_einvoice_tenant_cufe`: son capas del mismo patrón, NO 1.617 incidentes distintos.

**Demostrado:** se intenta crear un registro que viola unicidad y luego se usa una Session que requiere rollback. **Por demostrar:** si el duplicado viene del dispatcher/outbox concurrente con la emisión síncrona, un retry, una relación ORM obsoleta, soft-delete u otro camino. La existencia de ambos caminos hace plausible la carrera, pero no identifica por sí sola cuál ocurrió.

**Archivos a revisar en el release efectivo:** `backend/app/api/routes/sales.py` (emisión síncrona y excepción), `backend/app/fiscal/events.py`, `backend/app/fiscal/service.py` (flush), worker de reconciliación e idempotencia. El checkout local accede a `sale.id` en el handler de excepción ANTES de `db.rollback()`; si ese atributo expiró, puede volver a disparar acceso DB sobre la Session fallida. Es un riesgo de camino determinista del checkout, no una conclusión de que el traceback truncado pruebe esa línea en producción.

**Cambio recomendado:** capturar IDs primitivos antes de operaciones fiscales; recuperar la Session antes de cualquier log/acceso ORM posterior al flush fallido; delimitar la transacción fiscal según el contrato venta/outbox. Unificar creación/consulta idempotente por `(tenant_id, sale_id)` para emisión síncrona y asíncrona; resolver conflictos concurrentes por constraint y recargar el documento existente. El job de reconciliación debe aislar cada documento y hacer rollback/limpieza de Session antes de continuar tras un fallo. No desactivar unicidad ni regenerar CUFE para esconder conflictos.

**Pruebas obligatorias en PostgreSQL aislado:** dos emisiones concurrentes de la misma venta → un solo documento; retry mismo idempotency key → misma venta; conflicto CUFE → documento existente/no cross-tenant; flush fallido → no `PendingRollbackError` al loguear ni al procesar el siguiente documento; la venta ya confirmada conserva estado y outbox y el cliente no induce duplicado. Comprobar también documentos soft-deleted y emisión parcial.

### P0-2. WhatsApp: sesión compartida desconectada y entregas inciertas (R02)

**Evidencia:** #772 (QR requerido, acumulado 676), #25072 (593), #25078 (436 ciclos abortados), #25001 (365 recuperaciones fallidas), #25082 (298 cierres fallidos); #81 registra DOWN hasta el 10-oct. Sofía conserva **42 alertas con entrega incierta**, 41 por `ReadTimeout` y 1 por HTTP 500 `TargetCloseError`. Hay 72 filas sent y ninguna pending ordinaria en el corte; filas enviadas no acreditan lectura/entrega final al destinatario.

**Demostrado:** los callers recibieron sesión inactiva, no fue restaurada en esos intentos y determinadas respuestas de envío no permiten saber si el mensaje llegó. **Pendiente:** causa de pérdida de la sesión Chromium (crash, memoria, pairing, concurrencia de recuperaciones, versión). No convertir 42 inciertas en 42 mensajes no entregados.

**Reparación:** coordinar un solo propietario/lock de recuperación para `default` entre Mayor, Sofía y watchdogs; health semántico de sesión conectado, no solo HTTP 200; circuit breaker para no reintentar cada destinatario cuando está caída; cola con estado `delivery_uncertain` y reconciliación por ID/acuse verificable antes de reenviar. Conservar pairing y rate limits. No enviar una alerta por WhatsApp como único mecanismo para avisar que WhatsApp murió: revisar el fallback de correo sin enviar mensajes reales durante tests.

**Pruebas:** desconexión aborta el ciclo sin recorrer destinatarios; dos workers solicitan recuperación → una operación; timeout después de aceptación → no duplicado automático; 500/TargetCloseError → incierto, no éxito; reconexión → reanuda solo mensajes cuya no-entrega se confirmó. El código actual de Sofía ya distingue entrega incierta: conservar esa protección.

### P0-3. Reservas WMS Mayor/Packing: conflicto de stock y ownership (R05)

**Evidencia reciente:** #29297..#29300 (9 acumuladas cada uno) y #7122 (36; 13 retenidas). Ejemplo: `on_hand=0` propuesto contra `reserved=3`, pedido Mayor 7995, SKU 902410, bin `BIN:3:1-Z01-F02-R03-L02-B02`, `order_reference=7973`; otro intento pide reservar 4 con stock disponible 0. #7122: no hay bin con stock suficiente para SKU 737996/pedido 6675.

**Demostrado:** las guards de Packing rechazan una deducción incompatible con las reservas y la posterior reserva sin stock. El código compara EXACTAMENTE `order_reference` en eventos RESERVE/RELEASE y no libera reservas ajenas. **Hipótesis:** DocEntry/DocNum/id local no equivalentes, reservas huérfanas, datos obsoletos o stock físico realmente insuficiente. Todavía NO se consultó el ledger de estos pedidos; no afirmar cuál es la causa de sus datos.

**Reparación:** auditar identidad pedido Mayor/SAP DocEntry/SAP DocNum por tenant/empresa/bodega/bin/SKU; comparar ledger RESERVE/RELEASE/PICK_CONFIRM/ADJUST y stock con documentos ya facturados. Reutilizar `deduct_wms_inventory_for_order`, `InventoryService.adjust_inventory` y la reconciliación existente. Canonizar identidad en todas las rutas y migrar alias ambiguos solo mediante plan específico aprobado. Evitar read-modify-write absoluto no protegido; operación idempotente y atómica con control concurrente. Reintentos de fallo permanente deben ir a revisión operativa, no repetirse indefinidamente.

**NO hacer:** liberar todas las reservas, fijar reserved a 0, eliminar eventos, quitar `CK_wms_bin_inventory_reserved_le_on_hand` o recrear reservas de pedidos ya facturados. La guard protege otros pedidos y debe preservarse.

**Pruebas:** misma operación repetida no dobla descuento; DocEntry != DocNum resuelve ownership correcto; pedidos A/B sobre mismo SKU/bin no se roban reservas; factura parcial/liberación previa/stock cero/bin alternativo/rollback; reconciliar usa PICK_CONFIRM y ledger real, no solo SUM(RESERVE).

### P0-4. Sofía tiene huecos confirmados de observabilidad y revisión automática

1. **Logs pasivos inexistentes:** config señala `mayor/backend/logs/app.log` y `Packing/backend/logs/app.log`; ambos no existen. `log_service.py:39-41` retorna silenciosamente. NSSM sí tiene `apps/service-logs/MayorBackend.err.log` y `PackingBackend.err.log`. No cambiar la ruta ciegamente: SDK ya reporta errores y stderr incluye INFO; diseñar fuentes/parsing multiline y deduplicación cross-source. Un archivo ausente debe generar un estado de monitor inválido, no éxito silencioso.
2. **Revisión nocturna bloqueada:** `SofiaBackend.err.log:29940` registra el 9-oct a las 19:04:59 local `Codex CLI no encontrado en PATH`. `nightly_review_service.py:173-177` lo convierte en RuntimeError. Sofía Monitor usa Codex; Sofía AI de Mayor usa Devin y no es el mismo subsistema. Verificar disponibilidad bajo **LocalSystem**, no solamente en la sesión ahmed. Decidir CLI soportado/ruta explícita después de aprobación; no instalar/ejecutar asistentes ni alterar auth como parte del reporte. Testear CLI ausente, salida inválida y reporte fail-safe visible sin auto-apply.
3. **Cobertura:** no existen probes dedicados en la config capturada para VisionBackend/VisionMedia/vms-api; `local_host_service.WATCHED_SERVICES` tampoco los incluye. Los eventos genéricos Windows capturan algunos crashes, pero no sustituyen health de negocio. DIAPI está deshabilitado y `restore_service.py` atribuye a Packing el middleware 9000, aunque su cliente registrado falla en 8001. Distinguir ambos middlewares; no asumir que health 9000 valida Packing.
4. **Agrupación/contexto:** `_fingerprint` usa primeros 300 caracteres del mensaje y solo neutraliza algunos números/UUIDs; los JSON incluyen datos/correlation/timestamp y el scrape corta a 500 caracteres. Se fragmentan problemas recurrentes y se pierden pistas. Definir fingerprint estructurado y versionado por evento/ruta/excepción/contexto semántico; conservar el raw seguro fuera del mensaje. No colapsar errores distintos por borrar todos los IDs ni alterar datos históricos sin plan.
5. **Pruebas contaminan producción:** hay URLs `testserver`/localhost, SQLite y contenedores de pruebas en la misma base de issues. `ingest.py` conserva `environment` pero no separa esos eventos. Separar entorno/release y transporte; mocks/spool de SDK en tests. Un issue etiquetado prod pero con testserver NO acredita fallo productivo.
6. **Documentación heredada con secretos:** al resolver la codificación del AGENTS del servidor se detectó un token en texto plano. No se reproduce su valor ni se incorpora en las nuevas instrucciones; la copia temporal fue retirada. Retirar la credencial de documentación y acordar rotación/actualización con el operador, sin modificar autenticación durante esta auditoría.

**Validación:** pruebas aisladas para path ausente/rotación/parsing multiline/dedup; falsos 404 de probes; payload de test no dispara alertas; evento equivalente JSON agrupa y errores semánticos distintos no; cuenta reciente sale de occurrences, no SUM(count). Validar snapshot SQL de solo lectura y no usar arranque de app como test documental.

### P1-1. Recursos del host y contenedores de prueba (R04/R16/R17)

**Confirmado:** OOM del kernel con Node/Vitest eliminado (#14269/#14270, entre otros), y el 3-oct Postgres invocó oom-killer y se eliminó systemd (#24633/#24634). El proceso que invoca oom-killer no es necesariamente el culpable principal. Windows registró memoria disponible 0,3% (#25454), CPU sostenida, crashes de middleware SAP, VisionClient, ffmpeg y RustDesk. Falta el diagnóstico del crash completo para atribuirlos a una causa única.

Los contenedores de test muestran `No space left on device` en WAL, `out of shared memory`, roles/esquema incorrectos y constraints; no son evidencia automática de esos mismos defectos en tec-db productivo. `tec-sofia-regression-20261002` sigue activo: primero identificar propósito, mount y recursos; no borrarlo de oficio.

**Reparación:** builds/tests fuera del host productivo, límites de memoria/CPU/disco por job y serialización; cleanup de artefactos solo con aprobación exacta, nunca volúmenes de datos. Capturar lock/shared-memory/HINT de Postgres antes de ajustar configuración; reproducir fixtures, roles, search_path y migraciones en entorno aislado. No dar superuser ni elevar límites para ocultar un fixture roto.

### P1-2. Integraciones, rutas y toolchain (R06/R07/R08/R09/R12/R24/R26/R27/R30)

- **SQL/SAP:** conexión rechazada/timeouts/SLD/COM confirmados. Puerto SQL conocido en las instrucciones no sustituye la comprobación del listener/instancia/firewall/hora del fallo. Usar pool existente, readiness por empresa y middleware específico, retries acotados e idempotentes. Deadlock #28154 en `audit_service.get_recent_orders` confirmado; obtener deadlock graph antes de decidir índices/orden de locks/aislamiento. Nunca NOLOCK ni desactivar constraints como arreglo.
- **DMCE:** #2983/#2987: página HTTP 200 con `login_error=1`, por lo tanto login rechazado, no éxito de conexión. Revisar contrato/login/sesión y credenciales mediante operador autorizado, sin imprimirlas; causa credencial/CAPTCHA/cambio de portal aún pendiente. Tests con HTML de rechazo, redirects y sesión expirada.
- **Archivos UNC:** #1123 demuestra AccessDenied histórico en imágenes SAP; #1122 volvió a registrar wrapper de imagen el 10-oct, con traceback truncado. No asumir que ambas fechas tienen exactamente la misma excepción. Verificar identidad LocalSystem y permisos mínimos share+NTFS, y acceso del proceso real; no Everyone FullControl. Fallback seguro en imagen ausente/denegada y escritura atómica de Excel sin atribuir PermissionDenied automáticamente a archivo abierto.
- **Playwright/PDF/ffmpeg:** #1188/#16 indican navegador ausente en perfil systemprofile/ahmed; #6509 KeyError `.webp`; #10365 ffmpeg no encontrado. Provisionar la versión de navegador requerida en ubicación compartida aprobada y legible para la cuenta del servicio; usar API async o proceso/worker aislado; verificar compatibilidad de formato de imagen. No reparar con downloads flotantes ni arrancar una segunda app.
- **Paths Windows:** #3611 usa `/tmp/backup_orders_sent.json` inexistente. Usar directorio runtime apropiado existente, escritura atómica y permisos correctos. No crear `/tmp` como parche para un path POSIX hardcodeado.
- **HTTP resources:** #29670 registra tres `Unclosed client session`; el shutdown de Packing YA llama `PackingSapClient.close_session()`. No afirmar que falta ese cierre: localizar instancia/exception/loop que escapó y probar cancelación/cierre de la sesión real.

### P1-3. Frontend, datos y negocio (R10/R11/R13/R19/R20/R21/R22/R23/R28/R29)

- **Assets:** 1.653 issues Docker de recursos ausentes/index prohibido en la categoría R13. Hay chunks plausibles y rutas de escaneo (`.php`, `${...}`, directorio assets). Separar solicitudes legítimas de scans por ruta/referer/release; cache viejo NO explica automáticamente todos. Para chunks legítimos: HTML revalidado, assets versionados y retención de versiones durante deploy, recuperación de lazy load acotada. No responder HTML 200 a un JS inexistente.
- **Esquema Postgres:** columnas/tablas ausentes, FK/settings, tipos y roles incorrectos confirmados; #29228 `si.tenant_id` no existe el 9-oct. Falta statement completo/application_name/origen para distinguir query de diagnóstico de bug de app o migración. No crear columnas especulativas ni rol postgres solo para silenciar un probe; validar esquema y migración del release real.
- **Rendimiento:** slow requests medidos en scoreboard, details-batch, packing search/add-bulto, health y alertas de inventario. Son síntomas, no prueba de falta de índice. Medir ruta/DB/cola/CPU en entorno seguro; verificar bloqueo async, N+1, paginación y límites. EXPLAIN de lecturas según plan aprobado; no EXPLAIN ANALYZE de writes en producción.
- **Yappy/retenciones:** 216 registros de `sales.payment.verify.no_amount` en la familia semántica, 45 retenidos. COMPLETED sin monto no prueba conciliación del importe: contract validar monto/moneda/referencia e idempotencia antes de reconocer pago. Retenciones sin cliente o saldo CxC suficiente requieren validación de dominio antes de efectos fiscales; no convertir una ley en warning silencioso ni modificar saldos para ocultarla.
- **Correo:** supresión/bounce/saas_dunning confirmados. Preservar lista de supresión y evitar retries permanentes; corregir destinatario/canal según procedimiento autorizado. No remover supresión para forzar envío.
- **Browser/auth:** expiración/401, refresh 502/timeout, ErrorBoundary, useAuth y transitions mezclan producción/dev. Reproducir por release y sourcemap; single-flight de refresh, provider correcto, cancelación/estado stale. Transitions/ResizeObserver requieren comprobar impacto antes de priorizarlas igual que una excepción de venta.
- **Support runner/IA:** runner unavailable, HTTP 503 y errores LLM requieren readiness y causa concreta de proveedor (cuota/auth/timeout); no rotar credenciales ni desactivar auth para desbloquear. Tests con proveedor mock, errores legibles y sin efectos externos.
- **Stock bajo:** `inventory.low_stock` es señal operativa, no automáticamente bug de código. Agrupar por entidad/estado para evitar miles de issues, mantener la decisión de reposición y umbrales bajo responsabilidad de negocio.

### P1-4. Backups e historial de restauración (R15)

#11978: una verificación de backup falló el 20-sep. Después hay eventos INFO de restauración OK (incluido 1-oct) y réplicas hasta 11-oct. No declarar todos los backups corruptos ni cerrar el fichero fallido sin la verificación específica. Revisar sidecar/hash/gzip/contenido/restauración aislada; conservar copias, no sobrescribir origen.

Sofía registra **6 restores de túnel fallidos y 6 E2E fallidos**. Algunos mencionan `remote port forwarding failed for listen port 5180`; la restauración de ACL+restart no corrige un bind remoto ocupado o rechazado. El motivo exacto requiere listener/sshd policy/sesión remota: no afirmar puerto ocupado solo por ese texto. Coordinar ambos probes para que no compitan restaurando un único túnel; testear estados y fallo de bind, no matar todos los SSH. Existen restores antiguos pending, que no equivalen a trabajo activo en memoria después de reiniciar Sofía; reconciliar estado tras restart con auditoría, sin borrar historia.

## 5. Backlog exhaustivo por categoría

Categorías de TRIAJE orientativas, asignadas una vez por issue mediante reglas visibles en `_audit_sofia_catalog.py`. Pueden contener causas distintas; revisar cada fila antes de implementar. El CSV conserva TODOS los IDs, servicio, nivel, fuente, entorno, acumulado, occurrences retenidas, fechas, familia, mensaje y detalle redactados.

| Categoría | Issues | Acumulado | Occ. retenidas | Acción y condición de cierre |
|---|---:|---:|---:|---|
| R01-fiscal-transacciones | 17 | 1664 | 41 | Idempotencia fiscal y Session recuperada; concurrencia PostgreSQL sin doble documento. |
| R02-whatsapp | 88 | 5835 | 2846 | Sesión compartida y entrega incierta; sin duplicados ni recovery concurrente. |
| R03-tunel | 2 | 12 | 4 | Diagnóstico del túnel y recovery coordinado; probe local y E2E cumplen contrato. |
| R04-recursos-host | 23 | 31 | 1 | Capacidad y límites; pruebas/builds aislados y ningún OOM nuevo bajo carga aprobada. |
| R05-wms-reservas | 124 | 317 | 96 | Ownership y ledger WMS; consistencia sin liberar reservas ajenas. |
| R06-sql-conectividad | 142 | 2916 | 0 | Listener SQL/instancia/red/pool; readiness por empresa y reintentos acotados. |
| R07-dmce | 3 | 647 | 16 | Contrato login DMCE; no confundir HTML 200 con sesión válida. |
| R08-sql-deadlocks | 14 | 16 | 3 | Deadlock graph y transacción; retries solo de unidad idempotente. |
| R09-sap-di | 163 | 1804 | 100 | COM/SLD/DIAPI por puerto/empresa; preservar slots y health real. |
| R10-postgres-esquema-y-datos | 77 | 93 | 1 | Statement/origen y esquema del release; migración probada o query corregida. |
| R11-rendimiento | 578 | 7074 | 838 | Perf de endpoint/DB/cola; regresión medible sin suponer índice. |
| R12-archivos-y-permisos | 105 | 1897 | 2 | Identidad del servicio/share/NTFS; fallback y escritura segura. |
| R13-assets-y-escaneos | 1653 | 1919 | 1 | Separar chunks legítimos y escaneos; deploy/cache sin JS faltante. |
| R14-observabilidad-ssh | 6 | 43 | 4 | Liveness SSH/cursor/backfill; errores identificables sin perder logs. |
| R15-backups | 93 | 94 | 31 | Validación backup/restauración aislada; probar el artefacto que falló. |
| R16-contenedores-no-mapeados | 236 | 248 | 0 | Atribuir contenedor/entorno/mounts; fixtures y recursos aislados. |
| R17-windows-y-procesos | 118 | 133 | 15 | Evento Windows completo/dump; crash y recursos diagnosticados por proceso. |
| R18-disponibilidad-y-deploy | 28 | 283 | 8 | Diferenciar deploy/crash/probe; evidencia de recuperación y no solo RUNNING. |
| R19-stock-bajo | 1290 | 1293 | 557 | Reposición de negocio y agrupación de señales; no esconder bajo stock. |
| R20-correo | 260 | 265 | 32 | Supresión permanente/canal; no retries a destinatario suprimido. |
| R21-pagos-y-retenciones | 244 | 308 | 45 | Monto/moneda/retenciones y contrato de negocio; nunca pago sin conciliación. |
| R22-runner-soporte | 153 | 153 | 61 | Readiness del runner de soporte; fallo visible sin bypass de autenticación. |
| R23-browser-y-auth | 138 | 782 | 103 | Repro por release/provider/refresh; cancelación y sourcemaps. |
| R24-recursos-http | 1 | 3 | 3 | Encontrar owner de ClientSession y cierre en todas las salidas. |
| R25-otros-requieren-trazado | 328 | 1326 | 22 | NO CERRAR: trazar IDs residuales con evidencia de causa y test dedicado. |
| R26-renderizado-impresion-toolchain | 9 | 159 | 6 | Toolchain bajo cuenta del servicio; renderizado offline aprobado. |
| R27-rutas-y-persistencia | 10 | 155 | 0 | Directorio runtime Windows y atomicidad; sin path POSIX hardcodeado. |
| R28-validacion-negocio | 46 | 84 | 0 | Validación de dominio previa; error controlado sin efectos parciales. |
| R29-proveedores-ia | 17 | 17 | 1 | Contrato/error proveedor IA; mock, timeout, fallback sin alterar secretos. |
| R30-api-transporte-y-callbacks | 16 | 122 | 4 | Contrato transporte/callback; timeouts/retries/idempotencia y reanudación. |

### Casos residuales que NO se dan por solucionados (R25)

328 issues (1.326 acumulados, 22 occurrences retenidas) necesitan trazado adicional. Ejemplos priorizados: #10383 `UnboundLocalError auth_type`, #14408 `list | set`, #3796 error SQL de parámetros, #5307 conversión varchar Y a int, #12709 OUTPUT incompatible con triggers, wrappers de Hikvision/export Excel/login/transfers y schedulers sin excepción completa. Estos mecanismos/errores están registrados, pero su reparación exacta requiere leer el código del release y entrada que los disparó. No convertir todos los wrappers en la causa de otro issue por proximidad temporal.

**Procedimiento obligatorio por cada ID residual:** obtener trace/origen/release; clasificar entorno y efecto; reproducir con mocks o datos aislados; test antes/después; cambio mínimo; reauditar; registrar evidencia y riesgo residual. Si el log ya se purgó, marcar MANUAL_GAP y mejorar instrumentación; nunca inventar la causa. El CSV permite filtrar por R25 y CRITICAL para no omitirlos.

## 6. Cobertura completa por service_id histórico

La ausencia de occurrences retenidas indica falta de detalle individual reciente, no resolución probada. Los nombres de contenedores experimentales no se convierten en aplicaciones productivas por aparecer en Sofía.

| service_id | Issues | Acumulado | Occ. retenidas | ERROR | CRITICAL | WARNING | INFO |
|---|---:|---:|---:|---:|---:|---:|---:|
| backup-replica | 93 | 94 | 31 | 3 | 1 | 0 | 89 |
| droplet-adoring_rubin | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-elenatec-wompi-gate-20261002-2232 | 5 | 5 | 0 | 1 | 0 | 4 | 0 |
| droplet-flamboyant_agnesi | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-host | 23 | 31 | 1 | 0 | 15 | 8 | 0 |
| droplet-jolly_robinson | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-jolly_yalow | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-peaceful_sutherland | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-qb-pg-rehearsal | 5 | 5 | 0 | 1 | 0 | 4 | 0 |
| droplet-ssh | 6 | 43 | 4 | 3 | 0 | 3 | 0 |
| droplet-tec-backend-green | 4 | 4 | 0 | 1 | 0 | 3 | 0 |
| droplet-tec-backend-run-03a8cc1c77c0 | 1 | 1 | 0 | 1 | 0 | 0 | 0 |
| droplet-tec-backend-run-11681e687021 | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-319036c3c21f | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-38a8b955e154 | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-38f9b1914eac | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-39984a108ee4 | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-44f5a8083db4 | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-4b0b9789dc2a | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-842a75d2c02e | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-a8547a47e6de | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-ba5f6235e2cf | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-be12102edd9c | 2 | 2 | 0 | 0 | 0 | 2 | 0 |
| droplet-tec-backend-run-c8af9767dd46 | 3 | 3 | 0 | 2 | 0 | 1 | 0 |
| droplet-tec-backend-run-d1a42b2a56fe | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-ddec49353e4a | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-backend-run-e921ee43325e | 2 | 2 | 0 | 0 | 0 | 2 | 0 |
| droplet-tec-frontend-green | 4 | 4 | 0 | 1 | 0 | 3 | 0 |
| droplet-tec-frontend-run-030888bab9b0 | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-frontend-run-54709dade286 | 2 | 2 | 0 | 1 | 0 | 1 | 0 |
| droplet-tec-qb-migration-check-1790975116 | 14 | 14 | 0 | 10 | 0 | 4 | 0 |
| droplet-tec-sofia-regression-20261002 | 15 | 27 | 0 | 9 | 2 | 4 | 0 |
| droplet-wompi_test_pg | 143 | 143 | 0 | 141 | 0 | 2 | 0 |
| mayor | 673 | 14065 | 3339 | 520 | 16 | 137 | 0 |
| ngrok | 1 | 2 | 2 | 0 | 1 | 0 | 0 |
| packing | 549 | 5604 | 373 | 346 | 6 | 197 | 0 |
| packing_frontend | 1 | 11 | 1 | 0 | 1 | 0 | 0 |
| server-sofia | 118 | 133 | 15 | 17 | 0 | 101 | 0 |
| sofiatunnel | 1 | 6 | 2 | 0 | 1 | 0 | 0 |
| sofiatunnel_e2e | 1 | 6 | 2 | 0 | 1 | 0 | 0 |
| tec | 2542 | 6808 | 1052 | 92 | 10 | 2440 | 0 |
| tec-db | 79 | 641 | 9 | 76 | 3 | 0 | 0 |
| tec-frontend | 1658 | 1939 | 3 | 1654 | 1 | 3 | 0 |
| wppconnect | 1 | 62 | 7 | 0 | 1 | 0 | 0 |

## 7. Orden de trabajo y verificación de cierre

1. **Preservar evidencia y reparar medición:** snapshots read-only, etiquetas test/prod/release, logs monitorizados reales, readiness por dependencia y revisión nocturna visible. No habilitar auto-fix ni cambiar retención/umbrales durante la auditoría.
2. **Datos críticos:** fiscal → WMS → pagos/retenciones. Correcciones aisladas, tests de concurrencia e idempotencia, sin datos productivos como fixtures. Para reparación persistente pedir operación exacta, alcance, backup restaurable, plan forward-only y aprobación.
3. **Canales y plataforma:** WhatsApp/túnel → SQL/DIAPI → memoria/disk/toolchain. Operaciones de servicio solo en ventana autorizada, no reinicios de prueba.
4. **UX y rendimiento:** assets/auth → queries y async → restantes de R25. No omitir el backlog porque los principales probes estén verdes.
5. **Cierre por issue/familia:** release verificado, test dedicado aprobado, verificación de flujo de negocio y recurrencia observada en ventana acordada; entonces marcar resuelto con evidencia. No marcar todos resolved ni purgar como mecanismo de limpieza.

## 8. Aprendizajes que se guardan para próximas tareas

- `AGENTS.md` de Sofía: fuente de verdad, rutas/puertos actuales, cuentas NSSM, distinción Monitor vs IA, snapshots seguros, unidad de conteo, límites de causa raíz y pruebas sin efectos.
- Skills nuevas en `.devin/skills/`: `sofia-incident-audit` para auditoría read-only reproducible; `sofia-root-cause-fix` para reparar con prueba, invariantes y autorizaciones.
- Apéndice acotado en `C:/Users/ahmed/AGENTS.md`, preservando bytes/codificación del contenido previo y evitando copiar secretos.
- Skills existentes: WhatsApp exige diagnóstico y autorización antes de reiniciar la sesión compartida; troubleshooting de Packing aclara que mismatch de order_reference es hipótesis hasta cotejar el ledger y que limpieza de reservas exige aprobación específica.
- No se normalizan instrucciones históricas de otros repos ni se altera su runtime. Las rutas históricas D:/ y comandos de dev no son receta de producción SERVER-SOFIA.

## 9. Artefactos y comprobaciones

- `auditoria_sofia_2026-10-11_inventario.csv`: inventario completo, una fila por issue, protegido frente a fórmulas CSV.
- `auditoria_sofia_2026-10-11_datos.json`: captura redactada, esquema, config allowlist, métricas, restores, cola y todos los issues.
- `auditoria_sofia_2026-10-11_grupos.txt`, `_categorias.txt`, `_resumen.txt`, `_evidencia.txt`: vistas derivadas y ejemplos revisados.
- `_audit_sofia_extract.py`: consultas explícitas de solo lectura; **NO reejecutarlo para validar este reporte**, porque produciría otro corte. `_audit_sofia_catalog.py`, `_audit_sofia_analyze.py`, `_audit_sofia_report.py` procesan la captura existente.
- Reconciliación comprobada: SUM issues por servicio/categoría = 5.982; SUM(count) = 29.693; SUM(retained_occurrences) = 4.841; todos los IDs asignados una vez. No hay filtros que oculten fuentes o niveles.
- No se ejecutan builds/tests de aplicación por una modificación documental en este servidor productivo. La validación aplicable es estructura/discovery de skills, sintaxis del material de auditoría, enlaces, cobertura de inventario, redacción de datos y revisión del diff. Los tests funcionales listados son requisitos PARA LOS FIXES futuros, no gates que ya hayan pasado.

**Estado de cierre:** auditoría del inventario conservado y plan de remediación entregados; aplicación de fixes y causas pendientes explícitamente NO completadas. No sería veraz declarar resueltos todos los problemas solo con este reporte.
