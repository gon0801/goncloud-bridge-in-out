# GONCLOUD BRIDGE — DOCUMENTO CANÓNICO MAESTRO

> **Propósito:** Este documento es suficiente para reinstalar, recuperar o reproducir
> el sistema completo desde cero sin ningún contexto adicional.
> **Fecha:** 2026-03-01 · **Versión:** 1.0

---

## ÍNDICE

1. [¿Qué es este sistema?](#1-qué-es-este-sistema)
2. [Requisitos de infraestructura](#2-requisitos-de-infraestructura)
3. [Arquitectura completa](#3-arquitectura-completa)
4. [Servicios Docker](#4-servicios-docker)
5. [Estructura de archivos](#5-estructura-de-archivos)
6. [Instalación desde cero](#6-instalación-desde-cero)
7. [Configuración completa](#7-configuración-completa)
8. [Schema de base de datos](#8-schema-de-base-de-datos)
9. [Flujos de negocio](#9-flujos-de-negocio)
10. [Reglas de negocio selladas](#10-reglas-de-negocio-selladas)
11. [Lógica de cada tool/script](#11-lógica-de-cada-toolscript)
12. [Timers del sistema (systemd)](#12-timers-del-sistema-systemd)
13. [MeLi OAuth — tokens](#13-meli-oauth--tokens)
14. [Amazon SP-API — credenciales y polling](#14-amazon-sp-api--credenciales-y-polling)
15. [Odoo — integración JSON-RPC](#15-odoo--integración-json-rpc)
16. [Tipo de cambio USD/MXN](#16-tipo-de-cambio-usdmxn)
17. [SKU mapping](#17-sku-mapping)
18. [Deduplicación y estados de job](#18-deduplicación-y-estados-de-job)
19. [Operaciones cotidianas](#19-operaciones-cotidianas)
20. [Diagnóstico y troubleshooting](#20-diagnóstico-y-troubleshooting)
21. [Recuperación de órdenes atascadas](#21-recuperación-de-órdenes-atascadas)
22. [Bugs resueltos — NO reintroducir](#22-bugs-resueltos--no-reintroducir)
23. [Deploy de fixes](#23-deploy-de-fixes)
24. [Outbound — sincronización de stock](#24-outbound--sincronización-de-stock)
25. [Módulo Odoo bridge_connector](#25-módulo-odoo-bridge_connector)

---

## 1. ¿Qué es este sistema?

**GONCLOUD Bridge** es una integración bidireccional entre los marketplaces
MercadoLibre y Amazon con el ERP Odoo 17.

### Dirección INBOUND (marketplace → Odoo)
- Órdenes de MercadoLibre y Amazon se convierten en Sale Orders + facturas pagadas en Odoo.
- El sistema maneja cancelaciones y refunds creando Credit Notes.

### Dirección OUTBOUND (Odoo → marketplace)
- Cambios de stock en Odoo se propagan a listings de MeLi y Amazon.

### Datos clave del entorno de producción
| Parámetro | Valor |
|-----------|-------|
| Servidor | VPS Hetzner — alias `gonserver` o `goncloud` |
| Hostname | `goncloud` |
| IP pública | `65.109.4.81` |
| IP Tailscale | `100.127.167.103` |
| Usuario SSH | `root` |
| Llave SSH (cliente) | `~/.ssh/goncloud-mexico` |
| OS | Ubuntu 24.04.3 LTS |
| URL pública | `https://meli.goncloud.cc` |
| Base de datos | SQLite en `/mnt/data/appdata/bridge/data/bridge.db` |
| Odoo ERP | Base de datos `EHV`, Odoo 17 |
| Repo | `https://github.com/gon0801/goncloud-bridge-in-out.git` |
| Ruta en server | `/mnt/data/appdata/bridge/` |

> **Servidor anterior (deprecado 2026-05-03):** `192.168.0.200` con user `gon`. Cualquier referencia histórica en este documento (en secciones de "Diario de cambios" o ejemplos) puede mencionar el servidor antiguo — son registros del momento, no se actualizan retroactivamente.

---

## 2. Requisitos de infraestructura

### Host
- Linux (Ubuntu 22.04 recomendado)
- Docker + Docker Compose v2
- Python 3.11+ (para scripts del host)
- SQLite 3.35+
- `systemd` para timers
- Red Docker externa `goncloud-net` ya creada

### Dependencias Python (bridge-api / bridge-worker)
```
fastapi==0.110.0
uvicorn[standard]==0.29.0
redis==5.0.1
requests==2.32.3
```

### Dependencias Python (workers inbound)
```
requests==2.32.5
redis==7.1.0
```

### Red Docker
```bash
docker network create goncloud-net
```

### Directorios en el host (deben existir antes de levantar)
```bash
mkdir -p /mnt/data/appdata/bridge/data
mkdir -p /mnt/data/appdata/bridge/app
mkdir -p /mnt/data/appdata/bridge/tools
mkdir -p /mnt/data/appdata/bridge/redis
mkdir -p /mnt/data/appdata/bridge/docs
```

---

## 3. Arquitectura completa

```
  MercadoLibre API          Amazon SP-API
  (webhooks push)         (polling cada 5 min)
         │                       │
         ▼                       ▼
┌─────────────────────────────────────────┐
│          bridge-api  (FastAPI :8099)     │
│  POST /webhooks/meli/orders/{secret}     │
│  POST /webhooks/amazon/orders/{secret}   │
│  GET  /oauth/start  /oauth/callback      │
│  POST /oauth/refresh                     │
│  GET  /mapper  /amazon/mapper            │
│  GET  /v1/settings                       │
│  GET  /v1/health                         │
│  POST /v1/stock-snapshot                 │
└──────────────────┬──────────────────────┘
                   │  inyecta jobs a Redis
                   ▼
┌─────────────────────────────────────────┐
│              bridge-redis               │
│  ml_orders_jobs        (MeLi inbound)   │
│  amazon_orders_jobs    (Amazon inbound) │
│  stock_jobs            (outbound)       │
│  amazon_orders_dead    (DLQ Amazon)     │
└──────┬────────────────┬─────────────────┘
       │                │
       ▼                ▼
┌─────────────┐  ┌─────────────────────────┐
│  bridge-    │  │  bridge-amazon-         │
│  inbound-   │  │  inbound-worker         │
│  worker     │  │  (amazon_inbound_       │
│ (inbound_   │  │   worker.py  v2.7)      │
│  worker.py  │  │                         │
│  v8.4)      │  │  Concurrencia con locks  │
│             │  │  Heartbeat cada 30s     │
│  subprocess │  │  subprocess →tools/     │
│  →tools/    │  │                         │
└──────┬──────┘  └────────────┬────────────┘
       │                      │
       └──────────┬────────────┘
                  │  JSON-RPC
                  ▼
┌─────────────────────────────────────────┐
│              Odoo 17 (EHV)              │
│  sale.order  ·  account.move            │
│  stock.picking  ·  res.partner          │
│  product.product  ·  res.currency.rate  │
└─────────────────────────────────────────┘
                  ▲
┌─────────────────────────────────────────┐
│  bridge-worker (outbound)               │
│  stock_jobs → MeLi PUT + Amazon PATCH   │
└─────────────────────────────────────────┘
```

---

## 4. Servicios Docker

### docker-compose.yml completo

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: bridge-redis
    command: ["redis-server", "--appendonly", "yes", "--maxmemory", "256mb", "--maxmemory-policy", "allkeys-lru"]
    volumes:
      - /mnt/data/appdata/bridge/redis:/data
    restart: unless-stopped
    networks:
      - goncloud-net

  bridge-api:
    env_file:
      - .env
    build: ./app
    container_name: bridge-api
    working_dir: /app
    dns:
      - 8.8.8.8
      - 1.1.1.1
    volumes:
      - /mnt/data/appdata/bridge/data:/data
      - /mnt/data/appdata/bridge/app:/app
    environment:
      - ENABLE_MISSING_ZERO_CHANNELS=amazon_fbm,meli
      - BRIDGE_DB=/data/bridge.db
      - REDIS_URL=redis://bridge-redis:6379/0
    ports:
      - "127.0.0.1:8099:8099"
    depends_on:
      - redis
    restart: unless-stopped
    networks:
      - goncloud-net

  bridge-worker:
    build: ./app
    container_name: bridge-worker
    working_dir: /app
    volumes:
      - /mnt/data/appdata/bridge/data:/data
      - /mnt/data/appdata/bridge/app:/app
    environment:
      - ODOO_URL=${ODOO_URL}
      - ODOO_DB=${ODOO_DB}
      - ODOO_USER=${ODOO_USER}
      - ODOO_PASSWORD=${ODOO_PASSWORD}
      - BRIDGE_DB=/data/bridge.db
      - REDIS_URL=redis://bridge-redis:6379/0
      - QUEUE_NAME=stock_jobs
    command: ["python", "/app/worker.py"]
    depends_on:
      - redis
    restart: unless-stopped
    networks:
      - goncloud-net

  bridge-inbound-worker:
    image: python:3.11-slim
    container_name: bridge-inbound-worker
    working_dir: /app
    volumes:
      - /mnt/data/appdata/bridge/data:/data
      - /mnt/data/appdata/bridge/app:/app
    environment:
      - BRIDGE_DB=/data/bridge.db
      - REDIS_URL=redis://bridge-redis:6379/0
    command: ["python", "/app/inbound_worker.py"]
    depends_on:
      - redis
    restart: unless-stopped
    networks:
      - goncloud-net

  bridge-amazon-inbound-worker:
    image: python:3.11-slim
    container_name: bridge-amazon-inbound-worker
    working_dir: /app
    volumes:
      - /mnt/data/appdata/bridge/data:/data
      - /mnt/data/appdata/bridge/app:/app
    environment:
      - BRIDGE_DB=/data/bridge.db
      - REDIS_URL=redis://bridge-redis:6379/0
    command: ["python", "/app/amazon_inbound_worker.py"]
    depends_on:
      - redis
    restart: unless-stopped
    networks:
      - goncloud-net

networks:
  goncloud-net:
    external: true
```

### Levantar / bajar servicios
```bash
cd /mnt/data/appdata/bridge
sudo docker compose up -d            # arrancar todo
sudo docker compose down             # bajar todo
sudo docker compose restart          # reinicio limpio
sudo docker restart bridge-api
sudo docker restart bridge-inbound-worker
sudo docker restart bridge-amazon-inbound-worker
```

### Ver logs
```bash
sudo docker logs bridge-api --tail 50 -f
sudo docker logs bridge-inbound-worker --tail 50 -f
sudo docker logs bridge-amazon-inbound-worker --tail 50 -f
sudo docker logs bridge-worker --tail 50 -f
```

---

## 5. Estructura de archivos

```
/mnt/data/appdata/bridge/
├── docker-compose.yml
├── .env                              ← NO en git (ver sección 7)
│
├── app/                              ← montado como /app en contenedores
│   ├── main.py                       # FastAPI: webhooks, OAuth, mapper UI, settings
│   ├── inbound_worker.py             # Worker MeLi inbound v8.4
│   ├── amazon_inbound_worker.py      # Worker Amazon inbound v2.7
│   ├── worker.py                     # Worker outbound stock sync
│   ├── db_init.py                    # Schema SQLite (legacy, mínimo)
│   ├── amazon_fba_paid_one_shot.py   # Copia sync de tools/ (gate2 fallback)
│   ├── requirements.txt              # fastapi, uvicorn, redis, requests
│   ├── requirements.inbound.txt      # requests, redis (workers)
│   ├── Dockerfile                    # Python 3.11-slim
│   ├── sku_mapper.html               # UI mapeo SKU MeLi
│   ├── amazon_mapper.html            # UI mapeo SKU Amazon
│   └── static/setup.html            # Setup wizard UI
│
├── tools/                            ← FUENTE DE VERDAD de los tools
│   ├── amazon_fba_paid_one_shot.py
│   ├── amazon_fbm_paid_one_shot.py
│   ├── amazon_fba_refund_and_cancel.py
│   ├── amazon_fbm_refund_and_cancel.py
│   ├── amazon_orders_poll.py
│   ├── inbound_full_paid_one_shot_no_stock.py
│   ├── inbound_full_so_refund_and_cancel.py
│   ├── inbound_fbm_so_apply_paid_one_shot.py
│   ├── inbound_fbm_so_refund_and_cancel.py
│   ├── recover_manual_review.py
│   ├── push_fx_to_odoo.py
│   ├── diagnose_inbound.py
│   ├── debug_meli_order.py
│   └── sku_audit.py
│
├── data/                             ← montado como /data en contenedores
│   ├── bridge.db                     # SQLite principal
│   ├── .meli_tokens.json             # Tokens OAuth MeLi (NO en git)
│   └── *.py                          ← COPIAS de tools/ con MÁXIMA PRIORIDAD
│                                        (el worker busca aquí PRIMERO)
│
├── docs/
│   ├── RUNBOOK.md
│   └── ANEXO_C_FULL_ML_STATUS.md
│
└── redis/                            ← datos persistentes de Redis (AOF)
```

### REGLA CRÍTICA: prioridad de tools

Los workers buscan scripts en este orden — **NO invertir**:

```
1. /data/{tool}.py          ← deployment target (SIEMPRE primero)
2. /mnt/data/appdata/bridge/tools/{tool}.py  ← fallback
```

Cualquier fix deploiado debe copiarse a `/data/`. Si no se copia, el worker
ejecutará la versión vieja del fallback.

---

## 6. Instalación desde cero

### Paso 1 — Clonar el repo en el host

```bash
sudo git clone https://github.com/gon0801/goncloud-bridge-in-out.git /tmp/goncloud-bridge-in-out
```

### Paso 2 — Crear directorios

```bash
sudo mkdir -p /mnt/data/appdata/bridge/{data,app,tools,redis,docs}
```

### Paso 3 — Copiar archivos

```bash
# App
sudo cp -r /tmp/goncloud-bridge-in-out/app/* /mnt/data/appdata/bridge/app/

# Tools (fuente de verdad)
sudo cp -r /tmp/goncloud-bridge-in-out/tools/* /mnt/data/appdata/bridge/tools/

# Docker compose
sudo cp /tmp/goncloud-bridge-in-out/docker-compose.yml /mnt/data/appdata/bridge/

# Docs
sudo cp -r /tmp/goncloud-bridge-in-out/docs/* /mnt/data/appdata/bridge/docs/
```

### Paso 4 — Copiar tools a /data/ (máxima prioridad)

```bash
sudo cp /mnt/data/appdata/bridge/tools/amazon_fba_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/amazon_fbm_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/amazon_fba_refund_and_cancel.py        /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/amazon_fbm_refund_and_cancel.py        /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/amazon_orders_poll.py                  /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/inbound_full_paid_one_shot_no_stock.py /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/inbound_full_so_refund_and_cancel.py   /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/inbound_fbm_so_apply_paid_one_shot.py  /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/inbound_fbm_so_refund_and_cancel.py    /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/recover_manual_review.py               /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/push_fx_to_odoo.py                     /mnt/data/appdata/bridge/data/
sudo cp /mnt/data/appdata/bridge/tools/diagnose_inbound.py                    /mnt/data/appdata/bridge/data/
```

### Paso 5 — Crear .env

```bash
sudo nano /mnt/data/appdata/bridge/.env
```

Contenido (ver sección 7 para todos los valores):

```env
ODOO_URL=https://tu-odoo.internal
ODOO_DB=EHV
ODOO_USER=odoo_bridge
ODOO_PASSWORD=SECRETO

MELI_CLIENT_ID=XXXXXXX
MELI_CLIENT_SECRET=XXXXXXX
MELI_REDIRECT_URI=https://meli.goncloud.cc/oauth/callback

BRIDGE_DB=/data/bridge.db
REDIS_URL=redis://bridge-redis:6379/0
```

### Paso 6 — Crear red Docker

```bash
docker network create goncloud-net
```

### Paso 7 — Levantar contenedores

```bash
cd /mnt/data/appdata/bridge
sudo docker compose up -d
```

### Paso 8 — Configurar via Setup Wizard

```
https://meli.goncloud.cc/v1/settings
```

Configurar en la UI:
- URL/DB/user/password de Odoo
- Credenciales SP-API Amazon
- Secrets de webhook
- Habilitar flags de canales

### Paso 9 — Autorizar MeLi OAuth

```
https://meli.goncloud.cc/oauth/start
```

Abre MeLi, autoriza, el callback guarda `access_token` + `refresh_token`
en `/data/.meli_tokens.json`.

### Paso 10 — Instalar timers systemd

```bash
# Copiar archivos de servicio
sudo cp /tmp/goncloud-bridge-in-out/app/run_amazon_poll.sh /mnt/data/appdata/bridge/app/
sudo cp /tmp/goncloud-bridge-in-out/app/run_meli_sync.sh   /mnt/data/appdata/bridge/app/
sudo chmod +x /mnt/data/appdata/bridge/app/*.sh

# Crear service + timer para Amazon poll
sudo tee /etc/systemd/system/amazon-poll.service <<'EOF'
[Unit]
Description=Amazon Orders Poll

[Service]
Type=oneshot
ExecStart=/mnt/data/appdata/bridge/app/run_amazon_poll.sh
EOF

sudo tee /etc/systemd/system/amazon-poll.timer <<'EOF'
[Unit]
Description=Amazon poll cada 5 minutos

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

# Crear service + timer para MeLi sync outbound
sudo tee /etc/systemd/system/meli-sync.service <<'EOF'
[Unit]
Description=MeLi Stock Sync

[Service]
Type=oneshot
ExecStart=/mnt/data/appdata/bridge/app/run_meli_sync.sh
EOF

sudo tee /etc/systemd/system/meli-sync.timer <<'EOF'
[Unit]
Description=MeLi sync cada 5 minutos

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now amazon-poll.timer
sudo systemctl enable --now meli-sync.timer
```

### Paso 11 — Verificar health

```bash
curl http://127.0.0.1:8099/v1/health
# Debe retornar: {"ok": true}
```

---

## 7. Configuración completa

### Variables de entorno (.env en raíz del compose)

| Variable | Descripción |
|----------|-------------|
| `ODOO_URL` | URL base Odoo (ej. `https://odoo.empresa.com`) |
| `ODOO_DB` | Nombre de la base de datos Odoo (`EHV`) |
| `ODOO_USER` | Usuario Odoo del bridge |
| `ODOO_PASSWORD` | Password del usuario Odoo |
| `MELI_CLIENT_ID` | App ID de la aplicación MeLi |
| `MELI_CLIENT_SECRET` | Client secret MeLi |
| `MELI_REDIRECT_URI` | `https://meli.goncloud.cc/oauth/callback` |
| `BRIDGE_DB` | Path SQLite dentro del contenedor (`/data/bridge.db`) |
| `REDIS_URL` | `redis://bridge-redis:6379/0` |

### Tabla bridge_settings (configuración en DB)

Todos los flags viven en `bridge_settings (key, value)`.

#### MeLi
| key | valor típico | descripción |
|-----|-------------|-------------|
| `meli_inbound_enabled` | `1` | master switch inbound MeLi |
| `meli_webhook_secret` | string random | valida firma del webhook |
| `meli_inbound_full_paid_enabled` | `1` | procesar FULL paid |
| `meli_inbound_full_cancel_enabled` | `1` | procesar FULL cancelled |
| `meli_inbound_full_refund_enabled` | `1` | procesar FULL refunded |
| `meli_inbound_fbm_paid_enabled` | `1` | procesar FBM paid |
| `meli_inbound_fbm_cancel_enabled` | `1` | procesar FBM cancelled |
| `meli_inbound_fbm_refund_enabled` | `1` | procesar FBM refunded |

#### Amazon
| key | valor típico | descripción |
|-----|-------------|-------------|
| `amazon_inbound_enabled` | `1` | master switch inbound Amazon |
| `amazon_webhook_secret` | string random | valida firma SNS |
| `amazon_sp_api_client_id` | `amzn1.application-oa2-client.xxx` | SP-API app client id |
| `amazon_sp_api_client_secret` | string | SP-API app secret |
| `amazon_sp_api_refresh_token` | `Atzr|xxx` | refresh token del seller |
| `amazon_marketplace_id` | `A1AM78C64UM0Y8` | MX. Para US: `ATVPDKIKX0DER` |
| `amazon_seller_id` | string | Seller ID de Amazon |
| `amazon_inbound_fba_paid_enabled` | `1` | FBA paid |
| `amazon_inbound_fba_refunds_enabled` | `1` | FBA refunds |
| `amazon_inbound_fbm_paid_enabled` | `1` | FBM paid |
| `amazon_inbound_fbm_refunds_enabled` | `1` | FBM refunds |

#### Odoo
| key | descripción |
|-----|-------------|
| `odoo_url` | URL JSON-RPC Odoo |
| `odoo_db` | Nombre DB Odoo |
| `odoo_user` | Usuario Odoo |
| `odoo_password` | Password Odoo |

#### Ver/editar flags
```bash
# Ver todos los flags
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT key, value FROM bridge_settings ORDER BY key;"

# Cambiar un flag
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_enabled';"
```

---

## 8. Schema de base de datos

La DB es SQLite en `/mnt/data/appdata/bridge/data/bridge.db`.
Modo WAL activado. Toda la esquema se crea automáticamente al arrancar los workers.

### Tablas principales

```sql
-- CONFIGURACIÓN
bridge_settings    (key TEXT PK, value TEXT)
bridge_metrics     (key TEXT PK, value TEXT, updated_at TEXT)

-- MELI INBOUND
inbound_events (
    id INTEGER PK AUTOINCREMENT,
    dedupe_key TEXT,
    resource TEXT,
    payload_json TEXT,
    status TEXT,
    created_at TEXT
)

inbound_job_payloads (
    dedupe_key TEXT PK,
    payload_json TEXT,
    ttl_at TEXT
)

processed_inbound_events (
    dedupe_key TEXT PK,
    processed_at TEXT,
    result TEXT,          -- success | manual_review | dead | error | deferred
    detail_json TEXT
)

inbound_orders_state (
    order_id TEXT PK,
    last_state TEXT,
    last_updated_at TEXT,
    pack_id TEXT
)

inbound_allowed_skus (
    sku TEXT PK,
    enabled INTEGER DEFAULT 1
)

inbound_processing_locks (
    dedupe_key TEXT PK,
    claimed_at TEXT,
    heartbeat_at TEXT
)

-- AMAZON INBOUND
amazon_processed_events (
    dedupe_key TEXT PK,
    processed_at TEXT,
    result TEXT,          -- success | manual_review | dead | error | deferred
    detail_json TEXT
)

amazon_job_payloads (
    dedupe_key TEXT PK,
    payload_json TEXT,
    ttl_at TEXT
)

amazon_processing_locks (
    dedupe_key TEXT PK,
    claimed_at TEXT,
    heartbeat_at TEXT
)

amazon_sku_mapping (
    seller_sku TEXT PK,
    odoo_default_code TEXT
)

amazon_orders_state (
    order_id TEXT PK,
    last_status TEXT,
    last_updated_at TEXT
)

-- OUTBOUND / COMPARTIDAS
sku_mapping (
    channel TEXT,
    remote_item_id TEXT,
    remote_variation_id TEXT,
    sku TEXT,
    site TEXT
)

events (                              -- snapshots de stock (outbound)
    id INTEGER PK AUTOINCREMENT,
    created_at TEXT,
    channel TEXT,
    item_count INTEGER,
    payload TEXT
)

snapshot_items (
    event_id INTEGER,
    channel TEXT,
    sku TEXT,
    qty INTEGER,
    derived_zero INTEGER,
    PRIMARY KEY (event_id, sku)
)

rejected_skus (
    event_id INTEGER,
    channel TEXT,
    sku TEXT,
    reason TEXT
)

processed_events (                    -- outbound genérico
    event_id INTEGER,
    channel TEXT,
    status TEXT,
    processed_at TEXT,
    details TEXT
)
```

---

## 9. Flujos de negocio

### MercadoLibre Inbound

#### Detección FULL vs FBM
```
logistic_type == "fulfillment"  →  FULL  (MeLi envía, sin picking)
logistic_type != "fulfillment"  →  FBM   (merchant envía, con picking)
```

Fuentes del `logistic_type` (en orden de precedencia):
1. `order.logistic_type`
2. `shipment.logistic_type`

Si no se puede determinar → `manual_review`.

#### Tabla de acciones

| Tipo | Estado MeLi | Acción Odoo | Prefijo SO |
|------|-------------|-------------|------------|
| FULL | `paid` | SO + Invoice pagada (sin picking) | `MLFULL` |
| FULL | `cancelled` / `canceled` | Credit Note + cancel SO | `MLFULL` |
| FULL | `refunded` | Credit Note + cancel SO | `MLFULL` |
| FULL | `returned` / `returning` / `to_be_returned` | **NADA** (esperar `refunded`) | — |
| FBM | `paid` | SO + Picking + Invoice pagada | `MLFBM` |
| FBM | `cancelled` / `canceled` | Credit Note + cancel picking | `MLFBM` |
| FBM | `refunded` | Credit Note + cancel picking | `MLFBM` |

#### Formato de campos en Odoo (MeLi)
```
client_order_ref  =  "{order_id} | {buyer_nickname}"
   Ej:  "2000011682284817 | COMPRADOR123"

note (SO)  =  "MeLi FULL | ORDER={pack_id_o_order_id} | {buyer_nickname}"
```

---

### Amazon Inbound

#### Detección FBA vs FBM
```
FulfillmentChannel == "AFN"  →  FBA  (Amazon envía, sin picking)
FulfillmentChannel == "MFN"  →  FBM  (merchant envía, con picking)
```

#### Perfiles por marketplace

| Marketplace | ID | Profiles activos |
|-------------|-----|-----------------|
| México | `A1AM78C64UM0Y8` | `FBA_MX`, `FLEX_MX`, `FBM_MX`, `EASY_MX` |
| USA | `ATVPDKIKX0DER` | `FBA_US`, `FBM_US` |

#### Tabla de acciones

| Tipo | Condición | Acción Odoo | Prefijo ref |
|------|-----------|-------------|-------------|
| FBA | `AFN` + `OrderStatus=Shipped` | SO + Invoice pagada (sin picking) | `AMZFBA:mkt:id` |
| FBA | `AFN` + `OrderStatus=Canceled` | Credit Note | `AMZFBA:mkt:id` |
| FBM | `MFN` + `OrderStatus=Unshipped/Shipped` | SO + Picking + Invoice pagada | `AMZFBM:mkt:id` |
| FBM | `MFN` + `OrderStatus=Canceled` | Credit Note + cancel picking | `AMZFBM:mkt:id` |
| Flex MX | `AFN` + marketplace MX + `OrderStatus=Pending` | SO + Picking + Invoice pagada | `AMZFBM:MX:id` |

#### Flex MX — lógica especial

```
Definición:
  FulfillmentChannel == "AFN"
  marketplace_id     == "A1AM78C64UM0Y8"   (México)
  OrderStatus        == "Pending"

Comportamiento:
  - Se procesa INMEDIATAMENTE como si fuera "paid"
  - Genera picking (como FBM, aunque sea AFN)
  - El status "Pending" en Flex MX NO se saltea en el poll

Problema Pending→Unshipped:
  - En "Pending": Amazon SP-API no devuelve BuyerName
  - SO se crea con client_order_ref = order_id (sin buyer)
  - En "Unshipped": SP-API devuelve BuyerName
  - El worker busca el SO por display_ref OR order_id-only
  - Si lo encuentra por order_id-only y ahora tiene buyer → actualiza client_order_ref
```

#### Formato de campos en Odoo (Amazon)
```
client_order_ref  =  "{order_id} | {buyer_name}"
   Ej:  "701-4611535-8534600 | Juan García"

note (SO)  =  "Amazon FBA | ORDER=701-4611535-8534600 | Juan García"
```

---

## 10. Reglas de negocio selladas

### ANEXO C — FULL MeLi (NO modificar sin nueva versión)

```
PRINCIPIO ABSOLUTO:
  Solo neutralizar contabilidad con señal explícita de REFUND.
  "returned" por sí solo NO garantiza refund → NO tocar contabilidad.

CLASE A — Ejecutar refund:    status == "refunded"
CLASE B — Ejecutar cancel:    status ∈ {"cancelled", "canceled"}
CLASE C — NO ejecutar:        status ∈ {"returned", "returning", "to_be_returned"}
CLASE D — NO ejecutar:        cualquier otro status no clasificado
```

**Estado final garantizado (cuando aplica FULL refund/cancel):**
- Sale Order: `cancel`
- Invoice original: `posted` / `paid`
- Credit Note: `posted` / `paid`
- Pickings: 0 (ya no había para FULL)
- Auditoría JSON persistida en `processed_inbound_events`

### Deduplicación

```
Bloquear procesamiento si result IN ('success', 'dead')
Reintentar si result IN ('manual_review', 'error', 'deferred')

Nunca bloquear estados retryables — el sistema es auto-curativo.
```

### Cálculo de precio Amazon (Sales Proceeds)

```
price_unit = (
    ItemPrice.Amount
    + ItemTax.Amount
    + ShippingPrice.Amount
    + ShippingTax.Amount
    + GiftWrapPrice.Amount
    + GiftWrapTax.Amount
) / Quantity

Nunca usar solo ItemPrice — Amazon cobra al cliente el total incluyendo envío y tax.
```

### Conversión USD → MXN

```
Fuente de verdad: order["OrderTotal"]["CurrencyCode"]
  - Si CurrencyCode == "USD" → convertir
  - Si CurrencyCode == "MXN" → usar directo
  - Otro valor → manual_review con mensaje claro

Sanity check: 1 USD debe ser entre 5 y 500 MXN.
  Fuera de rango → manual_review (evitar errores silenciosos)

Tasa: res.currency.rate en Odoo
  rate en Odoo es inverso: rate = 1/mxn_per_usd
  price_mxn = price_usd / rate

El env IS_USD_ORDER es fallback cuando ORDER_JSON no tiene OrderTotal.
```

### Timestamp Amazon SP-API

```
FORMATO OBLIGATORIO: strftime('%Y-%m-%dT%H:%M:%SZ')
  La "Z" al final es REQUERIDA por Amazon US.
  Sin "Z" → HTTP 400 "timestamp must follow ISO8601"
  NO usar .isoformat() que omite la Z.
```

### Prioridad de búsqueda de tools

```
SIEMPRE buscar en este orden:
  1. /data/{tool}.py                               ← deployment target
  2. /mnt/data/appdata/bridge/tools/{tool}.py      ← fallback

Invertir este orden causa que los fixes deploiados a /data/ sean ignorados.
```

---

## 11. Lógica de cada tool/script

### `amazon_fba_paid_one_shot.py`

**Propósito:** Crear SO + Invoice + Pago para órdenes FBA (Amazon envía).
**Sin picking** (FBA = Amazon gestiona stock).

**Algoritmo:**
1. Lee `ORDER_JSON` del env, parsea la orden
2. Detecta moneda desde `OrderTotal.CurrencyCode`
3. Aplica `amazon_sku_mapping` (legacy SKU → default_code Odoo)
4. Calcula Sales Proceeds (precio total = sum de todos los componentes)
5. Si USD → convierte a MXN con tasa de `res.currency.rate` en Odoo
6. Busca productos en Odoo por `default_code`
7. Crea SO (o reutiliza si ya existe por `client_order_ref`)
8. Confirma SO (auto-crea picking)
9. **Cancela los pickings** (FBA = sin movimiento de stock propio)
10. Crea Invoice manual ligada a líneas del SO
11. Paga Invoice via wizard `account.payment.register`

**Exit codes:** `0`=éxito · `1`=manual_review · `2`=deferred

---

### `amazon_fbm_paid_one_shot.py`

**Propósito:** Crear SO + Picking + Invoice + Pago para FBM.
**Con picking** (el merchant envía, el picking queda abierto para validar).

**Diferencia clave con FBA:**
- Pickings NO se cancelan (el cliente espera el envío)
- Si todos los precios son $0 → diferir la factura (esperar precio actualizado)
- Búsqueda de SO existente: OR entre `display_ref` Y `order_id`-only
  (fix para Flex MX donde Pending no trae BuyerName)

---

### `amazon_fba_refund_and_cancel.py`

**Propósito:** Crear Credit Note + cancelar SO para FBA canceled.

**Algoritmo:**
1. Busca SO por `client_order_ref` (order_id | buyer)
2. Si no existe → exit 2 (deferred, aún no fue procesado el paid)
3. Verifica que no haya Credit Note ya creada (idempotencia)
4. Crea Credit Note reversa de la Invoice
5. Valida (confirma) Credit Note
6. Registra pago de la Credit Note
7. Cancela el SO

---

### `amazon_fbm_refund_and_cancel.py`

**Propósito:** Crear Credit Note + cancelar picking + cancelar SO para FBM canceled.

Igual que FBA refund pero también cancela el picking antes de cancelar el SO.

---

### `inbound_full_paid_one_shot_no_stock.py`

**Propósito:** Crear SO + Invoice + Pago para FULL MeLi (sin movimiento de stock).

**Algoritmo:**
1. Parsea `ORDER_JSON` del env
2. Construye `display_ref` = `{order_id} | {buyer_nickname}`
3. Busca productos por `seller_sku` (directo) o `sku_mapping`
4. Crea/reutiliza SO
5. Confirma SO, cancela picking generado
6. Crea Invoice, paga Invoice

---

### `inbound_fbm_so_apply_paid_one_shot.py`

**Propósito:** Crear SO + Picking + Invoice + Pago para FBM MeLi.

Igual que FULL paid pero el picking permanece abierto para despacho.

---

### `inbound_full_so_refund_and_cancel.py`

**Propósito:** Credit Note + cancelar SO para FULL MeLi refund/cancel.

Reglas: solo ejecutar si status ∈ {cancelled, canceled, refunded}.
Si status == returned → exit 2 (NO ejecutar).

---

### `inbound_fbm_so_refund_and_cancel.py`

**Propósito:** Credit Note + cancelar picking + cancelar SO para FBM MeLi.

---

### `amazon_orders_poll.py`

**Propósito:** Consultar SP-API, inyectar órdenes en Redis para procesamiento.

**Algoritmo:**
1. Lee credenciales de `bridge_settings`
2. Calcula `LastUpdatedAfter` = ahora − N días
   Formato: `strftime('%Y-%m-%dT%H:%M:%SZ')` (con Z, obligatorio)
3. Llama `GET /orders/v0/orders` para cada marketplace configurado
4. Para cada orden:
   a. Determina perfil (FBA_MX, FLEX_MX, FBM_MX, FBA_US, FBM_US, etc.)
   b. Si status == "Pending" AND NOT is_flex_mx → **saltear**
   c. Si ya está en `amazon_orders_state` con mismo status → saltear
   d. Obtiene items: `GET /orders/v0/orders/{id}/orderItems`
   e. Construye payload JSON
   f. Inyecta en Redis `amazon_orders_jobs`
   g. Actualiza `amazon_orders_state`

---

### `recover_manual_review.py`

**Propósito:** Re-encolar órdenes atascadas en `manual_review` o `dead`.

```bash
# Dry-run (ver qué haría, no ejecuta)
python3 /data/recover_manual_review.py --channel amazon --hours 168 --include-dead --dry-run

# Ejecutar
python3 /data/recover_manual_review.py --channel amazon --hours 168 --include-dead
python3 /data/recover_manual_review.py --channel meli   --hours 168 --include-dead
```

**Qué hace:**
1. Lee entradas de `*_processed_events` con `result IN ('manual_review', 'dead')`
2. Lee el payload original de `*_job_payloads`
3. Borra el audit record
4. Re-inyecta en la cola Redis correspondiente

---

### `push_fx_to_odoo.py`

**Propósito:** Sincronizar tipo de cambio USD/MXN desde `accounting.db` a Odoo.

```bash
# Última tasa disponible
python3 /data/push_fx_to_odoo.py

# Fecha específica
python3 /data/push_fx_to_odoo.py 2026-02-23
```

**Qué hace:**
1. Lee `currency_rates` de `/mnt/data/appdata/accounting/data/accounting.db`
2. Convierte `rate` (MXN/USD) al formato inverso de Odoo (`rate = 1/mxn_per_usd`)
3. Crea o actualiza `res.currency.rate` en Odoo

**Cuándo ejecutar:**
- Al activar el sistema por primera vez
- Si aparecen errores de "No se pudo obtener tipo de cambio"
- El cron `sync_fx_rates.py` debería llamarlo diariamente a las 8am

---

### `diagnose_inbound.py`

**Propósito:** Diagnóstico rápido del estado del sistema.

```bash
sudo docker exec bridge-inbound-worker python3 /data/diagnose_inbound.py --hours 48
```

---

## 12. Timers del sistema (systemd)

### amazon-poll.timer
- **Qué hace:** Llama `amazon_orders_poll.py --days 2 --marketplace BOTH`
- **Frecuencia:** cada 5 minutos
- **Script:** `/mnt/data/appdata/bridge/app/run_amazon_poll.sh`

### meli-sync.timer
- **Qué hace:** Sincroniza stock outbound a MeLi
- **Frecuencia:** cada 5 minutos
- **Script:** `/mnt/data/appdata/bridge/app/run_meli_sync.sh`

```bash
# Ver estado
sudo systemctl status amazon-poll.timer
sudo systemctl status meli-sync.timer

# Ver próxima ejecución
sudo systemctl list-timers | grep -E "amazon|meli"

# Ejecutar manualmente sin esperar timer
sudo systemctl start amazon-poll.service
sudo systemctl start meli-sync.service
```

---

## 13. MeLi OAuth — tokens

### Flujo de autorización
1. Abrir en navegador: `https://meli.goncloud.cc/oauth/start`
2. MeLi pide autorización al usuario seller
3. Callback en `/oauth/callback` guarda `access_token` + `refresh_token`
4. Tokens guardados en `/data/.meli_tokens.json`

### Refresh del access_token
- `access_token` expira en **~6 horas** (`expires_in=21600`)
- `refresh_token` dura **6 meses** y se **rota en cada refresh** (MeLi lo requiere)
- Refresh manual (fallback):
  ```bash
  curl -X POST https://meli.goncloud.cc/oauth/refresh
  ```
- Si retorna `{"error":"no_refresh_token"}` → el archivo de tokens está corrupto
  o vacío → hacer OAuth completo desde `/oauth/start`

### Auto-refresh (ACTIVO ✓)
Corre cada 6 horas (`5 */6 * * *` UTC → 00:05, 06:05, 12:05, 18:05) vía cron del host:

- **Cron file:** `/etc/cron.d/goncloud_meli_refresh`
- **Script:** `/mnt/data/appdata/bridge/tools/meli_refresh_tokens.sh` (versionado en `tools/meli_refresh_tokens.sh` del repo)
- **Log:** `/mnt/data/appdata/bridge/data/meli_token_refresh.log`
- **Env:** `/mnt/data/appdata/bridge/.env.meli` (exporta `MELI_CLIENT_ID`, `MELI_CLIENT_SECRET`)

El script:
1. Valida `.env.meli` y `.meli_tokens.json` legibles
2. Llama `POST api.mercadolibre.com/oauth/token` con `grant_type=refresh_token`
3. Hace backup del archivo de tokens anterior (`.meli_tokens.json.BK.{ts}`)
4. Sobrescribe `/data/.meli_tokens.json` con el nuevo access + refresh token
5. `chmod 600`, `chown gon:gon`
6. Loguea `OK access_prefix=... refresh_prefix=... backup=...`

**Verificar estado:**
```bash
tail -20 /mnt/data/appdata/bridge/data/meli_token_refresh.log
# Deben verse entradas "OK" cada 6h
```

**Notas:**
- Los workers NO detectan 401 ni hacen retry. El refresh proactivo cada 6h es suficiente porque el token dura 6h, pero si por algún motivo el cron falla dos veces seguidas las peticiones empezarán a fallar con 401.
- Si necesitas margen extra, se puede bajar el cron a cada 5h (`0 */5 * * *`) sin problema.

### Verificar tokens actuales
```bash
sudo cat /mnt/data/appdata/bridge/data/.meli_tokens.json
# Debe tener: access_token, refresh_token, expires_at
```

---

## 14. Amazon SP-API — credenciales y polling

### Credenciales necesarias
| Campo | Dónde obtener |
|-------|---------------|
| `client_id` | Seller Central → Apps & Services → Develop Apps |
| `client_secret` | Mismo lugar |
| `refresh_token` | Se obtiene al autorizar la app SP-API en Seller Central |
| `marketplace_id` | MX: `A1AM78C64UM0Y8` · USA: `ATVPDKIKX0DER` |
| `seller_id` | Seller Central → Account Info |

### Activar Amazon (cuando la cuenta esté verificada)
```bash
# 1. Probar conexión
python3 /data/amazon_orders_poll.py --dry-run --days 7

# 2. Habilitar flags
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fba_paid_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fba_refunds_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fbm_paid_enabled';
UPDATE bridge_settings SET value='1' WHERE key='amazon_inbound_fbm_refunds_enabled';
"

# 3. Activar timer
sudo systemctl enable --now amazon-poll.timer
```

---

## 15. Odoo — integración JSON-RPC

### Endpoint
```
POST {ODOO_URL}/jsonrpc
```

### Autenticación
```python
# 1. Autenticar
uid = jcall('common', 'authenticate', [db, user, password, {}])

# 2. Llamar métodos
result = jcall('object', 'execute_kw', [db, uid, password, model, method, args, kwargs])
```

### Modelos usados

| Modelo Odoo | Uso |
|-------------|-----|
| `sale.order` | Crear/buscar Sales Orders |
| `sale.order.line` | Líneas de SO |
| `stock.picking` | Pickings (despachos) |
| `account.move` | Facturas e invoices |
| `account.payment.register` | Wizard de pago |
| `product.product` | Buscar productos por `default_code` |
| `res.partner` | Partner del cliente |
| `res.currency.rate` | Tasa de cambio USD/MXN |

### Búsqueda de productos
```python
# Prioridad 1: sale_ok=True
domain = [['default_code', '=', sku], ['sale_ok', '=', True]]

# Fallback: cualquier producto con ese default_code
domain = [['default_code', '=', sku]]
```

### Timeout
45 segundos por llamada JSON-RPC.

---

## 16. Tipo de cambio USD/MXN

### Problema resuelto (2026-02-24)
El cron `sync_fx_rates.py` escribía tasas en `accounting.db` pero **nunca** en Odoo.
Los tools leen la tasa SOLO de Odoo (`res.currency.rate`).

### Solución
`tools/push_fx_to_odoo.py` lee de `accounting.db` y escribe en Odoo.

### Verificar que Odoo tiene tasas
```bash
sudo docker exec bridge-amazon-inbound-worker python3 -c "
import requests, sqlite3
db = sqlite3.connect('/data/bridge.db')
def gs(k): return (db.execute('SELECT value FROM bridge_settings WHERE key=?',(k,)).fetchone() or [None])[0]
url,db_,user,pw = gs('odoo_url').rstrip('/'),gs('odoo_db'),gs('odoo_user'),gs('odoo_password')
def jcall(s,m,a):
    return requests.post(url+'/jsonrpc',json={'jsonrpc':'2.0','method':'call','params':
        {'service':s,'method':m,'args':a},'id':1},timeout=30).json().get('result')
uid = jcall('common','authenticate',[db_,user,pw,{}])
rates = jcall('object','execute_kw',[db_,uid,pw,'res.currency.rate','search_read',
    [[['currency_id.name','=','USD']]],{'fields':['rate','name'],'order':'name desc','limit':3}])
for r in (rates or []): print(r['name'], round(1.0/r['rate'],4), 'MXN/USD')
"
```

### Verificar tasas en accounting.db
```bash
sqlite3 /mnt/data/appdata/accounting/data/accounting.db \
  "SELECT rate_date, rate FROM currency_rates
   WHERE base_currency='MXN' AND quote_currency='USD'
   ORDER BY rate_date DESC LIMIT 7;"
```

### Backfill manual
```bash
# Última tasa disponible
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py

# Fecha específica
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py 2026-02-23
```

---

## 17. SKU mapping

### MeLi — resolución de SKU

```
1. seller_sku del ítem directamente (campo del item en la orden)
2. sku_mapping table (canal, remote_item_id, sku)
3. inbound_allowed_skus (auto-sync desde sku_mapping al guardar via API)
```

**UI:** `https://meli.goncloud.cc/mapper`

### Amazon — resolución de SKU

```
1. amazon_sku_mapping: seller_sku → odoo_default_code  (mapeo manual legacy)
2. product.product.default_code == SellerSKU (búsqueda directa en Odoo)

Si no se encuentra → manual_review con mensaje "missing products for SKUs: [...]"
```

**UI:** `https://meli.goncloud.cc/amazon/mapper`

### Agregar nuevo SKU mapping

```bash
# 1. Agregar via UI (/mapper o /amazon/mapper)

# 2. Inmediatamente reprocesar órdenes atascadas
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead

# 3. Verificar que procesó
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT dedupe_key, result FROM amazon_processed_events ORDER BY processed_at DESC LIMIT 5;"
```

---

## 18. Deduplicación y estados de job

### Estados posibles

| Estado | Significado | Acción del sistema |
|--------|-------------|-------------------|
| `success` | Procesado correctamente | **Bloquear** re-procesamiento |
| `dead` | Máximo de reintentos alcanzado | **Bloquear** (solo limpiar manualmente) |
| `manual_review` | Error que requiere intervención humana | **Retryable** (auto-curativo) |
| `error` | Error transitorio | **Retryable** |
| `deferred` | Esperando condición (ej. SO no existe aún) | **Retryable** |

### Formato de dedupe_key

**MeLi:** basado en SHA256 del payload (webhook-based)
```
rawsha:{sha256_primeros_100_chars}
```

**Amazon:** basado en la orden
```
amz:{marketplace_id}:{order_id}:{status}
```

### Reprocesar orden específica

```bash
# Amazon
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM amazon_processed_events WHERE dedupe_key LIKE '%701-XXXXXXX-XXXXXXX%';"
# Luego forzar poll
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 7 --marketplace BOTH

# MeLi
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM processed_inbound_events WHERE dedupe_key LIKE '%ORDER-ID%';"
# Re-enviar webhook desde MeLi (no hay payload persistido para rawsha)
```

---

## 19. Operaciones cotidianas

### Health check
```bash
curl http://127.0.0.1:8099/v1/health
# Esperado: {"ok": true}
```

### Ver estado de contenedores
```bash
sudo docker ps | grep bridge
```

### Ver colas Redis
```bash
sudo docker exec bridge-redis redis-cli LLEN ml_orders_jobs
sudo docker exec bridge-redis redis-cli LLEN amazon_orders_jobs
sudo docker exec bridge-redis redis-cli LLEN stock_jobs
sudo docker exec bridge-redis redis-cli LLEN amazon_orders_dead
```

### Conteo de resultados últimas 72h

```bash
# Amazon
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT result, COUNT(*) FROM amazon_processed_events
   WHERE processed_at >= datetime('now','-72 hours') GROUP BY result;"

# MeLi
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT result, COUNT(*) FROM processed_inbound_events
   WHERE processed_at >= datetime('now','-72 hours') GROUP BY result;"
```

### Buscar orden específica
```bash
# Amazon
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT dedupe_key, processed_at, result, detail_json
   FROM amazon_processed_events WHERE dedupe_key LIKE '%ORDER-ID%';"

# MeLi
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT dedupe_key, result, processed_at
   FROM processed_inbound_events WHERE dedupe_key LIKE '%ORDER-ID%';"
```

### Ver todos los settings
```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT key, value FROM bridge_settings ORDER BY key;"
```

### Polling manual Amazon
```bash
# Últimos 2 días, ambos marketplaces
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace BOTH

# Solo México
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace MX

# Solo USA
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace US
```

### Reiniciar services después de deploy
```bash
sudo docker restart bridge-inbound-worker
sudo docker restart bridge-amazon-inbound-worker
sudo docker restart bridge-api
```

---

## 20. Diagnóstico y troubleshooting

### Diagnóstico completo automático
```bash
sudo docker exec bridge-inbound-worker python3 /data/diagnose_inbound.py --hours 48
```

### Problema: órdenes en manual_review por SKU faltante

**Síntoma:** `ERROR missing products for SKUs: ['XX-XXXX']`

```bash
# Verificar si existe el mapping
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT seller_sku, odoo_default_code FROM amazon_sku_mapping WHERE seller_sku='XX-XXXX';"

# Si no existe → agregar en /amazon/mapper, luego:
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead
```

### Problema: error de tipo de cambio USD/MXN

**Síntoma:** `No se pudo obtener tipo de cambio` en detail_json

```bash
# 1. Verificar tasas en Odoo
sudo docker exec bridge-amazon-inbound-worker python3 -c "
import requests, sqlite3
db = sqlite3.connect('/data/bridge.db')
def gs(k): return (db.execute('SELECT value FROM bridge_settings WHERE key=?',(k,)).fetchone() or [None])[0]
url,db_,user,pw = gs('odoo_url').rstrip('/'),gs('odoo_db'),gs('odoo_user'),gs('odoo_password')
def jcall(s,m,a):
    return requests.post(url+'/jsonrpc',json={'jsonrpc':'2.0','method':'call','params':
        {'service':s,'method':m,'args':a},'id':1},timeout=30).json().get('result')
uid = jcall('common','authenticate',[db_,user,pw,{}])
rates = jcall('object','execute_kw',[db_,uid,pw,'res.currency.rate','search_read',
    [[['currency_id.name','=','USD']]],{'fields':['rate','name'],'order':'name desc','limit':3}])
print('rates:', rates)
"

# 2. Si vacío → backfill
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py

# 3. Reprocesar las órdenes afectadas
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead
```

### Problema: Amazon poll falla con "timestamp must follow ISO8601"

```bash
# Verificar formato del timestamp en el poll
grep "strftime\|isoformat" /mnt/data/appdata/bridge/data/amazon_orders_poll.py
# Debe mostrar: strftime('%Y-%m-%dT%H:%M:%SZ')
# Si muestra .isoformat() → versión vieja, hay que actualizar
```

### Problema: MeLi access_token expirado

```bash
# Intentar refresh
curl -X POST https://meli.goncloud.cc/oauth/refresh

# Si da error "no_refresh_token" → OAuth completo
# Abrir en navegador: https://meli.goncloud.cc/oauth/start
```

### Problema: tool busca en /tools/ antes que /data/ (causa de bugs recurrentes)

```bash
# Verificar orden en inbound_worker.py
grep -A5 "tool_candidates" /mnt/data/appdata/bridge/app/inbound_worker.py
# La primera línea DEBE ser "/data/..."

# Verificar orden en amazon_inbound_worker.py
grep -A5 "tool_candidates\|_candidates" /mnt/data/appdata/bridge/app/amazon_inbound_worker.py
# La primera línea DEBE ser "/data/..."
```

### Problema: Flex MX órdenes Pending no entran

```bash
# Verificar que el poll no saltea Pending para Flex MX
grep -A5 "FLEX_MX\|is_flex_mx" /mnt/data/appdata/bridge/data/amazon_orders_poll.py | head -30
# Debe haber lógica: if is_flex_mx: NO saltear Pending

# Forzar recuperación
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace MX
```

### Problema: órdenes dead "max_deferred_exceeded" para Canceled

**Explicación:** Órdenes que se cancelaron en Amazon antes de que el sistema las capturara
como Shipped/Pending. No hay SO en Odoo que cancelar. Es **benigno**, no requiere acción.

```bash
# Confirmar que son cancelaciones sin SO previo
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT dedupe_key, detail_json FROM amazon_processed_events
   WHERE result='dead' ORDER BY processed_at DESC LIMIT 10;"
```

---

## 21. Recuperación de órdenes atascadas

### Recover general (recomendado primero)
```bash
# Dry-run
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead --dry-run

# Ejecutar
sudo docker exec bridge-amazon-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead

# MeLi
sudo docker exec bridge-inbound-worker python3 /data/recover_manual_review.py \
  --channel meli --hours 168 --include-dead
```

### Reset completo de una orden (nuclear)
```bash
# 1. En Odoo: cancelar y eliminar todos los SOs/facturas de la orden

# 2. Borrar el audit
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM amazon_processed_events WHERE dedupe_key LIKE '%ORDER-ID%';"

# 3. Forzar re-poll
sudo docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 7 --marketplace BOTH
```

---

## 22. Bugs resueltos — NO reintroducir

| Fecha | Bug | Síntoma | Fix |
|-------|-----|---------|-----|
| 2026-02-20 | `is_already_completed` bloqueaba `manual_review` | Órdenes atascadas para siempre | Solo bloquear `success` y `dead` |
| 2026-02-21 | Poll Amazon sin `Z` en timestamp | HTTP 400 Amazon US | `strftime('%Y-%m-%dT%H:%M:%SZ')` |
| 2026-02-21 | tool errors → `RC=2` (deferred) | Se re-intentaban infinitamente sin notificar | Usar `RC=1` (manual_review) |
| 2026-02-22 | `client_order_ref` = `AMZFBM:mkt:id` | Prefijo interno visible en Odoo | `display_ref = f"{order_id} \| {buyer}"` |
| 2026-02-22 | `price_unit` solo usaba `ItemPrice.Amount` | Precio incorrecto (sin envío ni tax) | Sales Proceeds = suma de todos los componentes |
| 2026-02-23 | Worker FULL paid buscaba tool en `/tools/` primero | Fix en `/data/` ignorado | Invertir `tool_candidates`: `/data/` primero |
| 2026-02-23 | FULL refund con path hardcodeado | Fix en `/data/` ignorado para refunds | `_refund_candidates` list con `/data/` primero |
| 2026-02-23 | Poll salteaba `Pending` aunque fuera Flex MX | Flex MX nunca procesado | `is_flex_mx=True` → no saltear |
| 2026-02-24 | `res.currency.rate` vacío en Odoo | Órdenes Amazon US fallan por conversión | `push_fx_to_odoo.py` escribe el rate en Odoo |
| 2026-02-24 | Flex MX Pending→Unshipped creaba SO duplicado | Factura $0, SO sin picking o duplicado | Búsqueda OR por `display_ref` Y `order_id`; actualiza ref si aparece buyer |
| 2026-02-25 | `app/amazon_fba_paid_one_shot.py` desincronizada | `NameError: IS_USD_ORDER`, factura $0, precio incompleto | Sincronizar `app/` con `tools/` |
| 2026-02-25 | `IS_USD_ORDER` basado en marketplace ID | Si flag no llega, 84 USD → 84 MXN silencioso | Leer `OrderTotal.CurrencyCode` como fuente de verdad |

---

## 23. Deploy de fixes

### Después de cualquier cambio en tools/

```bash
# Actualizar repo
cd /tmp/goncloud-bridge-in-out && sudo git fetch && sudo git pull

# Copiar a /data/ (deployment target, máxima prioridad)
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_paid_one_shot.py            /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fba_refund_and_cancel.py        /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_fbm_refund_and_cancel.py        /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/amazon_orders_poll.py                  /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_full_paid_one_shot_no_stock.py /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_full_so_refund_and_cancel.py   /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_fbm_so_apply_paid_one_shot.py  /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/inbound_fbm_so_refund_and_cancel.py    /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/recover_manual_review.py               /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/push_fx_to_odoo.py                     /mnt/data/appdata/bridge/data/
sudo cp /tmp/goncloud-bridge-in-out/tools/diagnose_inbound.py                    /mnt/data/appdata/bridge/data/
```

### Si se modificó un worker

```bash
# Inbound worker MeLi
sudo cp /tmp/goncloud-bridge-in-out/app/inbound_worker.py /mnt/data/appdata/bridge/app/
sudo docker restart bridge-inbound-worker

# Inbound worker Amazon
sudo cp /tmp/goncloud-bridge-in-out/app/amazon_inbound_worker.py /mnt/data/appdata/bridge/app/
sudo docker restart bridge-amazon-inbound-worker

# API
sudo cp /tmp/goncloud-bridge-in-out/app/main.py /mnt/data/appdata/bridge/app/
sudo docker restart bridge-api
```

### Verificar versión activa de un tool
```bash
grep "^# v\|^VERSION\|version" /mnt/data/appdata/bridge/data/amazon_fba_paid_one_shot.py | head -3
grep "IS_USD_ORDER" /mnt/data/appdata/bridge/data/amazon_fba_paid_one_shot.py
# Debe mostrar: IS_USD_ORDER = os.getenv("IS_USD_ORDER", "0") == "1"
```

---

## 24. Outbound — sincronización de stock

### Fuente: módulo Odoo `bridge_connector`

El módulo Odoo envía un snapshot de stock al endpoint `/v1/stock-snapshot` del bridge-api.

- **Cron en Odoo:** `Bridge: Stock Snapshot (Primary Channels)` (cada 5 min aprox.)
- **Forzar manualmente:**
  ```bash
  docker exec -it odoo-odoo-1 bash -lc "
  odoo shell -d EHV --db_host db --db_user odoo --db_password odoo <<'PY'
  env['ir.cron'].browse(39).method_direct_trigger()
  print('SNAPSHOT OK')
  PY
  "
  ```

### Colas Redis outbound

- `stock_jobs` — bridge-worker procesa y actualiza listings

### MeLi outbound

```
PUT https://api.mercadolibre.com/items/{item_id}/variations/{var_id}
Body: {"available_quantity": N}
Auth: Bearer {access_token}
```

### Amazon outbound

```
PATCH https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/items/{seller_id}/{sku}
Body: JSON-PATCH de quantity
Auth: Bearer {sp_api_token}
```

### Troubleshooting outbound

```bash
# Ver SKUs rechazados
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT channel, sku, reason FROM rejected_skus ORDER BY rowid DESC LIMIT 20;"

# Ver métricas
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT key, value FROM bridge_metrics WHERE key LIKE 'skus_rejected_total%';"

# Ver snapshots recientes
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT id, created_at, channel, item_count FROM events ORDER BY id DESC LIMIT 5;"
```

---

## 25. Módulo Odoo bridge_connector

### Propósito

Módulo Odoo que envía snapshots de stock al bridge. Instalado en la instancia Odoo EHV.

### Archivos

```
bridge_connector/
├── __init__.py
├── __manifest__.py      # versión 1.0, depends: ['stock']
└── models.py            # sync_stock_fbm() → POST /v1/stock-snapshot
```

### Instalación en Odoo

```bash
# Copiar al addons path de Odoo
sudo cp -r /tmp/goncloud-bridge-in-out/bridge_connector/ \
  /mnt/data/appdata/odoo/addons/

# Actualizar lista de módulos en Odoo
# Settings → Technical → Update Apps List

# Instalar módulo
# Settings → Apps → buscar "bridge_connector" → instalar
```

### Verificar cron activo

```bash
docker exec -it odoo-odoo-1 bash -lc "
odoo shell -d EHV --db_host db --db_user odoo --db_password odoo <<'PY'
c = env['ir.cron'].search([('name','=','Bridge: Stock Snapshot (Primary Channels)')])
print(c.id, c.active)
PY
"
```

---

## Apéndice A — Checklist de health completo

```bash
# 1. Contenedores corriendo
sudo docker ps | grep bridge

# 2. Health API
curl http://127.0.0.1:8099/v1/health

# 3. Colas Redis vacías o procesando
sudo docker exec bridge-redis redis-cli LLEN ml_orders_jobs
sudo docker exec bridge-redis redis-cli LLEN amazon_orders_jobs

# 4. Sin manual_review recientes (últimas 24h)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT result, COUNT(*) FROM amazon_processed_events
   WHERE processed_at >= datetime('now','-24 hours') GROUP BY result;"

# 5. Tasa USD/MXN en Odoo existe
# (usar comando de la sección 16)

# 6. MeLi tokens válidos
sudo cat /mnt/data/appdata/bridge/data/.meli_tokens.json | python3 -m json.tool

# 7. Timers activos
sudo systemctl status amazon-poll.timer meli-sync.timer

# 8. Cron Odoo activo
# (usar comando de la sección 25)
```

## Apéndice B — Backup y restauración

```bash
# Backup de la DB
cp /mnt/data/appdata/bridge/data/bridge.db \
   /mnt/data/appdata/bridge/data/bridge.db.bak.$(date +%Y%m%d)

# Backup de tokens
cp /mnt/data/appdata/bridge/data/.meli_tokens.json \
   /mnt/data/appdata/bridge/data/.meli_tokens.json.bak

# Backup completo via restic
/usr/local/bin/restic-server-backup.sh
```

## Apéndice C — Contacto y repositorio

- **Repositorio:** `https://github.com/gon0801/goncloud-bridge-in-out.git`
- **Branch activo:** `claude/review-inbound-outbound-G9HeG`
- **URL producción:** `https://meli.goncloud.cc`
- **Servidor:** VPS Hetzner — alias `gonserver` / `goncloud` (IP pública `65.109.4.81`, Tailscale `100.127.167.103`, user `root`)
- **Transcripts de sesiones:** `/mnt/transcripts/`

---

*Documento generado 2026-03-01. Para actualizarlo, editar este archivo y hacer commit + push.*
