---
name: sofia-root-cause-fix
description: Convertir incidentes comprobados de Sofia en fixes minimos con pruebas aisladas, idempotencia y preservacion de datos, sesiones y reservas.
---

# Reparacion desde la causa raiz

## Alcance

Usar SOLO cuando el usuario autorice implementar. Un reporte/diagnostico no basta. Leer AGENTS y skills del repo objetivo; no importar las reglas de Mayor a Packing o ElenaTec por similitud de nombre.

## Gate antes de editar

- Seleccionar IDs/familia desde captura congelada, evidencia, entorno, release efectivo y efecto de negocio. Si falta causa demostrada, continuar diagnostico read-only y marcar gap.
- Trazar consumidores, contrato, permisos/tenant/empresa/bodega, transaccion y dependencias. Reutilizar seam existente y evitar refactor cosmetico.
- Reproducir en entorno aislado con test que falle, no POSTs en produccion, semillas productivas, lifespan de app real o callbacks externos.
- Diseno exacto: interfaz, propiedad de transaccion, idempotencia, concurrencia, invariantes, test antes/despues.
- Preservar cambios ajenos. No deploy/restart/CLI install/migracion/env/secrets ni cambios de datos sin aprobacion especifica de esa operacion.

## Invariantes por dominio

### Fiscal / SQLAlchemy

Unicidad tenant+sale/CUFE se conserva. Tras flush/commit fallido, recuperar Session antes de acceder ORM/loguear atributos expirados. Capturar IDs primitivos antes del fallo. Savepoint/transaccion debe seguir contrato real de venta/outbox; no anadir rollback indiscriminado que pierda la venta. Emision sync/dispatcher/retry deben converger al mismo documento. Test PostgreSQL de concurrencia, conflicto y procesamiento del siguiente documento; SQLite solo no prueba locking Postgres.

### WMS Mayor/Packing

Conservar reserved<=on_hand y reservas ajenas. No poner reserved=0 ni borrar ledger/constraints. Verificar identidad DocEntry/DocNum/id local por empresa/warehouse/bin/SKU y ledger RESERVE/RELEASE/PICK_CONFIRM/ADJUST antes de atribuir mismatch. Una limpieza requiere dry-run y aprobacion de filas/operacion concreta; pedido facturado no se re-reserva automaticamente. Testear doble retry, pedidos competidores, parcial, stock cero, alias y rollback.

### WhatsApp compartido

Preservar pairing, sesion default, shadow/live y locks/cooldowns existentes. HTTP 200 no acredita estado semantico. Timeout/500 despues de envio puede ser entrega incierta; no reenviar hasta probar no-entrega o deduplicar por ID confirmado. Recovery debe tener un unico propietario entre workers/watchdogs. Sin send-message/logout/close/start/restart ni pruebas reales sin autorizacion.

### SAP / infraestructura

Mayor 9000 y Packing 8001 deben comprobarse por contrato vigente, no asumir middleware compartido. COM no thread-safe: conservar slots/acquire/release. SAP productivo no es fixture. Deadlock se trata con transaccion corta/orden/indice sustentados en evidencia y retry de unidad idempotente, no NOLOCK. Pruebas/builds no compiten con produccion por RAM/disco.

### Monitor

Paths ausentes, cursor/rotacion, parsing multiline y agrupacion deben generar evidencia de cobertura. No sustituir un warning operacional o dato esperado por error critico ni ocultar errores reales mediante normalizacion general. Separar test/dev/prod/release, conservar origen temporal. Probe de tunel no equivale a TCP global cualquiera; recovery local/E2E debe coordinarse.

## Verificacion y cierre

Ejecutar pruebas minimas del seam modificado con DB/config temporales y HTTP/SSH/mensajeria/subprocess mockeados. Revisar el diff completo, edge cases y riesgos. Documentar comandos y resultados reales; no afirmar que los tests recomendados ya pasaron.

Requieren aprobacion: operacion persistente/restart/deploy, alcance, backup restaurable y recuperacion apropiada. Cerrar solo con causa demostrada, test antes/despues y release/flujo observados; marcar MANUAL_GAP cuando falte evidencia. No auto-resolver todos los issues ni purgar el historial.


## Patrones de regresion confirmados (2026-10-11)

- Un timeout despues de iterar stdout no limita una tuberia bloqueada. Probar stdin grande sin consumidor, stdout sin EOF y descendientes propios que heredan handles; preservar rc y salida parcial.
- En un tail, ni prelectura ni descarte de linea excesiva pueden adelantar cursor por encima de un registro pendiente de persistir. Mantener cursores de startup, rotacion y descarte coherentes, y probar DB fallida antes de una linea grande. Una linea parcial no es backlog drenables ni motivo para spin.
- Una factura enlazada a una entrega multi-pedido se atribuye por DocEntry + BaseLine/LineNum, no por pertenencia al documento completo. Pruebas con dos pedidos y el mismo SKU, vinculos directos y documentos cancelados.
- Un fallo leyendo SAP no produce hechos parciales validos de factura/cierre: parar descuento/liberacion y conservar tarea incompleta. RELEASED no demuestra cantidad descontada; contrastar presupuesto facturado con cantidades registradas y enviar discrepancia a reconciliacion sin tocar reservas ajenas.
- Definir codigos SQL por SQLSTATE/causa original y native-code delimitado, no por numeros en IDs o parametros. Un contextmanager no puede reejecutar el cuerpo del caller volviendo a yield; reintentar la unidad idempotente completa y abrir conexion nueva.
- El cache de preflight no se vuelve positivo por importar el paquete; validar ejecutable/browser. Cache negativo fechado al terminar la validacion y recuperable; mantener probes bloqueantes fuera del event loop.
- Un loader de tests que sustituye sys.modules debe hacerlo dentro de un contexto/fixture que restaure los objetos previos, nunca al recolectar tests ni borrando modulos ajenos con pop.
