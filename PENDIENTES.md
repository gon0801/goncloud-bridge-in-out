# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** este archivo es la **fuente de verdad única** de qué falta por hacer. Al iniciar sesión revísalo y recuérdaselos al usuario. Cuando termine una subtarea, marca el checkbox (`[ ]` → `[x]`). Cuando surja una nueva, agrégala. Al finalizar sesión: commit + push + PR + merge.

**Última actualización:** 2026-05-03
**Progreso:** 1/21 subtareas (5%)

```
Task 0 (VPS Hetzner): [░░░] 0/3   ← nuevo: migración server
Task 1 (Odoo):        [█░░░░░░] 1/7
Task 2 (SP-API):      [░░░] 0/3
Task 3 (Picking):     [░░░░] 0/4
Task 4 (Cleanup):     [░] 0/1
Task 5 (Huérfanos):   [░░░] 0/3
```

---

## 🎯 En curso

### 0. Verificar migración del bridge al VPS Hetzner

> Servidor de producción migrado el 2026-05-03 del LAN viejo (192.168.0.200) al VPS Hetzner (`gonserver` → `100.127.167.103`, user `root`). Solo `competitive-intel` está confirmado en el VPS — falta validar que los containers del bridge ya estén ahí.

- [ ] `ssh gonserver "docker ps --format '{{.Names}}' | grep -E 'bridge-(api|redis|worker|inbound-worker|amazon-inbound-worker)'"` — confirmar que los 5 containers del bridge corren en el VPS
- [ ] Verificar que el cron `0 */4 * * * docker exec bridge-api python3 /data/backfill_meli_mappings.py` esté en el crontab del VPS (no del server viejo)
- [ ] Verificar que `meli_refresh_tokens.sh` (cron `5 */6 * * *`) esté operativo en el VPS — `tail /mnt/data/appdata/bridge/data/meli_token_refresh.log`

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
