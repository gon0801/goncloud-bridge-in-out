# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** este archivo es la **fuente de verdad única**. Al iniciar sesión: leerlo y recordar al usuario. Al terminar subtarea: actualizar tabla + checkbox + contadores + commit + push + PR + merge.

**Última actualización:** 2026-05-06  
**Progreso:** 9/16 subtareas (56%)

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
| **3** | **Tools picking por canal** *(depende Task 1)* | Mapping `canal → almacén` en `bridge_settings` | ✅ Pre-codeado | Alta |
| 3.1 | | `inbound_full_paid_one_shot_no_stock.py` → Full/Stock | ✅ Pre-codeado | Alta |
| 3.2 | | `amazon_fba_paid_one_shot.py` → FBAMX/FBAUS/Stock | ✅ Pre-codeado | Alta |
| 3.3 | | Lógica de picking (⚠️ espera Task 1.5 phantom BOM) | ⏳ Bloqueada | **Crítica** |
| 3.4 | | Probar flujo completo con orden real en cada canal | ⏳ Pendiente | Alta |
| **4** | **Limpieza `manual_review` antiguos** | Automatizar script existente (cron o background) | ✅ Hecho | Baja |
| **5** | **MeLi huérfanos** | `MLM2787930515` — verificar si listing sigue activo en MeLi | ⏳ Pendiente | Baja |
| 5.1 | | `MLM2787902225` — verificar si listing sigue activo en MeLi | ⏳ Pendiente | Baja |
| 5.2 | | Correr `backfill_meli_mappings.py` (o esperar cron 4h) | ⏳ Pendiente | Baja |
| **6** | ~~AP-5 path-secret webhooks~~ | ~~Rotar secrets + mover a header `X-Goncloud-Secret`~~ | ❌ Cancelada | — |

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
- **Deployado y validado en VPS 2026-05-05** — poll corrió limpio a las 06:40 UTC, worker reiniciado sin errores

### 3. Modificar tools inbound FBA/FULL para picking por canal

*Depende de que la tarea 1 esté terminada. Código pre-codeado 2026-05-06 — listo para activar.*

- [x] Settings `warehouse_meli_full`, `warehouse_amazon_fba_mx`, `warehouse_amazon_fba_us` agregados en `bridge_settings` (vacíos hasta configurar Odoo)
- [x] Workers pasan `WAREHOUSE_NAME` como env var al tool según perfil (`FBA_US` → `warehouse_amazon_fba_us`, etc.)
- [x] Tools leen `WAREHOUSE_NAME`, buscan `warehouse_id` en Odoo por nombre, lo pasan al `sale.order` create. Si vacío → comportamiento actual sin cambios.
- [ ] **Activar:** correr los 3 UPDATEs en bridge_settings con nombres exactos de Odoo (sin restart ni redeploy)
- [ ] **Lógica de picking** ⚠️ *Bloqueada por Task 1.5 (phantom BOM)* — actualmente FBA cancela pickings, FULL no los genera
- [ ] Probar flujo completo con orden real en cada canal

**Para activar cuando Task 1 esté lista:**
```sql
UPDATE bridge_settings SET value='Meli-Full' WHERE key='warehouse_meli_full';
UPDATE bridge_settings SET value='FBA-MX'    WHERE key='warehouse_amazon_fba_mx';
UPDATE bridge_settings SET value='FBA-US'    WHERE key='warehouse_amazon_fba_us';
```

### 4. Limpieza periódica de `manual_review` antiguos ✅

- [x] `tools/cleanup_old_records.py` — limpieza con dry-run, retenciones configurables (success=90d, stuck=30d)
- [x] `tools/cron/goncloud_bridge_cleanup` — cron domingos 03:00 UTC, `docker exec bridge-amazon-inbound-worker`
- **Deploy:** `sudo cp tools/cron/goncloud_bridge_cleanup /etc/cron.d/ && sudo cp tools/cleanup_old_records.py /mnt/data/appdata/bridge/data/`

### 5. MeLi huérfanos

Revisado 2026-05-06: ambos listings tienen SKU `NH-ITA-CEN-DOR` en `sku_mapping` desde 2026-04-18, pero el cron de backfill (cada 4h) no los ha visto desde esa fecha → probablemente pausados o eliminados en MeLi.

- [ ] Verificar en panel MeLi si `MLM2787930515` sigue activo
- [ ] Verificar en panel MeLi si `MLM2787902225` sigue activo
- [ ] Si están activos y sin SKU real → asignar `SELLER_SKU` en atributos del listing en MeLi vendedor
- [ ] Correr `backfill_meli_mappings.py` (o esperar al cron automático de 4h)

### 6. ~~AP-5 follow-up — eliminar path-secret en webhooks~~ ❌ Cancelada

El flujo completo requiere rotar el secret (cambiar en bridge_settings + actualizar panel MeLi + re-suscribir SNS). Sin rotar el secret, mover la validación al header no aporta seguridad real. Cancelada 2026-05-05.

---

## ✅ Cerrados recientemente

**2026-05-06** — Task 3 pre-codeada: canal→almacén
- Workers pasan `WAREHOUSE_NAME` al tool según perfil; tools setean `warehouse_id` en SO create
- Settings vacíos en `bridge_settings` listos para activar con 3 UPDATEs SQL
- Lógica de picking pendiente de decisión Task 1.5 (phantom BOM)
- Redis dead queue limpiado (1,439 jobs históricos eliminados)

**2026-05-05** — Task 4: Limpieza `manual_review` antiguos
- `cleanup_old_records.py` (success=90d, stuck=30d, dry-run incluido)
- Cron semanal domingos 03:00 UTC vía `docker exec bridge-amazon-inbound-worker`
- Deploy pendiente: copiar script a `/data/` y cron a `/etc/cron.d/`

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
