"""Las ventas Flex MX salen del almacén que indique `warehouse_amazon_flex_mx`.

Por que existe
--------------
Flex se vende como FBA pero con nuestro nodo y su propio inventario. Con el
setting vacío (valor por omisión) el pedido de perfil FLEX_MX se comporta como
siempre: SO en el almacén por omisión (EHV-MX), picking desde EHV/Stock. Con un
nombre ("FBA - MX") la SO se crea en ese almacén y su picking sale de
FBAMX/Stock, sin cancelarse. Ningún otro perfil lee el setting.

El tool corre contra un Odoo simulado que modela solo lo que importa aquí: el
almacén de la SO decide la ubicación de origen del picking al confirmarla.
"""

import importlib.util
import json
import runpy
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MX = "A1AM78C64UM0Y8"
US = "ATVPDKIKX0DER"
SETTING = "warehouse_amazon_flex_mx"

AFN_MX = {"FulfillmentChannel": "AFN", "MarketplaceId": MX}
EASY_MX = {"FulfillmentChannel": "MFN", "MarketplaceId": MX, "EasyShipShipmentStatus": "PendingPickUp"}
FBM_MX = {"FulfillmentChannel": "MFN", "MarketplaceId": MX}
FBM_US = {"FulfillmentChannel": "MFN", "MarketplaceId": US}


def _cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def worker(tmp_path):
    path = tmp_path / "bridge.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE bridge_settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO bridge_settings VALUES ('amazon_inbound_fbm_paid_enabled', '1')")
    con.commit()
    con.close()
    mod = _cargar("amazon_inbound_worker_flex_wh", ROOT / "app" / "amazon_inbound_worker.py")
    mod.db = sqlite3.connect(path)
    yield mod
    mod.db.close()


def _entorno_del_tool(worker, orden, setting):
    worker.db.execute("DELETE FROM bridge_settings WHERE key=?", (SETTING,))
    if setting is not None:
        worker.db.execute("INSERT INTO bridge_settings VALUES (?, ?)", (SETTING, setting))
    worker._settings_cache.clear()
    llamadas = []
    worker.run_tool = lambda nombre, env: llamadas.append((nombre, env)) or (0, "", "")
    worker.map_tool_result = lambda rc, out, err: ("ok", {})
    worker._finalize = lambda *a, **k: False
    worker.process_fbm(orden, "111-1", orden["MarketplaceId"], "paid", "dk")
    assert [n for n, _ in llamadas] == ["amazon_fbm_paid_one_shot"]
    return llamadas[0][1]


@pytest.mark.parametrize("setting", [None, "", "   "])
def test_flex_con_el_setting_vacio_no_pide_almacen(worker, setting):
    env = _entorno_del_tool(worker, AFN_MX, setting)
    assert env.get("WAREHOUSE_NAME", "") == ""


def test_flex_con_el_setting_pide_ese_almacen(worker):
    env = _entorno_del_tool(worker, AFN_MX, " FBA - MX ")
    assert env["WAREHOUSE_NAME"] == "FBA - MX"


@pytest.mark.parametrize(
    "orden", [EASY_MX, FBM_MX, FBM_US], ids=["easy_mx", "fbm_mx", "fbm_us"]
)
def test_los_otros_perfiles_fbm_ignoran_el_setting(worker, orden):
    env = _entorno_del_tool(worker, orden, "FBA - MX")
    assert "WAREHOUSE_NAME" not in env


def test_fba_us_sigue_con_su_almacen_y_el_setting_flex_no_lo_toca(worker):
    worker.db.execute("INSERT INTO bridge_settings VALUES ('amazon_inbound_fba_paid_enabled', '1')")
    worker.db.execute("INSERT INTO bridge_settings VALUES ('warehouse_amazon_fba_us', 'FBA - US')")
    worker.db.execute("INSERT INTO bridge_settings VALUES (?, 'FBA - MX')", (SETTING,))
    worker._settings_cache.clear()
    llamadas = []
    worker.run_tool = lambda nombre, env: llamadas.append((nombre, env)) or (0, "", "")
    worker.map_tool_result = lambda rc, out, err: ("ok", {})
    worker._finalize = lambda *a, **k: False
    worker.process_fba({"FulfillmentChannel": "AFN", "MarketplaceId": US}, "111-1", US, "paid", "dk")
    assert [(n, e["WAREHOUSE_NAME"]) for n, e in llamadas] == [
        ("amazon_fba_paid_one_shot", "FBA - US")
    ]


ALMACENES = {
    "EHV-MX": {"id": 1, "origen": "EHV/Stock"},
    "FBA - MX": {"id": 2, "origen": "FBAMX/Stock"},
}
ALMACEN_POR_OMISION = 1


class OdooSimulado:
    def __init__(self):
        self.llamadas = []
        self.sos = {}
        self.pickings = {}

    def execute(self, modelo, metodo, args, kwargs):
        self.llamadas.append((modelo, metodo))
        if modelo == "sale.order" and metodo == "search_read":
            return []
        if modelo == "res.partner" and metodo == "search_read":
            return [{"id": 7}]
        if modelo == "product.product" and metodo == "search_read":
            return [{"id": 9, "name": "P"}]
        if modelo == "stock.warehouse" and metodo == "search_read":
            nombre = args[0][0][2]
            return [
                {"id": a["id"], "name": n} for n, a in ALMACENES.items() if n == nombre
            ]
        if modelo == "sale.order" and metodo == "create":
            vals = args[0]
            self.sos[1] = {"state": "draft", "picking_ids": [], **vals}
            return 1
        if modelo == "sale.order.line" and metodo == "search_read":
            return [{"id": 5, "name": "SKU1", "price_unit": 0}] if self._hay_lineas else []
        if modelo == "sale.order.line" and metodo == "create":
            self._hay_lineas = True
            return 5
        if modelo == "sale.order" and metodo == "read":
            so = self.sos[1]
            return [{"id": 1, "name": "S00001", "state": so["state"], "picking_ids": so["picking_ids"]}]
        if modelo == "sale.order" and metodo == "action_confirm":
            so = self.sos[1]
            wh = so.get("warehouse_id", ALMACEN_POR_OMISION)
            origen = next(a["origen"] for a in ALMACENES.values() if a["id"] == wh)
            self.pickings[10] = {"origen": origen, "state": "assigned"}
            so.update(state="sale", picking_ids=[10])
            return True
        raise AssertionError(f"llamada no esperada: {modelo}.{metodo}")

    _hay_lineas = False


def _correr_tool(monkeypatch, almacen):
    odoo = OdooSimulado()

    class Respuesta:
        def __init__(self, resultado):
            self._r = resultado

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": self._r}

    def post(url, json=None, timeout=None):
        p = json["params"]
        if p["service"] == "common":
            return Respuesta(3)
        _db, _uid, _pw, modelo, metodo, args, kwargs = p["args"]
        return Respuesta(odoo.execute(modelo, metodo, args, kwargs))

    import requests

    monkeypatch.setattr(requests, "post", post)
    orden = {
        "AmazonOrderId": "111-1",
        "OrderItems": [{"SellerSKU": "SKU1", "QuantityOrdered": 1}],
    }
    for k, v in {
        "ODOO_URL": "http://odoo.test",
        "ODOO_DB": "db",
        "ODOO_USER": "u",
        "ODOO_PASS": "p",
        "BRIDGE_DB": "/nonexistent/bridge.db",
        "ORDER_JSON": json.dumps(orden),
        "CLIENT_ORDER_REF": "AMZFBM:MX:111-1",
        "CHANNEL_LABEL": "Amazon Flex MX",
    }.items():
        monkeypatch.setenv(k, v)
    if almacen is None:
        monkeypatch.delenv("WAREHOUSE_NAME", raising=False)
    else:
        monkeypatch.setenv("WAREHOUSE_NAME", almacen)
    with pytest.raises(SystemExit) as salida:
        runpy.run_path(str(ROOT / "tools" / "amazon_fbm_paid_one_shot.py"), run_name="__main__")
    return odoo, salida.value.code


@pytest.mark.parametrize("almacen", [None, ""])
def test_el_tool_sin_almacen_crea_la_so_en_el_almacen_por_omision(monkeypatch, almacen):
    odoo, rc = _correr_tool(monkeypatch, almacen)
    assert rc == 0
    assert "warehouse_id" not in odoo.sos[1]
    assert odoo.pickings == {10: {"origen": "EHV/Stock", "state": "assigned"}}


def test_el_tool_con_fba_mx_saca_el_picking_de_fbamx_stock_sin_cancelarlo(monkeypatch):
    odoo, rc = _correr_tool(monkeypatch, "FBA - MX")
    assert rc == 0
    assert odoo.sos[1]["warehouse_id"] == 2
    assert odoo.pickings == {10: {"origen": "FBAMX/Stock", "state": "assigned"}}
    assert odoo.sos[1]["picking_ids"] == [10]
    assert not [c for c in odoo.llamadas if c[0] == "stock.picking"]


def test_el_tool_con_un_almacen_inexistente_aborta_sin_crear_la_so(monkeypatch):
    odoo, rc = _correr_tool(monkeypatch, "No existe")
    assert rc == 1
    assert odoo.sos == {}
