# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** este archivo es la **fuente de verdad única** de qué falta por hacer. Al iniciar sesión revísalo y recuérdaselos al usuario. Cuando termine una subtarea, marca el checkbox (`[ ]` → `[x]`). Cuando surja una nueva, agrégala. Al finalizar sesión: commit + push + PR + merge.

**Última actualización:** 2026-04-24
**Progreso:** 1/18 subtareas (6%)

```
Task 1 (Odoo):        [█░░░░░░] 1/7
Task 2 (SP-API):      [░░░] 0/3
Task 3 (Picking):     [░░░░] 0/4
Task 4 (Cleanup):     [░] 0/1
Task 5 (Huérfanos):   [░░░] 0/3
```

---

## 🎯 En curso

### 1. Setup almacenes Odoo FULL/FBA

- [x] Crear los 4 warehouses (EHV-MX, Meli-Full, FBA-MX, FBA-US)
- [ ] Confirmar **Resupply From = EHV-MX** en Meli-Full
- [ ] Confirmar **Resupply From = EHV-MX** en FBA-MX
- [ ] Desmarcar **Buy to Resupply** y **Manufacture to Resupply** en los 3 almacenes nuevos
- [ ] Confirmar **1 step** (incoming y outgoing) en los 3 nuevos
- [ ] **Decidir estrategia phantom BOM** (recomendación: migrar a "Manufacture this product" los SKUs que van a FULL/FBA) ⚠️ *Bloquea las siguientes tareas*
- [ ] Primera transferencia de prueba EHV/Stock → FBAMX/Stock con SKU piloto

---

## 📋 Backlog

### 2. Migrar Amazon SP-API Orders v0 → v2026-01-01

**Deadline:** 2027-03-27 (margen recomendado: antes de enero 2027)

- [ ] `tools/amazon_orders_poll.py` — reemplazar `getOrders` y `getOrderItems`
- [ ] `app/amazon_inbound_worker.py` — reemplazar `getOrder` y `getOrderItems`
- [ ] `app/debug_flex_order.py` — reemplazar `getOrder`

### 3. Modificar tools inbound FBA/FULL para picking por canal

*Depende de que la tarea 1 esté terminada.*

- [ ] Agregar mapping `canal → almacén` en `bridge_settings`
- [ ] Modificar `tools/inbound_full_paid_one_shot_no_stock.py` para generar picking desde `Full/Stock`
- [ ] Modificar `tools/amazon_fba_paid_one_shot.py` para generar picking desde `FBAMX/Stock` o `FBAUS/Stock`
- [ ] Probar flujo completo con orden real en cada canal

### 4. Limpieza periódica de `manual_review` antiguos

- [ ] Automatizar script existente (cron o background task)

### 5. Asignar SKU a listings MeLi huérfanos

- [ ] `MLM2787930515` — asignar `seller_custom_field` real en MeLi vendedor
- [ ] `MLM2787902225` — asignar `seller_custom_field` real en MeLi vendedor
- [ ] Correr `backfill_meli_mappings.py` (o esperar al cron automático de 4h)

### 6. AP-5 follow-up — eliminar path-secret en webhooks

C4 (commit `_check_webhook_secret`) ya hace `hmac.compare_digest` y deja de validar contra path/query como dependencia, pero **sigue aceptando** `/webhooks/{meli,amazon}/orders/{secret}` por compatibilidad con suscripciones vivas. uvicorn loggea la URL completa en access log → el secret queda visible en `docker logs bridge-api`.

Plan de migración (cuando se decida ventana):
- [ ] Generar nuevos `meli_webhook_secret` y `amazon_webhook_secret` en `bridge_settings`.
- [ ] Actualizar URL en panel MeLi (notifications) a `/webhooks/meli/orders` con header `X-Goncloud-Secret`.
- [ ] Re-suscribir SNS Amazon Notifications con header `X-Goncloud-Secret` (Amazon SNS soporta extra headers en HTTP subscriptions).
- [ ] Borrar las rutas con `{secret}` en `app/main.py` y dejar solo header.
- [ ] Rotar logs nginx/cloudflare/uvicorn que tengan path-secret histórico.

---

## ✅ Cerrados recientemente

**2026-04-18** — Bug crítico: MeLi separa variantes → oversell potencial
- PR #19: schema `sku_mapping` 1:N + worker outbound con `fetchall()` + loop
- PR #20: fix backfill para leer `attributes[SELLER_SKU]` (campo actual) en vez de `seller_custom_field` (deprecated)
- PR #21: cron `0 */4 * * *` agregado — discovery automático de splits cada 4h

**2026-04-18** — Documentación y versionado
- PR #17: `meli_refresh_tokens.sh` versionado en repo + MeLi auto-refresh documentado (ya estaba activo en prod)
- PR #18: snippet de deploy resiste `/tmp/` ausente
- PR #16: lista de pendientes activos como fuente de verdad
