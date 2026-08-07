# DOCUMENTO CANÓNICO — GONCLOUD SETUP WIZARD v1

**Fecha:** 2026-02-13
**Sistema:** GONCLOUD Bridge
**Servidor:** goncloud
**Versión:** 1.0
**Estado:** PRODUCCIÓN

---

## 1. PROPÓSITO

Wizard de configuración inicial para nuevas instalaciones de GONCLOUD.
Permite a clientes conectar Odoo + Amazon + MercadoLibre en 10 minutos.

**NO usar en sistemas ya configurados** — tiene protección automática.

---

## 2. UBICACIÓN DE ARCHIVOS

### Archivos del Wizard

| Archivo | Ubicación | Propósito |
|---------|-----------|-----------|
| `setup.html` | `/mnt/data/appdata/bridge/app/static/setup.html` | UI del wizard (HTML + JS + Tailwind) |
| `main.py` | `/mnt/data/appdata/bridge/app/main.py` | Endpoints API (FastAPI) |

### Dentro del Contenedor

| Host | Contenedor |
|------|------------|
| `/mnt/data/appdata/bridge/app/static/` | `/app/static/` |
| `/mnt/data/appdata/bridge/app/main.py` | `/app/main.py` |

---

## 3. URLs Y ENDPOINTS

### URLs Públicas

| URL | Descripción | Protección |
|-----|-------------|------------|
| `https://meli.goncloud.cc/setup` | Wizard (bloqueado si ya configurado) | Auto-detecta config existente |
| `https://meli.goncloud.cc/setup/force?secret=<SETUP_SECRET>` | Wizard forzado | Requiere secret |
| `https://meli.goncloud.cc/mapper` | SKU Mapper (post-setup) | Ninguna |

### Endpoints API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/setup` | GET | Sirve wizard HTML (si no configurado) |
| `/setup/force` | GET | Sirve wizard HTML (requiere `?secret=<SETUP_SECRET>`) |
| `/setup/api/test-odoo` | POST | Valida credenciales Odoo |
| `/setup/api/status` | GET | Estado actual (MeLi/Amazon conectados) |
| `/setup/api/auto-map` | POST | Mapeo automático de SKUs |
| `/setup/api/activate` | POST | Guarda config y activa GONCLOUD |

---

## 4. FLUJO DEL WIZARD (5 PASOS)
```
┌─────────────────────────────────────────────────────────────┐
│  PASO 1: Conectar Odoo                                      │
│  - URL, Base de datos, Usuario, Password                    │
│  - Botón "Verificar conexión" → POST /setup/api/test-odoo   │
│  - Muestra: productos, almacenes                            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PASO 2: Seleccionar Marketplaces                           │
│  - [✓] Amazon (MX, US, CA)                                  │
│  - [✓] MercadoLibre (MX, CO, CL)                           │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PASO 3: Conectar Cuentas                                   │
│  - Amazon: OAuth SP-API (manual por ahora)                  │
│  - MeLi: Redirect a /oauth/start (existente)                │
│  - Verifica tokens existentes via GET /setup/api/status     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PASO 4: Configurar Flujos                                  │
│  - Amazon: Inbound (FBM/FBA), Outbound, Intervalo           │
│  - MeLi: Inbound (FBM/FULL), Outbound, Intervalo            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PASO 5: Mapeo SKUs + Activar                               │
│  - Botón "Mapeo automático" → POST /setup/api/auto-map      │
│  - Muestra: X mapeados, Y pendientes                        │
│  - Botón "Activar GONCLOUD" → POST /setup/api/activate      │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  ÉXITO: "¡GONCLOUD Activado!"                               │
│  - Link a /mapper                                           │
│  - Link a /status                                           │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. PROTECCIÓN DE SISTEMAS EN PRODUCCIÓN

### Lógica de Bloqueo

El endpoint `/setup` verifica antes de servir el wizard:
```python
checks = [
    "SELECT value FROM bridge_settings WHERE key='setup_completed' AND value='1'",
    "SELECT 1 FROM sku_mapping LIMIT 1",
    "SELECT 1 FROM amazon_sku_mapping WHERE odoo_default_code IS NOT NULL LIMIT 1"
]
```

Si **cualquiera** de estas condiciones es verdadera → **bloqueado**.

### Respuesta Cuando Bloqueado
```json
{
    "error": "Sistema ya configurado",
    "message": "GONCLOUD ya está en producción. Usa /mapper para SKUs o /status para ver estado.",
    "links": {
        "mapper": "/mapper",
        "status": "/api/health"
    }
}
```

### Bypass (Solo Administrador)
```
/setup/force?secret=<SETUP_SECRET>
```

---

## 6. SETTINGS EN BASE DE DATOS

### Tabla: `bridge_settings`

El wizard guarda/actualiza estas keys:

#### Odoo
```sql
odoo_url          -- https://empresa.odoo.com
odoo_db           -- nombre de la base de datos
odoo_user         -- email del usuario
odoo_password     -- contraseña
```

#### Amazon
```sql
amazon_inbound_enabled          -- "1" o "0"
amazon_inbound_fbm_paid_enabled -- "1" o "0"
amazon_inbound_fba_paid_enabled -- "1" o "0"
amazon_outbound_enabled         -- "1" o "0"
amazon_outbound_interval        -- "5", "10", "15", "30"
```

#### MercadoLibre
```sql
meli_inbound_enabled            -- "1" o "0"
meli_inbound_fbm_paid_enabled   -- "1" o "0"
meli_inbound_full_paid_enabled  -- "1" o "0"
meli_outbound_enabled           -- "1" o "0"
meli_adapter_enabled            -- "1" o "0"
meli_outbound_interval          -- "5", "10", "15", "30"
```

#### Estado del Wizard
```sql
setup_completed     -- "1" cuando se completa
setup_completed_at  -- ISO timestamp
```

---

## 7. API PAYLOADS

### POST /setup/api/test-odoo

**Request:**
```json
{
    "url": "https://empresa.odoo.com",
    "db": "produccion",
    "user": "admin@empresa.com",
    "password": "secreto"
}
```

**Response OK:**
```json
{
    "ok": true,
    "products": 1234,
    "warehouses": 3,
    "uid": 2
}
```

**Response Error:**
```json
{
    "ok": false,
    "error": "Credenciales inválidas"
}
```

### POST /setup/api/auto-map

**Request:**
```json
{
    "amazon": true,
    "meli": true
}
```

**Response:**
```json
{
    "ok": true,
    "amazon": {"mapped": 38, "pending": 7},
    "meli": {"mapped": 120, "pending": 8}
}
```

### POST /setup/api/activate

**Request:**
```json
{
    "odoo": {
        "url": "https://empresa.odoo.com",
        "db": "produccion",
        "user": "admin@empresa.com",
        "password": "secreto"
    },
    "amazon": {
        "enabled": true,
        "inbound": true,
        "fbm": true,
        "fba": false,
        "outbound": true,
        "interval": "10"
    },
    "meli": {
        "enabled": true,
        "inbound": true,
        "fbm": true,
        "full": true,
        "outbound": true,
        "interval": "10"
    }
}
```

**Response:**
```json
{
    "ok": true
}
```

---

## 8. DEPENDENCIAS

### Python (en main.py)
```python
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import xmlrpc.client
import requests
import json
import os
import sqlite3
```

### Frontend (CDN)
```html
<script src="https://cdn.tailwindcss.com"></script>
```

No requiere build, npm, ni Node.js.

---

## 9. CONTENEDOR DOCKER

### Nombre
```
bridge-api
```

### Volúmenes
```yaml
volumes:
  - ./app:/app
  - ./data:/data
```

### Reiniciar
```bash
cd /mnt/data/appdata/bridge && sudo docker compose restart bridge-api
```

### Ver Logs
```bash
sudo docker logs bridge-api -f --tail 100
```

---

## 10. COMANDOS ÚTILES

### Verificar que sirve el wizard (bloqueado)
```bash
curl -s http://localhost:8099/setup | head -5
```

### Acceso forzado
```bash
curl -s "http://localhost:8099/setup/force?secret=<SETUP_SECRET>" | head -5
```

### Ver estado de conexiones
```bash
curl -s http://localhost:8099/setup/api/status | jq .
```

### Probar conexión Odoo
```bash
curl -s -X POST http://localhost:8099/setup/api/test-odoo \
  -H "Content-Type: application/json" \
  -d '{"url":"http://odoo-odoo-1:8069","db":"EHV","user":"admin@test.com","password":"test"}' | jq .
```

### Ver settings guardados
```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
SELECT key, value FROM bridge_settings
WHERE key LIKE 'odoo_%' OR key LIKE 'amazon_%' OR key LIKE 'meli_%' OR key LIKE 'setup_%'
ORDER BY key;"
```

### Resetear wizard (permitir re-ejecución)
```bash
sqlite3 /mnt/data/appdata/bridge/data/bridge.db "
DELETE FROM bridge_settings WHERE key='setup_completed';
DELETE FROM bridge_settings WHERE key='setup_completed_at';"
```

---

## 11. TROUBLESHOOTING

### Wizard no carga (404)
```bash
# Verificar que static/ existe
ls -la /mnt/data/appdata/bridge/app/static/

# Verificar que setup.html existe
ls -la /mnt/data/appdata/bridge/app/static/setup.html

# Reiniciar contenedor
cd /mnt/data/appdata/bridge && sudo docker compose restart bridge-api
```

### Error "Sistema ya configurado" pero quiero testear

Usar URL con secret:
```
https://meli.goncloud.cc/setup/force?secret=<SETUP_SECRET>
```

### Test Odoo falla pero credenciales son correctas
```bash
# Verificar que Odoo responde
curl -s http://odoo-odoo-1:8069/web/database/selector

# Desde el contenedor bridge-api
sudo docker exec bridge-api curl -s http://odoo-odoo-1:8069/web/database/selector
```

### Auto-mapeo retorna 0

Verificar:
1. Token MeLi válido en `/data/.meli_tokens.json`
2. Productos en Odoo tienen `default_code` (SKU)
3. Listings en MeLi tienen `SELLER_SKU` en atributos de variación

---

## 12. ARQUITECTURA
```
┌─────────────────────────────────────────────────────────────┐
│                    NAVEGADOR (Cliente)                       │
│                   setup.html + Tailwind                      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI (main.py)                         │
│                  Container: bridge-api                       │
│                      Puerto: 8099                            │
├─────────────────────────────────────────────────────────────┤
│  /setup                    GET   → HTML (protegido)          │
│  /setup/force              GET   → HTML (con secret)         │
│  /setup/api/test-odoo      POST  → Valida Odoo              │
│  /setup/api/status         GET   → Estado conexiones         │
│  /setup/api/auto-map       POST  → Mapeo SKUs               │
│  /setup/api/activate       POST  → Guarda config            │
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│   SQLite        │ │   Odoo ERP      │ │   MeLi API      │
│   bridge.db     │ │   (XML-RPC)     │ │   (REST)        │
│   - settings    │ │   - productos   │ │   - listings    │
│   - sku_mapping │ │   - almacenes   │ │   - variaciones │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

---

## 13. PARA NUEVOS CLIENTES (CHECKLIST)
```
□ 1. Instalar GONCLOUD en servidor del cliente
□ 2. Configurar dominio (ej: cliente.goncloud.cc)
□ 3. Cliente abre https://cliente.goncloud.cc/setup
□ 4. Paso 1: Ingresa credenciales Odoo → Verificar
□ 5. Paso 2: Selecciona marketplaces (Amazon/MeLi)
□ 6. Paso 3: Conecta cuentas (OAuth)
□ 7. Paso 4: Configura flujos (inbound/outbound)
□ 8. Paso 5: Auto-mapeo SKUs → Activar
□ 9. Sistema funcionando en ~10 minutos
```

---

## 14. SEGURIDAD

| Aspecto | Protección |
|---------|------------|
| Acceso a wizard | Bloqueado si ya hay config |
| Bypass administrativo | Requiere `?secret=<SETUP_SECRET>` |
| Credenciales Odoo | Guardadas en bridge_settings (no en logs) |
| Tokens MeLi | En archivo separado `.meli_tokens.json` |
| Secret del bypass | Cambiar en producción SaaS |

**⚠️ IMPORTANTE:** Cambiar el secret `<SETUP_SECRET>` antes de deploy SaaS.

---

## 15. BACKUPS

Archivos críticos a respaldar:
```
/mnt/data/appdata/bridge/app/static/setup.html
/mnt/data/appdata/bridge/app/main.py
/mnt/data/appdata/bridge/data/bridge.db
/mnt/data/appdata/bridge/data/.meli_tokens.json
```

---

**FIN DEL DOCUMENTO CANÓNICO v1**
