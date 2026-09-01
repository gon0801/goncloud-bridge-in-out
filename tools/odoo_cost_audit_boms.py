"""Auditoria de costos con estructura BoM (solo lectura).

Correr DENTRO de bridge-api:

    ssh gonserver "sudo docker exec -i bridge-api python3 -" < tools/odoo_cost_audit_boms.py

Enfoque (aclarado por el operador):
  - Los componentes (suministros, arras, estuches, charolas, cofres) deben tener
    costo propio; se flaggea costo 0 / negativo / bajo / outlier alto.
  - Los kits usan BoMs: se verifica que cada kit tenga BoM, que sus componentes
    tengan costo, y se compara costo estandar vs costo calculado del BoM.

Salidas: CSVs en /data/ (host: /mnt/data/appdata/bridge/data/) + resumen consola.
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
LOW_COST_THRESHOLD = 1.0
MAX_DEPTH = 5


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
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
    if not all([url, db, usr, pwd]):
        die("faltan credenciales odoo en bridge_settings")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, usr, pwd, {})
    if not uid:
        die("autenticacion Odoo rechazada")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def call(model: str, method: str, *args, **kw):
        return models.execute_kw(db, uid, pwd, model, method, list(args), kw)

    uoms = call(
        "uom.uom", "search_read", fields=["id", "name", "factor", "category_id"]
    )
    uom_by_id = {u["id"]: u for u in uoms}

    def qty_in_uom(qty: float, from_id: int, to_id: int):
        if from_id == to_id:
            return qty
        f, t = uom_by_id.get(from_id), uom_by_id.get(to_id)
        if not f or not t or f["category_id"][0] != t["category_id"][0]:
            return None
        return (qty * f["factor"]) / t["factor"]

    cats = call("product.category", "search_read", fields=["id", "display_name"])
    cat_name = {c["id"]: c["display_name"] for c in cats}

    prods = call(
        "product.product",
        "search_read",
        fields=[
            "id",
            "default_code",
            "name",
            "standard_price",
            "categ_id",
            "active",
            "type",
            "uom_id",
            "product_tmpl_id",
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
    # para costos solo cuentan los BoM activos (igual que el compute_price de Odoo)
    boms = [b for b in boms if b["active"]]
    arch_boms = call(
        "mrp.bom",
        "search_count",
        [("active", "=", False)],
    )
    print(f"boms activos: {len(boms)} (archivados ignorados: {arch_boms})")
    lines = call(
        "mrp.bom.line",
        "search_read",
        fields=["id", "bom_id", "product_id", "product_qty", "product_uom_id"],
    )
    lines_by_bom: dict[int, list] = {}
    for ln in lines:
        lines_by_bom.setdefault(ln["bom_id"][0], []).append(ln)

    # BoM aplicable por variante (prefiere especifica de variante) y por template
    bom_by_variant: dict[int, int] = {}
    bom_by_tmpl: dict[int, int] = {}
    # menor secuencia primero: ante varios BoM activos gana el de menor
    # secuencia, mismo criterio que usa Odoo al resolver el BoM
    for b in sorted(boms, key=lambda b: (b.get("sequence", 0), b["id"])):
        tid = b["product_tmpl_id"][0] if b["product_tmpl_id"] else None
        if tid and tid not in bom_by_tmpl:
            bom_by_tmpl[tid] = b["id"]
        if b.get("product_id") and b["product_id"][0] not in bom_by_variant:
            bom_by_variant[b["product_id"][0]] = b["id"]

    per_key: dict = {}
    for b in boms:
        key = (
            b["product_id"][0] if b.get("product_id") else f"t{b['product_tmpl_id'][0]}"
        )
        per_key[key] = per_key.get(key, 0) + 1
    dup_count = sum(1 for v in per_key.values() if v > 1)
    if dup_count:
        print(
            f"AVISO: {dup_count} productos con mas de un bom activo (se uso el de menor secuencia)"
        )

    type_counts: dict[str, int] = {}
    for b in boms:
        type_counts[b["type"]] = type_counts.get(b["type"], 0) + 1
    print(f"boms: {len(boms)} | por tipo: {type_counts}")

    # ---- Costo calculado de un BoM (recursivo con guarda de ciclos) ----
    def bom_cost(
        bom_id: int,
        qty_factor: float,
        depth: int,
        path: frozenset,
        missing: list,
        warnings: list,
        usage: dict,
    ):
        total = 0.0
        for ln in lines_by_bom.get(bom_id, []):
            comp = prod_by_id.get(ln["product_id"][0])
            if not comp:
                warnings.append(f"linea {ln['id']}: componente inexistente")
                continue
            comp_uom = (comp.get("uom_id") or (0, "?"))[0]
            qty = qty_in_uom(ln["product_qty"], ln["product_uom_id"][0], comp_uom)
            if qty is None:
                warnings.append(
                    f"{comp.get('default_code') or comp['id']}: uom incompatible"
                )
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
                usage[comp["id"]] = usage.get(comp["id"], 0) + 1
                total += bom_cost(
                    sub_bom, qty, depth + 1, path | {sub_bom}, missing, warnings, usage
                )
            else:
                usage[comp["id"]] = usage.get(comp["id"], 0) + 1
                total += qty * cost
        return total

    kit_rows = []
    components_usage: dict[int, int] = {}
    for p in prods:
        tid = p["product_tmpl_id"][0]
        bom_id = bom_by_variant.get(p["id"]) or bom_by_tmpl.get(tid)
        if not bom_id:
            continue
        bom = next(b for b in boms if b["id"] == bom_id)
        missing: list[str] = []
        warnings: list[str] = []
        usage: dict[int, int] = {}
        computed = bom_cost(
            bom_id, 1.0, 0, frozenset({bom_id}), missing, warnings, usage
        )
        for cid, n in usage.items():
            components_usage[cid] = components_usage.get(cid, 0) + n
        kit_rows.append(
            {
                "p": p,
                "bom_id": bom_id,
                "bom_type": bom["type"],
                "std": float(p["standard_price"] or 0),
                "computed": computed,
                "missing": missing,
                "warnings": warnings,
            }
        )

    kit_ids = {r["p"]["id"] for r in kit_rows}
    print(
        f"productos con bom: {len(kit_rows)} "
        f"(activos: {sum(1 for r in kit_rows if r['p']['active'])})"
    )

    # ---- Desglose por categoria (para mapear familias) ----
    print("\n=== POR CATEGORIA (todos, archivados incluidos) ===")
    by_cat: dict[str, dict] = {}
    for p in prods:
        cname = cat_name.get((p["categ_id"] or (0, "sin categoria"))[0], "?")
        d = by_cat.setdefault(
            cname, {"total": 0, "sin_costo": 0, "con_bom": 0, "activos": 0}
        )
        d["total"] += 1
        d["activos"] += 1 if p["active"] else 0
        d["sin_costo"] += 1 if float(p["standard_price"] or 0) == 0 else 0
        d["con_bom"] += 1 if p["id"] in kit_ids else 0
    for cname in sorted(by_cat, key=lambda c: -by_cat[c]["total"]):
        d = by_cat[cname]
        print(
            f"  {cname[:60]:60} total={d['total']:>5} activos={d['activos']:>5} "
            f"con_bom={d['con_bom']:>5} sin_costo={d['sin_costo']:>5}"
        )

    # ---- Componentes ----
    comp_ids = set(components_usage)
    comps = [prod_by_id[cid] for cid in comp_ids]
    comp_costs = sorted(
        float(c["standard_price"] or 0)
        for c in comps
        if float(c["standard_price"] or 0) > 0
    )

    def pct(data: list[float], q: float) -> float:
        return data[min(int(len(data) * q), len(data) - 1)] if data else 0.0

    p50, p99 = pct(comp_costs, 0.50), pct(comp_costs, 0.99)
    print(f"\n=== COMPONENTES USADOS EN BOMs: {len(comps)} ===")
    print(
        f"costos>0: {len(comp_costs)} | mediana={p50:.2f} p99={p99:.2f} "
        f"max={comp_costs[-1] if comp_costs else 0:.2f}"
    )

    def comp_flag(c: dict) -> str:
        v = float(c["standard_price"] or 0)
        if v < 0:
            return "negativo"
        if v == 0:
            return "sin_costo"
        if v < LOW_COST_THRESHOLD:
            return "muy_bajo"
        if v > max(p99, p50 * 20):
            return "outlier_alto"
        return "ok"

    comp_flagged = [(c, comp_flag(c)) for c in comps]
    sin_costo_comps = [(c, f) for c, f in comp_flagged if f == "sin_costo"]
    otros = [(c, f) for c, f in comp_flagged if f not in ("ok", "sin_costo")]
    print(f"sin costo: {len(sin_costo_comps)} | otros anormales: {len(otros)}")
    for c, f in otros[:15]:
        print(
            f"  [{f}] {c.get('default_code') or c['id']} "
            f"costo={float(c['standard_price'] or 0):.2f} "
            f"activo={c['active']} | {c['name']}"
        )

    print("\ncomponentes SIN COSTO, ordenados por kits afectados:")
    for c, _f in sorted(sin_costo_comps, key=lambda x: -components_usage[x[0]["id"]])[
        :40
    ]:
        print(
            f"  usado_x_{components_usage[c['id']]:>3} {c.get('default_code') or c['id']:38} "
            f"activo={c['active']} | {c['name']}"
        )
    if len(sin_costo_comps) > 40:
        print(f"  ... y {len(sin_costo_comps) - 40} mas (ver CSV)")

    # ---- Kits: estandar vs calculado ----
    act_kits = [r for r in kit_rows if r["p"]["active"]]
    k_std0 = [r for r in act_kits if r["std"] == 0]
    k_missing = [r for r in act_kits if r["missing"]]
    big_delta = [
        r
        for r in act_kits
        if r["computed"] > 0
        and r["std"] > 0
        and abs(r["computed"] - r["std"]) / max(r["std"], 0.01) > 0.20
    ]
    print(f"\n=== KITS ACTIVOS: {len(act_kits)} ===")
    print(f"std=0 (nunca calculado del bom): {len(k_std0)}")
    print(f"con componentes sin costo (bom incompleto): {len(k_missing)}")
    print(f"desvio std vs bom >20%: {len(big_delta)}")

    print("\ntop kits con mayor costo calculado (preview tarea 2):")
    for r in sorted(act_kits, key=lambda r: -r["computed"])[:15]:
        p = r["p"]
        print(
            f"  {p.get('default_code') or p['id']:42} std={r['std']:>8.2f} "
            f"bom={r['computed']:>8.2f} faltan={len(r['missing'])} | {p['name']}"
        )

    # ---- CSVs ----
    kits_csv = f"/data/cost_kits_{TODAY}.csv"
    with open(kits_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "sku",
                "nombre",
                "bom_tipo",
                "activo",
                "costo_std",
                "costo_bom_calc",
                "delta",
                "componentes_sin_costo",
                "avisos",
            ]
        )
        for r in sorted(kit_rows, key=lambda r: -r["computed"]):
            p = r["p"]
            delta = (r["computed"] - r["std"]) if r["std"] else ""
            w.writerow(
                [
                    p.get("default_code") or "",
                    p["name"],
                    r["bom_type"],
                    bool(p["active"]),
                    f"{r['std']:.4f}",
                    f"{r['computed']:.4f}",
                    f"{delta:.4f}" if delta != "" else "",
                    "; ".join(r["missing"]),
                    "; ".join(r["warnings"]),
                ]
            )
    print(f"\nCSV kits: {kits_csv}")

    comps_csv = f"/data/cost_componentes_{TODAY}.csv"
    with open(comps_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "sku",
                "nombre",
                "categoria",
                "costo",
                "activo",
                "tipo",
                "usado_en_kits",
                "flag",
            ]
        )
        for c in sorted(
            comps, key=lambda c: (comp_flag(c), -components_usage[c["id"]])
        ):
            w.writerow(
                [
                    c.get("default_code") or "",
                    c["name"],
                    (c["categ_id"] or ("", "?"))[1],
                    f"{float(c['standard_price'] or 0):.4f}",
                    bool(c["active"]),
                    c.get("type"),
                    components_usage[c["id"]],
                    comp_flag(c),
                ]
            )
    print(f"CSV componentes: {comps_csv}")


if __name__ == "__main__":
    main()
