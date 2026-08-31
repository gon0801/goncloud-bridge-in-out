"""Auditoria de mapeos Amazon<->Odoo en bridge.db + Odoo (solo lectura).

Correr DENTRO del contenedor bridge-api (monta /data/bridge.db y tiene red a Odoo):

    ssh gonserver "sudo docker exec -i bridge-api python3 -" < tools/amazon_mapping_audit.py

Valida, por cada fila de amazon_sku_mapping y amazon_sku_marketplace:
  - Odoo: que odoo_default_code exista como product.product (activo o archivado).
  - Amazon: que el asin guardado coincida con el asin "vivo" que reportan los
    caches amazon_fba_inventory / amazon_listing_prices.
Y en reversa: SellerSKUs vivos en Amazon sin mapeo y sin producto Odoo cuyo
default_code coincida (los que no resuelven por ningun camino).

Clasificacion de hallazgos por fila:
  BROKEN_ODOO   (Critica) odoo_default_code no existe en Odoo (ni archivado).
  ARCHIVED_ODOO (Alta)    el producto Odoo existe pero esta archivado.
  STALE_ASIN    (Alta)    asin guardado != asin vivo reportado por Amazon.
  EMPTY_CODE    (Alta)    odoo_default_code vacio.
  ASIN_FILL     (Media)   asin vacio pero Amazon reporta uno (se puede llenar).
  BAD_ASIN_FMT  (Media)   asin guardado no cumple formato B0XXXXXXXX.
  SKU_NOT_LIVE  (Baja)    el SKU no aparece en ningun cache vivo de Amazon.

Escribe TSVs a /data/mapping_audit_<fecha>/ (en host:
/mnt/data/appdata/bridge/data/mapping_audit_<fecha>/) e imprime resumen.
"""

import csv
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import defusedxml.xmlrpc as _defusedxml_xmlrpc

_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402

BRIDGE_DB = "/data/bridge.db"
OUT_DIR = Path(f"/data/mapping_audit_{date.today().isoformat()}")
ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")

SEVERITY = {
    "BROKEN_ODOO": "Critica",
    "ARCHIVED_ODOO": "Alta",
    "EMPTY_CODE": "Alta",
    "STALE_ASIN": "Alta",
    "BAD_ASIN_FMT": "Media",
    "ASIN_FILL": "Media",
    "SKU_NOT_LIVE": "Baja",
}


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch_odoo_products() -> dict[str, tuple[int, bool]]:
    """Baja todos los product.product (incluye archivados): default_code -> (id, active)."""
    con = sqlite3.connect(BRIDGE_DB)
    con.row_factory = sqlite3.Row

    def get(k: str) -> str:
        row = con.execute(
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

    products: dict[str, tuple[int, bool]] = {}
    offset, batch = 0, 500
    while True:
        rows = models.execute_kw(
            db,
            uid,
            pwd,
            "product.product",
            "search_read",
            [],
            {
                "fields": ["id", "default_code", "active"],
                "context": {"active_test": False},
                "offset": offset,
                "limit": batch,
            },
        )
        if not rows:
            break
        for r in rows:
            code = (r["default_code"] or "").strip()
            if code:
                pid, was_active = products.get(code, (r["id"], False))
                products[code] = (pid, was_active or r["active"])
        offset += batch
    con.close()
    return products


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(BRIDGE_DB)
    con.row_factory = sqlite3.Row

    # ---------- lado Amazon: asin(s) vivos por seller_sku ----------
    live_asins: dict[str, set[str]] = {}
    for r in con.execute(
        "SELECT DISTINCT seller_sku, asin FROM amazon_fba_inventory "
        "WHERE asin IS NOT NULL AND asin <> ''"
    ):
        live_asins.setdefault(r["seller_sku"], set()).add(r["asin"])
    for r in con.execute(
        "SELECT DISTINCT seller_sku, asin FROM amazon_listing_prices "
        "WHERE asin IS NOT NULL AND asin <> ''"
    ):
        live_asins.setdefault(r["seller_sku"], set()).add(r["asin"])

    fba_qty: dict[str, int] = {}
    for r in con.execute(
        "SELECT seller_sku, SUM(quantity_available) q FROM amazon_fba_inventory "
        "GROUP BY seller_sku"
    ):
        fba_qty[r["seller_sku"]] = r["q"] or 0

    listing_status: dict[str, str] = {}
    for r in con.execute(
        "SELECT seller_sku, status, COUNT(*) n FROM amazon_listing_prices "
        "GROUP BY seller_sku, status"
    ):
        cur = listing_status.get(r["seller_sku"], "")
        if r["status"] == "Active" or not cur:
            listing_status[r["seller_sku"]] = r["status"] or cur

    # ---------- lado Odoo ----------
    print("bajando product.product de Odoo...")
    odoo_products = fetch_odoo_products()
    print(f"productos Odoo con default_code: {len(odoo_products)}")

    # ---------- validar filas de mapeo ----------
    def audit_rows(table: str, rows: list) -> list[dict]:
        out = []
        for r in rows:
            sku = (r["seller_sku"] or "").strip()
            code = (r["odoo_default_code"] or "").strip()
            stored_asin = (r["asin"] or "").strip()
            flags = []

            if not code:
                flags.append("EMPTY_CODE")
            elif code not in odoo_products:
                flags.append("BROKEN_ODOO")
            elif not odoo_products[code][1]:
                flags.append("ARCHIVED_ODOO")

            vivo = live_asins.get(sku)
            if vivo is None:
                flags.append("SKU_NOT_LIVE")
            elif stored_asin and stored_asin not in vivo:
                flags.append("STALE_ASIN")
            elif not stored_asin and vivo:
                flags.append("ASIN_FILL")

            if stored_asin and not ASIN_RE.match(stored_asin):
                flags.append("BAD_ASIN_FMT")

            out.append(
                {
                    "tabla": table,
                    "seller_sku": sku,
                    "odoo_default_code": code,
                    "asin_guardado": stored_asin,
                    "asins_vivos": "|".join(sorted(vivo)) if vivo else "",
                    "fba_qty": fba_qty.get(sku, ""),
                    "listing_status": listing_status.get(sku, ""),
                    "odoo_product_id": odoo_products.get(code, ("",))[0],
                    "flags": "+".join(flags) if flags else "OK",
                }
            )
        return out

    map_rows = con.execute(
        "SELECT seller_sku, odoo_default_code, asin FROM amazon_sku_mapping"
    ).fetchall()
    mkp_rows = con.execute(
        "SELECT seller_sku, odoo_default_code, asin FROM amazon_sku_marketplace"
    ).fetchall()
    audited = audit_rows("amazon_sku_mapping", map_rows)
    audited += audit_rows("amazon_sku_marketplace", mkp_rows)

    # ---------- reversa: SKUs vivos sin resolver ----------
    mapped_skus = {r["seller_sku"] for r in map_rows} | {
        r["seller_sku"] for r in mkp_rows
    }
    unmapped = []
    for sku, asins in live_asins.items():
        if sku in mapped_skus or sku in odoo_products:
            continue
        qty = fba_qty.get(sku, 0)
        status = listing_status.get(sku, "")
        unmapped.append(
            {
                "seller_sku": sku,
                "asins": "|".join(sorted(asins)),
                "fba_qty": qty,
                "listing_status": status,
                "prioridad": "Alta" if (qty > 0 or status == "Active") else "Baja",
            }
        )
    unmapped.sort(key=lambda r: (-(r["fba_qty"] or 0), r["seller_sku"]))

    # ---------- salidas ----------
    def write_tsv(name: str, rows: list[dict], fields: list[str]) -> None:
        path = OUT_DIR / name
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter="\t")
            w.writeheader()
            w.writerows(rows)
        print(f"OUT={path} filas={len(rows)}")

    write_tsv(
        "mapeos_completo.tsv",
        audited,
        [
            "tabla",
            "seller_sku",
            "odoo_default_code",
            "asin_guardado",
            "asins_vivos",
            "fba_qty",
            "listing_status",
            "odoo_product_id",
            "flags",
        ],
    )
    write_tsv(
        "unmapped_live_skus.tsv",
        unmapped,
        ["seller_sku", "asins", "fba_qty", "listing_status", "prioridad"],
    )

    # ---------- resumen ----------
    total = len(audited)
    ok = sum(1 for r in audited if r["flags"] == "OK")
    print()
    print("=" * 60)
    print(f"RESUMEN: {total} filas de mapeo auditadas, {ok} OK sin flags")
    print("=" * 60)
    by_flag: dict[str, list[dict]] = {}
    for r in audited:
        for fl in r["flags"].split("+"):
            if fl != "OK":
                by_flag.setdefault(fl, []).append(r)
    for fl in [
        "BROKEN_ODOO",
        "EMPTY_CODE",
        "ARCHIVED_ODOO",
        "STALE_ASIN",
        "BAD_ASIN_FMT",
        "ASIN_FILL",
        "SKU_NOT_LIVE",
    ]:
        if fl not in by_flag:
            continue
        rows = by_flag[fl]
        print()
        print(f"--- {fl} [{SEVERITY[fl]}]: {len(rows)} filas ---")
        for r in rows[:15]:
            vivo = f" (vivo: {r['asins_vivos']})" if fl == "STALE_ASIN" else ""
            print(
                f"  [{r['tabla'][8:]}] {r['seller_sku']} -> "
                f"{r['odoo_default_code'] or '(vacio)'} | asin={r['asin_guardado']}"
                f"{vivo}"
            )
        if len(rows) > 15:
            print(f"  ... y {len(rows) - 15} mas (ver TSV)")
        write_tsv(
            f"finding_{fl.lower()}.tsv",
            rows,
            [
                "tabla",
                "seller_sku",
                "odoo_default_code",
                "asin_guardado",
                "asins_vivos",
                "fba_qty",
                "listing_status",
                "odoo_product_id",
                "flags",
            ],
        )

    alt = [u for u in unmapped if u["prioridad"] == "Alta"]
    print()
    print(
        f"REVERSA: {len(unmapped)} SKUs vivos en Amazon sin mapeo ni producto "
        f"Odoo con ese default_code ({len(alt)} con prioridad Alta: stock FBA>0 "
        f"o listing activo)"
    )
    for u in alt[:20]:
        print(
            f"  {u['seller_sku']} asin={u['asins']} fba_qty={u['fba_qty']} "
            f"listing={u['listing_status']}"
        )
    if len(alt) > 20:
        print(f"  ... y {len(alt) - 20} mas (ver TSV)")

    con.close()


if __name__ == "__main__":
    main()
