"""El almacen Flex no se publica en MeLi ni en Amazon FBM y sus entregas se cierran.

Por que existe
--------------
Las piezas apartadas para Amazon Flex viven en el almacen que nombra el
setting `warehouse_amazon_flex_mx`. Sin contexto, Odoo suma en qty_available y
outgoing_qty todos los almacenes, asi que los snapshotters publicarian esas
piezas. Y el cierre de entregas solo miraba EHV, asi que las salidas Flex
quedarian abiertas para siempre.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import stock_disponible as sd  # noqa: E402

ALMACENES = [
    {"id": 1, "name": "EHV-MX"},
    {"id": 2, "name": "Meli - Full"},
    {"id": 5, "name": "Flex - MX"},
]


def test_sin_apartado_no_hay_contexto():
    assert sd.contexto_publicable(ALMACENES, "") == {}


def test_el_apartado_queda_fuera_y_los_demas_cuentan():
    assert sd.contexto_publicable(ALMACENES, "Flex - MX") == {"warehouse": [1, 2]}


def test_un_apartado_que_no_existe_aborta():
    with pytest.raises(ValueError, match="no existe"):
        sd.contexto_publicable(ALMACENES, "Flex MX")


def test_si_solo_queda_el_apartado_aborta():
    with pytest.raises(ValueError, match="unico almacen"):
        sd.contexto_publicable([{"id": 5, "name": "Flex - MX"}], "Flex - MX")


@pytest.mark.parametrize(
    ("filas", "esperado"),
    [([], ""), ([("warehouse_amazon_flex_mx", "  Flex - MX ")], "Flex - MX"), ([("warehouse_amazon_flex_mx", None)], "")],
)
def test_el_apartado_sale_del_setting(filas, esperado):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE bridge_settings (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO bridge_settings VALUES (?, ?)", filas)
    assert sd.almacen_apartado(con) == esperado


@pytest.fixture(scope="module")
def cierre():
    ruta = ROOT / "tools" / "odoo_cerrar_entregas.py"
    spec = importlib.util.spec_from_file_location("cerrar_entregas_flex", ruta)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cerrar_entregas_flex"] = m
    spec.loader.exec_module(m)
    return m


class OdooQueGuardaElDominio:
    def __init__(self):
        self.dominio = None

    def kw(self, model, method, args, kwargs=None):
        assert (model, method) == ("stock.picking", "search_read")
        self.dominio = args[0]
        return []


def test_sin_flex_el_cierre_solo_lista_ehv(cierre):
    od = OdooQueGuardaElDominio()
    cierre.listar_entregas(od)
    assert od.dominio == [
        ["picking_type_code", "=", "outgoing"],
        ["picking_type_id.warehouse_id.code", "=", "EHV"],
        ["state", "in", ["waiting", "confirmed", "assigned"]],
        ["sale_id", "!=", False],
    ]


def test_con_flex_el_cierre_lista_ehv_o_flex(cierre):
    od = OdooQueGuardaElDominio()
    cierre.listar_entregas(od, "Flex - MX")
    assert od.dominio == [
        ["picking_type_code", "=", "outgoing"],
        "|",
        ["picking_type_id.warehouse_id.code", "=", "EHV"],
        ["picking_type_id.warehouse_id.name", "=", "Flex - MX"],
        ["state", "in", ["waiting", "confirmed", "assigned"]],
        ["sale_id", "!=", False],
    ]
