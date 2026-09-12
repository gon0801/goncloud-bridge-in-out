# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** fuente de verdad única de pendientes. Al iniciar sesión: leerlo y recordar al usuario.
> Al cerrar una tarea: actualizar checkbox + contador + commit.
> **No** convertir este archivo en diario — el histórico vive en `git log`.

**Última verificación contra producción:** 2026-09-12
**Abiertos:** 9 · **Bloqueantes:** 0

Todo lo de abajo fue verificado contra Odoo, `bridge.db`, la SP-API de Amazon y
el host `goncloud` el 2026-09-12. Lo cerrado se eliminó del archivo (ver
`git log` si hace falta el histórico).

---

## Tabla de estado

| # | Tarea | Estado | Prioridad |
|---|-------|--------|-----------|
| **1** | Ajustar `CHA-OVA-VIR-DOR` a 103 — reactiva 14 publicaciones | ⏳ Operación | **Alta** |
| **2** | `REP-GD` sin orden de compra, 47 salidas en 90d | ⏳ Operación | **Alta** |
| **3** | Confirmar las órdenes de compra en borrador | ⏳ Operación | Media |
| **4** | Cancelar en Odoo 2 SOs de órdenes canceladas en Amazon | ⏳ Operación | Media |
| **5** | Remote `bridge-offsite` de rclone + descomentar su cron | ⏳ Operación | Media |
| **6** | 3 SKUs sin listing vivo: ¿descontinuados o relistados? | ⏳ Operación | Baja |
| **7** | Almacenes por canal — **preparación, sin urgencia** | 🟡 En pausa | Baja |
| **8** | `recover_manual_review.py` nunca encuentra los payloads | ⏳ Abierta | Media |
| **9** | `amazon_fba_inventory` duplica el conteo MX/US | ⏳ Abierta | Baja |

---

## 1. `CHA-OVA-VIR-DOR` → 103 · Alta

Lo único que reactiva ventas hoy. El operador confirmó que hay **103 en total**
(3 en Odoo + 100 recibidas). Verificado kit por kit: reactiva **14 de las 22**
publicaciones que dependen de él; las otras 8 esperan a `4609` y `REP-GD`.

- [ ] Inventario → Ajustes de inventario, `EHV/Stock`, cantidad final **103**

---

## 2. `REP-GD` sin orden de compra · Alta

Stock 0, **47 salidas en 90 días**, última entrada 2026-01-25. Bloquea 4 kits
publicados y es el **único** de los 15 componentes críticos sin nada pedido.

- [ ] Levantar orden de compra

---

## 3. Órdenes de compra sin confirmar · Media

Las OCs existen pero Odoo no cuenta lo entrante, porque una OC en borrador o
enviada no genera recepción:

| SKU | Odoo espera | pendiente en OC |
|---|---|---|
| `EST-CAR-ROJ` | **0** | 569 |
| `ARR-22-PLA-PEZ` | **0** | 201 |
| `ARR-22-PLA-VBU` | 46 | 206 |
| `ARR-22-PLA-VCO` | 46 | 204 |
| `4405-BG` | 15 | 51 |
| `4527` | 18 | 49 |

Confirmarlas no trae la mercancía antes, pero hace visible lo que viene. Sin
eso nadie puede planear.

- [ ] Confirmar las OCs para que generen recepción

*(El conteo físico de los 8 componentes en falta quedó descartado por el
operador el 2026-09-12: la mercancía está en tránsito, no en el anaquel. Las 94
publicaciones se reactivan cuando llegue y se reciba.)*

Diagnóstico en cualquier momento:
`docker exec bridge-api python3 /data/odoo_componentes_criticos.py`

---

## 4. SOs vivos de órdenes canceladas · Media

Amazon las tiene canceladas; Odoo no. Su total en $0 es correcto — lo que no
cuadra es que el SO siga abierto. Ninguna tiene factura, así que es limpio.

| SO | Orden | Cancelada en Amazon |
|---|---|---|
| S01460 | `701-8620462-5049819` | 2026-05-19 |
| S02205 | `701-5442653-8913037` | 2026-09-11 |

- [ ] Cancelar ambos SOs en Odoo

---

## 5. Backup offsite · Media

`tools/bridge_backup_offsite.sh` está desplegado y su línea de cron está
**comentada a propósito**: necesita un remote de rclone llamado `bridge-offsite`
que no existe (hoy solo hay `onedrive:`). Habilitarlo antes de configurarlo
garantiza un fallo cada noche a las 03:30.

- [ ] `rclone config` → nombrar el remote `bridge-offsite`
- [ ] Descomentar la línea en `/etc/cron.d/goncloud_bridge_observability`

---

## 6. 3 SKUs sin ningún listing vivo · Baja

`NH-ITA-PEZ-DOR`, `NH-SOLO-GAM-AZU-SAN-PLA`, `NH-SOLO-GAM-AZU-VCO-PLA` dependen
solo de items borrados en MeLi. Puede ser correcto (descontinuados) o señal de
que se relistaron con otro ID y el backfill no los tomó.

- [ ] Revisarlos en el panel de MeLi
- [ ] Según eso, purgar los 4 items muertos restantes o corregir sus mappings

---

## 7. Almacenes por canal · 🟡 En pausa

**Medido el 2026-09-12: no hay a quién servirle.**

| | |
|---|---|
| Listings MeLi FULL | **0** — los 50 activos son `xd_drop_off` (FBM) |
| Órdenes MeLi FULL en el histórico | **0** de 1209 |
| Órdenes Amazon FBA reales | **0** de 1052 |
| Órdenes AFN | 50, **todas Flex MX** (que es FBM a propósito) |

`Meli - Full`, `FBA - MX` y `FBA - US` son almacenes para modelos de fulfillment
que hoy no se usan. El bloque no es incorrecto: es **preparación**.

Ya listo:

- [x] Recetas phantom restringidas a la entrega de EHV-MX (PR #45)
- [x] `tools/odoo_armar_kits.py` para crear kits armados en el canal (PR #47)

Falta, **solo cuando actives FULL o FBA**:

- [ ] **Contexto de almacén en el snapshotter.** Hoy lee `qty_available` global,
      así que aunque muevas stock a Meli-Full el canal seguiría recibiendo el
      total de la empresa. **Sin esto, activar los almacenes causa sobreventa.**
- [ ] Desmarcar Buy/Manufacture to Resupply en los 3 almacenes de canal
- [ ] Primera transferencia de prueba con un SKU piloto
- [ ] Los 3 UPDATE de `warehouse_*` en `bridge_settings`
- [ ] Lógica de picking por canal en los workers
- [ ] Probar el flujo completo con una orden real

---

## 8. `recover_manual_review.py` no encuentra los payloads · Media

Busca en `inbound_job_payloads` por la clave *action-aware* (`ml:{id}:paid`),
pero el payload se persiste bajo la clave *inicial* (`ml:{id}`). Nunca coinciden,
así que siempre reporta "sin payload persistido".

Eso explica la nota de CLAUDE.md sobre que las órdenes MeLi con clave `rawsha:`
son irrecuperables: no es que no se persistan, es que se buscan mal. Desde el
PR #42 la clave inicial es `ml:{order_id}`, así que el arreglo es quitar el
último segmento.

- [ ] Corregir la búsqueda y cubrirla con un test

---

## 9. `amazon_fba_inventory` duplica el conteo · Baja

Las filas de `amazon_mx` y `amazon_us` son idénticas — **1071 de 1071 con la
misma cantidad**. El reporte `GET_AFN_INVENTORY_DATA` no está scopeado por
marketplace, así que sumar por ambos da 65,580 unidades donde hay 32,790.

De paso: esas 32,790 unidades en FBA no generaron **una sola orden FBA** en tres
meses. Vale revisarlo comercialmente.

- [ ] Decidir si se guarda una sola vez o se marca el origen del reporte

---

## Nota: drift entre el repo y el servidor

`amazon_fbm_paid_one_shot.py` tiene md5 distinto en `tools/` del repo y en
`/data/` del servidor. La guarda de precio cero está en ambos, pero son
versiones distintas. El repo ya tiene `tools/check_tools_data_drift.sh` para
esto; conviene correrlo y decidir qué lado promover.
