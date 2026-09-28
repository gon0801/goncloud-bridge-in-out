# Arquitectura y reglas de negocio del bridge

Referencia trasladada del antiguo `CLAUDE.md`. Contrasta rutas, versiones y horarios con el código, `docker-compose.yml` y el host antes de operar producción.

## ¿Qué es este sistema?

**GONCLOUD Bridge** es una integración bidireccional entre los marketplaces
MercadoLibre y Amazon con Odoo 17 ERP.

- **Inbound:** Órdenes de MeLi/Amazon → crea Sale Orders + facturas pagadas en Odoo
- **Outbound:** Cambios de stock en Odoo → actualiza listings en MeLi/Amazon
- **Servidor:** VPS Hetzner — alias SSH `goncloud` (user `root`, IP pública `65.109.4.81`, Tailscale `100.127.167.103`). Migrado del servidor LAN viejo (192.168.0.200, user `gon`) el 2026-05-03.
- **Ingress — dos caminos distintos hacia `bridge-api`, no confundirlos:**
  1. **Webhooks** (`meli-webhooks.goncloud.cc`) → **Cloudflare Tunnel**, servicio del host `cloudflared.service`, config remota en el dashboard de Cloudflare (no hay `config.yml` en disco). Enruta a `http://localhost:8099`, así que el puerto **debe** publicarse en `127.0.0.1` (ver `ports` en `docker-compose.yml`). Ver reglas vivas: `journalctl -u cloudflared | grep originService`.
  2. **UI del mapper** (`mapper.goncloud.cc`) → nginx-proxy-manager, que requiere `bridge-api` en la red docker `proxy`.
  `meli.goncloud.cc` figura en documentos anteriores y era del servidor viejo.
- Un binding de puerto o una red que se pongan a mano se pierden en el siguiente recreate y **el inbound muere en silencio** — 19 días así desde 2026-08-24. Todo va declarado en el compose.
- **DB:** SQLite en `/mnt/data/appdata/bridge/data/bridge.db`
- **Repo:** `https://github.com/gon0801/goncloud-bridge-in-out.git`

---

## Arquitectura

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

## Componentes/Servicios

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

## Flujos de negocio

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

## Reglas de negocio selladas

### FULL MeLi — Canónico (ANEXO C — NO modificar sin nueva versión)

```
PRINCIPIO: SOLO neutralizar contabilidad con señal explícita de REFUND.
"returned" por sí solo NO garantiza refund → NO tocar contabilidad.

Ejecutar refund/cancel si status IN {cancelled, canceled, refunded}
NO ejecutar si status IN {returned, returning, to_be_returned}
```

### Deduplicación

`app/inbound_worker.py` (MeLi) considera terminados `success` y `dead`; permite
reintentar `manual_review`, `error` y `skipped`. En
`app/amazon_inbound_worker.py`, solo `success` y `skipped` son terminales:
`manual_review` y `dead` se pueden reintentar. Comprueba el worker del canal
antes de borrar auditorías o reencolar órdenes.

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

**Pero `/data/` no es el único lado vivo.** Los dos directorios corren, con
llamadores distintos — antes de promover un archivo, ver quién lo invoca:

| Llamador | Qué ejecuta |
|---|---|
| Workers (`run_tool()`) | `/data/` primero, `tools/` de fallback |
| Timers de systemd del host | `tools/` directo (`ExecStart=...${BRIDGE_BASE}/tools/...`) |
| `app/main.py` | algunos con ruta fija `/data/` (ej. `sync_meli_listings.py`) |

Copiar "al lado bueno" sin mirar el llamador puede deployar un fix donde nadie
lo lee, o pisar el que sí corre. Para ver el estado:
`bash tools/check_tools_data_drift.sh` — compara por AST y muestra qué cambia,
separando formato de comportamiento.

---

## Archivos clave

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
    ├── ANEXO_C_FULL_ML_STATUS.md     # Reglas FULL MeLi (selladas)
    ├── BRIDGE_REFERENCE.md           # Arquitectura y reglas de negocio
    ├── TROUBLESHOOTING.md            # Diagnóstico y operación
    └── HISTORY.md                    # Incidentes y estados históricos
```
