# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** fuente de verdad única de pendientes. Al iniciar sesión: leerlo y recordar al usuario.
> Al cerrar una tarea: **eliminarla** del archivo (no marcarla como hecha) y actualizar el contador.
> **No** convertir este archivo en diario — el histórico vive en `git log`.

**Última verificación contra producción:** 2026-08-10
**Abiertos:** 11 · **Bloqueantes:** 1

---

## Tabla de estado

| # | Tarea | Estado | Prioridad |
|---|-------|--------|-----------|
| **1** | MeLi inbound: `bad_resource` ensucia la DLQ | ⏳ Abierta | **Alta** |
| **2** | **Decidir estrategia phantom BOM** ⚠️ bloquea 4 y 5 | ⏳ Abierta | **Crítica** |
| **3** | Primera transferencia de prueba EHV/Stock → FBAMX/Stock | ⏳ Abierta | Alta |
| **4** | Activar mapping canal→almacén (3 UPDATE en `bridge_settings`) | ⏳ Abierta | Alta |
| **5** | Lógica de picking por canal ⚠️ bloqueada por 2 | ⏳ Bloqueada | Alta |
| **6** | Probar flujo completo con orden real en cada canal | ⏳ Abierta | Alta |
| **7** | Endurecer `auth_middleware.py` (rango Tailscale mal, docker gw, Cf-Access) | ⏳ Abierta | **Alta** |
| **8** | Rotar `MELI_CLIENT_SECRET` — está hardcodeado y en la historia de git | ⏳ Abierta | **Alta** |
| **9** | Pinear versiones en `requirements.txt` (el `>=` trajo FastAPI 0.141.1) | ⏳ Abierta | Media |
| **10** | Crons del host: stats / backup offsite / restore test + remote rclone | ⏳ Abierta | Media |
| **11** | Purgar 2 mappings MeLi muertos de `sku_mapping` | ⏳ Abierta | Baja |

---

## 1. MeLi inbound — `bad_resource` ensucia la DLQ · Alta

**Evidencia (2026-08-09):** 359 registros `dead` en 30 días en `processed_inbound_events`,
**100 % con `reason=bad_resource`**. Desglose por recurso:

| Recurso | Cantidad | Qué es |
|---|---|---|
| `/collections/{id}` | 303 (84 %) | notificaciones del topic **payments** |
| `/questions/{id}` | 9 | topic **questions** (preguntas de compradores) |
| UUID de 32 hex sin `/` | ~47 | formato nuevo de notificación de MeLi (topic por identificar) |

**Causa:** [`app/inbound_worker.py:1473`](app/inbound_worker.py) — el regex
`^/orders/([A-Z0-9\-]+)$` sólo acepta órdenes; **todo lo demás cae a `dead`**.

**NO se están perdiendo órdenes.** Las órdenes reales siguen entrando (10–46 `success`/día).

**Por qué importa igual:** falsas alarmas en el health endpoint, y una falla real de una orden
queda enterrada entre cientos de eventos de pago. `ml_orders_dead` tiene 9 entradas.

- [ ] Clasificar topics conocidos que no son órdenes como `ignored`/`skipped`, no `dead`
- [ ] Identificar qué topic manda los UUID de 32 hex
- [ ] Alternativa complementaria: desuscribir topics no usados en el panel de MeLi
- [ ] Test que cubra el caso

---

## 2–6. Almacenes y picking por canal

Estado real de los almacenes (verificado 2026-08-10):

| Almacén | code | Recepción | Entrega | Resupply From EHV-MX | Buy/Manufacture to Resupply |
|---|---|---|---|---|---|
| EHV-MX | `EHV` | one_step ✅ | ship_only ✅ | — (es el origen) | `True` / `True` (correcto) |
| Meli - Full | `Full` | one_step ✅ | ship_only ✅ | ✅ sí | ✅ `False` / `False` |
| FBA - MX | `FBAMX` | one_step ✅ | ship_only ✅ | ✅ sí | ✅ `False` / `False` |
| FBA - US | `FBAUS` | one_step ✅ | ship_only ✅ | ✅ sí | ✅ `False` / `False` |

La configuración de almacenes ya está completa. Lo que falta es la decisión de negocio y las pruebas.

- [ ] **(#2)** ⚠️ **Decidir estrategia phantom BOM** — bloquea #5 y #6
- [ ] **(#3)** Primera transferencia de prueba EHV/Stock → FBAMX/Stock con un SKU piloto
- [ ] **(#4)** Activar el mapping canal→almacén con los nombres exactos de Odoo:

```sql
UPDATE bridge_settings SET value='Meli - Full' WHERE key='warehouse_meli_full';
UPDATE bridge_settings SET value='FBA - MX'    WHERE key='warehouse_amazon_fba_mx';
UPDATE bridge_settings SET value='FBA - US'    WHERE key='warehouse_amazon_fba_us';
```

> Ojo con los espacios alrededor del guion — los nombres en Odoo son `Meli - Full`, no `Meli-Full`.
> No requiere restart ni redeploy: los tools resuelven el `warehouse_id` por nombre en cada corrida.

- [ ] **(#5)** Lógica de picking ⚠️ *bloqueada por #2* — hoy FBA cancela pickings y FULL no los genera
- [ ] **(#6)** Probar flujo completo con orden real en cada canal

---

## 7. Endurecer `auth_middleware.py` · Alta

El factor VPN se rescató del servidor tal cual corría (commit `6ab0702`), sin endurecerlo, para no
mezclar rescate con cambio de comportamiento. Quedan tres puntos por resolver:

- [ ] `_TAILSCALE_PREFIX = "100."` con `startswith` matchea **todo `100.0.0.0/8`**, no sólo el
      `100.64.0.0/10` de RFC 6598 que declara su propio comentario. `100.0.0.0/10` es espacio
      público ruteable. Cambiar a containment con `ipaddress.ip_network("100.64.0.0/10")`.
- [ ] `_DOCKER_GW = "172.18.0.1"` deja pasar sin auth cualquier request que llegue por el
      userland-proxy de Docker. La seguridad queda delegada a reglas `DOCKER-USER` de iptables
      que no viven en este repo.
- [ ] El header `Cf-Access` se acepta por mera presencia: no se valida el email contra allowlist
      ni se verifica la firma `Cf-Access-Jwt-Assertion`.

---

## 8. Rotar `MELI_CLIENT_SECRET` · Alta

`app/main.py` tiene `MELI_CLIENT_ID` y `MELI_CLIENT_SECRET` hardcodeados en texto plano, y ya están
en la historia de git. Se usan en `/oauth/start`, `/oauth/callback` y `/oauth/refresh`.

- [ ] Mover ambos a env vars (mismo patrón que `ODOO_PASSWORD`)
- [ ] Rotar el secret en el panel de desarrollador de MeLi
- [ ] Re-autorizar la app y verificar que el cron de refresh sigue funcionando

---

## 9. Pinear versiones en `requirements.txt` · Media

`fastapi>=0.115.0` y `uvicorn[standard]>=0.30.0` no tienen tope. El rebuild del 2026-08-10 saltó de
FastAPI 0.110.0 a **0.141.1** de golpe. Funcionó (smoke test e import de `main` limpios), pero el
próximo rebuild puede traer otra versión distinta sin aviso.

- [ ] Pinear a versiones exactas o con `~=` y actualizar deliberadamente

---

## 10. Crons del host pendientes · Media

Los scripts ya están en el checkout del servidor (sincronizado 2026-08-10), falta instalarlos.
`rclone` está instalado pero el único remote configurado es `onedrive:` — falta `bridge-offsite`.

- [ ] `tools/docker_stats_log.sh` → cron c/5 min
- [ ] `tools/bridge_backup_offsite.sh` → cron `30 3 * * *` + configurar remote rclone `bridge-offsite`
- [ ] `tools/bridge_restore_test.sh` → cron `0 4 1 * *`

*(Ya activos y verificados: `goncloud_bridge_cleanup`, `goncloud_bridge_backup`,
`goncloud_bridge_wal_checkpoint`, `goncloud_meli_refresh`, backfill c/4h, Redis SLOWLOG.)*

---

## 11. Mappings MeLi muertos · Baja

`MLM2787930515` y `MLM2787902225` (ambos SKU `NH-ITA-CEN-DOR`) siguen en `sku_mapping` con
`last_seen_at = 2026-04-18` — casi 4 meses sin que el backfill los vea, mientras el cron **sí corre**.
Conclusión: los dos listings están pausados o eliminados en MeLi.

- [ ] Confirmar en el panel de MeLi que ambos están inactivos
- [ ] `DELETE FROM sku_mapping WHERE remote_item_id IN ('MLM2787930515','MLM2787902225');`
