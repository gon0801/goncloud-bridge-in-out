"""El worker manda el stock a cada publicacion FBM de Amazon en su marketplace.

Por que existe
--------------
`app/worker.py` tomaba un solo seller SKU por codigo de Odoo (`fetchone()`)
y siempre lo mandaba al marketplace MX. Asi, de los codigos con varias
publicaciones solo una se actualizaba, las de US iban a MX y nunca recibian
stock, y las publicaciones AFN (Flex/FBA) recibian un PATCH con canal
DEFAULT.

El marketplace y el canal de cada publicacion salen de
`amazon_listing_prices` (el reporte de Amazon), no de
`amazon_sku_mapping.keepa_domain`: esa columna no existe en la tabla que crea
`amazon_inbound_worker.py` y el guardado de la UI la deja en 11. Por eso la
prueba corre con las dos formas de esa tabla.

Como se prueba
--------------
Se carga el worker real contra una SQLite temporal con las tablas que lee y
se reemplaza `urllib.request.urlopen` por un Amazon falso que registra cada
peticion. Las aserciones son sobre las peticiones que salen.
"""

import importlib.util
import io
import json
import sqlite3
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parent.parent / "app" / "worker.py"
MX = "A1AM78C64UM0Y8"
US = "ATVPDKIKX0DER"


def _cargar_worker(db_path: Path):
    spec = importlib.util.spec_from_file_location("worker_amazon_stock", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = str(db_path)
    mod.AMAZON_PATCH_MIN_INTERVAL_S = 0
    mod.AMAZON_RETRY_429_SLEEP_S = 0
    return mod


MAPEO_SIN_KEEPA = """
CREATE TABLE amazon_sku_mapping(seller_sku TEXT PRIMARY KEY,
  odoo_default_code TEXT);
"""
MAPEO_KEEPA_PISADO = """
CREATE TABLE amazon_sku_mapping(seller_sku TEXT PRIMARY KEY,
  odoo_default_code TEXT, keepa_domain INTEGER DEFAULT 11);
"""


def _crear_db(db_path: Path, mapeo_ddl: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(mapeo_ddl)
    conn.executescript(
        """
        CREATE TABLE bridge_settings(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO bridge_settings VALUES
          ('amazon_sp_api_refresh_token','rt'),('amazon_sp_api_client_id','cid'),
          ('amazon_sp_api_client_secret','cs'),('amazon_seller_id','SELLER'),
          ('amazon_marketplace_id','A1AM78C64UM0Y8');
        CREATE TABLE amazon_listing_prices(seller_sku TEXT, marketplace_id TEXT,
          fulfillment_channel TEXT, UNIQUE(seller_sku, marketplace_id));
        INSERT INTO amazon_sku_mapping(seller_sku, odoo_default_code) VALUES
          ('S-MX','SET-A'),('S-MX-FLEX','SET-A'),('S-US','SET-A'),
          ('S-OTRO','SET-B'),('S-RARO','SET-C'),('S-CA','SET-C');
        INSERT INTO amazon_listing_prices VALUES
          ('S-MX','A1AM78C64UM0Y8','DEFAULT'),
          ('S-MX-FLEX','A1AM78C64UM0Y8','AMAZON_NA'),
          ('S-MX-FLEX','ATVPDKIKX0DER','AMAZON_NA'),
          ('S-US','ATVPDKIKX0DER','DEFAULT'),
          ('S-OTRO','A1AM78C64UM0Y8','DEFAULT'),
          ('S-RARO','A1AM78C64UM0Y8','AMAZON_EU'),
          ('S-RARO','ATVPDKIKX0DER',''),
          ('S-CA','A2EUQ1WTGCTBG2','DEFAULT');
        """
    )
    conn.commit()
    conn.close()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AmazonFalso:
    """Responde LWA y Listings PATCH; `respuestas` fija la de un seller SKU."""

    def __init__(self):
        self.tokens = 0
        self.patches = []
        self.respuestas = {}

    def urlopen(self, req, timeout=None):
        if req.full_url == "https://api.amazon.com/auth/o2/token":
            self.tokens += 1
            return _Resp(
                json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
            )
        url = urllib.parse.urlparse(req.full_url)
        seller_sku = urllib.parse.unquote(url.path.rsplit("/", 1)[1])
        body = json.loads(req.data)
        self.patches.append(
            {
                "method": req.get_method(),
                "seller_sku": seller_sku,
                "marketplace": urllib.parse.parse_qs(url.query)["marketplaceIds"][0],
                "availability": body["patches"][0]["value"],
            }
        )
        pendientes = self.respuestas.get(seller_sku) or [{"status": "ACCEPTED"}]
        resp = pendientes.pop(0) if len(pendientes) > 1 else pendientes[0]
        if isinstance(resp, int):
            raise urllib.error.HTTPError(req.full_url, resp, "x", {}, io.BytesIO(b"{}"))
        return _Resp(json.dumps(resp).encode())


@pytest.fixture(
    params=[MAPEO_SIN_KEEPA, MAPEO_KEEPA_PISADO], ids=["sin_keepa", "keepa_pisado"]
)
def entorno(request, tmp_path, monkeypatch):
    db_path = tmp_path / "bridge.db"
    _crear_db(db_path, request.param)
    worker = _cargar_worker(db_path)
    amazon = AmazonFalso()
    monkeypatch.setattr(worker.urllib.request, "urlopen", amazon.urlopen)
    return worker, amazon


def test_cada_publicacion_fbm_recibe_el_stock_en_su_marketplace(entorno):
    worker, amazon = entorno
    detalle = worker.amazon_set_qty("SET-A", 7)

    enviados = sorted((p["seller_sku"], p["marketplace"]) for p in amazon.patches)
    assert enviados == [("S-MX", MX), ("S-US", US)]
    for p in amazon.patches:
        assert p["method"] == "PATCH"
        assert p["availability"] == [
            {"fulfillment_channel_code": "DEFAULT", "quantity": 7}
        ]
    assert "skip[S-MX-FLEX@MX] no_fbm:AMAZON_NA" in detalle
    assert "skip[S-MX-FLEX@US] no_fbm:AMAZON_NA" in detalle
    assert "FAIL[" not in detalle


def test_canal_distinto_de_default_o_marketplace_no_soportado_no_recibe_patch(
    entorno,
):
    worker, amazon = entorno
    detalle = worker.amazon_set_qty("SET-C", 4)

    assert amazon.patches == []
    assert "skip[S-RARO@MX] no_fbm:AMAZON_EU" in detalle
    assert "skip[S-RARO@US] no_fbm:vacio" in detalle
    assert "skip[S-CA@?] marketplace_no_soportado:A2EUQ1WTGCTBG2" in detalle


def test_el_token_de_lwa_se_reusa_entre_jobs(entorno):
    worker, amazon = entorno
    worker.amazon_set_qty("SET-A", 7)
    worker.amazon_set_qty("SET-B", 3)
    assert amazon.tokens == 1
    assert len(amazon.patches) == 3


def test_rechazo_de_amazon_queda_como_falla_y_429_se_reintenta(entorno):
    worker, amazon = entorno
    amazon.respuestas["S-MX"] = [
        {"status": "INVALID", "issues": [{"message": "cantidad invalida"}]}
    ]
    amazon.respuestas["S-US"] = [429, {"status": "ACCEPTED"}]

    detalle = worker.amazon_set_qty("SET-A", 7)

    assert "FAIL[S-MX@MX] status=INVALID cantidad invalida" in detalle
    assert "put[S-US@US] status=ACCEPTED" in detalle
    assert [p["seller_sku"] for p in amazon.patches].count("S-US") == 2
