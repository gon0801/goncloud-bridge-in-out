# CLAUDE.md — GONCLOUD Bridge (Inbound/Outbound)

> **INSTRUCCIÓN PARA CLAUDE:** Al finalizar cualquier sesión donde se hicieron cambios
> relevantes, actualiza la sección "Diario de cambios" y el "Estado actual" de este
> archivo, luego haz commit + push. Así la próxima sesión arranca con contexto completo.
>
> **REGLA ABSOLUTA ANTES DE INVESTIGAR CUALQUIER FALLA:**
> 1. Leer el worker relevante (`amazon_inbound_worker.py`, `inbound_worker.py`) para entender lógica especial
> 2. Leer este CLAUDE.md completo — el problema probablemente ya está documentado
> 3. Consultar las tablas de DB con queries directos antes de hacer suposiciones
> 4. **NO adivinar.** Si la info está en el repo o en la DB, úsala primero.

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
- **Servidor:** `gonserver` · **URL pública:** `https://meli.goncloud.cc`
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

### PROBLEMA 8: Cancelaciones Amazon llegan como `dead` — SO no cancelado en Odoo

**Síntoma:** Orden cancelada en Amazon, SO sigue en estado `sale` en Odoo.
DB muestra: `result=dead`, `detail_json={"reason":"max_deferred_exceeded","deferred_count":5}`.

**Causa raíz (resuelta 2026-03-01):** Los tools `amazon_fba_refund_and_cancel.py` y
`amazon_fbm_refund_and_cancel.py` **nunca se copiaron a `/data/`**. El worker usa la
versión baked-in del contenedor (imagen vieja), que busca el SO con:
```python
[[["client_order_ref", "=", CLIENT_ORDER_REF]]]  # "AMZFBM:mkt:order_id"
```
Pero el SO en Odoo tiene `client_order_ref = "order_id"` (formato nuevo sin prefijo).
→ NO MATCH → RC=2 → deferred 5 veces → dead.

**Verificar:**
```bash
# Confirmar que el tool correcto está en /data/:
sudo docker exec bridge-amazon-inbound-worker grep -n "search_read" /data/amazon_fbm_refund_and_cancel.py
# Debe mostrar: [[["client_order_ref", "like", order_id]]]  (con "like", NO "=")
```

**Fix permanente:** Copiar ambos refund tools a `/data/` (ya incluidos en el bloque de deploy arriba).

**Recovery para orden afectada:**
```bash
# 1. Copiar tools correctos a /data/
cd /tmp/goncloud-bridge-in-out && sudo git fetch && sudo git pull
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_refund_and_cancel.py /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_refund_and_cancel.py /mnt/data/appdata/bridge/data/

# 2. Borrar el registro dead para permitir reprocesar
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM amazon_processed_events WHERE dedupe_key LIKE '%ORDER-ID%:Canceled';"

# 3. Forzar poll para que reencole la cancelación
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 7 --marketplace BOTH
```

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
cd /tmp/goncloud-bridge-in-out && sudo git fetch && sudo git pull
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_full_paid_one_shot_no_stock.py /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_fbm_so_apply_paid_one_shot.py  /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/app/inbound_worker.py                        /mnt/data/appdata/bridge/app/
sudo docker restart bridge-inbound-worker
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
| 2026-03-01 | Cancelaciones Amazon van a `dead` — SO nunca cancelado en Odoo | Refund tools nunca copiados a `/data/`; contenedor usaba versión vieja con `= CLIENT_ORDER_REF` en lugar de `like order_id`. Fix: desplegar `amazon_fba/fbm_refund_and_cancel.py` a `/data/` |

---

## 10. Estado actual

**Fecha de última actualización:** 2026-03-01
**Branch activo:** `claude/fix-odoo-order-cancellation-d8Jmk`
**Worker MeLi:** v8.4 "Payload-Persistent"
**Worker Amazon:** v2.7 "Polish Pack"

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

### Pendiente — DEPLOY REQUERIDO
- **Copiar refund tools a `/data/`** (ver sección 7 PROBLEMA 8 y bloque de deploy en sección 6)
  ```bash
  sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_refund_and_cancel.py /mnt/data/appdata/bridge/data/
  sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_refund_and_cancel.py /mnt/data/appdata/bridge/data/
  ```
- **Recovery orden 702-2278843-9196212:** ver PROBLEMA 8 para comandos

### Pendiente — backlog
- Amazon SP-API: verificación de cuenta pendiente (polling se habilita al aprobar)
- Limpieza periódica de `manual_review` antiguos (script existe, no automatizado)

---

## 11. Diario de cambios

### 2026-03-01
- **BUG:** Cancelaciones Amazon terminaban en `dead` con `max_deferred_exceeded` — SO nunca cancelado en Odoo.
- **ROOT CAUSE:** `amazon_fba_refund_and_cancel.py` y `amazon_fbm_refund_and_cancel.py` nunca fueron copiados a `/data/`. El worker usaba la versión baked-in del contenedor (imagen vieja) que busca el SO con `[[["client_order_ref","=", CLIENT_ORDER_REF]]]` (exact match con prefijo `AMZFBM:mkt:order_id`). El SO en Odoo tiene `client_order_ref = "order_id"` (formato nuevo sin prefijo) → NO MATCH → RC=2 → 5 veces → dead.
- **FIX:** La versión correcta en el repo ya usa `like order_id` (con extracción del order_id del ref). Solo faltaba desplegarlo.
- **DOCS:** Agregados ambos refund tools al bloque de deploy en sección 6. Agregado PROBLEMA 8 en sección 7.
- **DEPLOY REQUERIDO:** Copiar refund tools a `/data/` y recuperar órdenes afectadas (ver PROBLEMA 8 y sección 10).

### 2026-02-24 — sesión 3
- **DOCS:** Sesión de soporte sin cambios de código.
- **SOPORTE:** `scp gon@gonserver:/tmp/FIX_FLEX_MX_BUYER_NAME.md` fallaba porque el archivo fue creado en el entorno Claude, no en gonserver.
- **SOLUCIÓN:** Provisto comando `cat > ~/FIX_FLEX_MX_BUYER_NAME.md << 'ENDDOC'...` para crear el archivo directamente en gonserver, luego `scp gon@192.168.0.200:~/FIX_FLEX_MX_BUYER_NAME.md ~/Desktop/` desde Mac.
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
