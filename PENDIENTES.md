# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** este archivo es la **fuente de verdad única**. Al iniciar sesión: leerlo y recordar al usuario. Al terminar subtarea: actualizar tabla + checkbox + contadores + commit + push + PR + merge.

**Última actualización:** 2026-05-04  
**Progreso:** 7/21 subtareas (33%)

---

## Tabla de estado

| # | Tarea | Subtarea | Estado | Prioridad |
|---|-------|----------|--------|-----------|
| **0** | **VPS Hetzner — validar crons** | Backfill cron + MeLi refresh cron | ✅ Hecho | — |
| **1** | **Setup almacenes Odoo FULL/FBA** | Crear los 4 warehouses | ✅ Hecho | Alta |
| 1.1 | | Resupply From = EHV-MX en Meli-Full | ⏳ Pendiente | Alta |
| 1.2 | | Resupply From = EHV-MX en FBA-MX | ⏳ Pendiente | Alta |
| 1.3 | | Desmarcar Buy/Manufacture to Resupply en los 3 nuevos | ⏳ Pendiente | Media |
| 1.4 | | Confirmar 1 step (incoming y outgoing) en los 3 nuevos | ⏳ Pendiente | Media |
| 1.5 | | **Decidir estrategia phantom BOM** ⚠️ bloquea Task 3 | ⏳ Pendiente | **Crítica** |
| 1.6 | | Primera transferencia prueba EHV/Stock → FBAMX/Stock | ⏳ Pendiente | Alta |
| **2** | **SP-API v0 → v2026-01-01** | `tools/amazon_orders_poll.py` | ✅ Hecho | Media |
| 2.1 | | `app/amazon_inbound_worker.py` | ✅ Hecho | Media |
| 2.2 | | `app/debug_flex_order.py` | ✅ Hecho | Baja |
| 2.3 | | **Deadline: 2027-03-27** (recomendado antes de enero 2027) | ✅ Completado con margen | — |
| **3** | **Tools picking por canal** *(depende Task 1)* | Mapping `canal → almacén` en `bridge_settings` | ⏳ Pendiente | Alta |
| 3.1 | | `inbound_full_paid_one_shot_no_stock.py` → Full/Stock | ⏳ Pendiente | Alta |
| 3.2 | | `amazon_fba_paid_one_shot.py` → FBAMX/FBAUS/Stock | ⏳ Pendiente | Alta |
| 3.3 | | Probar flujo completo con orden real en cada canal | ⏳ Pendiente | Alta |
| **4** | **Limpieza `manual_review` antiguos** | Automatizar script existente (cron o background) | ⏳ Pendiente | Baja |
| **5** | **MeLi huérfanos** | `MLM2787930515` — asignar SKU en MeLi vendedor | ⏳ Pendiente | Media |
| 5.1 | | `MLM2787902225` — asignar SKU en MeLi vendedor | ⏳ Pendiente | Media |
| 5.2 | | Correr `backfill_meli_mappings.py` (o esperar cron 4h) | ⏳ Pendiente | Baja |
| **6** | **AP-5 path-secret webhooks** | Rotar secrets + mover a header `X-Goncloud-Secret` | ⏳ Pendiente | Media |

---

## Detalle por tarea

### 0. Validar crons VPS Hetzner ✅

- [x] Backfill cron `0 */4 * * *` — faltaba; re-aplicado 2026-05-04
- [x] MeLi refresh `/etc/cron.d/goncloud_meli_refresh` — activo (4 OK el 2026-05-04)
- [x] Re-aplicar si falta — hecho

### 1. Setup almacenes Odoo FULL/FBA

- [x] Crear los 4 warehouses (EHV-MX, Meli-Full, FBA-MX, FBA-US)
- [ ] Confirmar **Resupply From = EHV-MX** en Meli-Full
- [ ] Confirmar **Resupply From = EHV-MX** en FBA-MX
- [ ] Desmarcar **Buy to Resupply** y **Manufacture to Resupply** en los 3 almacenes nuevos
- [ ] Confirmar **1 step** (incoming y outgoing) en los 3 nuevos
- [ ] **Decidir estrategia phantom BOM** ⚠️ *Bloquea Task 3*
- [ ] Primera transferencia de prueba EHV/Stock → FBAMX/Stock con SKU piloto

### 2. Migrar Amazon SP-API Orders v0 → v2026-01-01 ✅

**Deadline:** 2027-03-27 — **Completado 2026-05-04** (11 meses antes del deadline)

- [x] `tools/amazon_orders_poll.py` — URL v2026, params camelCase, paginationToken, normalize_to_v0()
- [x] `app/amazon_inbound_worker.py` — enrich con includedData, normalize_to_v0(), elimina getOrderItems
- [x] `app/debug_flex_order.py` — URL v2026, fix credential keys, display campos v2026
- **Deploy pendiente en VPS** (`git pull` + `cp tools/amazon_orders_poll.py /mnt/data/appdata/bridge/data/` + restart workers)

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

uvicorn loggea la URL completa → el secret queda visible en `docker logs bridge-api`.

- [ ] Generar nuevos `meli_webhook_secret` y `amazon_webhook_secret` en `bridge_settings`
- [ ] Actualizar URL en panel MeLi a `/webhooks/meli/orders` con header `X-Goncloud-Secret`
- [ ] Re-suscribir SNS Amazon con header `X-Goncloud-Secret`
- [ ] Borrar rutas con `{secret}` en `app/main.py`
- [ ] Rotar logs nginx/cloudflare/uvicorn con path-secret histórico

---

## ✅ Cerrados recientemente

**2026-05-04** — Task 0: Validar crons VPS Hetzner
- Backfill cron faltaba en el VPS nuevo → re-aplicado manualmente
- MeLi refresh cron `/etc/cron.d/goncloud_meli_refresh` activo y funcionando (4 OK el día de hoy)

**2026-04-18** — Bug crítico: MeLi separa variantes → oversell potencial
- PR #19: schema `sku_mapping` 1:N + worker outbound con `fetchall()` + loop
- PR #20: fix backfill para leer `attributes[SELLER_SKU]` (campo actual)
- PR #21: cron `0 */4 * * *` agregado — discovery automático de splits cada 4h

**2026-04-18** — Documentación y versionado
- PR #17: `meli_refresh_tokens.sh` versionado en repo + MeLi auto-refresh documentado
- PR #18: snippet de deploy resiste `/tmp/` ausente
- PR #16: lista de pendientes activos como fuente de verdad
