---
name: sofia-incident-audit
description: Auditar Sofia Monitor bajo NSSM y todos sus issues retenidos sin mutar produccion, con evidencia, conteos reconciliados y causas raiz graduadas.
---

# Auditoria de incidentes Sofia

## Cuando usar

Reportes de errores historicos o recientes de apps monitoreadas por Sofia, cobertura de observabilidad, NSSM, Docker remoto o revision nocturna. Leer AGENTS.md de Sofia y del servidor.

## Procedimiento

1. Definir alcance: inventario conservado completo o ventana exacta. Un reporte no autoriza fixes de runtime. Inventariar cambios previos y no tocarlos.
2. Verificar Win32_Service y parametros NSSM por allowlist: nombre/estado/cuenta/AppDirectory/Application/AppStdout/AppStderr/rotacion. No volcar registro completo ni variables secretas.
3. Leer esquema en db_service.py y config como JSON bruto por allowlist, sin importar app/main/config_service. Inventariar probes habilitados, expected_status, logs y existencia; contrastar con servicios NSSM. Identificar huecos y servicios dinamicos de Docker.
4. Autor de la consulta define SQL exacto y filtros; no sustituir tablas/campos por intuicion. Abrir SQLite con mode=ro y query_only, BEGIN para corte consistente. Si el esquema difiere, detenerse y documentar; no improvisar.
5. Capturar issues incluyendo resolved y todos los niveles/fuentes/entornos; occurrences agregadas por issue, rangos de retencion y metrics por ventana. No leer phone/message de alert_queue si basta estado/last_error. Queries de negocio/SAP/produccion requieren autoria y revision explicitas.
6. Redactar secretos, queries URL, tokens en paths, correos y telefonos ANTES de guardar o mostrar. CSV debe impedir formula injection. No copiar AGENTS completo si contiene secretos.
7. Congelar captura; todas las vistas/reporte salen de esa captura. No reconsultar para cambiar formato. Reconciliar IDs unicos, totales por servicio/categoria/nivel, SUM(count) y occurrences.
8. Diferenciar issue, ingesta acumulada, ocurrencia en ventana, incidente correlacionado e impacto. first_seen/last_seen Docker son ingesta; usar timestamp de origen. No mezclar UTC/NSSM local ni contar invocacion de OOM y proceso eliminado como dos OOM sin correlacionar.
9. Agrupar por semantica conservando distinciones; normalizacion regex es TRIAJE, no prueba de una unica causa. Separar produccion/dev/testserver/SQLite/contenedores temporales. No atribuir fallos de test a tec-db sin evidencia.
10. Para cada familia prioritaria, trazar codigo/release efectivo y eventos originales. Indicar mecanismo demostrado, causa pendiente o hipotesis, nunca presentar ranking como conclusion. HEAD local/host no prueba revision de imagen; registrar discrepancias.
11. Registrar plan concreto por familia: archivos/seam, accion, invariantes, test de regresion, permiso requerido y criterio de cierre. Mantener todos los IDs residuales en anexo; no declararlos resueltos ni omitirlos.
12. Verificar reporte y discovery documental sin iniciar servicios, suites contra DB real ni mensajes. Mantener gaps explicitos y entregar archivos accesibles al usuario.

## Trampas confirmadas

- UI/get_issues filtra por resolved/ventana/limite; no equivale a inventario completo.
- Retencion de occurrences no elimina todos los issues no resueltos antiguos.
- SDK y Docker/DB pueden registrar el mismo fallo; no sumar capas como incidentes unicos.
- Path pasivo inexistente retorna sin evento; contrastar existencia y fuentes NSSM.
- DOWN es resultado de probes/gracia, no causa de una caida; UP no prueba negocio correcto.
- expected_status=404 de ingest E2E es contrato legitimo.
- Revisión nocturna Monitor/Codex es distinta a Sofia AI Mayor/Devin.

## Fuentes de evidencia

`backend/app/services/db_service.py`, `health_service.py`, `log_service.py`, `droplet_docker_service.py`, `local_host_service.py`, `nightly_review_service.py`, `codex_cli_service.py`, `backend/app/routers/ingest.py`; metadata NSSM y registro de la app. Confirmar estas rutas antes de usarlas.
