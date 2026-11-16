import os
import sqlite3
import sys
import defusedxml.xmlrpc as _defusedxml_xmlrpc

# monkey_patch() DEBE correr antes de importar xmlrpc.client: parcha el parser
# XML de la stdlib contra entidades maliciosas. Como es una sentencia a nivel
# de modulo, ruff marca E402 en TODO import posterior; por eso los imports que
# siguen llevan `noqa: E402`. No los muevas arriba: romperias la mitigacion.
_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402
from datetime import datetime  # noqa: E402

from stock_disponible import (  # noqa: E402
    CAMPOS_ODOO,
    almacen_apartado,
    contexto_publicable,
    disponible,
)

DB = os.getenv("BRIDGE_DB", os.getenv("BRIDGE_SQLITE_PATH", "/data/bridge.db"))
URL = os.getenv("ODOO_URL", "http://odoo:8069")
DBN = os.getenv("ODOO_DB")
USR = os.getenv("ODOO_USER")
PWD = os.getenv("ODOO_PASSWORD")

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DBN, USR, PWD, {})
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")

con = sqlite3.connect(DB)
cur = con.cursor()

apartado = almacen_apartado(con)
almacenes = (
    models.execute_kw(
        DBN, uid, PWD, "stock.warehouse", "search_read", [[]], {"fields": ["id", "name"]}
    )
    if apartado
    else []
)
opciones = {"fields": ["id", "default_code", *CAMPOS_ODOO]}
try:
    contexto = contexto_publicable(almacenes, apartado)
except ValueError as e:
    con.close()
    print(f"[FATAL] {e}")
    sys.exit(1)
if contexto:
    opciones["context"] = contexto

# AMAZON FBM: solo variantes marcadas para Amazon FBM
recs = models.execute_kw(
    DBN,
    uid,
    PWD,
    "product.product",
    "search_read",
    [
        [
            ("sell_on_amazon_fbm", "=", True),
            ("active", "=", True),
            ("default_code", "!=", False),
        ]
    ],
    opciones,
)

cur.execute(
    "INSERT INTO events(created_at,channel,item_count,payload) VALUES(?,?,?,?)",
    (datetime.utcnow().isoformat(), "amazon_fbm", len(recs), "odoo_snapshot"),
)
eid = str(cur.lastrowid)

for r in recs:
    odoo_sku = r["default_code"]
    qty = disponible(r)

    # Siempre enviar SKU de Odoo - el worker hace la conversión
    cur.execute(
        """
        INSERT OR REPLACE INTO snapshot_items
            (event_id,channel,sku,qty,derived_zero)
        VALUES(?,?,?,?,?)
        """,
        (eid, "amazon_fbm", odoo_sku, qty, 0),
    )

con.commit()
print("SNAPSHOT_OK", {"channel": "amazon_fbm", "event_id": eid, "items": len(recs)})
