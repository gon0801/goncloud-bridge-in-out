import os, sqlite3, xmlrpc.client

DBPATH = "/data/bridge.db"
DELTA_ID = os.environ.get("DELTA_ID", "")
if not DELTA_ID:
    raise SystemExit("ABORT: missing DELTA_ID env var")

def get_setting(k, default="0"):
    con = sqlite3.connect(DBPATH)
    cur = con.cursor()
    cur.execute("SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (k,))
    row = cur.fetchone()
    con.close()
    return str(row[0]) if row else default

# Gates
if get_setting("meli_inbound_apply_stock_enabled") != "1":
    raise SystemExit("ABORT: apply disabled")
if get_setting("meli_inbound_dry_run") != "0":
    raise SystemExit("ABORT: dry_run enabled")

# Load delta
con = sqlite3.connect(DBPATH)
cur = con.cursor()
cur.execute("""
SELECT sku, qty_delta, applied_to_odoo
FROM inbound_stock_deltas
WHERE delta_id=?
""", (DELTA_ID,))
row = cur.fetchone()
con.close()

if not row:
    raise SystemExit("ABORT: delta not found")

sku, qty_delta, applied = row
if applied == 1:
    raise SystemExit("ABORT: delta already applied")
if qty_delta >= 0:
    raise SystemExit("ABORT: only negative delta allowed")

# Odoo env
url  = os.environ["ODOO_URL"]
db   = os.environ["ODOO_DB"]
user = os.environ["ODOO_USER"]
pwd  = os.environ["ODOO_PASS"]

common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
uid = common.authenticate(db, user, pwd, {})
if not uid:
    raise SystemExit("ABORT: odoo auth failed")

models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

# Product
prod_ids = models.execute_kw(db, uid, pwd, "product.product", "search",
                             [[["default_code","=",sku]]], {"limit": 1})
if not prod_ids:
    raise SystemExit(f"ABORT: SKU {sku} not found in Odoo")

product_id = prod_ids[0]

# Locations
loc_id = models.execute_kw(db, uid, pwd, "stock.location", "search",
                           [[["usage","=","internal"]]], {"limit": 1})[0]
scrap_loc_id = models.execute_kw(db, uid, pwd, "stock.location", "search",
                                 [[["scrap_location","=",True]]], {"limit": 1})[0]

# Scrap
scrap_id = models.execute_kw(db, uid, pwd, "stock.scrap", "create", [{
    "product_id": product_id,
    "scrap_qty": abs(qty_delta),
    "location_id": loc_id,
    "scrap_location_id": scrap_loc_id,
    "origin": DELTA_ID,
}])
models.execute_kw(db, uid, pwd, "stock.scrap", "action_validate", [[scrap_id]])

# Mark applied
con = sqlite3.connect(DBPATH)
cur = con.cursor()
cur.execute("""
UPDATE inbound_stock_deltas
SET applied_to_odoo=1, applied_at=datetime('now'), odoo_ref=?
WHERE delta_id=?
""", (f"stock.scrap:{scrap_id}", DELTA_ID))
con.commit()
con.close()

print("OK: applied delta via stock.scrap", scrap_id)
