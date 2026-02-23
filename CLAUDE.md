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
>
> Ejemplo de lo que NO hacer: asumir que un status "Pending" es un lag de Amazon sin
> verificar si existe lógica especial (Flex MX procesa Pending intencionalmente).

---

## Índice

1. [¿Qué es este sistema?](#1-qué-es-este-sistema)
2. [Arquitectura](#2-arquitectura)
3. [Contenedores Docker](#3-contenedores-docker)
4. [Canales y flujos de negocio](#4-canales-y-flujos-de-negocio)
5. [Reglas de negocio selladas](#5-reglas-de-negocio-selladas)
6. [Base de datos SQLite](#6-base-de-datos-sqlite)
7. [Archivos clave](#7-archivos-clave)
8. [Problemas conocidos y soluciones](#8-problemas-conocidos-y-soluciones)
9. [Comandos de diagnóstico](#9-comandos-de-diagnóstico)
10. [Bugs resueltos — NO volver a introducir](#10-bugs-resueltos--no-volver-a-introducir)
11. [Estado actual](#11-estado-actual)
12. [Diario de cambios](#12-diario-de-cambios)

---

## 1. ¿Qué es este sistema?

**GONCLOUD Bridge** conecta marketplaces (MercadoLibre, Amazon) con Odoo 17 ERP.

- **Inbound:** Órdenes de MeLi/Amazon → crean Sale Orders + facturas en Odoo
- **Outbound:** Cambios de stock en Odoo → actualizan listings en MeLi/Amazon
- **Servidor:** gonserver
- **URL pública:** `https://meli.goncloud.cc`
- **Bridge DB:** SQLite en `/mnt/data/appdata/bridge/data/bridge.db`
- **App en contenedor:** `/mnt/data/appdata/bridge/`
- **Repo git en gonserver:** `/tmp/goncloud-mcp/`

---

## 2. Arquitectura

```
Amazon SP-API / MeLi webhooks
        |
bridge-api (FastAPI :8099)
  /webhooks/meli/orders/{secret}
  /webhooks/amazon/orders/{secret}
        |
bridge-redis (colas)
  ml_orders_jobs / amazon_orders_jobs / stock_jobs
        |
bridge-inbound-worker (MeLi)   bridge-amazon-inbound-worker (Amazon)
        |                                    |
Tools: inbound_*.py / amazon_*.py
        |
Odoo 17 (XML-RPC / JSON-RPC)
  Sale Orders + Invoices + Stock Pickings
```

---

## 3. Contenedores Docker

| Contenedor | Rol | Puerto |
|------------|-----|--------|
| `bridge-api` | FastAPI: webhooks, setup, mapper | 127.0.0.1:8099 |
| `bridge-redis` | Colas Redis | interno |
| `bridge-worker` | Outbound stock sync | - |
| `bridge-inbound-worker` | Inbound MercadoLibre | - |
| `bridge-amazon-inbound-worker` | Inbound Amazon | - |

```bash
# Estado
sudo docker ps | grep bridge

# Logs
sudo docker logs bridge-inbound-worker --tail 50 -f
sudo docker logs bridge-amazon-inbound-worker --tail 50 -f
sudo docker logs bridge-api --tail 50 -f

# Reiniciar
sudo docker restart bridge-inbound-worker
sudo docker restart bridge-amazon-inbound-worker

# Para copiar script del repo al contenedor:
sudo cp /tmp/goncloud-mcp/tools/SCRIPT.py /mnt/data/appdata/bridge/data/
```

---

## 4. Canales y flujos de negocio

### MercadoLibre Inbound

| Tipo | Detección | Accion Odoo |
|------|-----------|-------------|
| **FULL paid** | `logistic_type=fulfillment` + `status=paid` | SO + Invoice pagada (sin picking) |
| **FULL cancelled/canceled** | `logistic_type=fulfillment` + `status=cancelled` | Credit Note + cancel SO |
| **FULL refunded** | `logistic_type=fulfillment` + `status=refunded` | Credit Note + cancel SO |
| **FULL returned** | `logistic_type=fulfillment` + `status=returned` | **NO tocar** (esperar `refunded`) |
| **FBM paid** | `logistic_type!=fulfillment` + `status=paid` | SO + Picking + Invoice pagada |
| **FBM cancelled** | `logistic_type!=fulfillment` + `status=cancelled` | Credit Note + cancel picking |

**Prefijos en Odoo:** `MLFULL` / `MLFBM`
**Nota de SO:** `#pack_id | nombre_comprador`

### Amazon Inbound

| Tipo | Deteccion | Accion Odoo |
|------|-----------|-------------|
| **FBA paid** | `FulfillmentChannel=AFN` + `OrderStatus=Shipped` | SO + Invoice pagada (sin picking) |
| **FBA cancelled** | `FulfillmentChannel=AFN` + `OrderStatus=Canceled` | Credit Note |
| **FBM paid** | `FulfillmentChannel=MFN` + `OrderStatus=Unshipped/Shipped` | SO + Picking + Invoice pagada |
| **FBM cancelled** | `FulfillmentChannel=MFN` + `OrderStatus=Canceled` | Credit Note + cancel picking |
| **Flex MX paid** | `FulfillmentChannel=AFN` + `Marketplace=MX` + `OrderStatus=Pending` | SO + Picking + Invoice pagada |

**Prefijos en Odoo:** `AMZFBM` / `AMZFBA`
**Nota de SO:** `orden_id | nombre_comprador`
**customer_reference:** `orden_id | nombre_comprador` (limpia prefijos AMZFBM/MLFBM/MLFULL)

### Amazon Flex MX — LOGICA ESPECIAL (MUY IMPORTANTE)

```
Flex MX = AFN channel + Marketplace ID A1AM78C64UM0Y8 (Mexico)
Estas ordenes llegan en status "Pending" y DEBEN procesarse inmediatamente.
El worker detecta: profile == "FLEX_MX" and status == "Pending" -> action = "paid"
El POLL no debe saltear estas ordenes aunque esten en Pending.
```

**Si una orden Flex MX no entro, verificar que `amazon_orders_poll.py` NO salte Pending para Flex MX.**

### Marketplaces Amazon
| Pais | Marketplace ID |
|------|----------------|
| Mexico | A1AM78C64UM0Y8 |
| USA | ATVPDKIKX0DER |

**Conversion de moneda:** Ordenes Amazon USA (USD) se convierten a MXN usando tipo de cambio de Odoo.

### Outbound
- MeLi: `meli-sync.timer` cada 5 min
- Amazon: `amazon-poll.timer` cada 5 min, `--days 2 --marketplace BOTH`

---

## 5. Reglas de negocio selladas

### FULL MeLi — Canonico (ANEXO C — NO modificar sin nueva version)

```
SOLO neutralizar contabilidad con senal explicita de REFUND.
"returned" por si solo NO garantiza refund -> NO tocar contabilidad.

Ejecutar refund/cancel si status IN {cancelled, canceled, refunded}
NO ejecutar si status IN {returned, returning, to_be_returned}
```

### Deduplicacion

```
is_already_completed: bloquear SOLO si result IN {success, dead}
manual_review y error son RETRYABLES (auto-curacion del sistema)
```

### Resolucion de SKU

```
Amazon:
  1. product_product.default_code = SellerSKU
  2. product_product.x_amazon_sku = SellerSKU (legacy)
  3. amazon_sku_mapping table (mapeo manual)

MeLi:
  1. seller_sku del item directamente
  2. sku_mapping table
  3. inbound_allowed_skus (auto-sync desde sku_mapping)
```

### Precios Amazon

```
price_unit = Sales Proceeds COMPLETOS:
  ItemPrice + ItemTax + Shipping + ShippingTax
(NO usar solo ItemPrice)
```

### Poll Amazon timestamp

```
Formato: ISO 8601 con "Z" al final -> strftime('%Y-%m-%dT%H:%M:%SZ')
Sin "Z" -> Amazon US devuelve HTTP 400
```

### Flujo al agregar nuevo SKU mapping

```
1. Agregar en /amazon/mapper o directo en amazon_sku_mapping
2. INMEDIATAMENTE ejecutar recover_manual_review.py para re-intentar atascadas
3. Verificar que proceso
```

---

## 6. Base de datos SQLite

**En contenedor:** `/data/bridge.db`
**En host:** `/mnt/data/appdata/bridge/data/bridge.db`

### Tablas principales

| Tabla | Proposito |
|-------|-----------|
| `bridge_settings` | Configuracion key-value (flags on/off, credenciales) |
| `processed_inbound_events` | Auditoria MeLi: success/manual_review/dead/error/skipped |
| `amazon_processed_events` | Auditoria Amazon (mismo esquema) |
| `inbound_job_payloads` | Payloads persistentes (fix reaper v8.4) |
| `inbound_processing_locks` | Locks transientes entre workers |
| `sku_mapping` | default_code <-> meli_item_id |
| `amazon_sku_mapping` | SKU legacy Amazon -> Odoo default_code |
| `inbound_allowed_skus` | Auto-sync desde sku_mapping |
| `amazon_orders_state` | Ultimo estado conocido de cada orden Amazon |

### Flags criticos (bridge_settings)

```bash
# Ver todos
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT key, value FROM bridge_settings ORDER BY key;"

# Flags principales
meli_inbound_enabled
meli_inbound_full_paid_enabled
meli_inbound_fbm_paid_enabled
amazon_inbound_enabled
amazon_inbound_fba_paid_enabled
amazon_inbound_fbm_paid_enabled
```

---

## 7. Archivos clave

```
/mnt/data/appdata/bridge/
├── app/
│   ├── main.py                     # FastAPI: webhooks, setup, oauth
│   ├── inbound_worker.py           # Worker MeLi inbound (v8.4+)
│   ├── amazon_inbound_worker.py    # Worker Amazon inbound
│   ├── amazon_fba_paid_one_shot.py # Tool: procesar FBA manualmente
│   ├── worker.py                   # Worker outbound stock
│   └── debug_flex_order.py         # Debug ordenes Flex/MeLi
├── tools/ (tambien en /data/ para el contenedor)
│   ├── amazon_orders_poll.py       # Polling SP-API -> Redis
│   ├── recover_manual_review.py    # Re-encolar ordenes atascadas
│   ├── diagnose_inbound.py         # Diagnostico completo del sistema
│   ├── amazon_fba_paid_one_shot.py # Reprocesar FBA manualmente
│   ├── amazon_fbm_paid_one_shot.py # Reprocesar FBM manualmente
│   ├── debug_meli_order.py         # Debug orden MeLi especifica
│   └── inbound_fbm_so_apply_paid_one_shot.py
├── data/
│   ├── bridge.db                   # SQLite principal
│   └── .meli_tokens.json           # Tokens OAuth MeLi
└── docs/
    ├── RUNBOOK.md                  # Runbook operativo completo
    └── ANEXO_C_FULL_ML_STATUS.md  # Reglas FULL MeLi (selladas)
```

**Scripts que deben estar siempre actualizados en `/data/`:**
- `amazon_orders_poll.py`
- `recover_manual_review.py`
- `diagnose_inbound.py`
- `amazon_fba_paid_one_shot.py`
- `amazon_fbm_paid_one_shot.py`

---

## 8. Problemas conocidos y soluciones

### PROBLEMA 1: Ordenes Flex MX que no entran a Odoo (status Pending)

**Flex MX = AFN + marketplace MX (A1AM78C64UM0Y8). Se procesan en `Pending` para crear SO+picking.**

El worker detecta esto: `if profile == "FLEX_MX" and status == "Pending": action = "paid"`

**El poll NO debe saltear Flex MX Pending.** Verificar fix:

```bash
grep -A5 "is_flex_mx\|Flex MX\|Pending" \
  /mnt/data/appdata/bridge/data/amazon_orders_poll.py | head -20

# Forzar recuperacion
docker exec bridge-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace MX
```

### PROBLEMA 2: Ordenes en manual_review por SKU faltante

**Sintoma:** `[FBM_PAID] ERROR missing products for SKUs: ['XX-XXXX-XXXX']`

```bash
# Verificar si el mapping existe
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "SELECT seller_sku, odoo_default_code FROM amazon_sku_mapping WHERE seller_sku='XX-XXXX';"

# Si no existe -> agregar en /amazon/mapper
# Luego SIEMPRE ejecutar recover:
docker exec bridge-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead --dry-run

# Sin dry-run para ejecutar real
docker exec bridge-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead
```

**IMPORTANTE:** Ejecutar `recover_manual_review.py` CADA VEZ que se agrega un nuevo SKU mapping.

### PROBLEMA 3: Poll Amazon falla con "timestamp must follow ISO8601"

**Causa:** Script usa `.isoformat()` que produce `+00:00`. Amazon requiere `Z`.

```bash
# Verificar
grep "created_after\|strftime\|isoformat" \
  /mnt/data/appdata/bridge/data/amazon_orders_poll.py
# Debe mostrar strftime('%Y-%m-%dT%H:%M:%SZ'), no isoformat()
```

### PROBLEMA 4: Scripts del repo no desplegados en servidor

```bash
sudo cp /tmp/goncloud-mcp/tools/SCRIPT.py \
        /mnt/data/appdata/bridge/data/
```

### PROBLEMA 5: MeLi `no_valid_items` o `manual_review`

```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, processed_at, result, detail_json
FROM processed_inbound_events
WHERE result IN ('manual_review', 'dead')
  AND processed_at >= datetime('now', '-48 hours')
ORDER BY processed_at DESC;"
```

**Nota:** Ordenes MeLi con `rawsha:` como dedupe_key NO tienen payload persistido.
Para recuperarlas: re-enviar el webhook desde MeLi o re-notificar desde la UI.

---

## 9. Comandos de diagnostico

```bash
# Diagnostico completo del sistema
docker exec bridge-inbound-worker python3 /data/diagnose_inbound.py --hours 48

# Estado de colas Redis
docker exec bridge-inbound-worker python3 -c "
import redis
r = redis.Redis(host='bridge-redis', port=6379, decode_responses=True)
for q in ['amazon_orders_jobs','amazon_orders_dead','ml_orders_jobs','ml_orders_dead']:
    print(q, r.llen(q))
"

# Conteo de resultados Amazon (ultimas 72h)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT result, COUNT(*) FROM amazon_processed_events
WHERE processed_at >= datetime('now', '-72 hours')
GROUP BY result ORDER BY COUNT(*) DESC;"

# Ver ultimos eventos MeLi
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, result, processed_at FROM processed_inbound_events
ORDER BY processed_at DESC LIMIT 10;"

# Buscar orden especifica (Amazon)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, processed_at, result, detail_json
FROM amazon_processed_events
WHERE dedupe_key LIKE '%ORDER-ID%';"

# SOs en Odoo por canal:
-- AMZFBM: SELECT name, client_order_ref FROM sale_order WHERE client_order_ref LIKE 'AMZFBM%' ORDER BY id DESC LIMIT 5;
-- MLFULL: SELECT name, client_order_ref FROM sale_order WHERE client_order_ref LIKE 'MLFULL%' ORDER BY id DESC LIMIT 5;

# Reprocesar orden (eliminar audit para re-inyectar)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db \
  "DELETE FROM amazon_processed_events WHERE dedupe_key='ORDEN_ID';"
```

---

## 10. Bugs resueltos — NO volver a introducir

| Fecha | Bug | Fix |
|-------|-----|-----|
| 2026-02-20 | `is_already_completed` bloqueaba `manual_review` y `dead` | Solo bloquear `success` y `dead` definitivos |
| 2026-02-20 | Worker Amazon: 3 bugs en path webhook | Corregidos en main.py y tools/worker.py |
| 2026-02-21 | Poll Amazon USA devolvía HTTP 400 | Timestamp con `Z` al final (strftime ISO8601) |
| 2026-02-21 | `manual_review` y `dead` no se reintentaban | Hacerlos retryables para auto-curación |
| 2026-02-22 | MeLi: `parse_items` no leía `seller_sku` directo | Leer `seller_sku` directo del item |
| 2026-02-22 | Amazon: SKUs legacy no mapeaban | Aplicar `amazon_sku_mapping` en worker y tools |
| 2026-02-22 | `sku_mapping` no sync a `inbound_allowed_skus` | Auto-sync al guardar sku_mapping |
| 2026-02-22 | Amazon US: precio en USD sin convertir | Convertir USD->MXN con tipo de cambio Odoo |
| 2026-02-22 | `price_unit` Amazon incompleto | Sales Proceeds completos (ItemPrice+Tax+Shipping) |
| 2026-02-23 | Amazon FBA: `display_ref` usaba CLIENT_ORDER_REF | Usar `display_ref` del SO directamente |
| 2026-02-23 | `customer_reference` no limpiaba prefijos MeLi | Limpiar MLFBM/MLFULL igual que AMZFBM |

---

## 11. Estado actual

**Fecha de ultima actualizacion:** 2026-02-23
**Branch activo:** `claude/review-inbound-outbound-G9HeG`
**Worker MeLi:** v8.4 "Payload-Persistent"

### Funcionando
- MeLi inbound: FULL paid/cancel/refund, FBM paid/cancel/refund
- Amazon inbound: FBA paid, FBM paid, Flex MX Pending, flows de cancel
- Outbound stock sync: MeLi y Amazon
- Setup wizard, SKU mapper
- Auto-curacion: `manual_review`/`dead` son retryables
- Conversion USD->MXN para Amazon US
- Notas de SO: `orden_id | nombre_comprador` (limpio, sin prefijos)

### Pendiente
- Amazon: verificacion de cuenta SP-API (polling se habilita cuando sea aprobado)
- Limpieza periodica de `manual_review` antiguos (script existe, no automatizado)

---

## 12. Diario de cambios

### 2026-02-23
- Fix: `amazon_fba_paid_one_shot` usa `display_ref` en lugar de `CLIENT_ORDER_REF`
- Fix: `amazon_fba_paid_one_shot` convierte USD->MXN y corrige `unit_price`
- Fix: `customer_reference` limpia prefijos MeLi (MLFBM/MLFULL)
- Creado `CLAUDE.md` unificado con contexto completo + diario

### 2026-02-22
- Fix: `customer_reference` en Odoo limpia prefijo AMZFBM, muestra `orden | comprador`
- Fix: auto-sync `sku_mapping` -> `inbound_allowed_skus`
- Fix: `price_unit` Amazon = Sales Proceeds completos
- Fix: conversion USD->MXN para Amazon US
- Fix: `amazon_sku_mapping` para SKUs legacy
- Fix: `parse_items` MeLi lee `seller_sku` directo
- Fix: `is_already_completed` solo bloquea `success`/`dead`

### 2026-02-21
- Fix: nota SO Amazon FBA incluye nombre del comprador
- Fix: nota SO MeLi muestra `#pack_id`
- Feat: `debug_meli_order.py` para diagnostico
- Fix: `manual_review` y `dead` son retryables
- Fix: poll timestamp usa `Z` (fix HTTP 400 Amazon US)
- Fix: tool errors -> `RC=1` (manual_review) no RC=2

### 2026-02-20
- Fix: 3 bugs criticos en path webhook Amazon inbound
- Fix: imports faltantes (`logging`, `urllib.parse`)
- Fix: errores de copia en `main.py` y `tools/worker.py`
- Initial clean import del proyecto al repo
