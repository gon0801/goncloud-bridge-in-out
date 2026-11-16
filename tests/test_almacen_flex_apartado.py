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
from datetime import UTC, datetime
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


AHORA = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


class OdooFlex:
    """Una venta Amazon Flex MX abierta en el almacen Flex que pide 2 piezas,
    con `reservado` ya reservadas y `libre` libres en FLEX/Stock."""

    def __init__(self, libre, reservado=0.0, creada="2026-11-14 10:00:00"):
        self.libre = libre
        self.creada = creada
        self.state = "confirmed"
        self.move = {
            "id": 11,
            "state": "confirmed",
            "product_uom_qty": 2.0,
            "quantity": reservado,
            "has_tracking": "none",
            "product_id": [40, "[SET-X] Set"],
            "location_id": [50, "FLEX/Stock"],
        }
        self.escrituras = []

    def kw(self, model, method, args, kwargs=None):
        if (model, method) == ("stock.picking", "search_read"):
            return [
                {
                    "id": 1,
                    "name": "FLEX/OUT/00001",
                    "sale_id": [9, "S00009"],
                    "create_date": self.creada,
                    "picking_type_id": [30, "Flex - MX: Entregas"],
                }
            ]
        if (model, method) == ("stock.picking.type", "read"):
            return [{"id": 30, "warehouse_id": [5, "Flex - MX"]}]
        if (model, method) == ("sale.order", "read"):
            return [
                {
                    "id": 9,
                    "name": "S00009",
                    "partner_id": [7, "Amazon Flex MX"],
                    "client_order_ref": "701-1234567-1234567",
                    "note": "",
                }
            ]
        if (model, method) == ("stock.picking", "read"):
            return [{"id": 1, "state": self.state, "move_ids": [11]}]
        if (model, method) == ("stock.move", "read"):
            return [dict(self.move)]
        if (model, method) == ("product.product", "read"):
            assert kwargs == {"context": {"location": 50}}
            return [
                {"id": 40, "type": "product", "free_qty": self.libre, "display_name": "[SET-X] Set"}
            ]
        self.escrituras.append((model, method))
        if (model, method) == ("stock.move", "write"):
            self.move.update(args[1])
        if (model, method) == ("stock.picking", "button_validate"):
            self.state = "done"
        return True


def _correr_flex(cierre, od):
    lineas = []
    r = cierre.correr(
        od,
        lambda entregas: {
            e.picking_id: cierre.Veredicto(cierre.Accion.VALIDAR, "Shipped") for e in entregas
        },
        True,
        AHORA,
        log=lineas.append,
        almacen_flex="Flex - MX",
    )
    return r, lineas


def test_una_salida_flex_sin_stock_se_deja_quieta(cierre):
    od = OdooFlex(libre=0.0)
    r, _ = _correr_flex(cierre, od)
    assert od.escrituras == []
    assert (r.validadas, r.dejadas, r.sin_stock, r.errores) == (0, 1, 1, 0)
    assert r.fallo() is False


def test_una_salida_flex_con_stock_a_medias_tampoco_se_valida(cierre):
    od = OdooFlex(libre=0.0, reservado=1.0)
    r, _ = _correr_flex(cierre, od)
    assert od.escrituras == []
    assert r.sin_stock == 1


def test_una_salida_flex_sin_stock_de_mas_de_5_dias_sale_1(cierre):
    od = OdooFlex(libre=0.0, creada="2026-11-08 10:00:00")
    r, lineas = _correr_flex(cierre, od)
    assert od.escrituras == []
    assert [e.picking for e in r.vencidas] == ["FLEX/OUT/00001"]
    assert r.fallo() is True
    assert (
        "DEJAR     FLEX/OUT/00001 S00009 Amazon Flex MX orden=701-1234567-1234567 "
        "estado=Shipped sin stock: [SET-X] Set pide 2, libre 0"
    ) in lineas


def test_una_salida_flex_cubierta_se_valida(cierre):
    od = OdooFlex(libre=1.0, reservado=1.0)
    r, _ = _correr_flex(cierre, od)
    assert od.escrituras == [
        ("stock.move", "write"),
        ("stock.picking", "button_validate"),
    ]
    assert (r.validadas, r.sin_stock) == (1, 0)
