# GONCLOUD BRIDGE — RUNBOOK OPERATIVO
**Versión:** 2.0
**Fecha:** 2026-02-04
**Sistema:** Integración MercadoLibre + Amazon → Odoo 17

---

## ARQUITECTURA GENERAL

```
┌─────────────────┐     ┌─────────────────┐
│  MercadoLibre   │     │     Amazon      │
│    (Webhook)    │     │ (Polling/SNS)   │
└────────┬────────┘     └────────┬────────┘
         │                       │
         ▼                       ▼
┌─────────────────────────────────────────┐
│            bridge-api (FastAPI)          │
│         https://meli.goncloud.cc         │
│   - /webhooks/meli/orders/{secret}       │
│   - /webhooks/amazon/orders/{secret}     │
│   - /v1/stock-snapshot                   │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│            bridge-redis                  │
│   Colas:                                 │
│   - ml_orders_jobs (MercadoLibre)        │
│   - amazon_orders_jobs (Amazon)          │
│   - stock_jobs (Outbound)                │
└────────────────┬────────────────────────┘
                 │
        ┌────────┴────────┐
        ▼                 ▼
┌───────────────┐  ┌───────────────────────┐
│ bridge-       │  │ bridge-amazon-        │
│ inbound-worker│  │ inbound-worker        │
│ (MercadoLibre)│  │ (Amazon)              │
└───────┬───────┘  └───────────┬───────────┘
        │                      │
        └──────────┬───────────┘
                   ▼
┌─────────────────────────────────────────┐
│              Odoo 17 (EHV)               │
│   - Sales Orders                         │
│   - Stock Pickings                       │
│   - Invoices / Credit Notes              │
└─────────────────────────────────────────┘
```

---

## CONTENEDORES DOCKER

| Contenedor | Función | Puerto |
|------------|---------|--------|
| bridge-api | API + Webhooks | 127.0.0.1:8099 |
| bridge-redis | Colas de trabajo | interno |
| bridge-worker | Outbound stock sync | - |
| bridge-inbound-worker | MercadoLibre inbound | - |
| bridge-amazon-inbound-worker | Amazon inbound | - |

### Comandos útiles
```bash
# Ver estado
sudo docker ps | grep bridge

# Logs
sudo docker logs bridge-inbound-worker --tail 50
sudo docker logs bridge-amazon-inbound-worker --tail 50
sudo docker logs bridge-api --tail 50

# Reiniciar
sudo docker restart bridge-api
sudo docker restart bridge-inbound-worker
sudo docker restart bridge-amazon-inbound-worker
```

---

## MERCADOLIBRE — INBOUND

### Flujos soportados

| Tipo | Estado ML | Acción Odoo |
|------|-----------|-------------|
| **FULL paid** | paid | SO + Invoice pagada (picking cancelado) |
| **FULL cancelled** | cancelled | Credit Note + SO cancelado |
| **FULL refunded** | refunded | Credit Note |
| **FBM paid** | paid | SO + Picking + Invoice pagada |
| **FBM cancelled** | cancelled | Credit Note + Picking cancel |
| **FBM refunded** | refunded | Credit Note |

### Flags de control
```sql
-- Ver flags
SELECT key, value FROM bridge_settings WHERE key LIKE 'meli%';

-- Flags principales
meli_inbound_enabled        -- Master switch
meli_webhook_enabled        -- Recibir webhooks
meli_inbound_full_paid_enabled
meli_inbound_full_cancel_enabled
meli_inbound_full_refund_enabled
meli_inbound_fbm_paid_enabled
meli_inbound_fbm_cancel_enabled
meli_inbound_fbm_refund_enabled
```

### Webhook
- **URL:** `https://meli.goncloud.cc/webhooks/meli/orders/{secret}`
- **Secret:** En `meli_webhook_secret`

### Tablas SQLite
- `inbound_events` — Auditoría de eventos recibidos
- `processed_inbound_events` — Eventos procesados
- `inbound_orders_state` — Estado de órdenes
- `meli_inbound_sales_orders` — Plan de SOs

---

## AMAZON — INBOUND

### Flujos soportados

| Tipo | Estado Amazon | Acción Odoo |
|------|---------------|-------------|
| **FBA paid** | Shipped | SO + Invoice pagada (picking cancelado) |
| **FBA cancelled** | Canceled | Credit Note |
| **FBM paid** | Unshipped/Shipped | SO + Picking + Invoice pagada |
| **FBM cancelled** | Canceled | Credit Note + Picking cancel |

### Detección FBA vs FBM
- `FulfillmentChannel = "AFN"` → FBA (Amazon envía)
- `FulfillmentChannel = "MFN"` → FBM (Merchant envía)

### Flags de control
```sql
-- Ver flags Amazon
SELECT key, value FROM bridge_settings WHERE key LIKE 'amazon%';

-- Flags principales
amazon_inbound_enabled           -- Master switch
amazon_webhook_enabled           -- Recibir webhooks SNS
amazon_inbound_fba_paid_enabled
amazon_inbound_fba_refunds_enabled
amazon_inbound_fbm_paid_enabled
amazon_inbound_fbm_refunds_enabled
```

### Credenciales SP-API
```sql
amazon_sp_api_client_id
amazon_sp_api_client_secret
amazon_sp_api_refresh_token
amazon_marketplace_id    -- A1AM78C64UM0Y8 (MX)
amazon_seller_id
```

### Marketplaces
| País | Marketplace ID |
|------|----------------|
| México | A1AM78C64UM0Y8 |
| USA | ATVPDKIKX0DER |

### Polling (cada 5 min, cuando esté habilitado)
```bash
# Habilitar timer
sudo systemctl enable --now amazon-poll.timer

# Ver estado
sudo systemctl status amazon-poll.timer

# Ejecutar manualmente
sudo /mnt/data/appdata/bridge/app/run_amazon_poll.sh
```

### Webhook SNS
- **URL:** `https://meli.goncloud.cc/webhooks/amazon/orders/{secret}`
- **Secret:** En `amazon_webhook_secret`

### Tablas SQLite
- `amazon_inbound_events` — Auditoría
- `amazon_processed_inbound_events` — Procesados
- `amazon_inbound_orders_state` — Estado de órdenes
- `amazon_inbound_sales_orders` — Plan de SOs
- `amazon_sku_mapping` — Mapeo SKU manual

### Resolución de SKU
1. `product_product.default_code = SellerSKU`
2. `product_product.x_amazon_sku = SellerSKU` (legacy)

---

## OUTBOUND — STOCK SYNC

### MercadoLibre
```bash
# Timer cada 5 minutos
sudo systemctl status meli-sync.timer

# Ejecutar manualmente
sudo /mnt/data/appdata/bridge/app/run_meli_sync.sh
```

### Tabla de mapeo
- `sku_meli_map` — default_code ↔ meli_item_id

---

## ODOO — CAMPOS CUSTOM

| Campo | Tabla | Uso |
|-------|-------|-----|
| x_meli_order_id | sale_order | ID orden MercadoLibre |
| x_amazon_sku | product_product | SKU legacy Amazon |

---

## ARCHIVOS IMPORTANTES

### Bridge App
```
/mnt/data/appdata/bridge/
├── app/
│   ├── main.py                    # API FastAPI
│   ├── inbound_worker.py          # Worker MercadoLibre
│   ├── amazon_inbound_worker.py   # Worker Amazon
│   ├── worker.py                  # Worker outbound
│   ├── run_meli_sync.sh           # Script sync ML
│   └── run_amazon_poll.sh         # Script polling Amazon
├── tools/
│   ├── inbound_full_paid_one_shot.py
│   ├── inbound_full_refund_and_cancel.py
│   ├── inbound_fbm_paid_one_shot.py
│   ├── inbound_fbm_refund_and_cancel.py
│   ├── amazon_fba_paid_one_shot.py
│   ├── amazon_fba_refund_and_cancel.py
│   ├── amazon_fbm_paid_one_shot.py
│   ├── amazon_fbm_refund_and_cancel.py
│   └── amazon_orders_poll.py
├── data/
│   ├── bridge.db                  # SQLite principal
│   └── *.py                       # Copias de tools
└── docs/
    └── RUNBOOK.md                 # Este documento
```

### Systemd
```
/etc/systemd/system/
├── meli-sync.service
├── meli-sync.timer
├── amazon-poll.service
└── amazon-poll.timer
```

---

## TROUBLESHOOTING

### Ver cola Redis
```bash
# MercadoLibre
sudo docker exec bridge-redis redis-cli LLEN ml_orders_jobs

# Amazon
sudo docker exec bridge-redis redis-cli LLEN amazon_orders_jobs
```

### Ver eventos procesados
```sql
-- MercadoLibre
SELECT dedupe_key, result, processed_at
FROM processed_inbound_events
ORDER BY processed_at DESC LIMIT 10;

-- Amazon
SELECT dedupe_key, result, processed_at
FROM amazon_processed_inbound_events
ORDER BY processed_at DESC LIMIT 10;
```

### Reprocesar orden
```bash
# Eliminar de procesados y re-encolar
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
DELETE FROM amazon_processed_inbound_events WHERE dedupe_key='xxx';
"

# Re-inyectar manualmente
sudo docker exec bridge-redis redis-cli RPUSH amazon_orders_jobs '{"dedupe_key":"xxx","order_json":{...}}'
```

### Verificar SO en Odoo
```sql
-- Por referencia Amazon
SELECT name, state, client_order_ref FROM sale_order
WHERE client_order_ref LIKE 'AMZFBM%' ORDER BY id DESC LIMIT 5;

-- Por referencia MercadoLibre
SELECT name, state, client_order_ref FROM sale_order
WHERE client_order_ref LIKE 'MLFULL%' OR client_order_ref LIKE 'MLFBM%'
ORDER BY id DESC LIMIT 5;
```

---

## ACTIVACIÓN AMAZON (cuando esté verificada la cuenta)

```bash
# 1. Probar conexión SP-API
python3 /mnt/data/appdata/bridge/data/amazon_orders_poll.py --dry-run --days 7

# 2. Habilitar flags
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fbm_paid_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fbm_refunds_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fba_paid_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fba_refunds_enabled';
"

# 3. Habilitar polling
sudo systemctl enable --now amazon-poll.timer

# 4. Verificar
sudo systemctl status amazon-poll.timer
sudo docker logs bridge-amazon-inbound-worker --tail 20
```

---

## CONTACTO / SOPORTE

- **Transcripts:** `/mnt/transcripts/`
- **Este documento:** `/mnt/data/appdata/bridge/docs/RUNBOOK.md`
