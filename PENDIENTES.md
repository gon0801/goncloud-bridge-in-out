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
| **1** | Drift `tools/` vs `data/`: 2 diferencias reales de 19 | ⏳ Abierta | Baja |
| **2** | Almacenes por canal — **preparación, sin urgencia** | 🟡 En pausa | Baja |

**Cerrados el 2026-09-12, después de la reescritura de este archivo:** ajuste de
`CHA-OVA-VIR-DOR` a 103 (reactivó 14 publicaciones) · S02205 cancelado · backup
offsite activado con `onedrive` y acotado con `sync` · búsqueda de payloads en
`recover_manual_review.py` · duplicado MX/US en `amazon_fba_inventory` · purga
de los 4 mappings muertos de 3 SKUs · `amazon_fbm_paid_one_shot.py` promovido
desde el repo al servidor (tenía un `except:` pelado).

**Descartados por el operador:** confirmar las OCs en borrador · el conteo físico
de los 8 componentes (la mercancía está en tránsito) · S01460, demasiado vieja ·
la orden de compra de `REP-GD`, que el operador maneja por su cuenta.

---

## 1. Drift entre `tools/` y `data/` en el servidor · Baja

El checker reporta 19 archivos distintos, pero comparados por **AST** (ignorando
el formato) solo **2 tienen diferencia real de comportamiento**. Los otros 17 son
el `ruff format` masivo del 2026-08-07: f-strings sin placeholder, imports en una
línea vs varias, espacios dentro de un literal SQL.

**Primero, un dato que no estaba escrito en ningún lado:** los dos directorios
están vivos, con llamadores distintos.

| Llamador | Qué corre |
|---|---|
| Los workers (`run_tool`) | `/data/` primero, `tools/` de fallback |
| Los timers de systemd del host | `tools/` directo (`ExecStart=...${BRIDGE_BASE}/tools/...`) |

Por eso "promover un lado" no es una sola decisión: depende de quién llama a cada
archivo. La regla sellada en CLAUDE.md (`/data/` primero) es la del worker, no la
del host.

### Las 2 reales

- [ ] **`inbound_full_so_refund_and_cancel.py`** (corre por worker desde `/data/`).
      Le falta la guarda del `die()`: llama `write_audit(audit)` sin verificar que
      `audit` exista. `audit` se arma después de autenticar contra Odoo, así que
      si muere antes — falta una env var, falla el auth — tira `NameError` y tapa
      el error real. **Latente:** hay 0 órdenes MeLi FULL en 1209 del histórico,
      este camino nunca se ejecutó.
- [ ] **`sync_meli_listings.py`** (`/data/`). Dos `except:` pelados, la misma
      clase de bug que el de `amazon_fbm_paid_one_shot.py`: también atrapan
      `KeyboardInterrupt` y `SystemExit`.

### Lo que NO es un problema

`amazon_prices_sync.py`: el fix del inventario duplicado MX/US **sí está vivo**.
El timer corre `tools/`, que coincide con el repo. La copia de `/data/` es un
sobrante viejo que nadie invoca.

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
