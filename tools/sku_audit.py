#!/usr/bin/env python3
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import json
import time
import sys
import requests

# =====================
# CONFIG
# =====================
OUT_DIR = Path("/tmp/sku_audit")
OUT_DIR.mkdir(parents=True, exist_ok=True)

TOK_PATH = Path("/mnt/data/appdata/bridge/data/.meli_tokens.json")
TIMEOUT = 30
SLEEP_BETWEEN = 0.12
LIMIT = 50
MAX_PAGES = 300  # safety

API = "https://api.mercadolibre.com"

# =====================
# UTILS
# =====================
def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"CMD failed: {' '.join(cmd)}\nSTDERR:\n{p.stderr.strip()}")
    return p.stdout

def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)

def load_token() -> str:
    if not TOK_PATH.exists():
        die(f"No existe token file: {TOK_PATH}")
    d = json.loads(TOK_PATH.read_text(encoding="utf-8"))
    tok = d.get("access_token")
    if not tok:
        die("No access_token en token file")
    return str(tok)

def req_json(url: str, headers: dict, retries: int = 4) -> dict:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code} {r.text[:200]}"
            # retry on transient
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.6 + i * 0.8)
                continue
            r.raise_for_status()
        except Exception as e:
            last = str(e)
            time.sleep(0.6 + i * 0.8)
    raise RuntimeError(f"GET failed after retries: {url}\nERR: {last}")

# =====================
# ODOO EXPORT
# =====================
def export_odoo_sell_on_meli() -> Path:
    ts = utc_ts()
    out = OUT_DIR / f"odoo_sell_on_meli_skus_{ts}.txt"

    sql = r"""
SELECT default_code
FROM (
  SELECT
    pp.default_code AS default_code,
    bool_or(pp.sell_on_meli) AS sell_on_meli_effective
  FROM product_product pp
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pt.active = true
    AND pp.default_code IS NOT NULL
    AND pp.default_code <> ''
  GROUP BY pp.default_code
) x
WHERE x.sell_on_meli_effective = true
ORDER BY default_code;
""".strip()

    txt = run([
        "sudo","docker","exec","-i","odoo-db-1",
        "psql","-U","odoo","-d","EHV","-At","-c",sql
    ])
    skus = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    out.write_text("\n".join(skus) + ("\n" if skus else ""), encoding="utf-8")
    print(f"ODOO_SELL_ON_MELI={len(skus)}")
    print(f"OUT={out}")
    return out

# =====================
# ML ACTIVE SCAN
# =====================
def get_user_id(headers: dict) -> str:
    d = req_json(f"{API}/users/me", headers=headers)
    uid = d.get("id")
    if uid is None:
        raise RuntimeError("No pude leer users/me id")
    return str(uid)

def iter_active_item_ids(headers: dict, user_id: str):
    """
    Usa search_type=scan para obtener items activos.
    OJO: ML puede devolver 400 si offset crece demasiado; aquí usamos scroll_id si aparece.
    """
    base = f"{API}/users/{user_id}/items/search?search_type=scan&status=active&limit={LIMIT}"
    d = req_json(base, headers=headers)

    results = d.get("results") or []
    scroll_id = d.get("scroll_id") or d.get("scrollId") or d.get("scroll")

    for it in results:
        yield str(it)

    pages = 1
    while True:
        if not scroll_id:
            # fallback: si no hay scroll_id, paramos (no arriesgamos offset=10000)
            break
        pages += 1
        if pages > MAX_PAGES:
            break
        time.sleep(SLEEP_BETWEEN)
        url = base + f"&scroll_id={scroll_id}"
        d = req_json(url, headers=headers)
        results = d.get("results") or []
        scroll_id = d.get("scroll_id") or d.get("scrollId") or d.get("scroll")
        if not results:
            break
        for it in results:
            yield str(it)

def extract_seller_sku_from_variation_full(vfull: dict) -> str | None:
    # Lo vimos en producción: attributes incluye id=SELLER_SKU
    for a in (vfull.get("attributes") or []):
        if a.get("id") == "SELLER_SKU":
            val = a.get("value_name")
            return str(val).strip() if val else None
    # fallback por si cambia: seller_custom_field
    val = vfull.get("seller_custom_field")
    return str(val).strip() if val else None

def build_ml_active_sku_map(headers: dict, user_id: str):
    """
    Regresa:
      sku_map: dict sku -> list[(item_id, variation_id)]
      active_skus: set
      total_items: int
    """
    sku_map: dict[str, list[tuple[str,str]]] = {}
    item_ids = list(iter_active_item_ids(headers, user_id))
    total = len(item_ids)
    print(f"items_activos_encontrados: {total}")

    done = 0
    for item_id in item_ids:
        time.sleep(SLEEP_BETWEEN)
        item = req_json(f"{API}/items/{item_id}", headers=headers)
        variations = item.get("variations") or []
        # Si no hay variaciones, no nos interesa para seller_sku por variación
        for v in variations:
            vid = v.get("id")
            if vid is None:
                continue
            vid = str(vid)
            time.sleep(SLEEP_BETWEEN)
            vfull = req_json(f"{API}/items/{item_id}/variations/{vid}", headers=headers)
            sku = extract_seller_sku_from_variation_full(vfull)
            if not sku:
                continue
            sku_map.setdefault(sku, []).append((item_id, vid))

        done += 1
        if done % 25 == 0 or done == total:
            print(f"procesados {done}/{total}")

    return sku_map, total

# =====================
# COMPARE
# =====================
def compare(odoo_skus: list[str], ml_sku_map: dict[str, list[tuple[str,str]]]):
    odoo_set = set(odoo_skus)
    ok = []
    missing = []
    dup = []

    for s in sorted(odoo_set):
        hits = ml_sku_map.get(s, [])
        if not hits:
            missing.append(s)
        elif len(hits) == 1:
            ok.append((s, hits[0][0], hits[0][1]))
        else:
            dup.append((s, hits))

    return ok, missing, dup

# =====================
# MAIN
# =====================
def main():
    ts = utc_ts()
    print("GONCLOUD SKU AUDIT — ODOO + ML ACTIVE")

    # Odoo list
    odoo_file = export_odoo_sell_on_meli()
    odoo_skus = [ln.strip() for ln in odoo_file.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ML Active map
    token = load_token()
    headers = {"Authorization": f"Bearer {token}"}
    user_id = get_user_id(headers)
    print(f"user_id: {user_id}")

    ml_map, _total_items = build_ml_active_sku_map(headers, user_id)

    # compare
    ok, missing, dup = compare(odoo_skus, ml_map)

    # outputs
    out_active_skus = OUT_DIR / f"ml_active_seller_skus_{ts}.txt"
    out_map = OUT_DIR / f"ml_active_sku_map_{ts}.tsv"
    out_missing = OUT_DIR / f"missing_in_ml_active_{ts}.txt"
    out_dup = OUT_DIR / f"duplicates_in_ml_active_{ts}.txt"
    out_report = OUT_DIR / f"report_{ts}.txt"

    active_sku_list = sorted(ml_map.keys())
    out_active_skus.write_text("\n".join(active_sku_list) + ("\n" if active_sku_list else ""), encoding="utf-8")

    with out_map.open("w", encoding="utf-8") as f:
        f.write("seller_sku\titem_id\tvariation_id\n")
        for sku in sorted(ml_map.keys()):
            for item_id, vid in ml_map[sku]:
                f.write(f"{sku}\t{item_id}\t{vid}\n")

    out_missing.write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")

    with out_dup.open("w", encoding="utf-8") as f:
        for sku, hits in dup:
            f.write(sku + "\n")
            for item_id, vid in hits:
                f.write(f"  {item_id}\t{vid}\n")

    rep = []
    rep.append("RESULTADO FINAL")
    rep.append(f"ODOO_SELL_ON_MELI={len(set(odoo_skus))}")
    rep.append(f"ML_ACTIVE_UNIQUE_SKUS={len(active_sku_list)}")
    rep.append("")
    rep.append(f"OK_EN_ML_ACTIVO={len(ok)}")
    rep.append(f"FALTAN_EN_ML_ACTIVO={len(missing)}")
    rep.append(f"DUPLICADOS_EN_ML_ACTIVO={len(dup)}")
    rep.append("")
    rep.append("SALIDAS:")
    rep.append(str(odoo_file))
    rep.append(str(out_active_skus))
    rep.append(str(out_map))
    rep.append(str(out_missing))
    rep.append(str(out_dup))
    rep.append(str(out_report))
    out_report.write_text("\n".join(rep) + "\n", encoding="utf-8")

    print("\n========================")
    print("RESULTADO FINAL")
    print("========================")
    print(f"OK_EN_ML_ACTIVO: {len(ok)}")
    print(f"FALTAN_EN_ML_ACTIVO: {len(missing)}")
    print(f"DUPLICADOS_EN_ML_ACTIVO: {len(dup)}")
    print("\nSalidas:")
    print(f"- {out_active_skus}")
    print(f"- {out_map}")
    print(f"- {out_missing}")
    print(f"- {out_report}")

if __name__ == "__main__":
    main()
