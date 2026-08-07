import sqlite3
import defusedxml.xmlrpc as _defusedxml_xmlrpc

# monkey_patch() DEBE correr antes de importar xmlrpc.client: parcha el parser
# XML de la stdlib contra entidades maliciosas. Como es una sentencia a nivel
# de modulo, ruff marca E402 en TODO import posterior; por eso los imports que
# siguen llevan `noqa: E402`. No los muevas arriba: romperias la mitigacion.
_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402
import sys  # noqa: E402

if len(sys.argv) < 2:
    print("Uso: python3 test_odoo_sku.py <SKU>")
    sys.exit(1)

sku = sys.argv[1]

# === Leer settings de bridge.db ===
con = sqlite3.connect("/mnt/data/appdata/bridge/data/bridge.db")
con.row_factory = sqlite3.Row
cur = con.cursor()


def get(k):
    r = cur.execute("SELECT value FROM bridge_settings WHERE key=?", (k,)).fetchone()
    return (r["value"] if r else "") or ""


url = get("odoo_url")
db = get("odoo_db")
usr = get("odoo_user")
pwd = get("odoo_password")

print("odoo_url:", url)
print("odoo_db:", db)
print("odoo_user:", usr)
print("SKU:", sku)

# === Conectar a Odoo ===
common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
uid = common.authenticate(db, usr, pwd, {})
print("uid:", uid)

models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

ids = models.execute_kw(
    db,
    uid,
    pwd,
    "product.product",
    "search",
    [[["default_code", "=", sku]]],
    {"limit": 10},
)

print("ids:", ids)

if not ids:
    print("NOT FOUND by default_code")
else:
    recs = models.execute_kw(
        db,
        uid,
        pwd,
        "product.product",
        "read",
        [ids],
        {"fields": ["id", "name", "default_code", "sale_ok", "active"]},
    )
    for r in recs:
        print(r)
