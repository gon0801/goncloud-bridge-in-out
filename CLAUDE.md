# CLAUDE.md — GONCLOUD Bridge (Inbound/Outbound)

> **INSTRUCCIÓN PARA CLAUDE — AL INICIAR SESIÓN:**
> Revisa la sección **"Pendientes activos"** (justo abajo) y recuérdaselos al usuario
> en el primer mensaje de la sesión. Mantén la lista viva:
> - Cuando se termine un pendiente → **elimínalo** de la lista
> - Cuando surja uno nuevo → **agrégalo** a la lista
>
> **INSTRUCCIÓN PARA CLAUDE — AL FINALIZAR SESIÓN:**
> Si hubo cambios relevantes, actualiza "Diario de cambios" + "Estado actual" +
> "Pendientes activos", luego haz commit + push. Así la próxima sesión arranca con
> contexto completo.
>
> **REGLA ABSOLUTA ANTES DE INVESTIGAR CUALQUIER FALLA:**
> 1. Leer el worker relevante (`amazon_inbound_worker.py`, `inbound_worker.py`) para entender lógica especial
> 2. Leer este CLAUDE.md completo — el problema probablemente ya está documentado
> 3. Consultar las tablas de DB con queries directos antes de hacer suposiciones
> 4. **NO adivinar.** Si la info está en el repo o en la DB, úsala primero.

---

## Pendientes activos

**Fuente de verdad única:** [PENDIENTES.md](PENDIENTES.md) (archivo dedicado en la raíz del repo, con barra de progreso y checkboxes).

Al iniciar sesión Claude debe leer ese archivo y recordar los pendientes al usuario. Al cerrar sesión: actualizar checkboxes + progreso + commit + push + PR + merge.

---

## Índice

1. [¿Qué es este sistema?](#1-qué-es-este-sistema)
2. [Arquitectura](#2-arquitectura)
3. [Componentes/Servicios](#3-componentesservicios)
4. [Flujos de negocio](#4-flujos-de-negocio)
5. [Reglas de negocio selladas](#5-reglas-de-negocio-selladas)
6. [Archivos clave](#6-archivos-clave)
7. [Problemas conocidos y soluciones](#7-problemas-conocidos-y-soluciones)
8. [Comandos útiles](#8-comandos-útiles)
9. [Bugs resueltos — NO volver a introducir](#9-bugs-resueltos--no-volver-a-introducir)
10. [Estado actual](#10-estado-actual)
11. [Diario de cambios](#11-diario-de-cambios)

---

## 1. ¿Qué es este sistema?

**GONCLOUD Bridge** es una integración bidireccional entre los marketplaces
MercadoLibre y Amazon con Odoo 17 ERP.

- **Inbound:** Órdenes de MeLi/Amazon → crea Sale Orders + facturas pagadas en Odoo
- **Outbound:** Cambios de stock en Odoo → actualiza listings en MeLi/Amazon
- **Servidor:** VPS Hetzner — alias SSH `gonserver` o `goncloud` (user `root`, IP pública `65.109.4.81`, Tailscale `100.127.167.103`). Migrado del servidor LAN viejo (192.168.0.200, user `gon`) el 2026-05-03.
- **Ingress — dos caminos distintos hacia `bridge-api`, no confundirlos:**
  1. **Webhooks** (`meli-webhooks.goncloud.cc`) → **Cloudflare Tunnel**, servicio del host `cloudflared.service`, config remota en el dashboard de Cloudflare (no hay `config.yml` en disco). Enruta a `http://localhost:8099`, así que el puerto **debe** publicarse en `127.0.0.1` (ver `ports` en `docker-compose.yml`). Ver reglas vivas: `journalctl -u cloudflared | grep originService`.
  2. **UI del mapper** (`mapper.goncloud.cc`) → nginx-proxy-manager, que requiere `bridge-api` en la red docker `proxy`.
  `meli.goncloud.cc` (lo que decía este archivo) es del servidor viejo y **no existe**.
- Un binding de puerto o una red que se pongan a mano se pierden en el siguiente recreate y **el inbound muere en silencio** — 19 días así desde 2026-08-24. Todo va declarado en el compose.
- **DB:** SQLite en `/mnt/data/appdata/bridge/data/bridge.db`
- **Repo:** `https://github.com/gon0801/goncloud-bridge-in-out.git`

---

## 2. Arquitectura

```
  MercadoLibre            Amazon SP-API
  (webhooks)           (polling cada 5 min)
       │                      │
       ▼                      ▼
┌─────────────────────────────────────────┐
│         bridge-api  (FastAPI :8099)      │
│  /webhooks/meli/orders/{secret}          │
│  /webhooks/amazon/orders/{secret}        │
│  /mapper  /amazon/mapper  /v1/settings   │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│             bridge-redis                 │
│  ml_orders_jobs · amazon_orders_jobs     │
│  stock_jobs                              │
└──────┬──────────────┬────────────────────┘
       │              │
       ▼              ▼
┌────────────┐  ┌──────────────────────┐
│ bridge-    │  │ bridge-amazon-       │
│ inbound-   │  │ inbound-worker       │
│ worker     │  │ (amazon_inbound_     │
│ (inbound_  │  │  worker.py v2.7)     │
│  worker.py │  │                      │
│  v8.4)     │  │  llama tools/ vía    │
│            │  │  run_tool()          │
└─────┬──────┘  └──────────┬───────────┘
      │                    │
      └──────────┬──────────┘
                 │  subprocess → tools/
                 ▼
┌─────────────────────────────────────────┐
│            Odoo 17 (JSON-RPC)            │
│  Sale Orders · Invoices · Pickings       │
└─────────────────────────────────────────┘
                 ▲
┌─────────────────────────────────────────┐
│  bridge-worker (outbound)               │
│  stock_jobs → MeLi + Amazon listings    │
└─────────────────────────────────────────┘
```

---

## 3. Componentes/Servicios

| Contenedor | Archivo principal | Rol | Puerto |
|------------|-------------------|-----|--------|
| `bridge-api` | `app/main.py` | FastAPI: webhooks, OAuth MeLi, setup, SKU mapper UI | 127.0.0.1:8099 |
| `bridge-redis` | — | Colas Redis para jobs inbound/outbound | interno |
| `bridge-inbound-worker` | `app/inbound_worker.py` | Procesa órdenes MercadoLibre (v8.4 "Payload-Persistent") | — |
| `bridge-amazon-inbound-worker` | `app/amazon_inbound_worker.py` | Procesa órdenes Amazon (v2.7 "Polish Pack") | — |
| `bridge-worker` | `app/worker.py` | Sincroniza stock outbound a MeLi y Amazon | — |

**Timers del host (systemd):**
- `meli-sync.timer` — outbound MeLi cada 5 min
- `amazon-poll.timer` — `amazon_orders_poll.py --days 2 --marketplace BOTH` cada 5 min
- `amazon-prices-sync.timer` — `amazon_prices_sync.py` (precios + FBA inventory MX/US) cada 6h (00:35, 06:35, 12:35, 18:35 UTC)

**Cron del host (user crontab):**
- `0 */4 * * *` — `backfill_meli_mappings.py` — descubre listings nuevos/post-split de MeLi y actualiza `sku_mapping`. Log: `/mnt/data/appdata/bridge/data/backfill.log`. Corre cada 4h.
- `5 */6 * * *` (en `/etc/cron.d/goncloud_meli_refresh`) — `meli_refresh_tokens.sh` — refresca el access_token de MeLi.

---

## 4. Flujos de negocio

### MercadoLibre Inbound

| Tipo | Detección | Acción Odoo | Prefijo SO |
|------|-----------|-------------|------------|
| **FULL paid** | `logistic_type=fulfillment` + `status=paid` | SO + Invoice pagada (sin picking) | `MLFULL` |
| **FULL cancelled/refunded** | `logistic_type=fulfillment` + `status∈{cancelled,refunded}` | Credit Note + cancel SO | `MLFULL` |
| **FULL returned** | `logistic_type=fulfillment` + `status=returned` | **NO tocar** (esperar `refunded`) | — |
| **FBM paid** | `logistic_type!=fulfillment` + `status=paid` | SO + Picking + Invoice pagada | `MLFBM` |
| **FBM cancelled** | `logistic_type!=fulfillment` + `status=cancelled` | Credit Note + cancel picking | `MLFBM` |

### Amazon Inbound

| Tipo | Detección | Acción Odoo | Ref interna |
|------|-----------|-------------|-------------|
| **FBA paid** | `FulfillmentChannel=AFN` + `OrderStatus=Shipped` | SO + Invoice pagada (sin picking) | `AMZFBA:mkt:id` |
| **FBA cancelled** | `FulfillmentChannel=AFN` + `OrderStatus=Canceled` | Credit Note | `AMZFBA:mkt:id` |
| **FBM paid** | `FulfillmentChannel=MFN` + `OrderStatus=Unshipped/Shipped` | SO + Picking + Invoice pagada | `AMZFBM:mkt:id` |
| **FBM cancelled** | `FulfillmentChannel=MFN` + `OrderStatus=Canceled` | Credit Note + cancel picking | `AMZFBM:mkt:id` |
| **Flex MX paid** | `AFN` + `Marketplace=MX` + `OrderStatus=Pending` | SO + Picking + Invoice pagada | `AMZFBM:MX:id` |

### Amazon — Perfiles por marketplace

| Marketplace | ID | Profiles |
|-------------|-----|----------|
| Mexico | `A1AM78C64UM0Y8` | `FBA_MX`, `FLEX_MX`, `FBM_MX`, `EASY_MX` |
| USA | `ATVPDKIKX0DER` | `FBA_US`, `FBM_US` |

### Amazon Flex MX — LÓGICA ESPECIAL

```
Flex MX = AFN + Marketplace MX (A1AM78C64UM0Y8)
Llegan en status "Pending" y DEBEN procesarse inmediatamente como "paid"
(generan picking como FBM, NO como FBA).

Worker detecta:
  if profile == "FLEX_MX" and status == "Pending": action = "paid"

El poll NO debe saltear Pending si is_flex_mx == True.
```

### Formato de campos en Odoo (TODOS los canales)

```
client_order_ref  =  "numero_pedido | nombre_comprador"
   Ej Amazon:  "701-4611535-8534600 | Juan García"
   Ej MeLi:    "2000011682284817 | COMPRADOR123"

note del SO  =  "{CANAL} | ORDER={id} | {comprador}"
   Ej:  "Amazon FBA | ORDER=701-4611535-8534600 | Juan García"
```

---

## 5. Reglas de negocio selladas

### FULL MeLi — Canónico (ANEXO C — NO modificar sin nueva versión)

```
PRINCIPIO: SOLO neutralizar contabilidad con señal explícita de REFUND.
"returned" por sí solo NO garantiza refund → NO tocar contabilidad.

Ejecutar refund/cancel si status IN {cancelled, canceled, refunded}
NO ejecutar si status IN {returned, returning, to_be_returned}
```

### Deduplicación

```
is_already_completed: bloquear SOLO si result IN {success, dead}
manual_review y error son RETRYABLES (auto-curación del sistema)
```

### Resolución de SKU

```
Amazon:
  1. product_product.default_code = SellerSKU
  2. amazon_sku_mapping (mapeo manual para SKUs legacy)

MeLi:
  1. seller_sku del item directamente
  2. sku_mapping table
  3. inbound_allowed_skus (auto-sync desde sku_mapping)
```

### Precios Amazon

```
price_unit = Sales Proceeds COMPLETOS (NO solo ItemPrice):
  ItemPrice + ItemTax + ShippingPrice + ShippingTax + GiftWrapPrice + GiftWrapTax

Amazon US (USD): convertir a MXN usando res.currency.rate de Odoo
  (WHERE currency_id.name = 'USD', registro más reciente)
```

### Poll Amazon timestamp

```
Formato: ISO 8601 con "Z" al final → strftime('%Y-%m-%dT%H:%M:%SZ')
Sin "Z" → Amazon US devuelve HTTP 400
```

### Prioridad de búsqueda de tools (CRÍTICO — no invertir)

```
El worker busca scripts en este orden:
  1. /data/{tool}.py                          ← deployment target (SIEMPRE primero)
  2. /mnt/data/appdata/bridge/tools/{tool}.py ← fallback

Si el orden se invierte, los fixes deploiados a /data/ se ignoran.
Esto causó el bug recurrente de client_order_ref (resuelto 2026-02-23).
```

### Flujo al agregar nuevo SKU mapping

```
1. Agregar en /amazon/mapper o /mapper
2. INMEDIATAMENTE ejecutar recover_manual_review.py para re-intentar atascadas
3. Verificar que procesó
```

---

## 6. Archivos clave

```
/mnt/data/appdata/bridge/
├── app/                              ← baked en imagen Docker / montado por volumen
│   ├── main.py                       # FastAPI: webhooks, OAuth, mapper UI, settings
│   ├── inbound_worker.py             # Worker MeLi inbound v8.4
│   ├── amazon_inbound_worker.py      # Worker Amazon inbound v2.7
│   ├── worker.py                     # Worker outbound stock sync
│   ├── amazon_fba_paid_one_shot.py   # Versión app/ del FBA tool (gate2 fallback)
│   ├── db_init.py                    # Schema SQLite
│   └── Dockerfile.*
│
├── tools/                            ← fallback path del worker
│   ├── amazon_fba_paid_one_shot.py   # Tool FBA: SO + Invoice + Pago
│   ├── amazon_fbm_paid_one_shot.py   # Tool FBM: SO + Picking + Invoice + Pago
│   ├── amazon_fba_refund_and_cancel.py
│   ├── amazon_fbm_refund_and_cancel.py
│   ├── inbound_full_paid_one_shot_no_stock.py  # MeLi FULL paid (sin picking)
│   ├── inbound_fbm_so_apply_paid_one_shot.py   # MeLi FBM paid (con picking)
│   ├── inbound_full_so_refund_and_cancel.py
│   ├── inbound_fbm_so_refund_and_cancel.py
│   ├── amazon_orders_poll.py         # Polling SP-API → Redis
│   ├── recover_manual_review.py      # Re-encolar órdenes atascadas
│   └── diagnose_inbound.py           # Diagnóstico completo
│
├── data/                             ← /data/ en contenedor (persistent volume)
│   ├── bridge.db                     # SQLite principal
│   ├── .meli_tokens.json             # Tokens OAuth MeLi
│   └── *.py                          ← tools copiados aquí tienen prioridad
│
└── docs/
    ├── RUNBOOK.md
    └── ANEXO_C_FULL_ML_STATUS.md     # Reglas FULL MeLi (selladas)
```

### Scripts que DEBEN estar en `/data/` — copiar tras cualquier fix

```bash
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_refund_and_cancel.py        /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_refund_and_cancel.py        /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_full_paid_one_shot_no_stock.py /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_fbm_so_apply_paid_one_shot.py  /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_full_so_refund_and_cancel.py   /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_fbm_so_refund_and_cancel.py    /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_orders_poll.py                  /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/recover_manual_review.py               /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/diagnose_inbound.py                    /mnt/data/appdata/bridge/data/
# Si se modificó el worker:
sudo cp /tmp/goncloud-bridge-in-out/app/inbound_worker.py /mnt/data/appdata/bridge/app/
sudo docker restart bridge-inbound-worker
```

---

## 7. Problemas conocidos y soluciones

### PROBLEMA 1: `client_order_ref` muestra formato bruto (`AMZFBM:mkt:id`)

**Causa raíz (resuelta 2026-02-23):** Worker buscaba script FULL paid en
`/mnt/.../tools/` primero, ignorando el fix deploiado en `/data/`.

```bash
# Verificar orden correcto:
grep -n "tool_candidates" /mnt/data/appdata/bridge/app/inbound_worker.py
# Debe mostrar "/data/inbound_full_paid_one_shot_no_stock.py" PRIMERO

# Verificar display_ref en /data/:
grep "client_order_ref" /mnt/data/appdata/bridge/data/inbound_full_paid_one_shot_no_stock.py
# Debe mostrar: "client_order_ref": display_ref
```

### PROBLEMA 2: Órdenes Flex MX no entran (status Pending)

```bash
grep -A5 "FLEX_MX\|is_flex_mx" /mnt/data/appdata/bridge/data/amazon_orders_poll.py | head -20
# Debe ver que NO se saltea Pending si is_flex_mx == True

# Forzar recuperación:
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace MX
```

### PROBLEMA 3: Órdenes en `manual_review` por SKU faltante

**Síntoma:** `ERROR missing products for SKUs: ['XX-XXXX']`

```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT seller_sku, odoo_default_code FROM amazon_sku_mapping WHERE seller_sku='XX-XXXX';"

# Agregar mapping en /amazon/mapper, luego:
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead
```

### PROBLEMA 4: Poll Amazon falla con "timestamp must follow ISO8601"

```bash
grep "strftime\|isoformat" /mnt/data/appdata/bridge/data/amazon_orders_poll.py
# Debe mostrar strftime('%Y-%m-%dT%H:%M:%SZ'), NO .isoformat()
```

### PROBLEMA 6: Órdenes Amazon US en `manual_review` — "No se pudo obtener tipo de cambio"

**Causa raíz:** `res.currency.rate` en Odoo está vacío. El cron `sync_fx_rates.py`
escribe en `accounting.db` pero no en Odoo. Los tools leen SOLO de Odoo.

```bash
# Verificar que Odoo tiene rates:
sudo docker exec bridge-amazon-inbound-worker python3 -c "
import requests, sqlite3
db = sqlite3.connect('/data/bridge.db')
def gs(k): return (db.execute('SELECT value FROM bridge_settings WHERE key=?',(k,)).fetchone() or [None])[0]
url,db_,user,pw = gs('odoo_url').rstrip('/'),gs('odoo_db'),gs('odoo_user'),gs('odoo_password')
def jcall(s,m,a):
    return requests.post(url+'/jsonrpc',json={'jsonrpc':'2.0','method':'call','params':{'service':s,'method':m,'args':a},'id':1},timeout=30).json().get('result')
uid = jcall('common','authenticate',[db_,user,pw,{}])
rates = jcall('object','execute_kw',[db_,uid,pw,'res.currency.rate','search_read',
    [[['currency_id.name','=','USD']]],{'fields':['rate','name'],'order':'name desc','limit':3}])
print('rates:', rates)
"

# Backfill (correr en gonserver, no en Docker):
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py 2026-02-23

# Verificar accounting.db para fechas disponibles:
sqlite3 /mnt/data/appdata/accounting/data/accounting.db \
  "SELECT rate_date, rate FROM currency_rates WHERE base_currency='MXN' AND quote_currency='USD' ORDER BY rate_date DESC LIMIT 7;"
```

**Deploy del fix (una vez):**
```bash
# 1. Copiar push_fx_to_odoo.py al servidor
sudo cp /tmp/goncloud-bridge-in-out/tools/push_fx_to_odoo.py /mnt/data/appdata/bridge/data/

# 2. Backfill con la fecha más reciente disponible en accounting.db
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py

# 3. Actualizar sync_fx_rates.py para que llame push_fx_to_odoo.py diariamente
# (ver sección "Tipo de cambio USD/MXN" en sección 8)
```

### PROBLEMA 7: Flex MX — factura en $0 (pedido 702-9477496-5819444 y similares)

**Causa raíz:** En status `Pending`, Amazon SP-API no devuelve `BuyerName`. La tool
usaba `client_order_ref = order_id` al crear el SO. En status `Unshipped`, con buyer
disponible, el lookup buscaba `order_id | buyer` → no encontraba el SO → creaba uno
nuevo sin picking, o bien creaba la factura con $0.

**Fix deploiado:** `amazon_fbm_paid_one_shot.py` ahora busca con dominio `|` (ambos refs).

**Recovery para ordenes ya afectadas:**
```bash
# 1. En Odoo: buscar los SOs de la orden
#    - Hay probable un SO con picking y precio $0
#    - Hay probable un SO sin picking y factura $0 (o precio correcto con factura pagada)
#
# 2. Opción A — Fix manual en Odoo (recomendado si la factura correcta ya existe):
#    a) Cancelar y eliminar el SO huérfano ($0, sin factura útil)
#    b) Si la factura del segundo SO tiene precio correcto y está pagada: listo
#    c) Si la factura está en $0: cancelar factura, ajustar precio en SO, crear nueva factura
#
# 3. Opción B — Reset completo y reprocesar:
# En Odoo: cancelar y eliminar TODOS los SOs/facturas de la orden
# En bridge.db: borrar auditoría para que el poll la reprocese
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM amazon_processed_events WHERE dedupe_key LIKE '%702-9477496-5819444%';"
# Luego forzar poll (el fix ya está activo):
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 7 --marketplace MX
```

### PROBLEMA 8: Amazon US — precios sin convertir a MXN (ej. 84.41 USD queda como 84.41 MXN)

**Causa raíz:** La versión en `/data/amazon_fba_paid_one_shot.py` del servidor es OLD (no tiene `IS_USD_ORDER`) o es la versión de `app/` (que tenía `IS_USD_ORDER` sin definir → NameError).

**Diagnóstico rápido en el servidor:**
```bash
# 1. Ver qué versión está en /data/
sudo grep -n "IS_USD_ORDER" /mnt/data/appdata/bridge/data/amazon_fba_paid_one_shot.py
# Debe mostrar: IS_USD_ORDER = os.getenv("IS_USD_ORDER", "0") == "1"
# Si no muestra nada → versión vieja sin conversión

# 2. Ver qué versión está en tools/ del host
sudo grep -n "IS_USD_ORDER" /mnt/data/appdata/bridge/tools/amazon_fba_paid_one_shot.py

# 3. Verificar qué registró el worker para la orden
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT dedupe_key, result, detail_json FROM amazon_processed_events
   WHERE dedupe_key LIKE '%114-6204816-4453067%';"

# 4. Verificar que Odoo tiene rate USD
sudo docker exec bridge-amazon-inbound-worker python3 -c "
import requests, sqlite3
db = sqlite3.connect('/data/bridge.db')
def gs(k): return (db.execute('SELECT value FROM bridge_settings WHERE key=?',(k,)).fetchone() or [None])[0]
url,db_,user,pw = gs('odoo_url').rstrip('/'),gs('odoo_db'),gs('odoo_user'),gs('odoo_password')
def jcall(s,m,a):
    return requests.post(url+'/jsonrpc',json={'jsonrpc':'2.0','method':'call','params':{'service':s,'method':m,'args':a},'id':1},timeout=30).json().get('result')
uid = jcall('common','authenticate',[db_,user,pw,{}])
rates = jcall('object','execute_kw',[db_,uid,pw,'res.currency.rate','search_read',
    [[['currency_id.name','=','USD']]],{'fields':['rate','name'],'order':'name desc','limit':3}])
for r in (rates or []): print(r['name'], round(1.0/r['rate'],4), 'MXN/USD')
"
```

**Fix (siempre deployar desde tools/, nunca desde app/):**
```bash
cd /tmp/goncloud-bridge-in-out && sudo git fetch && sudo git pull
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_paid_one_shot.py  /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_paid_one_shot.py  /mnt/data/appdata/bridge/data/
# Verificar:
sudo grep "IS_USD_ORDER" /mnt/data/appdata/bridge/data/amazon_fba_paid_one_shot.py
```

**Recovery para orden 114-6204816-4453067 (precio mal registrado):**
```bash
# En Odoo: cancelar y eliminar el SO/factura con precio en USD (84.41 MXN incorrecto)
# En bridge.db: borrar auditoría para que se reprocese automáticamente
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM amazon_processed_events WHERE dedupe_key LIKE '%114-6204816-4453067%';"
# Forzar poll:
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 7 --marketplace BOTH
```

### PROBLEMA 9: MeLi outbound — 1 SKU en N listings (split de variantes)

**Síntoma:** Stock de MeLi diverge del de Odoo. Un SKU tiene múltiples listings (MLM IDs distintos) en el seller, pero `sku_mapping` solo contiene uno.

**Diagnóstico:**
```bash
# Ver mappings del SKU problemático
sudo sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT channel, sku, remote_item_id, remote_variation_id, last_seen_at
   FROM sku_mapping WHERE channel='meli' AND sku='SKU-A-REVISAR';"

# Comparar con stock real Odoo vs suma de stocks en MeLi
# (ver script sync_split_variants_stock.py para modo automatizado)
```

**Fix:**

Si el schema tiene PK vieja `(channel, sku)`, migrar primero:
```bash
sudo docker exec bridge-api python3 /data/migrate_sku_mapping_1n.py
```

Después, descubrir todos los listings activos del seller:
```bash
sudo docker exec bridge-api python3 /data/backfill_meli_mappings.py
```

Fix manual de un SKU específico (emergencia):
```bash
sudo docker exec bridge-api python3 /data/sync_split_variants_stock.py \
  --sku NH-CAR-AZU-CEN-DOR \
  --listings MLM5164542984,MLM5164542986,MLM5164542988,MLM5164542990,MLM5164542992,MLM5164542994
```

Después de cualquiera de los anteriores, forzar re-sync outbound encolando stock_jobs o esperando al siguiente tick del `meli-sync.timer`.

### PROBLEMA 10: Órdenes no entran a Odoo — `authentication_failed` tras cambio de contraseña

**Síntoma:** Worker procesa órdenes pero todas terminan en `dead` con `[FBM_PAID] ERROR authentication_failed` (o FBA/FULL). Las colas Redis se llenan de dead.

**Causa raíz:** `amazon_inbound_worker.py` y `inbound_worker.py` leen las credenciales de Odoo (`odoo_url`, `odoo_password`) **una sola vez al arrancar** en `init_db()` y las guardan en `Config`. Si se cambia la contraseña en Odoo sin actualizar `bridge_settings` y reiniciar los workers, el worker sigue usando las credenciales viejas indefinidamente.

**Procedimiento cuando cambias contraseña de Odoo:**
```bash
# 1. Actualizar bridge_settings con nueva URL y/o contraseña
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
UPDATE bridge_settings SET value='http://NUEVA_URL:PUERTO', updated_at=datetime('now') WHERE key='odoo_url';
UPDATE bridge_settings SET value='NUEVA_PASSWORD', updated_at=datetime('now') WHERE key='odoo_password';
SELECT key, value FROM bridge_settings WHERE key IN ('odoo_url','odoo_password');
"

# 2. Verificar que las credenciales funcionan desde el container:
docker exec bridge-amazon-inbound-worker python3 -c "
import requests, sqlite3
conn = sqlite3.connect('/data/bridge.db')
url = conn.execute(\"SELECT value FROM bridge_settings WHERE key='odoo_url'\").fetchone()[0]
user = conn.execute(\"SELECT value FROM bridge_settings WHERE key='odoo_user'\").fetchone()[0]
pw = conn.execute(\"SELECT value FROM bridge_settings WHERE key='odoo_password'\").fetchone()[0]
db_ = conn.execute(\"SELECT value FROM bridge_settings WHERE key='odoo_db'\").fetchone()[0]
resp = requests.post(url+'/jsonrpc', json={'jsonrpc':'2.0','method':'call','id':1,'params':{'service':'common','method':'authenticate','args':[db_,user,pw,{}]}}, timeout=10)
print('uid:', resp.json().get('result'))
"

# 3. Reiniciar AMBOS workers para que lean las nuevas credenciales
docker restart bridge-amazon-inbound-worker bridge-inbound-worker

# 4. Reencolar órdenes que murieron durante el outage
docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead

# 5. Poll de recuperación para cubrir el gap completo
docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace BOTH
```

**Ocurrió:** 2026-05-04. Contraseña cambiada el 2026-05-03 en Odoo. URL también cambió de `http://odoo-odoo-1:8069` a `http://65.109.4.81:8082`. ~22h de outage, ~21 órdenes recuperadas.

---

### PROBLEMA 5: MeLi `no_valid_items` / órdenes en `manual_review`

```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, processed_at, result, detail_json
FROM processed_inbound_events
WHERE result IN ('manual_review','dead')
  AND processed_at >= datetime('now','-48 hours')
ORDER BY processed_at DESC;"
```

> Órdenes MeLi con `rawsha:` como dedupe_key NO tienen payload persistido.
> Para recuperarlas: re-enviar el webhook desde MeLi.

---

## 8. Comandos útiles

### Estado y logs

```bash
sudo docker ps | grep bridge
sudo docker logs bridge-inbound-worker --tail 50 -f
sudo docker logs bridge-amazon-inbound-worker --tail 50 -f
sudo docker logs bridge-api --tail 50 -f
sudo docker restart bridge-inbound-worker
sudo docker restart bridge-amazon-inbound-worker
```

### Diagnóstico completo

```bash
sudo docker exec bridge-inbound-worker python3 /data/diagnose_inbound.py --hours 48
```

### Colas Redis

```bash
sudo docker exec bridge-inbound-worker python3 -c "
import redis; r = redis.Redis(host='bridge-redis', port=6379, decode_responses=True)
for q in ['amazon_orders_jobs','amazon_orders_dead','ml_orders_jobs','ml_orders_dead']:
    print(q, r.llen(q))"
```

### DB — Consultas frecuentes

```bash
# Resultados últimas 72h (Amazon)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT result, COUNT(*) FROM amazon_processed_events
   WHERE processed_at >= datetime('now','-72 hours') GROUP BY result;"

# Buscar orden (Amazon)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT dedupe_key, processed_at, result, detail_json
   FROM amazon_processed_events WHERE dedupe_key LIKE '%ORDER-ID%';"

# Buscar orden (MeLi)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT dedupe_key, result, processed_at FROM processed_inbound_events
   WHERE dedupe_key LIKE '%ORDER-ID%';"

# Ver todos los flags
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT key, value FROM bridge_settings ORDER BY key;"

# Reprocesar orden (borrar audit para re-inyectar)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM amazon_processed_events WHERE dedupe_key='ORDEN_ID';"
```

### Reprocesar órdenes atascadas

```bash
# Dry-run primero
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead --dry-run
# Sin dry-run para ejecutar
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead

# Polling manual (últimos 2 días, ambos marketplaces)
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace BOTH
```

### Tipo de cambio USD/MXN

```bash
# Ver rates en Odoo (deben existir):
sqlite3 /mnt/data/appdata/accounting/data/accounting.db \
  "SELECT rate_date, rate FROM currency_rates WHERE base_currency='MXN' AND quote_currency='USD' ORDER BY rate_date DESC LIMIT 5;"

# Push manual a Odoo (última tasa disponible):
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py

# Push manual a Odoo (fecha específica):
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py 2026-02-23

# Verificar en Odoo que llegó:
sudo docker exec bridge-amazon-inbound-worker python3 -c "
import requests, sqlite3
db = sqlite3.connect('/data/bridge.db')
def gs(k): return (db.execute('SELECT value FROM bridge_settings WHERE key=?',(k,)).fetchone() or [None])[0]
url,db_,user,pw = gs('odoo_url').rstrip('/'),gs('odoo_db'),gs('odoo_user'),gs('odoo_password')
def jcall(s,m,a):
    return requests.post(url+'/jsonrpc',json={'jsonrpc':'2.0','method':'call','params':{'service':s,'method':m,'args':a},'id':1},timeout=30).json().get('result')
uid = jcall('common','authenticate',[db_,user,pw,{}])
rates = jcall('object','execute_kw',[db_,uid,pw,'res.currency.rate','search_read',
    [[['currency_id.name','=','USD']]],{'fields':['rate','name'],'order':'name desc','limit':3}])
for r in (rates or []): print(r['name'], round(1.0/r['rate'],4), 'MXN/USD')
"
```

**Cron actualizado en `/mnt/data/appdata/accounting/scripts/sync_fx_rates.py`:**
Debe llamar a `push_fx_to_odoo.py` al final del `sync_fx()`. Ver sección 7 PROBLEMA 6 para instrucciones de deploy.

### Deploy desde repo

```bash
# Paso 0: clonar si no existe (Linux borra /tmp en reinicios)
if [ ! -d /tmp/goncloud-bridge-in-out ]; then
  sudo git clone https://github.com/gon0801/goncloud-bridge-in-out.git /tmp/goncloud-bridge-in-out
else
  cd /tmp/goncloud-bridge-in-out && sudo git fetch && sudo git pull
fi

# Paso 1: copiar tools al runtime (/data/)
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_full_paid_one_shot_no_stock.py /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_fbm_so_apply_paid_one_shot.py  /mnt/data/appdata/bridge/data/

# Paso 2 (solo si cambió el worker): copiar a app/ y restart
sudo cp /tmp/goncloud-bridge-in-out/app/inbound_worker.py /mnt/data/appdata/bridge/app/
sudo docker restart bridge-inbound-worker

# Paso 3 (solo si cambió meli_refresh_tokens.sh): copiar al path del cron
sudo cp /tmp/goncloud-bridge-in-out/tools/meli_refresh_tokens.sh /mnt/data/appdata/bridge/tools/
sudo chmod +x /mnt/data/appdata/bridge/tools/meli_refresh_tokens.sh
```

---

## 9. Bugs resueltos — NO volver a introducir

| Fecha | Bug | Fix |
|-------|-----|-----|
| 2026-02-20 | `is_already_completed` bloqueaba `manual_review` y `dead` | Solo bloquear `success` y `dead` definitivos |
| 2026-02-20 | 3 bugs críticos en path webhook Amazon inbound | Corregidos en `main.py` y `tools/worker.py` |
| 2026-02-20 | Imports faltantes (`logging`, `urllib.parse`) | Agregados |
| 2026-02-21 | Poll Amazon USA devolvía HTTP 400 | Timestamp con `Z` → `strftime('%Y-%m-%dT%H:%M:%SZ')` |
| 2026-02-21 | `manual_review`/`dead` no se reintentaban | Hacerlos retryables (auto-curación) |
| 2026-02-21 | Tool errors → `RC=2` (deferred) en lugar de `RC=1` (manual_review) | Usar `RC=1` |
| 2026-02-21 | Nota SO Amazon FBA sin nombre del comprador | `BUYER_NAME` en env del tool |
| 2026-02-22 | MeLi: `parse_items` no leía `seller_sku` directo | Leer `seller_sku` antes del mapping |
| 2026-02-22 | Amazon: SKUs legacy no mapeaban | Aplicar `amazon_sku_mapping` en worker y tools |
| 2026-02-22 | `sku_mapping` no sincronizaba a `inbound_allowed_skus` | Auto-sync al guardar via API |
| 2026-02-22 | Amazon US: precio en USD sin convertir | Convertir USD→MXN con tipo de cambio Odoo |
| 2026-02-22 | `price_unit` Amazon incompleto (solo ItemPrice) | Sales Proceeds = ItemPrice+Tax+Shipping+GiftWrap |
| 2026-02-23 | `client_order_ref` mostraba `AMZFBM:mkt:id` | Tools usan `display_ref = f"{order_id} \| {buyer}"` |
| 2026-02-23 | Mismo bug en MeLi (`MLFBM:MLM:id`, `MLFULL:MLM:id`) | `display_ref` con `order_id \| buyer_nick` |
| 2026-02-23 | **ROOT CAUSE bug recurrente:** worker FULL paid buscaba tool en `/mnt/.../tools/` primero | Invertir `tool_candidates`: `/data/` siempre primero |
| 2026-02-23 | FULL refund con path hardcodeado sin fallback a `/data/` | `_refund_candidates` list con `/data/` primero |
| 2026-02-23 | Flex MX: poll salteaba órdenes `Pending` | Poll detecta `is_flex_mx` y no saltea |
| 2026-02-23 | Poll con `--days 1` dejaba gap `Pending→Unshipped` | Cambiar a `--days 2` |
| 2026-02-24 | `res.currency.rate` vacío → órdenes Amazon US fallan por conversión USD→MXN | `push_fx_to_odoo.py` escribe el rate en Odoo; cron `sync_fx_rates.py` actualizado para llamarlo |
| 2026-02-24 | Flex MX: factura se crea en $0 — el job de Unshipped no encontraba el SO del Pending | `amazon_fbm_paid_one_shot.py`: búsqueda por `display_ref OR order_id`; si encontrado sin buyer_name, actualiza `client_order_ref` |
| 2026-02-25 | `app/amazon_fba_paid_one_shot.py` desincronizada con `tools/`: `IS_USD_ORDER` no definido (NameError), solo usaba `ItemPrice.Amount` (sin Sales Proceeds), factura via wizard (produce $0 para FBA) | Sincronizar `app/` con `tools/` — siempre deployar desde `tools/` a `/data/` |
| 2026-02-25 | `IS_USD_ORDER` basado en marketplace ID (frágil) — si el flag no llega, 84 USD entra como 84 MXN silenciosamente | Tools leen `order["OrderTotal"]["CurrencyCode"]` como fuente de verdad; env flag es fallback |
| 2026-04-18 | `sku_mapping` con PK `(channel, sku)` solo permitía 1 listing por SKU. Tras "separación de variantes" de MeLi, 1 SKU queda en N listings y solo 1 recibía sync de stock → oversell potencial. Caso encontrado: SKU `NH-CAR-AZU-CEN-DOR` con stock Odoo=190, suma de 6 listings MeLi=289. | Schema migrado a PK `(channel, remote_item_id, remote_variation_id)` + worker con `fetchall()` + loop PUT. Script backfill para descubrir listings post-split. |

---

## 10. Estado actual

**Fecha de última actualización:** 2026-05-08 (auditoría de seguridad completa)
**Branch activo:** `main`
**Worker MeLi:** v8.4 "Payload-Persistent" (en `app/inbound_worker.py`, copiado de `inbound_worker_v8_4.py`)
**Worker Amazon:** v2.7 "Polish Pack"
**Servidor:** `goncloud` (Hetzner Ubuntu 24.04) — reemplazó a `gonserver` que cayó 2026-05-01.
**Containers activos del bridge:** `bridge-redis`, `bridge-api`, `bridge-worker`, `bridge-inbound-worker`, `bridge-amazon-inbound-worker` (los últimos 2 agregados al compose el 2026-05-02 — antes corrían fuera del compose).
**Systemd timers:** `meli-sync` c/10min (stock outbound), `amazon-sync` c/10min (stock outbound), `amazon-poll` c/5min (orders polling — creado 2026-05-02), `amazon-prices-sync` c/6h (precios + FBA inventory — creado 2026-05-02).

### Funcionando ✓
- MeLi inbound: FULL paid/cancel/refund, FBM paid/cancel/refund
- Amazon inbound: FBA paid/cancel, FBM paid/cancel, Flex MX Pending
- Outbound stock sync: MeLi y Amazon
- Setup wizard, SKU mapper UI (`/mapper`, `/amazon/mapper`)
- Auto-curación: `manual_review`/`dead` son retryables
- Conversión USD→MXN para Amazon US (via `push_fx_to_odoo.py` → `res.currency.rate`)
- `client_order_ref` limpio: `orden_id | comprador` en todos los canales
- Worker busca tools en `/data/` primero (fix definitivo del bug recurrente)
- Tipo de cambio USD/MXN se actualiza diariamente a las 8am vía cron
- **Detección de moneda por `OrderTotal.CurrencyCode`** (no por marketplace ID — fuente real de verdad)

### Funcionando (adicional) ✓
- Cancelaciones Amazon FBA/FBM: tools correctos en `/data/`, buscan SO con `like order_id`
- Cancelaciones sin SO (orden cancelada antes de ser pagada): RC=0 idempotente
- **MeLi OAuth auto-refresh** vía cron del host `/etc/cron.d/goncloud_meli_refresh` (cada 6h). Script: `tools/meli_refresh_tokens.sh` (versionado en el repo + copia en `/mnt/data/appdata/bridge/tools/`). Log: `/mnt/data/appdata/bridge/data/meli_token_refresh.log`. Hace backup antes de sobrescribir y rota el refresh_token con cada refresh (MeLi lo requiere).
- **Seguridad (auditoría 2026-05-08):** defusedxml monkey-patch, multi-stage Dockerfile, PKCE S256 OAuth, circuit breaker MeLi, health alerts dead-queue, X-Request-ID correlación, /v1/metrics endpoint, Redis SLOWLOG. Ver PENDIENTES.md para crons del host pendientes de configurar.

### Pendiente — backlog
Ver la sección **"Pendientes activos"** al inicio de este archivo — fuente de verdad única.

---

## 11. Diario de cambios

### 2026-05-08 — auditoría de seguridad completa (B411, D1.7, D1.8, D3.2, D4.2, D4.3, D4.6, D4.8, D4.9, D5.2–D5.8, D6.8–D6.10, D7.2, D7.3, D7.5, D7.6, SNS)
- **B411:** `defusedxml` monkey-patch en `app/main.py` (protege `xmlrpc.client` de XML bomb/injection).
- **D1.7:** Índice UNIQUE en `inbound_sales_orders(ml_order_id)` + limpieza idempotente de registros UNKNOWN huérfanos al init.
- **D1.8:** Columna `rawsha TEXT` en `inbound_events`; migration block con `try/except` para idempotencia.
- **D3.2:** `fastapi>=0.115.0`, `uvicorn[standard]>=0.30.0` — versiones con fixes de seguridad.
- **D4.2:** SNS signature verification con `certifi` — log WARNING si `SignatureVersion=1` (SHA1 legacy).
- **D4.3/D4.8:** Validaciones de settings vía API reforzadas.
- **D4.6:** `tools/meli_refresh_tokens.sh` — eliminado `chown gon:gon` (user inexistente en goncloud).
- **D4.9:** Circuit breaker MeLi: threshold 5 fallos → cooldown 120s, estado module-level.
- **D5.2:** Middleware `X-Request-ID` en todas las respuestas FastAPI (genera UUID si no viene en header).
- **D5.3:** Endpoint `/v1/metrics` (secret-gated) — métricas SQLite + Redis queue depths + resultados inbound 24h.
- **D5.4:** Health endpoint expone dead queues (ml >50, amazon >0 → `ok: false`).
- **D5.5:** Detección y log de fallos parciales en sync stock MeLi outbound.
- **D5.7:** Redis SLOWLOG activado en docker-compose (`--slowlog-log-slower-than 10000 --slowlog-max-len 128`).
- **D5.8:** `tools/docker_stats_log.sh` — script host para loggear `docker stats` c/5min con rotación 10k líneas.
- **D6.8:** `app/Dockerfile` convertido a multi-stage build (builder + final) — secrets/source no se bake en la imagen.
- **D6.9:** `tools/bridge_backup_offsite.sh` — sync offsite vía rclone (S3/B2/R2/GDrive). Cron sugerido: `30 3 * * *`.
- **D6.10:** `tools/bridge_restore_test.sh` — test mensual de restore: `PRAGMA integrity_check` + table count + row counts. Cron sugerido: `0 4 1 * *`.
- **D7.2:** Circuit breaker implementado en `ml_get()` (guard al entrar + try/except que incrementa contador).
- **D7.3:** Sentinel `.meli_reauth_required` escrito cuando `invalid_grant`; health lo detecta y devuelve `ok: false`.
- **D7.5:** PKCE S256 completo en OAuth MeLi: `code_verifier` en Redis, `code_challenge` en URL de auth.
- **D7.6:** OAuth callback rechaza con HTTP 422 si no viene `refresh_token`; borra sentinel `reauth_required` al éxito.
- **PENDIENTES de operación (no código):** configurar cron host para `docker_stats_log.sh`, `bridge_backup_offsite.sh`, `bridge_restore_test.sh`; configurar `rclone` con remote "bridge-offsite"; reconstruir imagen Docker para activar `fastapi>=0.115.0` y multi-stage build; reiniciar `bridge-redis` para activar SLOWLOG (hacerlo cuando queues estén vacías).

### 2026-05-04 — fix autenticación Odoo tras cambio de contraseña
- **BUG:** Todas las órdenes Amazon (y MeLi) fallaban con `authentication_failed`. ~22h de outage (desde 2026-05-03 21:16 UTC).
- **ROOT CAUSE:** La contraseña de Odoo se cambió el 2026-05-03. Los workers cachean las credenciales al arrancar en `init_db()` → no leen el cambio en bridge_settings hasta reiniciarse.
- **FIX:** Actualizado `bridge_settings` con nueva URL (`http://65.109.4.81:8082`) y contraseña. Restart de ambos workers. 21 órdenes muertas recuperadas con `recover_manual_review.py`. Poll de 2 días confirmó 0 gaps.
- **DOCS:** Agregado PROBLEMA 10 en CLAUDE.md con el procedimiento completo para futuros cambios de contraseña.
- **LECCIÓN:** Cuando cambias la contraseña de Odoo: (1) actualizar bridge_settings, (2) reiniciar workers, (3) recover dead orders.

### 2026-05-03 — docs/memoria post-migración
- **DOCS:** Actualizado en CLAUDE.md sección 1 + "Datos clave" de MASTER_RUNBOOK.md con datos del VPS Hetzner (IP pública `65.109.4.81`, Tailscale `100.127.167.103`, hostname `goncloud`, user `root`, Ubuntu 24.04.3 LTS, llave SSH cliente `~/.ssh/goncloud-mexico`). Sin cambios de código.
- **MEMORIA:** Guardado `memory/project_servidor_produccion.md` para que próximas sesiones reconozcan el VPS desde el primer mensaje.
- **PENDIENTES:** Agregada **Task 0** en `PENDIENTES.md` para verificar `bridge-*` containers en el VPS (la entrada del 2026-05-02 ya documenta que están deployados; falta validar el cron del backfill y el cron del meli-refresh sobre el nuevo host).
- Las entradas históricas del diario (referencias a `gon@gonserver` o `192.168.0.200` antes del 2026-05-02) NO se modifican — son fieles al momento de cada sesión.

### 2026-05-02 — recuperación del bridge tras migración gonserver→goncloud
- **Contexto:** El 2026-05-01/02 se migró el VPS de `gonserver` (caído) a `goncloud` (Hetzner Ubuntu 24.04). En la migración solo se trajo `bridge-worker` (stock) — los 2 inbound workers se levantaban en gonserver fuera del compose y nunca estuvieron versionados ahí. Resultado: webhooks MeLi entraban a `ml_orders_jobs` sin ser consumidos; Amazon ni siquiera se polleaba. **Última orden a Odoo: `S01321` 2026-05-01 02:48:27 UTC.**
- **Diagnóstico:** `ml_orders_processing` con 3,278 jobs stale del worker viejo (`worker=1@f813e734393b`, `started_at` hasta 2026-04-30T23:26:17 UTC). `amazon_orders_jobs` no existía. `inbound_metrics` confirmaba última actividad 2026-04-30 23h.
- **Fixes aplicados:**
  - `cp inbound_worker_v8_4.py app/inbound_worker.py` (raíz tenía v8.4 Payload-Persistent; `app/` tenía v3.2 Feb-12 desactualizada). Backup en `app/inbound_worker.py.bak.pre-v84.20260502`.
  - Agregado al `docker-compose.yml`: services `bridge-inbound-worker` (consume `ml_orders_jobs`) y `bridge-amazon-inbound-worker` (consume `amazon_orders_jobs`), ambos con `build: ./app` y networks `goncloud-net + odoo_odoo_net`. Backup compose en `docker-compose.yml.bak.pre-inbound-workers.20260502T1715Z`.
  - **`requirements.txt`:** agregado `httpx==0.27.0` — `amazon_orders_poll.py` lo importa pero faltaba (causó `ModuleNotFoundError` en primer run del poll).
  - **`run_amazon_poll.sh`:** corregido typo `bridge-inbound-worker` → `bridge-amazon-inbound-worker`.
  - **Systemd nuevos:** `/etc/systemd/system/amazon-poll.{service,timer}` (cada 5 min, llama `app/run_amazon_poll.sh`). Enabled + started.
  - `DEL ml_orders_processing` (3,286 stale entries — solo metadata observabilidad, no son jobs procesables; el worker hace RPUSH al iniciar pero no LREM al completar, crece sin tope, safe DEL periódico).
  - Sweep manual `amazon_orders_poll.py --days 7 --marketplace BOTH` para recuperar ventana de outage: 53 órdenes encontradas, 1 nueva pushed (52 ya procesadas por dedupe).
- **Verificación:** 7 órdenes nuevas en Odoo en 30 min post-fix (`S01322`–`S01328`, mezcla MeLi FBM + Amazon EASY_MX + FBM_US + FLEX_MX). Restart counts en 0.
- **Lección:** El `MASTER_RUNBOOK.md` sección 12 tiene la receta correcta de los 5 services; el compose vivo NO la reflejaba. Después de migrar, validar el compose vs runbook, no asumir que está completo.
- **Bonus fix (`accounting/app/dashboard.py`):** `float(fx_row.get('rate', 20.5))` → `float(fx_row.get('rate') or 20.5)` en líneas 540, 678, 975, 1218. El `default=20.5` no aplica si la key existe con valor None — causaba `TypeError: float() argument must be a string or a real number, not 'NoneType'`. Reportado en `goncloud-dashboard.service` logs.

### 2026-04-18 — sesión 5 (cron automation del backfill)
- **CRON AGREGADO:** `0 */4 * * * docker exec bridge-api python3 /data/backfill_meli_mappings.py >> /mnt/data/appdata/bridge/data/backfill.log 2>&1`
- Con esto, cualquier split/nuevo listing de MeLi se detecta en máximo 4h y los mappings se actualizan solos. El sync de stock del worker outbound hace el resto.
- **Validado:** corrida inicial detectó 8 mappings nuevos correctos (post-fix del PR #20). Total 321 mappings en `sku_mapping`. Aparecieron SKUs multi-listing adicionales: `NH-ITA-CEN-DOR` (3 listings), familias `NH-CAR-AZU-*-DOR` (2 c/u) y `SET-CAR-AZU-*-DOR` (2 c/u).
- Documentación de crons del host añadida a sección 3.

### 2026-04-18 — sesión 4 (fix backfill: leer SELLER_SKU attribute)
- **BUG DESCUBIERTO post-deploy:** `backfill_meli_mappings.py` leía `seller_custom_field` (campo legacy deprecated de MeLi). En listings post-split, MeLi copia el SKU del padre a `seller_custom_field` de todos los hijos, mientras que el SKU real de cada variante vive en `attributes[id=SELLER_SKU]`.
- **Consecuencia real:** durante la sesión 3, el backfill asumió que los 6 listings post-split del producto Arras Matrimoniales tenían el mismo SKU (`NH-CAR-AZU-CEN-DOR`) cuando en realidad cada uno tiene un SKU distinto: CEN-DOR (qty 190), VBU-DOR (89), COR-DOR (36), SET-SAN-DOR (60), SET-VCO-DOR (18), SET-PEZ-DOR (46). Al ejecutar el push de stock, todos quedaron en 190 → oversell temporal de ~700 unidades. Revertido inmediatamente con SKUs correctos (MLM echo confirmado en los 6 listings).
- **FIX:**
  - `tools/backfill_meli_mappings.py`: función `extract_item_sku()` y `extract_variation_sku()` con precedencia `attributes[SELLER_SKU]` → `seller_custom_field` → fallback. Query incluye `attributes` en el response.
  - `app/main.py` (`/api/meli/refresh-listings`): mismo fallback en el auto-discover para listings sin variaciones.
- **LECCIÓN:** MeLi tiene 2 campos para SKU — `seller_custom_field` (legacy, puede estar desfasado) y `attributes[SELLER_SKU]` (actual, fuente de verdad). Siempre leer primero el de atributos.
- Sin impacto actualmente: los SKUs reales ya fueron corregidos manualmente durante la sesión. El backfill arreglado evita que vuelva a pasar.

### 2026-04-18 — sesión 3 (fix outbound split variants)
- **BUG CRÍTICO encontrado:** MeLi "separa variantes" en listings independientes (feature reciente). El schema `sku_mapping` tenía PK `(channel, sku)` → solo permitía 1 listing por SKU → los otros N listings quedaban sin sync de stock.
- **Caso real:** SKU `NH-CAR-AZU-CEN-DOR`, mapping apuntaba a `MLM2163404350` (listing pre-split, 2026-02-10), pero en MeLi hoy hay 6 listings activos (`MLM5164542984..94`) con ese mismo SKU. Suma de stock MeLi=289 vs Odoo=190 → oversell potencial de 99 unidades.
- **FIX:**
  - Migración de schema: PK → `(channel, remote_item_id, remote_variation_id)` + índice `(channel, sku)` para lookup rápido — `tools/migrate_sku_mapping_1n.py` (idempotente, con backup).
  - Worker outbound ([app/worker.py](app/worker.py)): `fetchone()` → `fetchall()` + loop PUT con log por listing.
  - Backfill global ([tools/backfill_meli_mappings.py](tools/backfill_meli_mappings.py)): escanea todos los listings activos del seller y pobla mappings para todos los SKUs split.
  - Script urgente Fase 1 ([tools/sync_split_variants_stock.py](tools/sync_split_variants_stock.py)): para aplicar manualmente a un SKU específico.
  - CREATE TABLE sku_mapping agregado a [app/main.py:init_db](app/main.py) (antes solo existía en la DB, no en código).
- **Listings huérfanos identificados:** `MLM2787930515`, `MLM2787902225` sin SKU — requiere asignación manual en MeLi (agregado al backlog).
- Sin impacto en inbound (el SKU resuelve correctamente desde el webhook).

### 2026-04-18 — sesión 2
- **HALLAZGO:** MeLi OAuth refresh **SÍ es automático**. Encontrado cron en `/etc/cron.d/goncloud_meli_refresh` ejecutando `/mnt/data/appdata/bridge/tools/meli_refresh_tokens.sh` cada 6h (`5 */6 * * *`). Log muestra 29 refreshes `OK` consecutivos en últimos 7 días. Documentación previa ("no existe auto-refresh") estaba desactualizada.
- **FIX:** Versionado el script `meli_refresh_tokens.sh` en `tools/` del repo. Antes vivía solo en el servidor (si gonserver se rebuildeaba se perdía).
- **OBSERVACIÓN:** Hay un segundo cron en el user crontab (cada 4h) apuntando a `/mnt/data/appdata/accounting/scripts/refresh_meli_token.py` cuyo log `/var/log/meli_token_refresh.log` no existe → cron muerto, no aplica al bridge (vive en proyecto `accounting`).
- **BACKLOG:** Eliminado pendiente #4 "MeLi OAuth refresh automático" — ya resuelto en producción.
- Sin cambios de código del bridge.

### 2026-04-18
- **AVISO AMAZON:** Recibido email de Amazon Selling Partner API Services Team notificando deprecación de 6 operaciones del Orders API v0 con removal date **2027-03-27**. Hay que migrar a Orders API **v2026-01-01**.
- **HALLAZGO:** El bridge usa 3 de las 6 operaciones deprecadas — `getOrders`, `getOrder`, `getOrderItems` — en `tools/amazon_orders_poll.py`, `app/amazon_inbound_worker.py` y `app/debug_flex_order.py`. No usamos `getOrderBuyerInfo`, `getOrderAddress`, ni `getOrderItemsBuyerInfo`.
- **CORRECCIÓN:** "Amazon SP-API: verificación de cuenta pendiente" en backlog estaba desactualizado. SP-API está activo en producción desde hace semanas (evidencia: órdenes procesadas 114-6204816-4453067, 702-9477496-5819444, 701-4904380-4144244, 701-4611535-8534600, etc.).
- **BACKLOG:** Agregada la migración v0→v2026-01-01 como tarea con deadline.
- **CONTEXTO ODOO:** El usuario está configurando almacenes por canal en Odoo (EHV-MX, Meli-Full, FBA-MX, FBA-US) para que las ventas FULL/FBA descuenten del almacén correcto. Hoy las tools FBA/FULL paid NO generan picking. Tarea agregada al backlog.
- Sin cambios de código.

### 2026-02-27
- **INVESTIGACIÓN:** Revisión de la implementación del OAuth refresh token de MeLi.
- **HALLAZGO:** No existe ningún mecanismo automático de refresh. El `access_token` expira en ~6h (`expires_in=21600`). El refresh es manual vía `POST /oauth/refresh` (main.py:230).
- **HALLAZGO:** Los workers (`inbound_worker.py`, `worker.py`) leen el token del archivo en cada llamada pero no detectan 401 ni hacen retry automático.
- **PENDIENTE:** Implementar refresh automático (cron o background task en FastAPI).
- Sin cambios de código.

### 2026-02-25 — sesión 4
- **DIAGNÓSTICO:** Investigación de identificador `PkM9CcwfD` visto en orden Flex MX `701-4904380-4144244`.
- **CONCLUSIÓN:** El código NO existe en SP-API. Consultados `GetOrder` y `GetOrderItems`: los únicos IDs disponibles son `AmazonOrderId`, `OrderItemId`, `ASIN`, `SellerSKU`. `PkM9CcwfD` es un identificador interno del sistema logístico de Amazon Flex (app del repartidor), no expuesto por SP-API.
- **BuyerInfo** también vacía para esa orden — `client_order_ref` queda solo con `order_id`.
- Sin cambios de código. Sistema estable.

### 2026-02-25 — sesión 3
- **DIAGNÓSTICO:** Sesión de soporte sin cambios de código.
- **DLQ revisada:** 2 entradas `dead` en `amazon_processed_events`:
  - `701-5035844-5906656:Canceled` — `max_deferred_exceeded` (5 intentos)
  - `701-2734217-1461012:Canceled` — `max_deferred_exceeded` (5 intentos)
- **CONCLUSIÓN:** Benignos — ambas son órdenes Canceled de MX que llegaron sin SO previo en Odoo (se cancelaron antes de ser capturadas como Shipped/Pending). No hay contabilidad que revertir.
- **CONFIRMACIÓN estado post-PROBLEMA 8:** `114-6204816-4453067` reprocesada a $1,451.60 MXN ✓ · `111-2739896-7592244` procesada a $2,325.89 MXN ✓. Sistema estable.

### 2026-02-25 — sesión 2
- **FIX:** `IS_USD_ORDER` dependía del marketplace ID (`AMZ_MX_MARKETPLACE`), no de la moneda real del pedido.
- **ROOT CAUSE:** Si el marketplace llega mal clasificado o el flag no se setea correctamente, la conversión USD→MXN nunca ocurre y el precio entra como si fuera MXN (ej. 84 USD → 84 MXN).
- **FIX:** `tools/amazon_fba_paid_one_shot.py` y `tools/amazon_fbm_paid_one_shot.py` ahora leen `order["OrderTotal"]["CurrencyCode"]` como fuente de verdad después de parsear el ORDER_JSON. El env `IS_USD_ORDER` se mantiene como fallback para el caso webhook-sin-enrich donde `OrderTotal` puede no estar presente. `app/amazon_fba_paid_one_shot.py` sincronizada.
- **NUEVO COMPORTAMIENTO:** `[FBA_PAID] order currency=USD IS_USD_ORDER=True` en logs confirma que la moneda se detectó del JSON. Monedas no soportadas (no MXN/USD) causan `manual_review` con mensaje claro.

### 2026-02-25
- **INVESTIGACIÓN:** Orden Amazon US 114-6204816-4453067 registrada en Odoo a 84.41 MXN (sin conversión desde USD).
- **ROOT CAUSE identificado en repo:** `app/amazon_fba_paid_one_shot.py` tenía 3 bugs vs `tools/`:
  1. `IS_USD_ORDER` nunca definido → `NameError` en línea 225 para cualquier orden → si esta versión llega a `/data/`, crashea antes de crear el SO (manual_review)
  2. `parse_items()` solo usaba `ItemPrice.Amount` (ignoraba ItemTax, ShippingPrice, etc.)
  3. Factura via wizard `sale.advance.payment.inv` → factura $0 para FBA (pickings cancelados, delivered_qty=0)
- **FIX:** `app/amazon_fba_paid_one_shot.py` sincronizada completamente con `tools/amazon_fba_paid_one_shot.py`
- **CAUSA PROBABLE EN PRODUCCIÓN:** versión vieja de `tools/` (pre-IS_USD_ORDER) en `/mnt/.../tools/` del servidor, o `/data/` con versión incorrecta. Ver diagnóstico abajo.
- **DEPLOY REQUERIDO EN SERVER:** ver instrucciones en sección 7 PROBLEMA 8

### 2026-02-24 — sesión 3
- **DOCS:** Sesión de soporte sin cambios de código.
- **SOPORTE:** `scp gon@gonserver:/tmp/FIX_FLEX_MX_BUYER_NAME.md` fallaba porque el archivo fue creado en el entorno Claude, no en gonserver.
- **SOLUCIÓN:** Provisto comando `cat > ~/FIX_FLEX_MX_BUYER_NAME.md << 'ENDDOC'...` para crear el archivo directamente en gonserver, luego `scp root@100.127.167.103:~/FIX_FLEX_MX_BUYER_NAME.md ~/Desktop/` desde Mac.
- Sin cambios pendientes — sistema estable.

### 2026-02-24 — sesión 2
- **FIX:** Flex MX: `amazon_fbm_paid_one_shot.py` buscaba el SO existente SOLO con `client_order_ref = display_ref`.
- **ROOT CAUSE:** En status `Pending`, Amazon SP-API no devuelve `BuyerName` → `display_ref = order_id` (sin buyer). En status `Unshipped`, sí devuelve buyer → `display_ref = "order_id | buyer"`. Búsqueda exacta fallaba → creaba SO duplicado (sin picking, con factura $0 o con precio correcto pero picking ya creado en el SO de Pending).
- **FIX:** Búsqueda con dominio `| client_order_ref = display_ref OR client_order_ref = order_id`. Si encontrado por `order_id`-only y ahora hay buyer, actualiza el ref automáticamente.
- **ORDEN AFECTADA:** 702-9477496-5819444 — requiere fix manual (ver instrucciones abajo).

### 2026-02-24
- **FIX:** `res.currency.rate` en Odoo estaba vacío → órdenes Amazon US fallaban al intentar convertir USD→MXN.
- **ROOT CAUSE:** `sync_fx_rates.py` (cron 8am) escribía en `accounting.db` pero NUNCA en Odoo. Ambos sistemas existían sin conectarse.
- **FIX:** Nuevo `tools/push_fx_to_odoo.py` — lee la tasa de `accounting.db` y crea/actualiza `res.currency.rate` en Odoo.
- **DEPLOY REQUERIDO:** Actualizar `sync_fx_rates.py` en servidor (ver instrucciones abajo) y backfill manual con `push_fx_to_odoo.py`.

### 2026-02-23 — tercera parte
- **DOCS:** Sesión de revisión sin cambios de código.
- **DOCS:** Análisis completo del repo confirmó que CLAUDE.md cubre todo correctamente.
- Notas adicionales identificadas: `bridge_connector/` (módulo Odoo en repo), `docker-compose.yml` en raíz, directorio `hardening/`, `RECOVERY_CHECKLIST.txt`.
- Sin cambios pendientes — sistema estable.

### 2026-02-23 — segunda parte
- **FIX ROOT CAUSE bug recurrente:** `inbound_worker.py` buscaba
  `inbound_full_paid_one_shot_no_stock.py` en `/mnt/.../tools/` PRIMERO →
  ignoraba el fix deploiado en `/data/`. Invertido el orden (= igual que FBM y Amazon).
- **FIX:** FULL refund de path hardcodeado → `_refund_candidates` list con `/data/` primero.
- **DOCS:** CLAUDE.md reconstruido con template estándar de 12 secciones.
- **DOCS:** Lista completa y comandos de deploy actualizados en sección 6.

### 2026-02-23 — primera parte
- Fix: `app/amazon_fba_paid_one_shot` usa `display_ref` en lugar de `CLIENT_ORDER_REF`
- Fix: `app/amazon_fba_paid_one_shot` convierte USD→MXN y corrige `unit_price`
- Fix: `client_order_ref` limpia prefijos MeLi (MLFBM/MLFULL) con `display_ref`
- Fix: poll no saltea órdenes Flex MX en status `Pending`
- Fix: poll usa `--days 2` para cubrir gap `Pending→Unshipped`

### 2026-02-22
- Fix: `client_order_ref` en Odoo muestra `orden | comprador` (limpia prefijo AMZFBM)
- Fix: auto-sync `sku_mapping` → `inbound_allowed_skus`
- Fix: `price_unit` Amazon = Sales Proceeds completos
- Fix: conversión USD→MXN para Amazon US
- Fix: `amazon_sku_mapping` para SKUs legacy en worker y tools
- Fix: `parse_items` MeLi lee `seller_sku` directo del item

### 2026-02-21
- Fix: nota SO Amazon FBA incluye nombre del comprador
- Fix: nota SO MeLi muestra `#pack_id` visible
- Feat: `debug_meli_order.py` para diagnóstico de órdenes MeLi
- Fix: `manual_review` y `dead` son retryables para auto-curación
- Fix: poll timestamp usa `Z` (fix HTTP 400 Amazon US)
- Fix: tool errors → `RC=1` (manual_review) en lugar de RC=2

### 2026-02-20
- Fix: 3 bugs críticos en path webhook Amazon inbound
- Fix: imports faltantes (`logging`, `urllib.parse`)
- Fix: errores de copia en `main.py` y `tools/worker.py`
- Initial clean import del proyecto al repo

<!-- >>> QUALITY-KIT CALIDAD SECTION START -- managed by quality-kit's init-repo.ps1. Do not hand-edit between these markers; re-running init-repo.ps1 will refresh this block cleanly. -->
## Calidad (quality-kit)

Candados de commit instalados (pre-commit):
- base (limpieza de archivos)
- ruff (lint + formato Python)

Comandos para correr los candados a mano:
- `pre-commit run --all-files` (todos los candados de commit)

Reglas de hierro:
1. Si un candado falla, se arregla el problema real -- JAMAS se usa `--no-verify` ni se saltea un candado.
2. Cada bug arreglado incluye, en el mismo cambio, una prueba que lo habria atrapado.
<!-- >>> QUALITY-KIT CALIDAD SECTION END -->
