# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** fuente de verdad única de pendientes. Al iniciar sesión: leerlo y recordar al usuario.
> Al cerrar una tarea: actualizar checkbox + contador + commit.
> **No** convertir este archivo en diario — el histórico vive en `git log`.

**Última verificación contra producción:** 2026-09-12
**Abiertos:** 4 · **Bloqueantes:** 0

Todo lo de abajo fue verificado contra Odoo, `bridge.db`, la SP-API de Amazon y
el host `goncloud` el 2026-09-12. Lo cerrado se eliminó del archivo (ver
`git log` si hace falta el histórico).

---

## Tabla de estado

| # | Tarea | Estado | Prioridad |
|---|-------|--------|-----------|
| **1** | Purgar (o no) los 3 SKUs sin listing vivo | ⏳ Decisión | Baja |
| **2** | `REP-GD` sin orden de compra — el operador ya está enterado | ⏳ Operación | Media |
| **3** | Drift: `amazon_fbm_paid_one_shot.py` difiere entre repo y servidor | ⏳ Abierta | Baja |
| **4** | Almacenes por canal — **preparación, sin urgencia** | 🟡 En pausa | Baja |

**Cerrados el 2026-09-12, después de la reescritura de este archivo:** ajuste de
`CHA-OVA-VIR-DOR` a 103 (reactivó 14 publicaciones) · S02205 cancelado · backup
offsite activado con `onedrive` y acotado con `sync` · búsqueda de payloads en
`recover_manual_review.py` · duplicado MX/US en `amazon_fba_inventory`.

**Descartados por el operador:** confirmar las OCs en borrador · el conteo físico
de los 8 componentes (la mercancía está en tránsito) · S01460, demasiado vieja.

---

## 1. Los 3 SKUs sin listing vivo · Baja

Verificado contra la API de MeLi el 2026-09-12 — **los cuatro listings están
muertos**, no relistados con otro ID:

| SKU | listing | estado |
|---|---|---|
| `NH-ITA-PEZ-DOR` | MLM4734057258 | `inactive` / forbidden, deleted |
| `NH-ITA-PEZ-DOR` | MLM5209074728 | `closed` / deleted |
| `NH-SOLO-GAM-AZU-SAN-PLA` | MLM2727257503 | `closed` / deleted |
| `NH-SOLO-GAM-AZU-VCO-PLA` | MLM2727257503 | `closed` / deleted |

Los tres títulos son "Arras Matrimoniales". Parecen descontinuados.

- [ ] Confirmar que son descontinuados y purgar sus filas de `sku_mapping`

---

## 2. `REP-GD` sin orden de compra · Media

Stock 0, **47 salidas en 90 días**, última entrada 2026-01-25. Bloquea 4 kits
publicados y es el único de los 15 componentes críticos sin nada pedido. El
operador quedó enterado el 2026-09-12.

- [ ] Levantar orden de compra

Diagnóstico en cualquier momento:
`docker exec bridge-api python3 /data/odoo_componentes_criticos.py`

---

## 3. Drift entre el repo y el servidor · Baja

`amazon_fbm_paid_one_shot.py` tiene md5 distinto en `tools/` del repo y en
`/data/` del servidor. La guarda de precio cero está en ambos, pero son
versiones distintas — o sea hay cambios en un lado que el otro no tiene.

- [ ] `bash tools/check_tools_data_drift.sh` y decidir qué lado promover

---

## 4. Almacenes por canal · 🟡 En pausa

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
