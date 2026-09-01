"""Auditoria de costos de productos en Odoo (solo lectura).

Correr DENTRO del contenedor bridge-api (monta /data/bridge.db y tiene red a Odoo):

    ssh gonserver "sudo docker exec -i bridge-api python3 -" < tools/odoo_cost_audit.py

Que hace:
  - Lee credenciales Odoo de bridge_settings (bridge.db).
  - Baja todos los product.product (incluye archivados) con costo, tipo, categoria y stock.
  - Flaggea: costo 0, costo negativo, costo < 1 (sospechosamente bajo),
    outliers altos (> percentil 99 de los costos positivos).
  - Escribe CSV completo a /data/cost_audit_<fecha>.csv (en host:
    /mnt/data/appdata/bridge/data/cost_audit_<fecha>.csv) e imprime resumen.
"""

import csv
import sqlite3
import sys
from datetime import date

import defusedxml.xmlrpc as _defusedxml_xmlrpc

_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402

BRIDGE_DB = "/data/bridge.db"
CSV_PATH = f"/data/cost_audit_{date.today().isoformat()}.csv"
LOW_COST_THRESHOLD = 1.0  # 0 < costo < esto => sospechosamente bajo


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
        die("faltan odoo_url/odoo_db/odoo_user/odoo_password en bridge_settings")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, usr, pwd, {})
    if not uid:
        die("autenticacion Odoo rechazada")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def call(model: str, method: str, *args, **kw):
        return models.execute_kw(db, uid, pwd, model, method, list(args), kw)

    # Moneda de la compania del usuario, para etiquetar el costo
    currency = "?"
    try:
        user = call("res.users", "read", [uid], fields=["company_id"])[0]
        if user.get("company_id"):
            comp = call(
                "res.company", "read", [user["company_id"][0]], fields=["currency_id"]
            )[0]
            if comp.get("currency_id"):
                currency = comp["currency_id"][1]
    except Exception as exc:  # la moneda es cosmética, no frenar la auditoría
        print(f"(no pude leer moneda: {exc})")

    fields = [
        "id",
        "default_code",
        "name",
        "standard_price",
        "type",
        "categ_id",
        "active",
        "purchase_ok",
        "qty_available",
        "uom_id",
    ]
    prods = call(
        "product.product",
        "search_read",
        fields=fields,
        context={"active_test": False},
        order="standard_price asc",
    )
    print(f"productos leidos: {len(prods)} (moneda de costo: {currency})")

    costs = [float(p["standard_price"] or 0) for p in prods]
    positives = sorted(c for c in costs if c > 0)

    def pct(data: list[float], q: float) -> float:
        if not data:
            return 0.0
        idx = min(int(len(data) * q), len(data) - 1)
        return data[idx]

    p50 = pct(positives, 0.50)
    p99 = pct(positives, 0.99)
    high_threshold = max(p99, p50 * 20)  # outliers altos por estadistica

    stats = (
        f"costos positivos: {len(positives)} | mediana={p50:.2f} "
        f"p90={pct(positives, 0.90):.2f} p99={p99:.2f} "
        f"max={positives[-1] if positives else 0:.2f}"
    )
    print(stats)

    def flag(p: dict) -> str:
        c = float(p["standard_price"] or 0)
        if c < 0:
            return "negativo"
        if c == 0:
            return "sin_costo"
        if c < LOW_COST_THRESHOLD:
            return "muy_bajo"
        if c > high_threshold:
            return "outlier_alto"
        return "ok"

    for p in prods:
        p["flag"] = flag(p)

    flagged = [p for p in prods if p["flag"] != "ok"]
    flagged.sort(key=lambda p: (p["flag"], -float(p["standard_price"] or 0)))

    counts: dict[str, int] = {}
    for p in flagged:
        counts[p["flag"]] = counts.get(p["flag"], 0) + 1
    print(f"flaggeados: {len(flagged)} de {len(prods)} -> {counts}")

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "flag",
                "id",
                "sku",
                "nombre",
                "costo",
                "tipo",
                "categoria",
                "activo",
                "purchase_ok",
                "stock",
                "uom",
            ]
        )
        for p in prods:
            w.writerow(
                [
                    p["flag"],
                    p["id"],
                    p.get("default_code") or "",
                    p.get("name") or "",
                    float(p["standard_price"] or 0),
                    p.get("type") or "",
                    (p.get("categ_id") or ("", ""))[1],
                    bool(p.get("active")),
                    bool(p.get("purchase_ok")),
                    float(p.get("qty_available") or 0),
                    (p.get("uom_id") or ("", ""))[1],
                ]
            )
    print(f"CSV completo: {CSV_PATH}")

    # Resumen en consola: primero los que impactan valorizacion (con stock)
    def line(p: dict) -> str:
        c = float(p["standard_price"] or 0)
        return (
            f"  [{p['flag']}] id={p['id']} sku={p.get('default_code') or '-'} "
            f"costo={c:.2f} stock={float(p.get('qty_available') or 0):.2f} "
            f"activo={bool(p.get('active'))} | {p.get('name')}"
        )

    with_stock = [p for p in flagged if float(p.get("qty_available") or 0) > 0]
    print(f"\n== FLAGGEADOS CON STOCK (impactan valorizacion): {len(with_stock)} ==")
    for p in with_stock[:60]:
        print(line(p))
    if len(with_stock) > 60:
        print(f"  ... y {len(with_stock) - 60} mas (ver CSV)")

    print(f"\n== TOP OUTLIERS ALTOS (>{high_threshold:.2f}) ==")
    highs = [p for p in flagged if p["flag"] == "outlier_alto"]
    highs.sort(key=lambda p: -float(p["standard_price"] or 0))
    for p in highs[:25]:
        print(line(p))


if __name__ == "__main__":
    main()
