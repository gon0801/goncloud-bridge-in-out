ANEXO C — MAPEO CANÓNICO DE ESTADOS ML (FULL) + PRUEBAS CONTROLADAS
Proyecto: GONCLOUD — INBOUND MercadoLibre → Odoo 17 (VENTAS)
Fecha: 2026-02-03
Estado: SELLADO · OPERATIVO · ANTI-ENGAÑO CONTABLE
Autoridad: Fuente única de verdad operativa (FULL)

=====================================================================
1) OBJETIVO ABSOLUTO
=====================================================================

Definir reglas SELLADAS para interpretar estados/eventos de MercadoLibre
en órdenes FULL y decidir CUÁNDO ejecutar el flujo contable canónico
(ANEXO A) sin romper contabilidad.

=====================================================================
2) PRINCIPIO CANÓNICO (ANTI-ENGAÑO)
=====================================================================

SOLO se neutraliza contabilidad cuando exista señal explícita de REFUND
(dinero devuelto).
Un “return” por sí solo NO garantiza refund.

=====================================================================
3) CLASIFICACIÓN DE ESTADOS FULL
=====================================================================

FULL se detecta por:
logistic_type == "fulfillment"

---------------------------------------------------------------------
CLASE A — REFUND CONFIRMADO → EJECUTAR
---------------------------------------------------------------------
order.status == "refunded"

Acción:
- Ejecutar ANEXO A (refund + credit note + pago + cancel SO)

---------------------------------------------------------------------
CLASE B — CANCELACIÓN CON EFECTO CONTABLE → EJECUTAR
---------------------------------------------------------------------
order.status == "cancelled" | "canceled"

Acción:
- Ejecutar ANEXO A
(En este sistema, cancel implica neutralización contable)

---------------------------------------------------------------------
CLASE C — RETURN SIN REFUND → NO EJECUTAR
---------------------------------------------------------------------
Ejemplos:
- returned
- returning
- to_be_returned

Acción:
- NO tocar contabilidad
- Esperar evento refunded

---------------------------------------------------------------------
CLASE D — UNKNOWN → NO EJECUTAR
---------------------------------------------------------------------
Cualquier estado FULL no clasificado arriba.

=====================================================================
4) IMPLEMENTACIÓN ACTUAL (CONFIRMADA)
=====================================================================

El worker ejecuta ANEXO A si:
- FULL == True
- status ∈ {cancelled, canceled, refunded}

returned NO dispara refund.

=====================================================================
5) PRUEBAS CONTROLADAS (SIN VENTAS REALES)
=====================================================================

Se usan order_json + shipment_json (test hook).

PRECONDICIONES:
- meli_inbound_enabled = 1
- meli_inbound_full_refunds_enabled = 1
- Odoo accesible desde worker
- Script existe:
  /mnt/data/appdata/bridge/tools/inbound_full_so_refund_and_cancel.py

---------------------------------------------------------------------
P0 — FULL + cancelled → DEBE ejecutar
---------------------------------------------------------------------
status: cancelled
Resultado: FULL_REFUND_OK + audit JSON

---------------------------------------------------------------------
P1 — FULL + refunded → DEBE ejecutar
---------------------------------------------------------------------
status: refunded
Resultado: idempotente

---------------------------------------------------------------------
P2 — FULL + returned → NO debe ejecutar
---------------------------------------------------------------------
status: returned
Resultado: planned_full / manual_review

---------------------------------------------------------------------
P3 — FBM + cancelled → NO debe ejecutar FULL
---------------------------------------------------------------------
logistic_type != fulfillment

---------------------------------------------------------------------
P4 — Idempotencia
---------------------------------------------------------------------
Mismo dedupe_key → SKIP

=====================================================================
6) ESTADO FINAL GARANTIZADO (CUANDO APLICA)
=====================================================================

- Sale Order: cancel
- Invoice original: posted / paid
- Credit Note: posted / paid
- Pickings: 0
- Stock: 0
- Auditoría JSON persistida

=====================================================================
7) ESTADO
=====================================================================

ANEXO C SELLADO.
Cualquier cambio requiere nueva versión.
