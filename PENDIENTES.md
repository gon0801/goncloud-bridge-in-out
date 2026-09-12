# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** fuente de verdad única de pendientes. Al iniciar sesión: leerlo y recordar al usuario.
> Al cerrar una tarea: actualizar checkbox + contador + commit.
> **No** convertir este archivo en diario — el histórico vive en `git log`.

**Última verificación contra producción:** 2026-09-12
**Abiertos:** 14 · **Bloqueantes:** 1

Todo lo de abajo fue verificado contra Odoo, `bridge.db` y el host `goncloud` el 2026-08-09.
Lo que ya estaba hecho se eliminó del archivo (ver `git log` si hace falta el histórico).

---

## Tabla de estado

| # | Tarea | Estado | Prioridad |
|---|-------|--------|-----------|
| **1** | MeLi inbound: `bad_resource` ensucia la DLQ (359 dead/30d) | ⏳ Abierta | **Alta** |
| **2** | Odoo: desmarcar Buy/Manufacture to Resupply en los 3 almacenes nuevos | ⏳ Abierta | Media |
| **3** | **Decidir estrategia phantom BOM** ⚠️ bloquea 5 y 6 | ⏳ Abierta | **Crítica** |
| **4** | Primera transferencia de prueba EHV/Stock → FBAMX/Stock | ⏳ Abierta | Alta |
| **5** | Activar mapping canal→almacén (3 UPDATE en `bridge_settings`) | ⏳ Abierta | Alta |
| **6** | Lógica de picking por canal ⚠️ bloqueada por #3 | ⏳ Bloqueada | Alta |
| **7** | Probar flujo completo con orden real en cada canal | ⏳ Abierta | Alta |
| **8** | Rebuild imagen Docker — sigue en `fastapi 0.110.0` | ⏳ Abierta | Media |
| **9** | Crons del host: stats / backup offsite / restore test + remote rclone | ⏳ Abierta | Media |
| **10** | Purgar 2 mappings MeLi muertos de `sku_mapping` | ⏳ Abierta | Baja |
| **11** | Apagón de ingress MeLi — resuelto; falta ver el primer webhook real | 🟡 Verificando | Media |
| **12** | Nada alertó en 19 días — el health ya estaba rojo por otra causa | ⏳ Abierta | **Alta** |
| **13** | `MELI_REDIRECT_URI` apunta a un host que no existe → re-auth OAuth imposible | ⏳ Abierta | Media |
| **14** | Caché de Amazon congelado desde 2026-08-10 pese a que el timer corre c/6h | ⏳ Abierta | **Alta** |
| **15** | 113 `stock_jobs` encolados sin drenar | ⏳ Abierta | Media |

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
Estos son topics a los que la app está suscrita y el worker no maneja.

**Por qué importa igual:**
- Falsas alarmas en el health endpoint (D5.4 dispara con `ml_orders_dead > 50`).
- Una falla real de una orden queda enterrada entre cientos de eventos de pago.
- `ml_orders_dead` tiene 9 entradas ahora mismo.

- [ ] Clasificar topics conocidos que no son órdenes como `ignored`/`skipped`, no `dead`
- [ ] Identificar qué topic manda los UUID de 32 hex antes de decidir qué hacer con ellos
- [ ] Alternativa complementaria: desuscribir topics no usados en el panel de MeLi
- [ ] Test que cubra el caso (regla del quality-kit: todo fix lleva su prueba)

---

## 2–4. Almacenes en Odoo

Los 4 almacenes existen y ya están casi configurados. Estado real verificado:

| Almacén | code | Recepción | Entrega | Resupply From EHV-MX | Buy/Manufacture to Resupply |
|---|---|---|---|---|---|
| EHV-MX | `EHV` | one_step ✅ | ship_only ✅ | — (es el origen) | `True` / `True` |
| Meli - Full | `Full` | one_step ✅ | ship_only ✅ | ✅ sí | ❌ `True` / `True` |
| FBA - MX | `FBAMX` | one_step ✅ | ship_only ✅ | ✅ sí | ❌ `True` / `True` |
| FBA - US | `FBAUS` | one_step ✅ | ship_only ✅ | ✅ sí | ❌ `True` / `True` |

**Resupply From y 1 step ya quedaron** (PENDIENTES.md viejo los daba por pendientes — estaban hechos).

- [ ] **(#2)** Desmarcar **Buy to Resupply** y **Manufacture to Resupply** en Meli - Full, FBA - MX y FBA - US
- [ ] **(#3)** ⚠️ **Decidir estrategia phantom BOM** — bloquea #6 y #7
- [ ] **(#4)** Primera transferencia de prueba EHV/Stock → FBAMX/Stock con un SKU piloto

---

## 5–7. Picking por canal

Código ya pre-codeado (2026-05-06): los workers pasan `WAREHOUSE_NAME` al tool según perfil y
los tools resuelven el `warehouse_id` en Odoo por nombre. Si el setting está vacío → comportamiento
actual sin cambios. Los 3 settings **siguen vacíos**, así que no está activo.

> ⚠️ **Ojo:** el SQL de activación del archivo viejo tenía los nombres mal.
> En Odoo los almacenes se llaman con espacios alrededor del guion. Los nombres correctos son:

```sql
UPDATE bridge_settings SET value='Meli - Full' WHERE key='warehouse_meli_full';
UPDATE bridge_settings SET value='FBA - MX'    WHERE key='warehouse_amazon_fba_mx';
UPDATE bridge_settings SET value='FBA - US'    WHERE key='warehouse_amazon_fba_us';
```

No requiere restart ni redeploy.

- [ ] **(#5)** Correr los 3 UPDATE de arriba (después de cerrar #2 y #4)
- [ ] **(#6)** Lógica de picking ⚠️ *bloqueada por #3* — hoy FBA cancela pickings y FULL no los genera
- [ ] **(#7)** Probar flujo completo con orden real en cada canal

---

## 8. Rebuild de la imagen Docker · Media

La auditoría de seguridad del 2026-05-08 subió `requirements.txt` a `fastapi>=0.115.0` y convirtió
`app/Dockerfile` a multi-stage, pero **la imagen nunca se reconstruyó**: el contenedor corre
`fastapi 0.110.0`. Los fixes de seguridad de FastAPI y el multi-stage no están activos.

- [ ] `docker compose up -d --build` (hacerlo con las colas vacías)
- [ ] Verificar después: `docker exec bridge-api pip show fastapi` → debe decir ≥ 0.115.0

---

## 9. Crons del host pendientes · Media

De la auditoría del 2026-05-08. Los scripts están versionados en `tools/` pero **no desplegados**.
`rclone` sí está instalado, pero el único remote configurado es `onedrive:` — falta `bridge-offsite`.

- [ ] `tools/docker_stats_log.sh` → copiar al host + cron c/5 min
- [ ] `tools/bridge_backup_offsite.sh` → copiar + cron `30 3 * * *` + configurar remote rclone `bridge-offsite`
- [ ] `tools/bridge_restore_test.sh` → copiar + cron `0 4 1 * *`

*(Ya activos y verificados: `goncloud_bridge_cleanup`, `goncloud_bridge_backup`,
`goncloud_bridge_wal_checkpoint`, `goncloud_meli_refresh`, backfill c/4h, Redis SLOWLOG.)*

---

## 10. Mappings MeLi muertos · Baja

`MLM2787930515` y `MLM2787902225` (ambos SKU `NH-ITA-CEN-DOR`) siguen en `sku_mapping` con
`last_seen_at = 2026-04-18` — casi 4 meses sin que el backfill los vea, mientras que el cron
**sí está corriendo** (17 mappings refrescados en los últimos 7 días sobre 336 totales).

Conclusión: los dos listings están pausados o eliminados en MeLi. No hay nada que arreglar del lado
del bridge; sólo queda purgar las filas para que dejen de aparecer como huérfanos.

- [ ] Confirmar en el panel de MeLi que ambos están inactivos
- [ ] `DELETE FROM sku_mapping WHERE remote_item_id IN ('MLM2787930515','MLM2787902225');`

---

## 11. Apagón de ingress MeLi (2026-08-24 → 2026-09-12) · Crítica

Los webhooks entran por un **Cloudflare Tunnel** (`cloudflared.service` en el
host, config remota en el dashboard) que enruta
`meli-webhooks.goncloud.cc` → `http://localhost:8099`. El compose publicaba el
puerto **solo en la interfaz WireGuard** (`10.13.13.1:8099:8099`), así que tras
el recreate del 2026-08-24 02:00 UTC cloudflared resolvió `localhost` a `[::1]`
y se comió un `connection refused` en cada entrega:

```
ERR Unable to reach the origin service ... dial tcp [::1]:8099: connection refused
    originService=http://localhost:8099
    dest=https://meli-webhooks.goncloud.cc/webhooks/meli/orders/***
```

**138 intentos rechazados por día.** MeLi nunca dejó de entregar — la
suscripción sigue viva. Último webhook recibido: 01:44 UTC, 16 min antes del
recreate. Amazon no se vio afectado (polling). Impacto verificado contra el catálogo completo de MeLi: **18 órdenes creadas
durante el apagón nunca llegaron a Odoo** (0 huérfanas fuera del rango de rescate).
Otras 39 órdenes previas cambiaron de estado durante la ventana y también se
reencolan: entre ellas puede haber cancelaciones que nunca se aplicaron.

Ruta aparte, también rota y de consecuencias mucho menores: `bridge-api` quedó
fuera de la red docker `proxy`, así que `mapper.goncloud.cc` (UI del SKU mapper)
devolvía 502. Reconectado en caliente el 2026-09-12 y ya declarado en el compose.

**Resuelto el 2026-09-12** (PR #34). Puerto publicado en `127.0.0.1` + `10.13.13.1`,
red `proxy` declarada, 0 `connection refused` desde el fix. Rescate corrido:
57 encoladas → 55 `success`, 2 `manual_review` benignas (órdenes creadas y
reembolsadas dentro del apagón: no hay SO que revertir). Cobertura verificada
contra el catálogo completo de MeLi: **18/18** de las creadas durante la ventana.

Suscripción confirmada viva vía API:
`notifications_callback_url = https://meli-webhooks.goncloud.cc/webhooks/meli/orders/***`

- [ ] **Único pendiente:** confirmar la llegada del primer webhook real post-fix.
      Al cierre de la sesión aún no había entrado ninguno (volumen bajo de sábado).
      Si pasan >24h sin ninguno: revisar la suscripción en el DevCenter de MeLi.
      Comprobar con: `sqlite3 bridge.db "SELECT MAX(received_at) FROM inbound_events WHERE dedupe_key LIKE 'rawsha:%';"`
- [ ] Sincronizar `meli_orders_backfill.py` del servidor con `main` (solo difieren
      los docstrings; el del servidor explica mal la idempotencia)

---

## 12. Nada alertó durante 19 días · Alta

El health endpoint (D5.4) vigila profundidad de *dead queues*, pero
`ml_orders_jobs` estaba en **0**: no había nada atascado, simplemente no
llegaba nada. Un inbound en silencio total no dispara ninguna alarma hoy.

**Y hay algo peor:** `/v1/health` lleva devolviendo `ok: false` **desde el
2026-08-10** por el caché de Amazon (ver #14). Cuando el apagón empezó el
24-ago, el semáforo ya estaba en rojo por una causa distinta e inofensiva.
No faltaba monitoreo — estaba saturado. Agregar un chequeo nuevo sin limpiar
el rojo viejo lo deja igual de ignorado.

- [ ] Chequeo de *staleness*: si no entra un evento MeLi en N horas → `ok: false`
- [ ] Que ese chequeo llegue a algún lado (uptime-kuma ya corre en el host)
- [ ] Test que cubra el caso (regla del quality-kit)

---

## 13. `MELI_REDIRECT_URI` apunta a un host inexistente · Media

[`app/main.py:155`](app/main.py) tiene hardcodeado
`https://meli.goncloud.cc/oauth/callback`. Ese host **no existe**: no tiene
proxy host ni certificado en `nginx-proxy-manager` (revisada la tabla completa,
incluidos los borrados). Es del servidor viejo. El refresh diario funciona
porque usa el `refresh_token` y no necesita redirect — pero **una re-auth desde
cero fallaría**, justo cuando más urge.

**Confirmado contra la API el 2026-09-12** (`GET /applications/{app_id}`):

```
callback_url               = https://meli.goncloud.cc/oauth/callback      ← host inexistente
notifications_callback_url = https://meli-webhooks.goncloud.cc/webhooks/… ← este sí funciona
```

Ojo: cambiar el valor en el código sin cambiarlo también en el panel de MeLi
rompe OAuth. Las dos puntas tienen que moverse juntas.

- [ ] Decidir el host definitivo (`mapper.goncloud.cc` es el que sí existe)
- [ ] Actualizar el redirect URI registrado en el DevCenter de MeLi
- [ ] Actualizar `app/main.py:155`
- [ ] Limpiar `meli.goncloud.cc` de `MASTER_RUNBOOK.md`, `docs/RUNBOOK.md` y `SETUP_WIZARD_CANONICAL_v1.md` (15 menciones)

---

## 14. Caché de Amazon congelado · Alta

`/v1/health` reporta `amazon_cache_age_hours: 802` (~33 días) y
`amazon_cache_warn: stale_802h`. El último refresco fue el **2026-08-10**, pese
a que `amazon-prices-sync.timer` corre cada 6h y aparece como activo.

Es la causa de que el health esté en rojo desde entonces, que es lo que tapó el
apagón de MeLi durante 19 días (ver #12). Mientras no se arregle, cualquier
alerta nueva que cuelgue del health nace ignorada.

- [ ] Ver por qué falla en silencio: `journalctl -u amazon-prices-sync --since "7 days ago"`
- [ ] Arreglar la causa y confirmar que `amazon_cache_age_hours` baja
- [ ] Test que cubra el caso (regla del quality-kit)

---

## 15. `stock_jobs` sin drenar · Media

`/v1/health` reporta `stock_jobs_queued: 113`. Sin diagnosticar todavía — puede
ser backlog normal del sync outbound o un consumidor atascado.

- [ ] Revisar si el worker outbound los está consumiendo o están parados
- [ ] Si están parados, encontrar por qué antes de purgar
