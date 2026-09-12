# Pendientes activos — GONCLOUD Bridge

> **Para Claude:** fuente de verdad única de pendientes. Al iniciar sesión: leerlo y recordar al usuario.
> Al cerrar una tarea: actualizar checkbox + contador + commit.
> **No** convertir este archivo en diario — el histórico vive en `git log`.

**Última verificación contra producción:** 2026-09-12
**Abiertos:** 7 · **Bloqueantes:** 1

Todo lo de abajo fue verificado contra Odoo, `bridge.db` y el host `goncloud` el 2026-08-09.
Lo que ya estaba hecho se eliminó del archivo (ver `git log` si hace falta el histórico).

---

## Tabla de estado

| # | Tarea | Estado | Prioridad |
|---|-------|--------|-----------|
| **1** | ~~MeLi inbound: `bad_resource` ensucia la DLQ~~ | ✅ Cerrada 2026-09-12 | — |
| **2** | Odoo: desmarcar Buy/Manufacture to Resupply en los 3 almacenes nuevos | ⏳ Abierta | Media |
| **3** | **Decidir estrategia phantom BOM** ⚠️ bloquea 5 y 6 | ⏳ Abierta | **Crítica** |
| **4** | Primera transferencia de prueba EHV/Stock → FBAMX/Stock | ⏳ Abierta | Alta |
| **5** | Activar mapping canal→almacén (3 UPDATE en `bridge_settings`) | ⏳ Abierta | Alta |
| **6** | Lógica de picking por canal ⚠️ bloqueada por #3 | ⏳ Bloqueada | Alta |
| **7** | Probar flujo completo con orden real en cada canal | ⏳ Abierta | Alta |
| **8** | ~~Rebuild imagen Docker~~ — ya estaba hecho, tracker desactualizado | ✅ Cerrada 2026-09-12 | — |
| **9** | Crons del host — 2 de 3 instalados; offsite espera remote de rclone | 🟡 Parcial | Media |
| **10** | ~~Purgar 2 mappings MeLi muertos~~ — hecho; aparecieron 4 más | 🟡 Parcial | Baja |
| **11** | ~~Apagón de ingress MeLi~~ — resuelto y verificado | ✅ Cerrada 2026-09-12 | — |
| **12** | ~~Nada alertó en 19 días~~ — chequeo de frescura en `/v1/health` | ✅ Cerrada 2026-09-12 | — |
| **13** | `MELI_REDIRECT_URI` apunta a un host que no existe → re-auth OAuth imposible | ⏳ Abierta | Media |
| **16** | Cada orden MeLi se reprocesa ~6 veces (dedupe por `rawsha`) | ⏳ Abierta | Baja |
| **14** | ~~Health en rojo por `amazon_inventory_cache` sin refresco~~ | ✅ Cerrada 2026-09-12 | — |
| **15** | ~~113 `stock_jobs` encolados~~ — drenaban normal, hoy en 0 | ✅ Cerrada 2026-09-12 | — |

---

## 1. ~~DLQ con `bad_resource`~~ · ✅ Cerrada 2026-09-12

Resuelto en PR #36, desplegado y `bridge-inbound-worker` reiniciado.

Los topics conocidos que el worker no procesa (`payments`, `messages`,
`questions`) ahora se registran como `skipped` con `reason=topic_not_handled`.
Un topic **desconocido** sigue yendo a `dead` a propósito.

Los "UUID de 32 hex de topic por identificar" eran **`messages`** (mensajería
post-venta). Inventario completo: `orders_v2` 3474 · `payments` 1671 ·
`messages` 267 · `questions` 105 · `orders` 29.

Queda como opción del operador, no del código: desuscribir en el panel de MeLi
los topics que no se usen. `questions` y `messages` podrían querer manejarse.

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

## 8. ~~Rebuild de la imagen Docker~~ · ✅ Cerrada 2026-09-12

El tracker estaba desactualizado: la imagen ya se había reconstruido (hace ~4
semanas). Verificado en el contenedor vivo:

```
fastapi  0.141.1   (requirements pide >=0.115.0) ✓
uvicorn  0.52.1                                   ✓
multi-stage activo (/venv presente, sin compiladores) ✓
imágenes rollback-20260810T060922Z preservadas
```

---

## 9. Crons del host · 🟡 Parcial

Los tres scripts ya estaban desplegados y con md5 idéntico al repo. Lo que
faltaba eran los crons — ninguno los llamaba. **Y los tres carecían de bit de
ejecución**, en el repo y en el servidor: de haberlos cableado así habrían
fallado con "Permission denied" en cada corrida, en silencio. Lo detectó
`tests/test_schedulers_apuntan_a_scripts_reales.py` antes de desplegar.

Definiciones versionadas en `tools/cron.d/goncloud_bridge_observability`.

- [x] `docker_stats_log.sh` → cron c/5 min, verificado corriendo
- [x] `bridge_restore_test.sh` → cron día 1 a las 04:00 UTC. **Primera corrida:
      PASSED** (integrity_check ok, 41 tablas, conteos sanos sobre el backup de
      2.5 GB). Nunca se había probado un backup.
- [ ] `bridge_backup_offsite.sh` → **desactivado a propósito**: necesita un
      remote de rclone llamado `bridge-offsite` que no existe (solo hay
      `onedrive:`). Habilitarlo antes de configurarlo garantiza un fallo cada
      noche a las 03:30 — el mismo patrón de rojo crónico que causó el apagón
      de agosto. La línea está en el cron, comentada, lista para descomentar.
      Configurar con: `rclone config` (nombrar el remote `bridge-offsite`).

---

## 10. Mappings MeLi muertos · 🟡 Parcial

`MLM2787930515` y `MLM2787902225` purgados (336 → 334 filas). Confirmados
`status=closed, sub_status=['deleted']` vía API antes de borrar. El SKU
`NH-ITA-CEN-DOR` conserva `MLM2874375637`, activo. Filas respaldadas en
`data/sku_mapping_purge_20260912.sql`.

**Auditoría completa de los 73 items mapeados** (no estaba en el pendiente):

| estado real | items | qué hacer |
|---|---|---|
| `active` | 50 | dejar |
| `paused` / `out_of_stock` | 14 | **dejar** — vuelven cuando haya stock |
| `closed` / `deleted` | 4 | muertos |
| `inactive` / `forbidden,deleted` | 2 | muertos |
| sin respuesta de la API | 3 | investigar |

Quedan **4 items muertos** más. Borrar sus filas dejaría **3 SKUs sin ningún
mapping**: `NH-ITA-PEZ-DOR`, `NH-SOLO-GAM-AZU-SAN-PLA`,
`NH-SOLO-GAM-AZU-VCO-PLA`. Eso puede ser correcto (producto descontinuado) o
señal de que el listing se recreó con otro ID y el backfill no lo tomó.
Decisión del operador.

- [ ] Revisar esos 3 SKUs en el panel de MeLi: ¿descontinuados o relistados?
- [ ] Según eso, purgar los 4 items muertos restantes o corregir sus mappings
- [ ] Investigar los 3 items sin respuesta de la API

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

- [x] Ingress verificado end-to-end sin esperar tráfico: el `curl` a
      `/v1/health` atravesó Cloudflare → tunnel → cloudflared → docker-proxy →
      `bridge-api` y la app respondió (el 503 era su veredicto de salud, no un
      error de ruteo). Suscripción confirmada vía `GET /applications/{app_id}`.
      No tiene sentido esperar un webhook como prueba: el volumen real es de
      **6-12 órdenes por semana**, así que pueden pasar días entre uno y otro.
- [x] Sincronizar `meli_orders_backfill.py` del servidor con `main` (2026-09-12)

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

## 14. ~~Health en rojo por `amazon_inventory_cache`~~ · ✅ Cerrada 2026-09-12

Resuelto en PR #35, desplegado y verificado: **`/v1/health` devuelve `ok: true`**
por primera vez desde el 2026-08-10.

**El diagnóstico anterior era falso.** Este archivo decía que
`amazon-prices-sync.timer` "falla en silencio pese a correr cada 6h". Ese timer
funciona perfecto (exit 0, 1071 items MX + 258 listings US + 1071 FBA US). El
problema real: health y sync miran tablas distintas.

| | |
|---|---|
| Health lee | `amazon_inventory_cache` |
| El sync escribe | `amazon_listing_prices`, `amazon_fba_inventory` |

`amazon_inventory_cache` solo alimenta la lista de SKUs del mapper UI y se
escribía a mano. Ahora la refresca `amazon-inventory-refresh.timer` c/6h
(00,06,12,18:55 UTC). Units versionadas en `tools/systemd/`.

---

## 15. ~~`stock_jobs` sin drenar~~ · ✅ Cerrada 2026-09-12

Falsa alarma. Los 113 encolados eran backlog normal del sync outbound: se
drenaron solos, medido tres veces seguidas en 0. No había consumidor atascado.

---

## Nota: la cola `ml_orders_dead` (9 entradas)

Residuo obsoleto del **2026-05-08**, dos órdenes que sí terminaron en Odoo:

| orden | SO | estado | monto |
|---|---|---|---|
| `2000016327680524` | S01378 | sale | $1,488.54 |
| `2000016330666386` | S01377 | sale | $1,310.87 |

No hay nada perdido. Se puede purgar (`DEL ml_orders_dead`) para dejar de sumar
ruido al health, pero es borrado en producción: decisión del operador.

---

## 16. Cada orden MeLi se reprocesa ~6 veces · Baja

El webhook fija `dedupe_key = rawsha:{sha256}` en el job y el worker lo respeta,
así que cada re-entrega de la MISMA orden genera una clave distinta y vuelve a
pasar por todo el flujo. Medido sobre 60 días:

```
órdenes MeLi distintas .......... 156
eventos success:paid ............ 976    (~6.3 por orden)
```

**No duplica nada en Odoo** — verificado, 0 órdenes con SO duplicada: los tools
buscan la SO por `client_order_ref` antes de crear. El costo real no es de
carga (12 órdenes/semana × 6 = trivial), es que **la tabla de auditoría y
cualquier métrica derivada de ella mienten**: contar `success:paid` da ~6x las
ventas reales. Eso ya causó un error de diagnóstico en la sesión del 2026-09-12.

El arreglo obvio sería que el worker derive `ml:{order_id}` en vez de confiar
en el `rawsha` del webhook. **Pero tiene un riesgo real:** una segunda
notificación de la misma orden a veces trae datos que la primera no tenía — es
exactamente el caso Flex MX documentado en CLAUDE.md PROBLEMA 7 (en `Pending`
Amazon no manda `BuyerName`; llega en `Unshipped` y el segundo pase corrige el
`client_order_ref`). Deduplicar por orden+acción se saltaría esa corrección.

- [ ] Verificar si MeLi tiene el mismo patrón de enriquecimiento tardío
- [ ] Si no lo tiene: deduplicar por `ml:{order_id}:{action}`
- [ ] Si lo tiene: dejar el reprocesamiento y contar ventas por `inbound_sales_orders`
      o por Odoo, nunca por `processed_inbound_events`
