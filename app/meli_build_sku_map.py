import json
import time
import csv
import urllib.request
from pathlib import Path

# === CONFIG ===
TOK = json.load(open("/mnt/data/appdata/bridge/.meli_tokens.json"))["access_token"]
ITEMS = json.load(open("/mnt/data/appdata/bridge/data/meli_item_ids.json"))["items"]

OUT_DIR = Path("/mnt/data/appdata/bridge/data")
OUT_JSON = OUT_DIR / "meli_sku_map.json"
OUT_CSV = OUT_DIR / "meli_sku_map.csv"

# === HELPERS ===
def get(url):
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + TOK}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def pick_sku(item):
    # 1) seller_custom_field (prioridad máxima)
    scf = item.get("seller_custom_field")
    if scf and str(scf).strip():
        return str(scf).strip()

    # 2) attributes comunes
    for a in item.get("attributes") or []:
        aid = (a.get("id") or "").upper()
        val = a.get("value_name") or a.get("value_id")
        if not val:
            continue
        if aid in ("SELLER_SKU", "SKU", "MODEL", "PART_NUMBER"):
            return str(val).strip()

    return None

# === MAIN ===
OUT_DIR.mkdir(parents=True, exist_ok=True)

out_rows = []
sku_map = {}
missing = 0

for idx, item_id in enumerate(ITEMS, 1):
    item = get(f"https://api.mercadolibre.com/items/{item_id}")
    sku = pick_sku(item)

    variations = item.get("variations") or []

    if variations:
        any_vsku = False
        for v in variations:
            vsku = v.get("seller_custom_field")
            if vsku and str(vsku).strip():
                any_vsku = True
                vsku = str(vsku).strip()
                sku_map[vsku] = {
                    "item_id": item_id,
                    "variation_id": v.get("id")
                }
                out_rows.append([
                    vsku,
                    item_id,
                    v.get("id") or "",
                    item.get("status"),
                    (item.get("title") or "")[:120]
                ])

        if not any_vsku:
            if sku:
                sku_map[sku] = {
                    "item_id": item_id,
                    "variation_id": None
                }
                out_rows.append([
                    sku,
                    item_id,
                    "",
                    item.get("status"),
                    (item.get("title") or "")[:120]
                ])
            else:
                missing += 1
    else:
        if sku:
            sku_map[sku] = {
                "item_id": item_id,
                "variation_id": None
            }
            out_rows.append([
                sku,
                item_id,
                "",
                item.get("status"),
                (item.get("title") or "")[:120]
            ])
        else:
            missing += 1

    if idx % 20 == 0:
        time.sleep(0.25)

# === OUTPUT ===
with open(OUT_JSON, "w") as f:
    json.dump(sku_map, f, indent=2)

with open(OUT_CSV, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["sku", "item_id", "variation_id", "status", "title"])
    w.writerows(out_rows)

print("DONE")
print("items_total =", len(ITEMS))
print("mapped_skus =", len(sku_map))
print("missing_sku =", missing)
print("WROTE", OUT_JSON)
print("WROTE", OUT_CSV)
