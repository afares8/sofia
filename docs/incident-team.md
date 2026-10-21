# Equipo de incidentes de Sofia Monitor

Esta mejora usa `occurrences`, la configuración `app_repos`, los sandboxes Git y el runner Codex existentes. Se ve en **AI Engineer → Equipo de incidentes**. No instala otra plataforma, no cambia el puente Devin y no contiene un ejecutor de producción.

## Recorrido implementado

```text
occurrence → pending → analyzing → preparing → reviewing → awaiting_ci
                                                           ↓
                                                awaiting_approval
                                                           ↓ operador
                                                deployment_requested
                                                (ejecutor desconectado)
```

- `occurrences.context` congela servicio, empresa (`tags.tenant_id`), entorno, release, fuente y mensaje redactado al ingerir. No toma contexto de un issue mutable: el agrupamiento histórico de issues no separa empresas. Las filas anteriores sin contexto quedan fuera de ejecución, con un gap visible.
- La clave de deduplicación incluye repositorio, servicio, empresa, entorno, release, fuente y fingerprint. Cada ID de ocurrencia tiene un receipt único; otra ocurrencia del mismo contexto incrementa el contador, no abre otra sesión. El snapshot inicial no cambia.
- Activar el lector por primera vez guarda el highwater y empieza **desde ahora**. No procesa backlog histórico. Encolado manual: `POST /api/autonomy/incidents/occurrences/{id}`. Un reset no vacío de IDs detiene el lector; `MAX(id)=0` por retención no rebobina el cursor.
- Claim, reserva de presupuesto y cambio de estado son una transacción SQLite corta. Tres sesiones y cuatro acciones lógicas (análisis, preparación, revisión, commit) se reservan antes de crear el sandbox. No se mantiene el lock durante Git, modelos o GitHub. Los límites son globales al nuevo circuito, persistentes, con ventanas calendario UTC.
- La selección del worker es FIFO (`status=pending ORDER BY id`) y usa un índice propio, sin el límite de la vista. La API de historial pagina con `limit` (1–100, default 50) y `before_id`; el panel permite cargar páginas anteriores. Si se revoca un scope o se habilita un despachador incompatible, ese pendiente pasa a `blocked` mediante CAS y auditoría, sin reservar presupuesto: no bloquea al siguiente. Un presupuesto temporalmente agotado conserva `pending`.
- El analista y revisor usan `read-only`; el engineer usa `workspace-write`, sin red ni raíces temporales adicionales. Tres invocaciones efímeras distintas; el revisor recibe incidente y diff completo, sin las afirmaciones del engineer. JSON estricto: un string `"false"`, claves duplicadas, acciones pendientes o riesgo alto bloquean.
- Cada stage tiene deadline (300 s por defecto, máximo 600 s), salida acotada y entorno por allowlist. La salida de herramientas/modelos no se imprime en logs; la evidencia guardada está redactada. Se usa el mensaje final del CLI, separado del banner. No se invoca Codex durante los tests.
- La redacción visita JSON y dict/list literales de forma recursiva, incluso dentro de mensajes serializados; reconoce alias/camelCase/separadores y claves escapadas. Conserva evidencia segura y usa un fallback léxico para texto incompleto. El gate de diff comparte los alias y bloquea literales de credenciales genéricas antes de revisión/transmisión/commit, incluidos dicts JSON/Python y JSON serializado. El escaneo está acotado y no certifica ausencia de cualquier secreto posible. La redacción cubre el contexto y la evidencia nuevos; no limpia datos históricos ni modifica las columnas raw del monitor heredado.
- El clone queda sin `origin`. El controller bloquea paths de datos/configuración/credenciales/gobernanza, escapes, enlaces, submódulos, diffs binarios, secretos conocidos y diffs mayores de 60.000 caracteres o 12 archivos. Incluye archivos nuevos, staged y borrados. El engineer no puede aportar su propio commit. Se verifica HEAD y hash del diff otra vez después de la revisión; el controller crea el commit con hooks deshabilitados.
- El controller **no ejecuta comandos de test del código generado en el host**. La verificación determinista ocurre en CI aislada. Falta de tests/checks configurados, checks omitidos/pendientes, otro SHA o emisor, workflow incorrecto y evidencia ambigua bloquean la decisión.
- GitHub se consulta desde el servidor con `gh api`, en el repositorio y SHA exactos. Se exige `github-actions`, nombres de checks explícitos y ruta de workflow acordada; también se verifica la ejecución Actions asociada. No hay endpoint para que un agente declare que la CI pasó.
- La aprobación humana vuelve a consultar CI y produce un manifiesto inmutable de candidato, contexto y rollback. **No ejecuta deploy, restart, merge, restore, SSH ni notificaciones**. La referencia de rollback es una declaración humana; un futuro ejecutor tendrá que validar que esa versión estaba desplegada y que el artefacto existe y se restaura.
- Un worker muerto queda `uncertain` al expirar el lease, sin reintento ni devolución de reservas. `blocked` y `uncertain` retienen la deduplicación: reconciliar efectos y gasto antes de cancelar y permitir un nuevo intento con una ocurrencia nueva. No hay auto-reintento de envío incierto.
- Publicar rama/PR ya no resuelve issues en autonomía legacy; tampoco un fix nocturno local. Los jobs promovidos siguen inhibiendo un nuevo autofix del mismo issue. Cuando este circuito gestiona un repo, sus fixes/promociones legacy y la aplicación nocturna quedan bloqueados. El puente independiente Devin requiere exclusión operativa explícita; no se modifica ni se contacta automáticamente.

## Habilitación posterior, con autorización específica

Por defecto `autonomy.incident_team.enabled=false`, `execution_enabled=false`, presupuesto monetario cero y confirmaciones falsas. Ninguna política previa de auto-fix habilita este circuito.

Antes de habilitar ejecución:

1. Autorizar un worker **realmente aislado**, bajo identidad sin acceso a producción, datos de empresas, servicios Windows, SSH, WhatsApp ni otros repositorios. El flag `isolated_worker_confirmed` es una declaración del operador, **no crea esa separación**. La sandbox del CLI limita herramientas; no sustituye un contenedor/VM/cuenta restringida ni una política de lectura. No habilitar en LocalSystem ni junto a secretos productivos.
2. Confirmar autenticación del CLI ya disponible en ese perfil y versión compatible, sin instalar CLI ni crear nuevas claves automáticamente. El modelo se selecciona por el mecanismo existente `SOFIA_CODEX_MODEL`; no se cambia la cuenta ni se realiza un probe con gasto. Esta tarea solo verificó contratos con mocks; no verificó autenticación ni soporte efectivo en un worker real.
3. Configurar una clave de operador **ya autorizada** (`SOFIA_API_KEY`) y proteger la API por HTTPS/entorno interno. El router nuevo exige Bearer incluso cuando la API legacy tiene auth opcional. Es una API central de operadores, no una API de usuarios/empresas: no ofrece RBAC multiusuario. La UI conserva la clave solo en memoria, nunca en localStorage. No compartir esa clave con el worker/modelos.
4. Configurar `incident_team_enabled` en el repo exacto, `incident_github_repo=owner/repo`, `incident_tenant_ids`, `incident_environments`, `incident_required_checks` y `incident_ci_workflow`. Solo hay mapeo exacto `service_id == app_repos.id`; un servicio desconocido no cae por defecto al repo de Sofia. Añadir mapeos distintos requiere un contrato explícito.
5. Desactivar `autofix_enabled` para ese repo y excluir ese contexto del despachador Devin/otros workers. Confirmar `legacy_dispatch_excluded_confirmed`. No arrancar dos equipos sobre el mismo incidente. Esta tarea no cambió tareas programadas, procesos ni política Devin.
6. Autorizar por separado presupuesto y mecanismo de corte **externo real** del proveedor, antes de `upstream_spend_cap_confirmed=true`. `session_cost_ceiling_cents` y `max_reserved_cost_cents_per_day` son reservas conservadoras de admisión, no medición de factura ni un límite duro del proveedor. El CLI no aporta aquí contabilidad monetaria fiable; si el corte externo no existe, mantener ejecución apagada. Los límites de llamadas, acciones, tiempo y salida sí se imponen localmente. No se reembolsa una reserva incierta automáticamente.
7. Crear un backup SQLite consistente con WAL y revisar la migración aditiva (`context` y tablas `incident_*`) antes de desplegar código. Los tests aplican la migración exclusivamente a DBs temporales.

No se hicieron esos pasos operativos en esta tarea. SERVER-SOFIA y producción siguen fuera del alcance autorizado.

**Gate obligatorio de identidad de ingesta:** `/api/ingest/event` es una excepción de autenticación heredada. Permanece compatible con SDKs públicos para captura/alertas legacy, así que servicio/empresa/entorno/release/fuente declarados por el payload no prueban quién los emitió. La nueva automatización no acepta esos eventos sin procedencia verificada: el servidor construye `VerifiedIngestIdentity`, la liga al contexto exacto y la persiste sin credenciales; ni un campo del payload ni sus tags pueden suplirla. Solo está implementado el adaptador de la identidad central existente: exige el Bearer real en **esa petición**, más repo/tenant/entorno autorizados. Tener `SOFIA_API_KEY` en el entorno no autentica una petición sin header. Eventos públicos, intentos de suplantación y pendientes anteriores sin procedencia se excluyen/bloquean sin abrir modelos ni consumir cuotas. `authenticated_ingest_confirmed` es otro gate apagado por defecto; no fabrica procedencia ni reemplaza esa validación por evento.

Antes de activar un worker externo, autorizar y verificar un adaptador de emisor con identidad/alcance de mínimo privilegio y transporte protegido. **No distribuir la clave central a SDKs, navegadores ni modelos.** Ese adaptador por emisor no se provisionó en esta tarea; continúa siendo un bloqueo de activación. La identidad central sirve para ingesta explícita de un operador ya autorizado, no para atribuir identidad de empresa a usuarios finales. La exposición de captura/alertas raw legacy es una limitación heredada; el riesgo nuevo de gastar presupuesto/atribuir trabajo a otra empresa se cierra con este gate. No se cambiaron credenciales, permisos, conexiones ni datos reales.

## CI, publicación y una decisión humana

El candidato queda en la rama y path de sandbox registrados. Un operador revisa/publica ese SHA en el repositorio exacto mediante el flujo Git autorizado; este circuito no hace push automático. No publicar snapshots, DB, evidencia de clientes ni credenciales. El siguiente paso es la CI del SHA candidato, no una señal verde de otra rama.

`.github/workflows/incident-team-ci.yml` es un workflow **manual**, con `contents: read`, checkout sin credenciales persistidas, fixtures temporales, mocks y build frontend. No se dispara en push/PR ni se ejecutó remotamente en esta tarea; activarlo/ejecutarlo requiere una decisión separada sobre cómputo y entorno. Para usarlo como gate: `incident_required_checks=["incident-team"]`, `incident_ci_workflow=".github/workflows/incident-team-ci.yml"`. Las ramas nuevas necesitan una ejecución manual en ese ref. Proteger la definición del workflow y mantener suites/oráculos independientes que el agente no pueda debilitar. No suministrar secretos, red productiva ni runners privilegiados al código candidato.

**Verificación remota del PR #3 (2026-10-21):** la rama predeterminada es `main` (`77cbb4e04e20dda1d3f3cad0ef71cf1611eefb1a`), sin carpeta `.github` ni CI de pruebas anterior. La API de Actions devuelve `total_count=0`; el PR está abierto en borrador, `statusCheckRollup=[]` y no hay runs de su rama. El workflow nuevo solo existe en el PR y no escucha `push`/`pull_request`. Además, GitHub exige que un workflow con `workflow_dispatch` esté en la rama predeterminada antes de poder dispararlo, incluso cuando se selecciona otro ref ([documentación oficial](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)). Por eso no hay una ejecución remota disponible que esperar o relanzar; no es un test remoto fallido ni un efecto de mantener el PR en borrador. No se intentó dispatch, no hubo denegación y no se cambió ningún trigger. Registrar el workflow en `main` o autorizar un bootstrap de CI para PR es una decisión posterior; no se hace merge para conseguir CI. La validación disponible sigue siendo local: **94 tests aprobados**, build TypeScript/Vite correcto, YAML/AST/diff y escaneo de secretos verificados. La CI remota queda pendiente de ese bootstrap, no acreditada como exitosa.

API, todas las rutas con Bearer de operador:

| Operación | Ruta |
| --- | --- |
| Cola y tarjetas paginadas | `GET /api/autonomy/incidents?limit=50&before_id=N` |
| Cobertura / último ciclo / gaps | `GET /api/autonomy/incidents/status` |
| Snapshot, evidencia y digests | `GET /api/autonomy/incidents/{id}` |
| Incluir ocurrencia explícita | `POST /api/autonomy/incidents/occurrences/{occurrence_id}` |
| Refrescar CI desde GitHub | `POST /api/autonomy/incidents/{id}/ci` |
| Aprobar manifiesto, sin ejecutarlo | `POST /api/autonomy/incidents/{id}/approve-deployment` |
| Cancelar un estado sin worker en vuelo | `POST /api/autonomy/incidents/{id}/cancel` |

La aprobación envía `candidate_sha` exacto, `rollback_sha` de 40 caracteres y `rollback_artifact=sha256:<64 hex>`. La UI ofrece esa decisión cuando CI pasó. No crea claves ni establece que rollback fue ensayado. Un deployment_requested sigue siendo un incidente abierto.

## Errores que no aparecen en logs

`sdk/sofia_contracts.py` ofrece evaluaciones puras que devuelven `ProbeResult`; un resultado fallido genera el payload del endpoint de ingesta existente, con `source=business_invariant|synthetic` y contexto completo. Un resultado sano no crea un issue. No hace HTTP, SQL, navegación ni transacciones de negocio. El adaptador de cada app ejecutará el recorrido **en fixtures o sandbox**, comparará con un oráculo propio y enviará el payload usando el transporte autorizado de ingesta. No poner pedidos, clientes, carritos ni secretos en el payload; la evidencia detallada queda en CI aislada.

Cobertura mínima de ejemplo probada:

| Contrato | Detecta | Límite |
| --- | --- | --- |
| Venta | Total esperado distinto del observado | El oráculo de impuestos/descuentos pertenece a la app |
| Devolución | Cantidad devuelta negativa o mayor a la vendida | Ejemplo con cantidades enteras; no ejecuta reembolso |
| Compra FX | Conversión decimal y redondeo esperado distintos | Fórmula de ejemplo; comisiones/redondeo por línea son contratos propios |
| Recuperar carrito | Fixture recuperado distinto del esperado | No abre sesiones ni usa carritos/clientes reales |

Un test integra contrato fallido → ingesta FastAPI aislada → occurrence → incidente pendiente. Son conectores reutilizables y ejemplos, **no recorridos E2E implantados en Mayor, Packing o ElenaTec**, ni certificación de sus reglas de negocio. Fuente/tenant declarados por un SDK son datos no confiables; no confieren permisos ni demuestran causa. Un detector malicioso sigue sujeto a cuotas, revisión y CI, nunca puede autoaprobar producción.

El test integrado anterior usa una identidad central sintética verificada por header; un SDK público sin identidad verificada solo conserva la captura legacy y no entra al worker.

## Rollback y parada

- Parada lógica: activar el kill switch existente y apagar `incident_team.execution_enabled`. Se comprueba antes de cada llamada; una llamada ya iniciada queda limitada por su deadline. No se mata ningún proceso ajeno. Si hace falta intervención inmediata, identificar únicamente el proceso/árbol propio del worker.
- Cancelar un plan invalida la decisión; un futuro ejecutor debe consultar estado y aprobación vigentes antes de actuar. No consumir manifiestos exportados sin volver a validar.
- Si el worker quedó incierto, inspeccionar sandbox, evidencia redactada, gasto externo y estado de cualquier publicación manual. No reintentar por suposición. La acción de cancelar no deshace un despliegue: este código no ejecuta ninguno.
- Rollback de esta mejora: mantener el circuito deshabilitado, restaurar el release anterior por un despliegue **autorizado** y conservar tablas/columna aditivas para auditoría. No borrar DB ni purgar issues. El despliegue posterior requiere backup consistente, ventana y plan de restauración propios.
- Futuro ejecutor: autorización de entorno protegido, identidad de mínimo privilegio, validación de release efectivo + artefacto previo restaurable, canary limitado, probe de negocio con contrato semántico, observación posterior y reversión al artefacto exacto ante regresión. Ninguno de esos permisos está concedido por este PR.

## Métricas y evaluación verificable

No hay benchmark contra Codex/Datadog ni mediciones de producción. Las pruebas de guardrails no acreditan MTTR, precisión de causa ni éxito de despliegue. Para evaluar una activación autorizada, congelar fixtures/casos, versión efectiva y reloj de origen; comparar baseline y circuito con el mismo corpus, por empresa/entorno/release:

| Métrica | Definición / evidencia | Estado de esta entrega |
| --- | --- | --- |
| Detección | P50/P95 desde fallo inyectado hasta `created_at`; recall = casos detectados / inyectados | Poll configurado a 15 s; latencia/recall reales no medidos |
| Precisión | Incidentes accionables confirmados por adjudicación independiente / detectados | Sin corpus adjudicado; no estimada por acuerdo de agentes |
| Preparación | Detección → evidencia `awaiting_ci`; indicar fallidos e inciertos | Timestamps persistentes; tests mockeados |
| MTTR | Inicio de impacto → negocio sano sostenido en release desplegado | No medido; aprobar/publicar no cuenta como recuperación |
| Regresiones | Releases con contrato degradado / releases observados; ventana explícita | Sin releases ejecutados |
| Reversión | Rollbacks restaurados y verificados / intentados, y P95 de tiempo | Manifiesto implementado; ejecutor/ensayo pendientes |
| Carga humana | Decisiones e intervenciones por incidente; minutos activos de operador | Una aprobación del plan, publicación/CI todavía manuales; no medida en uso |
| Costo | Sesiones ejecutadas, reservas y factura reconciliada por incidente | `incident_reservations` y evidencia de sesiones; costo real desconocido |

Los contadores salen de `incident_runs`, `incident_receipts`, `incident_evidence` y `incident_reservations`. `scope_gaps` evita ocultar fuentes sin contexto. Los hashes permiten comprobar consistencia de snapshots/evidencia; no son firmas ni protegen contra un administrador de la DB. Retención de auditoría y límites de disco requieren una política operativa posterior; no purgar evidencia para simular mejora.

## Verificación local

Desde un checkout aislado, con dependencias de `backend/requirements-dev.txt`:

```powershell
cd backend
../.venv/Scripts/python.exe -m pytest tests/test_incident_team.py tests/test_business_contracts.py tests/test_codex_cli_service.py tests/test_db_service.py tests/test_models.py tests/test_ingest.py tests/test_restore_health.py tests/test_self_health.py -q
cd ../frontend
npm ci --ignore-scripts
npm run build
```

No importar `app.main`, usar lifespan, arrancar Sofia, leer config productiva ni llamar modelos/proveedores durante la verificación. Las fixtures configuran DB/config temporales y simulan modelos/GitHub/HTTP; solo se crean procesos Git/repos temporales y procesos Python de prueba propios. En entornos Windows que bloquean `socketpair`, asyncio necesita permiso para su comunicación local interna.

Flags contrastados con documentación oficial: [CLI](https://developers.openai.com/codex/cli/reference), [configuración de sandbox](https://developers.openai.com/codex/config-reference). El binario Codex no está instalado en este workspace; no se instaló ni se contactó un modelo.

Revisión independiente del PR: se reprodujeron dos fallos de credenciales con tests rojos (JSON sin redactar y dict genérico admitido al candidato), y se agregaron regresiones de 62 incidentes, claims concurrentes, revocación del primero de la cola, claves anidadas/alias/escapadas, conservación de evidencia segura e identidad de ingesta. Resultado posterior: **138 tests aprobados**, build TypeScript/Vite correcto. La CI remota sigue sin bootstrap; los 94 tests citados en la captura remota inicial son la validación previa, no el conteo final.
