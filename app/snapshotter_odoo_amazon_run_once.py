import os, sqlite3
import xmlrpc.client
from datetime import datetime

DB = os.getenv("BRIDGE_DB", os.getenv("BRIDGE_SQLITE_PATH", "/data/bridge.db"))
URL = os.getenv("ODOO_URL", "http://odoo:8069")
DBN = os.getenv("ODOO_DB")
USR = os.getenv("ODOO_USER")
PWD = os.getenv("ODOO_PASSWORD")

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DBN, USR, PWD, {})
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")

# AMAZON FBM: solo variantes marcadas para Amazon FBM
recs = models.execute_kw(
    DBN, uid, PWD,
    "product.product", "search_read",
    [[("sell_on_amazon_fbm", "=", True), ("active", "=", True), ("default_code", "!=", False)]],
    {"fields": ["id", "default_code", "qty_available", "outgoing_qty"]}
)

con = sqlite3.connect(DB)
cur = con.cursor()

cur.execute(
    "INSERT INTO events(created_at,channel,item_count,payload) VALUES(?,?,?,?)",
    (datetime.utcnow().isoformat(), "amazon_fbm", len(recs), "odoo_snapshot")
)
eid = str(cur.lastrowid)

for r in recs:
    odoo_sku = r["default_code"]
    qty = int(max((r.get("qty_available") or 0) - (r.get("outgoing_qty") or 0), 0))
    
    # Siempre enviar SKU de Odoo - el worker hace la conversión
    cur.execute(
        """
        INSERT OR REPLACE INTO snapshot_items
            (event_id,channel,sku,qty,derived_zero)
        VALUES(?,?,?,?,?)
        """,
        (eid, "amazon_fbm", odoo_sku, qty, 0)
    )

con.commit()
print("SNAPSHOT_OK", {"channel": "amazon_fbm", "event_id": eid, "items": len(recs)})

