# GONCLOUD Bridge — Base de conocimiento para Claude

Este archivo documenta problemas recurrentes, sus causas raíz y los comandos exactos para resolverlos.
**Leer ANTES de investigar cualquier falla de inbound.**

---

## Arquitectura rápida

```
Amazon SP-API / MeLi webhooks
        ↓
bridge-inbound-worker (Redis queue)
        ↓
amazon_inbound_worker.py / inbound_worker.py
        ↓
Tools: amazon_fbm_paid_one_shot.py / amazon_fba_paid_one_shot.py / inbound_fbm_so_apply_paid_one_shot.py
        ↓
Odoo 17 (JSON-RPC)
```

**Tablas clave:**
- `amazon_processed_events` — resultado de cada orden Amazon (success/manual_review/dead/skipped)
- `processed_inbound_events` — resultado de cada orden MeLi
- `amazon_orders_state` — último estado conocido de cada orden Amazon
- `amazon_sku_mapping` — mapeo Amazon SellerSKU → Odoo default_code (para SKUs legacy)
- `sku_mapping` — mapeo MeLi item_id+variation_id → Odoo SKU
- `bridge_settings` — kill-switches y configuración (odoo_url, odoo_db, etc.)

**Scripts de operación (deben estar en `/mnt/data/appdata/bridge/data/` para que los corra el contenedor):**
- `amazon_orders_poll.py` — polling SP-API → Redis
- `recover_manual_review.py` — re-encola órdenes atascadas
- `diagnose_inbound.py` — diagnóstico completo (kill-switches, colas, errores)

---

## PROBLEMA 1: Órdenes que aparecen en el dashboard pero no entran a Odoo

### Síntoma
Dashboard muestra N órdenes, Odoo tiene N-1 (o menos).

### Diagnóstico rápido
```bash
# Ver las últimas 48h de Amazon
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, processed_at, result
FROM amazon_processed_events
WHERE processed_at >= datetime('now', '-48 hours')
ORDER BY processed_at DESC LIMIT 30;"

# Ver si la orden específica existe
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, result, detail_json
FROM amazon_processed_events
WHERE dedupe_key LIKE '%ORDER-ID-AQUI%';"
```

### Causa A: Orden en status "Pending" en SP-API (la más común)
**El dashboard de ventas lee de Seller Central (tiempo real). La SP-API tiene delay de 1-4 horas.**

El poll ignora órdenes Pending (`status=Pending, skipping`). Con `--days 2` en el cron, la próxima ejecución la capturará cuando SP-API actualice a `Unshipped`.

**No hacer nada** — se resuelve sola en el siguiente poll.

### Causa B: La orden NUNCA llegó al worker (no está en `amazon_orders_state` ni en `amazon_processed_events`)
El poll la salteó por Pending y el webhook no llegó. Con `--days 2` el siguiente poll la captura.

Para forzar recuperación inmediata:
```bash
docker exec bridge-inbound-worker python3 /data/amazon_orders_poll.py \
  --days 2 --marketplace MX   # o BOTH para MX+US
```

### Causa C: La orden está en `manual_review` (SKU faltante u otro error)
Ver PROBLEMA 2.

---

## PROBLEMA 2: Órdenes en manual_review por SKU faltante

### Síntoma
```
[FBM_PAID] ERROR missing products (sale_ok=true) for SKUs: ['XX-XXXX-XXXX']
```

### Causa raíz
El Amazon SellerSKU no tiene mapeo en `amazon_sku_mapping` O el producto Odoo mapeado no tiene `sale_ok=True`.

### Verificar si el mapping existe
```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT seller_sku, odoo_default_code FROM amazon_sku_mapping
WHERE seller_sku = 'XX-XXXX-XXXX';"
```

### Si el mapping NO existe → agregar desde la UI del bridge
Ir a `/amazon/mapper` en la UI del bridge y agregar el mapeo `Amazon SKU → Odoo default_code`.

### Si el mapping SÍ existe pero sigue fallando
La orden fue procesada ANTES de que se agregara el mapping. Está atascada en `manual_review`.
**Solución: re-encolar con recover_manual_review.py**

```bash
# Dry-run primero para ver qué se recuperaría
docker exec bridge-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead --dry-run

# Ejecutar real
docker exec bridge-inbound-worker python3 /data/recover_manual_review.py \
  --channel amazon --hours 168 --include-dead
```

**IMPORTANTE:** Ejecutar `recover_manual_review.py` CADA VEZ que se agrega un nuevo SKU mapping. Las órdenes atascadas no se recuperan solas.

### Hay 27 manual_review activos (Feb 2026)
SKUs mapeados correctamente pero órdenes atascadas desde Feb 21-22:
`2L-ZLFN-R9R5`, `FZ-4QXB-23FK`, `UN-G9LH-KH5G`, `YU-EPEN-SB3Y`, `48-YDJB-SE8Z`, `QS-Y0TK-KLMX`, `UN-4O7B-FJ96`, `PI-ILHD-JJYH`

---

## PROBLEMA 3: Poll de Amazon falla con "timestamp must follow ISO8601"

### Síntoma
```
[poll] ERROR getting orders: 400 {"errors": [{"code": "InvalidInput", "message": "timestamp must follow ISO8601"}]}
```

### Causa
La versión desplegada del script usa `.isoformat()` que produce `+00:00`. Amazon SP-API requiere `Z`.

### Fix (en el servidor)
```bash
sudo sed -i "s/timedelta(days=args.days)).isoformat()/timedelta(days=args.days)).strftime('%Y-%m-%dT%H:%M:%SZ')/" \
  /mnt/data/appdata/bridge/data/amazon_orders_poll.py

# Verificar
grep "created_after" /mnt/data/appdata/bridge/data/amazon_orders_poll.py
# Debe mostrar: .strftime('%Y-%m-%dT%H:%M:%SZ')
```

---

## PROBLEMA 4: Scripts del repo no están desplegados en el servidor

### Contexto
Los scripts en el repo (`/home/user/goncloud-bridge-in-out/tools/`) deben estar en `/mnt/data/appdata/bridge/data/` para que el contenedor los ejecute como `/data/xxx.py`.

### Para desplegar un script
```bash
sudo cp /home/user/goncloud-bridge-in-out/tools/SCRIPT.py \
        /mnt/data/appdata/bridge/data/
```

### Scripts que deben estar siempre actualizados en /data/
- `amazon_orders_poll.py`
- `recover_manual_review.py`
- `diagnose_inbound.py`
- `amazon_fba_paid_one_shot.py`
- `amazon_fbm_paid_one_shot.py`

---

## PROBLEMA 5: MeLi `no_valid_items` o `manual_review`

### Diagnóstico
```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, processed_at, result, detail_json
FROM processed_inbound_events
WHERE result IN ('manual_review', 'dead')
  AND processed_at >= datetime('now', '-48 hours')
ORDER BY processed_at DESC;"
```

### Para recuperar una orden MeLi con `rawsha` dedupe_key
```bash
# Ver el resource/order_id original
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT ie.resource, ie.payload_json
FROM inbound_events ie
WHERE ie.dedupe_key = 'rawsha:XXXX';"
```

Las órdenes MeLi con `rawsha` como dedupe_key NO tienen payload persistido en `inbound_job_payloads`.
Para recuperarlas: re-enviar el webhook desde MeLi o re-notificar desde la UI del bridge.

---

## Comandos de diagnóstico rápido (correr en servidor)

```bash
# Diagnóstico completo (kill-switches, colas Redis, errores últimas 24h)
BRIDGE_DB=/mnt/data/appdata/bridge/data/bridge.db \
docker exec bridge-inbound-worker python3 /data/diagnose_inbound.py --hours 48

# Estado de colas Redis
docker exec bridge-inbound-worker python3 -c "
import redis, json
r = redis.Redis(host='bridge-redis', port=6379, decode_responses=True)
for q in ['amazon_orders_jobs','amazon_orders_dead','ml_orders_jobs','ml_orders_dead']:
    print(q, r.llen(q))
"

# Conteo de resultados Amazon (últimas 72h)
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT result, COUNT(*) FROM amazon_processed_events
WHERE processed_at >= datetime('now', '-72 hours')
GROUP BY result ORDER BY COUNT(*) DESC;"

# Ordenes específicas por order_id
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT dedupe_key, processed_at, result, detail_json
FROM amazon_processed_events
WHERE dedupe_key LIKE '%ORDER-ID%';"
```

---

## Configuración del cron

El poll de Amazon corre vía `run_amazon_poll.sh`:
```bash
docker exec bridge-inbound-worker python3 /data/amazon_orders_poll.py --days 2 --marketplace BOTH
```

**Importante:** `--days 2` (no 1) para cubrir el gap entre Pending→Unshipped.

---

## Flujo correcto cuando se agrega un nuevo SKU mapping

1. Agregar en la UI del bridge (`/amazon/mapper`) o directo en `amazon_sku_mapping`
2. **Inmediatamente después**, ejecutar recover para re-intentar órdenes atascadas:
   ```bash
   docker exec bridge-inbound-worker python3 /data/recover_manual_review.py \
     --channel amazon --hours 168 --include-dead
   ```
3. Verificar que la orden procesó:
   ```bash
   sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
   SELECT dedupe_key, result FROM amazon_processed_events
   WHERE result = 'success' AND processed_at >= datetime('now', '-1 hour')
   ORDER BY processed_at DESC LIMIT 5;"
   ```
