"""Recalcula el costo estandar de todos los kits activos desde su BoM phantom.

    ssh gonserver "sudo docker exec -i bridge-api python3 -" < tools/odoo_cost_recompute_kits.py        # DRY-RUN
    ssh gonserver "sudo docker exec -i bridge-api python3 - apply" < tools/odoo_cost_recompute_kits.py  # APLICAR

Preview con calculo local (misma logica del audit). Al aplicar usa el
compute_price nativo de Odoo (mrp) si coincide con el calculo local en un kit
de prueba; si no, escribe el valor local. Verifica lectura final y deja CSV
con los cambios en /data/cost_update_kits_<fecha>.csv.
"""

import csv
import sqlite3
import sys
from datetime import date

import defusedxml.xmlrpc as _defusedxml_xmlrpc

_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402

BRIDGE_DB = "/data/bridge.db"
TODAY = date.today().isoformat()
TOLERANCE = 0.05
CHUNK = 100
MAX_DEPTH = 5


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

    uoms = call(
        "uom.uom", "search_read", fields=["id", "name", "factor", "category_id"]
    )
    uom_by_id = {u["id"]: u for u in uoms}

    def qty_in_uom(qty, from_id, to_id):
        if from_id == to_id:
            return qty
        f, t = uom_by_id.get(from_id), uom_by_id.get(to_id)
        if not f or not t or f["category_id"][0] != t["category_id"][0]:
            return None
        return (qty * f["factor"]) / t["factor"]

    prods = call(
        "product.product",
        "search_read",
        fields=[
            "id",
            "default_code",
            "name",
            "standard_price",
            "uom_id",
            "product_tmpl_id",
            "active",
        ],
        context={"active_test": False},
    )
    prod_by_id = {p["id"]: p for p in prods}

    boms = call(
        "mrp.bom",
        "search_read",
        fields=["id", "product_tmpl_id", "product_id", "type", "active", "sequence"],
        context={"active_test": False},
    )
    boms = [b for b in boms if b["active"] and b["type"] == "phantom"]
    lines = call(
        "mrp.bom.line",
        "search_read",
        fields=["id", "bom_id", "product_id", "product_qty", "product_uom_id"],
    )
    lines_by_bom: dict[int, list] = {}
    for ln in lines:
        lines_by_bom.setdefault(ln["bom_id"][0], []).append(ln)

    bom_by_variant: dict[int, int] = {}
    bom_by_tmpl: dict[int, int] = {}
    for b in sorted(boms, key=lambda b: (b.get("sequence", 0), b["id"])):
        tid = b["product_tmpl_id"][0] if b["product_tmpl_id"] else None
        if tid and tid not in bom_by_tmpl:
            bom_by_tmpl[tid] = b["id"]
        if b.get("product_id") and b["product_id"][0] not in bom_by_variant:
            bom_by_variant[b["product_id"][0]] = b["id"]

    def bom_cost(bom_id, qty_factor, depth, path, missing):
        total = 0.0
        for ln in lines_by_bom.get(bom_id, []):
            comp = prod_by_id.get(ln["product_id"][0])
            if not comp:
                continue
            comp_uom = (comp.get("uom_id") or (0,))[0]
            qty = qty_in_uom(ln["product_qty"], ln["product_uom_id"][0], comp_uom)
            if qty is None:
                qty = ln["product_qty"]
            qty *= qty_factor
            cost = float(comp["standard_price"] or 0)
            sub_tid = comp["product_tmpl_id"][0]
            sub_bom = bom_by_variant.get(comp["id"]) or bom_by_tmpl.get(sub_tid)
            if cost == 0 and not (
                sub_bom and sub_bom not in path and depth < MAX_DEPTH
            ):
                missing.append(comp.get("default_code") or str(comp["id"]))
            if sub_bom and sub_bom not in path and depth < MAX_DEPTH:
                total += bom_cost(sub_bom, qty, depth + 1, path | {sub_bom}, missing)
            else:
                total += qty * cost
        return total

    kits = []  # (product, expected)
    for p in prods:
        if not p["active"]:
            continue
        tid = p["product_tmpl_id"][0]
        bom_id = bom_by_variant.get(p["id"]) or bom_by_tmpl.get(tid)
        if not bom_id:
            continue
        missing: list[str] = []
        expected = bom_cost(bom_id, 1.0, 0, frozenset({bom_id}), missing)
        if missing:
            print(
                f"ABORT: bom incompleto en {p.get('default_code') or p['id']}: {missing}"
            )
            sys.exit(1)
        kits.append((p, expected))
    print(f"kits activos a recalcular: {len(kits)}")

    pending = [
        (p, exp)
        for p, exp in kits
        if abs(float(p["standard_price"] or 0) - exp) > 0.005
    ]
    print(
        f"con costo distinto al bom: {len(pending)} "
        f"(en cero: {sum(1 for p, _ in pending if float(p['standard_price'] or 0) == 0)})"
    )
    for p, exp in sorted(
        pending, key=lambda k: -abs(k[1] - float(k[0]["standard_price"] or 0))
    )[:15]:
        print(
            f"  {p.get('default_code') or p['id']:42} {float(p['standard_price'] or 0):>9.2f} -> {exp:>8.2f}"
        )

    if not do_apply:
        print("\nDRY-RUN: nada escrito (pasa 'apply' para ejecutar)")
        return

    method = "local_write"
    probe_p, probe_exp = kits[0]
    try:
        call("product.product", "compute_price", [probe_p["id"]])
        back = call(
            "product.product", "read", [probe_p["id"]], fields=["standard_price"]
        )[0]
        if abs(float(back["standard_price"]) - probe_exp) <= TOLERANCE:
            method = "odoo_compute_price"
        else:
            print(
                f"compute_price nativo divergió en {probe_p.get('default_code')}: "
                f"{back['standard_price']} vs esperado {probe_exp:.2f} -> uso calculo local"
            )
    except xmlrpc.client.Fault as exc:
        print(
            f"compute_price nativo no disponible ({str(exc)[:90]}...) -> uso calculo local"
        )

    print(f"metodo: {method}")
    for i in range(0, len(pending), CHUNK):
        chunk = pending[i : i + CHUNK]
        ids = [p["id"] for p, _ in chunk]
        if method == "odoo_compute_price":
            call("product.product", "compute_price", ids)
        else:
            vals = {p["id"]: exp for p, exp in chunk}
            for pid in ids:
                call("product.product", "write", [pid], {"standard_price": vals[pid]})
        print(f"  chunk {i // CHUNK + 1}/{(len(pending) + CHUNK - 1) // CHUNK} listo")

    back = call(
        "product.product",
        "read",
        [p["id"] for p, _ in pending],
        fields=["default_code", "standard_price"],
    )
    back_by_id = {b["id"]: b for b in back}
    mismatches = []
    changes_csv = f"/data/cost_update_kits_{TODAY}.csv"
    with open(changes_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "costo_anterior", "costo_nuevo", "metodo"])
        for p, exp in pending:
            new = float(back_by_id[p["id"]]["standard_price"])
            w.writerow(
                [
                    p.get("default_code") or p["id"],
                    f"{float(p['standard_price'] or 0):.2f}",
                    f"{new:.2f}",
                    method,
                ]
            )
            if abs(new - exp) > TOLERANCE:
                mismatches.append((p.get("default_code") or p["id"], new, exp))
    print(
        f"\ncambios escritos: {len(pending)} | verificacion: "
        f"{'OK todos conforme al bom' if not mismatches else 'DIVERGENCIAS:'}"
    )
    for sku, new, exp in mismatches[:20]:
        print(f"  {sku}: quedo en {new:.2f}, bom dice {exp:.2f}")
    print(f"CSV cambios: {changes_csv}")


if __name__ == "__main__":
    main()
