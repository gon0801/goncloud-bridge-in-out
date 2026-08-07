import json
import urllib.request
import pathlib

tok = pathlib.Path("/tmp/meli_tok.txt").read_text().strip()

pairs = [
    ("NH-CAR-AZU-CEN-DOR", "MLM2163404350", "178100430981", 89),
    ("NH-CAR-AZU-COR-DOR", "MLM2163404350", "178100430993", 89),
    ("NH-CAR-AZU-SAN-DOR", "MLM2163404350", "178100430983", 89),
    ("NH-CAR-AZU-VBU-DOR", "MLM2163404350", "178100430987", 89),
    ("NH-CAR-AZU-VCO-DOR", "MLM2163404350", "178100430985", 89),
    ("NH-CAR-AZU-PEZ-DOR", "MLM2163404350", "194936751959", 0),
    ("NH-CAR-AZU-CEN-PLA", "MLM1890605671", "178099757201", 99),
    ("NH-CAR-AZU-COR-PLA", "MLM1890605671", "178099757213", 99),
    ("NH-CAR-AZU-SAN-PLA", "MLM1890605671", "178099757203", 98),
    ("NH-CAR-AZU-VBU-PLA", "MLM1890605671", "178099757207", 99),
    ("NH-CAR-AZU-VCO-PLA", "MLM1890605671", "178099757205", 99),
    ("NH-CAR-AZU-PEZ-PLA", "MLM1890605671", "195062806985", 0),
]


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


ok = 0
for sku, item, var, expected in pairs:
    j = get(f"https://api.mercadolibre.com/items/{item}/variations/{var}")
    got = j.get("available_quantity")
    status = "OK" if got == expected else "MISMATCH"
    ok += status == "OK"
    print(f"{status}\t{sku}\texpected={expected}\tgot={got}\titem={item}\tvar={var}")

print(f"\nSUMMARY ok={ok}/{len(pairs)}")
