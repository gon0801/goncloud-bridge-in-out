"""Actualiza costos de arras activas: 16mm = $95, 19mm = $100 MXN (decision del operador).

    ssh gonserver "sudo docker exec -i bridge-api python3 -" < tools/odoo_cost_update_arras.py        # DRY-RUN
    ssh gonserver "sudo docker exec -i bridge-api python3 - apply" < tools/odoo_cost_update_arras.py   # APLICAR

Sin arg solo muestra el antes/despues. Solo toca productos activos cuyo costo
difiera de la regla.
"""

import sqlite3
import sys

import defusedxml.xmlrpc as _defusedxml_xmlrpc

_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402

BRIDGE_DB = "/data/bridge.db"
RULE = {"16": 95.0, "19": 100.0}  # mm -> costo MXN


def main() -> None:
    do_apply = len(sys.argv) > 1 and sys.argv[1] == "apply"
    con = sqlite3.connect(BRIDGE_DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    def get(k: str) -> str:
        row = cur.execute(
            "SELECT value FROM bridge_settings WHERE key=?", (k,)
        ).fetchone()
        return (row["value"] or "") if row else ""

    url, db, usr, pwd = (
        get("odoo_url"),
        get("odoo_db"),
        get("odoo_user"),
        get("odoo_password"),
    )
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, usr, pwd, {})
    if not uid:
        print("ERROR: autenticacion rechazada", file=sys.stderr)
        sys.exit(1)
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def call(model: str, method: str, *args, **kw):
        return models.execute_kw(db, uid, pwd, model, method, list(args), kw)

    # Costeo/valorizacion de las categorias de arras (impacto contable del write)
    cats = call(
        "product.category",
        "search_read",
        [("name", "in", ["Arras", "Arras Sets", "All"])],
        fields=["name", "property_cost_method", "property_valuation"],
    )
    for c in cats:
        print(
            f"categoria '{c['name']}': costeo={c['property_cost_method']} "
            f"valorizacion={c['property_valuation']}"
        )

    arras = call(
        "product.product",
        "search_read",
        [("default_code", "=like", "ARR-%"), ("active", "=", True)],
        fields=["id", "default_code", "standard_price", "qty_available"],
        order="default_code",
    )
    changes = []
    conform = 0
    for a in arras:
        sku = a["default_code"] or ""
        mm = sku.split("-")[1] if len(sku.split("-")) > 2 else ""
        if mm not in RULE:
            continue
        current = float(a["standard_price"] or 0)
        if abs(current - RULE[mm]) > 1e-9:
            changes.append((a, RULE[mm]))
        else:
            conform += 1
    print(
        f"\narras activas 16/19mm bajo la regla: {conform + len(changes)} "
        f"(ya conforman: {conform}, a cambiar: {len(changes)})"
    )
    for a, target in changes:
        print(
            f"  {a['default_code']}: {float(a['standard_price'] or 0):8.2f} -> "
            f"{target:.2f}  (stock={float(a['qty_available'] or 0)})"
        )

    if not changes:
        print("nada que cambiar")
        return
    if not do_apply:
        print("\nDRY-RUN: nada escrito (pasa 'apply' para ejecutar)")
        return

    for a, target in changes:
        call("product.product", "write", [a["id"]], {"standard_price": target})
    print(f"\nAPLICADO: {len(changes)} escrituras")

    ids = [a["id"] for a, _ in changes]
    back = call(
        "product.product", "read", ids, fields=["default_code", "standard_price"]
    )
    bad = [
        b
        for b, (_, t) in zip(
            sorted(back, key=lambda x: x["default_code"]),
            sorted(changes, key=lambda x: x[0]["default_code"]),
        )
        if abs(float(b["standard_price"]) - t) > 1e-9
    ]
    print(
        "verificacion lectura:",
        "OK todos conforman la regla" if not bad else f"NO CONFORMES: {bad}",
    )


if __name__ == "__main__":
    main()
