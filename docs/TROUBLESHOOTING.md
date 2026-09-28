# Diagnóstico y operaciones del bridge

Guía trasladada del antiguo `CLAUDE.md`. Los comandos reflejan incidentes pasados. Confirma las rutas, el esquema y el estado actual antes de ejecutar comandos que cambian datos o servicios.

## Operaciones puntuales

### Flujo al agregar nuevo SKU mapping

```
1. Agregar en /amazon/mapper o /mapper
2. INMEDIATAMENTE ejecutar recover_manual_review.py para re-intentar atascadas
3. Verificar que procesó
```

### Desplegar el archivo que ejecuta cada llamador

Para cambios de scripts o workers, usa el procedimiento de [deploy desde repo](#deploy-desde-repo).
El destino depende del servicio que llama al archivo; copiar toda la carpeta
`tools/` a `/data/` puede dejar sin actualizar los timers del host.

## Problemas conocidos y soluciones

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

**Causa del incidente de 2026-02-24:** `res.currency.rate` en Odoo estaba vacío.
Entonces `sync_fx_rates.py` escribía en `accounting.db`, pero no en Odoo. Los
tools consultaban Odoo. Si reaparece el síntoma, comprueba primero las tasas
actuales y si la sincronización hacia Odoo sigue funcionando.

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

# Backfill ejecutado en el host para el incidente de 2026-02-24:
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py 2026-02-23

# Verificar accounting.db para fechas disponibles:
sqlite3 /mnt/data/appdata/accounting/data/accounting.db \
  "SELECT rate_date, rate FROM currency_rates WHERE base_currency='MXN' AND quote_currency='USD' ORDER BY rate_date DESC LIMIT 7;"
```

**Despliegue realizado para el incidente de 2026-02-24:** verifica el estado
actual antes de reutilizar estos comandos; consulta el
[procedimiento de deploy](#deploy-desde-repo).
```bash
# 1. Copiar push_fx_to_odoo.py al servidor
sudo cp /tmp/goncloud-bridge-in-out/tools/push_fx_to_odoo.py /mnt/data/appdata/bridge/data/

# 2. Backfill con la fecha más reciente disponible en accounting.db
python3 /mnt/data/appdata/bridge/data/push_fx_to_odoo.py

# 3. Actualizar sync_fx_rates.py para que llame push_fx_to_odoo.py diariamente
# Ver "Tipo de cambio USD/MXN" más abajo.
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

**Causa del incidente de 2026-02-25:** la versión en
`/data/amazon_fba_paid_one_shot.py` era anterior a la conversión USD→MXN, o
provenía de `app/` con `IS_USD_ORDER` sin definir (`NameError`). Comprueba la
moneda en `OrderTotal.CurrencyCode`, las tasas y la versión que ejecuta el worker
antes de atribuir un caso nuevo a esta causa.

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

**Despliegue usado en el incidente de 2026-02-25:** estos comandos muestran qué
archivos se copiaron entonces; comprueba los llamadores y las versiones actuales
con el [procedimiento de deploy](#deploy-desde-repo) antes de reutilizarlos.
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

## Comandos útiles

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
Debe llamar a `push_fx_to_odoo.py` al final del `sync_fx()`. Ver "PROBLEMA 6" en esta guía para las instrucciones de despliegue.

### Deploy desde repo

1. En el host, confirma el checkout y el commit que quieres desplegar. Compara
   `docker-compose.yml` con los contenedores y los timers o crons instalados.
2. Identifica el llamador del archivo cambiado. Los `run_tool()` de ambos workers
   prefieren `/data/{tool}.py` y usan `tools/` del host como fallback. Los timers
   del host pueden ejecutar `tools/` directamente; algunos llamados de `app/main.py`
   usan rutas fijas en `/data/`. Comprueba la ruta efectiva antes de copiar.
3. Ejecuta `bash tools/check_tools_data_drift.sh` en el checkout del host para
   comparar `tools/` con `/data/`. Revisa el diff del archivo elegido y copia solo
   a las rutas que ejecutan los llamadores afectados.
4. Si cambió un worker, actualiza su archivo en `app/` y reinicia solo ese
   contenedor. Si cambió un script de timer o cron, confirma la ruta instalada y
   verifica su siguiente ejecución. Comprueba logs y resultado del flujo afectado.

---
