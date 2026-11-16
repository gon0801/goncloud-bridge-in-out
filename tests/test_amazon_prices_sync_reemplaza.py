"""amazon-prices-sync deja en la tabla solo lo que trae el reporte de Amazon.

Por que existe
--------------
`parse_and_save` hacia upsert sin borrar. Una publicacion que salio del
reporte (borrada o pasada a FBA) seguia con su ultima fila, y el worker de
stock, que decide por `fulfillment_channel`, le seguia mandando PATCH como si
fuera DEFAULT. En produccion habia filas con `fetched_at` de abril.
"""

import importlib.util
import sqlite3
from pathlib import Path

SYNC = Path(__file__).resolve().parent.parent / "tools" / "amazon_prices_sync.py"
MX = "A1AM78C64UM0Y8"
US = "ATVPDKIKX0DER"
CABECERA = (
    "seller-sku\tasin1\tlisting-id\tprice\tquantity\t"
    "fulfillment-channel\titem-name\tstatus\n"
)


def _cargar():
    spec = importlib.util.spec_from_file_location("amazon_prices_sync_t", SYNC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db(sync):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    sync.init_table(db)
    db.executemany(
        "INSERT INTO amazon_listing_prices(seller_sku, marketplace_id, "
        "marketplace_name, fulfillment_channel, quantity) VALUES (?,?,?,?,?)",
        [
            ("FANTASMA", MX, "amazon_mx", "DEFAULT", 5),
            ("VIGENTE", MX, "amazon_mx", "AMAZON_NA", None),
            ("FANTASMA", US, "amazon_us", "DEFAULT", 2),
        ],
    )
    db.commit()
    return db


def _filas(db):
    return sorted(
        tuple(r)
        for r in db.execute(
            "SELECT seller_sku, marketplace_id, fulfillment_channel, quantity "
            "FROM amazon_listing_prices"
        )
    )


def test_el_reporte_reemplaza_las_filas_de_su_marketplace():
    sync = _cargar()
    db = _db(sync)
    tsv = CABECERA + "VIGENTE\tB01\tL1\t10.0\t3\tDEFAULT\tTaza\tActive\n"

    sync.parse_and_save(tsv, db, MX, "amazon_mx")

    assert _filas(db) == [
        ("FANTASMA", US, "DEFAULT", 2),
        ("VIGENTE", MX, "DEFAULT", 3),
    ]


def test_un_reporte_vacio_no_borra_nada():
    sync = _cargar()
    db = _db(sync)
    antes = _filas(db)

    sync.parse_and_save(CABECERA, db, MX, "amazon_mx")

    assert _filas(db) == antes
