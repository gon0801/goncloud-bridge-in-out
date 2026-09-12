# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** fuente de verdad única de pendientes. Al iniciar sesión: leerlo y recordar al usuario.
> Al cerrar una tarea: actualizar checkbox + contador + commit.
> **No** convertir este archivo en diario — el histórico vive en `git log`.

**Última verificación contra producción:** 2026-09-12
**Abiertos:** 2 · **Bloqueantes:** 0

Todo lo de abajo fue verificado contra Odoo, `bridge.db`, la SP-API de Amazon y
el host `goncloud` el 2026-09-12. Lo cerrado se eliminó del archivo (ver
`git log` si hace falta el histórico).

---

## Tabla de estado

| # | Tarea | Estado | Prioridad |
|---|-------|--------|-----------|
| **1** | El checkout del servidor está parado en el 31-ago | ⏳ Abierta | Baja |
| **2** | Almacenes por canal — **preparación, sin urgencia** | 🟡 En pausa | Baja |

**Cerrados el 2026-09-12, después de la reescritura de este archivo:** ajuste de
`CHA-OVA-VIR-DOR` a 103 (reactivó 14 publicaciones) · S02205 cancelado · backup
offsite activado con `onedrive` y acotado con `sync` · búsqueda de payloads en
`recover_manual_review.py` · duplicado MX/US en `amazon_fba_inventory` · purga
de los 4 mappings muertos de 3 SKUs · `amazon_fbm_paid_one_shot.py` promovido
desde el repo al servidor (tenía un `except:` pelado) · las 2 diferencias
reales de drift: la guarda del `die()` en `inbound_full_so_refund_and_cancel.py`
y los dos `except:` pelados de `sync_meli_listings.py` · `check_tools_data_drift`
reescrito para comparar por AST y mostrar qué cambia · S02172 y S02173
facturadas (`INV/2026/01851` y `01852`, posteadas y pagadas a 2,274.21 c/u) ·
los 2 SKUs de Amazon que las habían tumbado ya mapeados (`ST-MV02-LRFL` →
`NH-CAR-AZU-20C-PLA`, `VO-0U7E-EWSL` → `NH-CAR-AZU-MAX-PLA`) · detector diario
extendido para cazar "entregado y nunca facturado", que antes no veía nadie.

**Descartados por el operador:** confirmar las OCs en borrador · el conteo físico
de los 8 componentes (la mercancía está en tránsito) · S01460, demasiado vieja ·
la orden de compra de `REP-GD`, que el operador maneja por su cuenta.

---

## 1. El checkout del servidor está parado en el 31-ago · Baja

Las 2 diferencias reales de drift **ya están cerradas** (ver más abajo). Al
arreglarlas salió la causa de fondo.

`/mnt/data/appdata/bridge` es un checkout de git de `main`, pero su HEAD es
`8a701b3` del **2026-08-31** — semanas atrás — y encima tiene ediciones a mano
sin commitear (`app/main.py`, `app/inbound_worker.py`, `docker-compose.yml`,
varios de `tools/`).

Por eso `tools/` del servidor no tiene los fixes de esta sesión: las guardas de
precio $0 y la búsqueda de payloads viven en `/data/` y en el repo, pero no en
el `tools/` del servidor. Hoy no rompe nada — cada archivo tiene su versión
buena en el lado que su llamador lee — pero el fallback está viejo.

**No se toca a la ligera.** Entre las ediciones locales está el
`docker-compose.yml`, que es exactamente el archivo cuyo desajuste dejó el
inbound muerto 19 días. Un `git pull` o un `checkout` ahí necesita revisar
primero si esas ediciones ya están en el repo o si se perderían.

- [ ] Comparar cada edición local del servidor contra el repo
- [ ] Poner el checkout al día sin pisar lo que solo existe en el servidor

---

## 2. Almacenes por canal · 🟡 En pausa

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
