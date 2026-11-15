"""Cierre de entregas de venta segun el estado real en la plataforma.

Por que existe
--------------
El bridge confirma la venta y deja la entrega de EHV-MX sin validar. Un proceso
externo las validaba a ciegas a los ~3 dias; se perdio al migrar de maquina el
2026-11-09 y en seis dias se juntaron 136 entregas abiertas. Este job escribe
en Odoo de produccion, asi que cada camino de escritura lleva su prueba.
"""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "odoo_cerrar_entregas.py"
AHORA = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("cerrar_entregas", TOOL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cerrar_entregas"] = m
    spec.loader.exec_module(m)
    return m


class FakeOdoo:
    """Lo minimo de Odoo 17 que toca el job: pickings, moves y ventas."""

    def __init__(self, button_validate_devuelve=True):
        self.pickings = {}
        self.moves = {}
        self.sos = {}
        self.escrituras = []
        self.button_validate_devuelve = button_validate_devuelve

    def entrega(self, pid, so_id, canal, ref, nota, creada, moves, state="assigned"):
        self.sos[so_id] = {
            "id": so_id,
            "name": f"S{so_id:05d}",
            "partner_id": [7, canal],
            "client_order_ref": ref,
            "note": nota,
        }
        self.pickings[pid] = {
            "id": pid,
            "name": f"EHV/OUT/{pid:05d}",
            "sale_id": [so_id, f"S{so_id:05d}"],
            "create_date": creada,
            "state": state,
            "move_ids": [m["id"] for m in moves],
        }
        for m in moves:
            self.moves[m["id"]] = {"has_tracking": "none", "state": "assigned", **m}

    def kw(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        if (model, method) == ("stock.picking", "search_read"):
            abiertos = ("waiting", "confirmed", "assigned")
            return [p for p in self.pickings.values() if p["state"] in abiertos]
        if (model, method) == ("sale.order", "read"):
            return [self.sos[i] for i in args[0]]
        if (model, method) == ("stock.picking", "read"):
            return [self.pickings[i] for i in args[0]]
        if (model, method) == ("stock.move", "read"):
            return [self.moves[i] for i in args[0]]
        if (model, method) == ("stock.move", "write"):
            self.escrituras.append(("move.write", args[0][0], args[1]))
            self.moves[args[0][0]].update(args[1])
            return True
        if (model, method) == ("stock.picking", "button_validate"):
            pid = args[0][0]
            self.escrituras.append(("validate", pid, kwargs.get("context")))
            res = self.button_validate_devuelve
            if res is True:
                for mid in self.pickings[pid]["move_ids"]:
                    m = self.moves[mid]
                    if (
                        m["state"] != "cancel"
                        and m.get("quantity") == m["product_uom_qty"]
                    ):
                        m["state"] = "done"
                if all(
                    self.moves[mid]["state"] in ("done", "cancel")
                    for mid in self.pickings[pid]["move_ids"]
                ):
                    self.pickings[pid]["state"] = "done"
            return res
        if (model, method) == ("stock.picking", "action_cancel"):
            self.escrituras.append(("cancel", args[0][0], None))
            self.pickings[args[0][0]]["state"] = "cancel"
            return True
        raise AssertionError(f"llamada inesperada {model}.{method}")


def _odoo_con_cuatro():
    od = FakeOdoo()
    od.entrega(
        1,
        101,
        "Amazon Easy MX",
        "701-1111111-1111111",
        "",
        "2026-11-14 10:00:00",
        [
            {"id": 11, "product_uom_qty": 2.0, "quantity": 2.0},
            {"id": 12, "product_uom_qty": 1.0, "quantity": 0.0, "state": "confirmed"},
        ],
    )
    od.entrega(
        2,
        102,
        "Amazon FBM US",
        "113-2222222-2222222",
        "",
        "2026-11-13 10:00:00",
        [{"id": 21, "product_uom_qty": 1.0, "quantity": 1.0}],
    )
    od.entrega(
        3,
        103,
        "MercadoLibre FBM",
        "2000015948650289 | COMPRADOR",
        "<p>MercadoLibre FBM | ORDER=2000015948650289 | COMPRADOR</p>",
        "2026-11-01 10:00:00",
        [{"id": 31, "product_uom_qty": 1.0, "quantity": 1.0}],
    )
    od.entrega(
        4,
        104,
        "Cliente mostrador",
        "PO-77",
        "",
        "2026-11-01 10:00:00",
        [{"id": 41, "product_uom_qty": 1.0, "quantity": 1.0}],
    )
    return od


def _consulta(mod, por_orden):
    def consultar(entregas):
        return {e.picking_id: por_orden[e.orden] for e in entregas}

    return consultar


def test_valida_cancela_y_deja_segun_la_plataforma(mod):
    od = _odoo_con_cuatro()
    lineas = []
    consultar = _consulta(
        mod,
        {
            "701-1111111-1111111": mod.decidir_amazon("Shipped"),
            "113-2222222-2222222": mod.decidir_amazon("Canceled"),
            "2000015948650289": mod.decidir_meli(
                {"status": "paid"}, {"status": "ready_to_ship", "substatus": "printed"}
            ),
        },
    )
    r = mod.correr(od, consultar, aplicar=True, ahora=AHORA, log=lineas.append)

    assert od.pickings[1]["state"] == "done"
    assert od.pickings[2]["state"] == "cancel"
    assert od.pickings[3]["state"] == "assigned"
    assert od.pickings[4]["state"] == "assigned"
    assert od.escrituras == [
        ("move.write", 11, {"quantity": 2.0, "picked": True}),
        ("move.write", 12, {"quantity": 1.0, "picked": True}),
        ("validate", 1, {"skip_backorder": True, "skip_sms": True}),
        ("cancel", 2, None),
    ]
    assert (r.validadas, r.canceladas, r.dejadas, r.errores) == (1, 1, 1, 0)
    assert [e.so for e in r.vencidas] == ["S00103"]
    assert r.fallo() is True
    assert lineas[-2].startswith(
        "RESUMEN   modo=aplicado revisadas=3 validar=1 cancelar=1 dejar=1 errores=0 vencidas=1 ignoradas=1"
    )
    assert (
        lineas[-1]
        == "VENCIDA   EHV/OUT/00003 S00103 MercadoLibre FBM orden=2000015948650289 14 dias"
    )


def test_en_prueba_no_escribe_nada(mod):
    od = _odoo_con_cuatro()
    lineas = []
    consultar = _consulta(
        mod,
        {
            "701-1111111-1111111": mod.decidir_amazon("Shipped"),
            "113-2222222-2222222": mod.decidir_amazon("Canceled"),
            "2000015948650289": mod.decidir_meli(
                {"status": "paid"}, {"status": "delivered"}
            ),
        },
    )
    r = mod.correr(od, consultar, aplicar=False, ahora=AHORA, log=lineas.append)

    assert od.escrituras == []
    assert {p["state"] for p in od.pickings.values()} == {"assigned"}
    assert (r.validadas, r.canceladas, r.vencidas) == (2, 1, [])
    assert "-> validaria" in lineas[0] and "-> cancelaria" in lineas[1]


def test_una_entrega_ya_cerrada_por_otro_no_se_toca(mod):
    od = _odoo_con_cuatro()
    consultar_original = _consulta(
        mod,
        {
            "701-1111111-1111111": mod.decidir_amazon("Shipped"),
            "113-2222222-2222222": mod.decidir_amazon("Shipped"),
            "2000015948650289": mod.decidir_meli(
                {"status": "paid"}, {"status": "shipped"}
            ),
        },
    )

    def consultar(entregas):
        od.pickings[1]["state"] = "done"
        return consultar_original(entregas)

    lineas = []
    r = mod.correr(od, consultar, aplicar=True, ahora=AHORA, log=lineas.append)
    assert [x for x in od.escrituras if x[1] == 1] == []
    assert "-> ya estaba done" in lineas[0]
    assert r.errores == 0


def test_si_odoo_pide_un_asistente_es_error_y_no_queda_hecha(mod):
    od = _odoo_con_cuatro()
    od.button_validate_devuelve = {
        "type": "ir.actions.act_window",
        "res_model": "stock.backorder.confirmation",
    }
    lineas = []
    consultar = _consulta(
        mod,
        {
            "701-1111111-1111111": mod.decidir_amazon("Shipped"),
            "113-2222222-2222222": mod.decidir_amazon("Pending"),
            "2000015948650289": mod.decidir_meli(
                {"status": "paid"}, {"status": "handling"}
            ),
        },
    )
    r = mod.correr(od, consultar, aplicar=True, ahora=AHORA, log=lineas.append)
    assert od.pickings[1]["state"] == "assigned"
    assert r.errores == 1
    assert "Odoo pidio confirmacion (stock.backorder.confirmation)" in lineas[0]


def test_una_consulta_fallida_cuenta_como_error_y_no_escribe(mod):
    od = _odoo_con_cuatro()
    falla = mod.ErrorCierre("MeLi HTTP 401 en /orders/2000015948650289")
    consultar = _consulta(
        mod,
        {
            "701-1111111-1111111": mod.decidir_amazon("Unshipped"),
            "113-2222222-2222222": mod.decidir_amazon("Unshipped"),
            "2000015948650289": falla,
        },
    )
    r = mod.correr(od, consultar, aplicar=True, ahora=AHORA, log=lambda _: None)
    assert od.escrituras == []
    assert r.errores == 1
    assert [e.so for e in r.vencidas] == ["S00103"]


def test_el_limite_frena_las_escrituras(mod):
    od = _odoo_con_cuatro()
    consultar = _consulta(
        mod,
        {
            "701-1111111-1111111": mod.decidir_amazon("Shipped"),
            "113-2222222-2222222": mod.decidir_amazon("Shipped"),
            "2000015948650289": mod.decidir_meli(
                {"status": "paid"}, {"status": "shipped"}
            ),
        },
    )
    r = mod.correr(
        od, consultar, aplicar=True, ahora=AHORA, limite=1, log=lambda _: None
    )
    assert [p["state"] for p in od.pickings.values()] == [
        "done",
        "assigned",
        "assigned",
        "assigned",
    ]
    assert (r.validadas, r.dejadas) == (1, 2)


def test_con_lote_o_serie_no_valida(mod):
    od = FakeOdoo()
    od.entrega(
        1,
        101,
        "Amazon Flex MX",
        "701-1111111-1111111",
        "",
        "2026-11-14 10:00:00",
        [{"id": 11, "product_uom_qty": 1.0, "quantity": 1.0, "has_tracking": "serial"}],
    )
    lineas = []
    consultar = _consulta(mod, {"701-1111111-1111111": mod.decidir_amazon("Shipped")})
    r = mod.correr(od, consultar, aplicar=True, ahora=AHORA, log=lineas.append)
    assert od.escrituras == []
    assert r.errores == 1
    assert "requiere lote o serie" in lineas[0]


@pytest.mark.parametrize(
    "estado, accion",
    [
        ("Shipped", "VALIDAR"),
        ("InvoiceUnconfirmed", "VALIDAR"),
        ("Canceled", "CANCELAR"),
        ("Unshipped", "DEJAR"),
        ("PartiallyShipped", "DEJAR"),
        ("Pending", "DEJAR"),
        ("no encontrada", "DEJAR"),
        (None, "DEJAR"),
    ],
)
def test_amazon(mod, estado, accion):
    assert mod.decidir_amazon(estado).accion.value == accion


@pytest.mark.parametrize(
    "orden, envio, accion",
    [
        (
            {"status": "paid"},
            {"status": "shipped", "substatus": "out_for_delivery"},
            "VALIDAR",
        ),
        ({"status": "paid"}, {"status": "delivered"}, "VALIDAR"),
        (
            {"status": "paid"},
            {"status": "ready_to_ship", "substatus": "picked_up"},
            "VALIDAR",
        ),
        (
            {"status": "paid"},
            {"status": "ready_to_ship", "substatus": "ready_to_print"},
            "DEJAR",
        ),
        ({"status": "paid"}, {"status": "not_delivered"}, "DEJAR"),
        ({"status": "cancelled"}, {"status": "cancelled"}, "CANCELAR"),
        ({"status": "cancelled"}, None, "CANCELAR"),
        ({"status": "paid"}, None, "DEJAR"),
        # Pack: una orden cancelada pero el envio comun sigue vivo.
        (
            {"status": "cancelled"},
            {"status": "ready_to_ship", "substatus": "printed"},
            "DEJAR",
        ),
        ({"status": "cancelled"}, {"status": "shipped"}, "VALIDAR"),
        (None, None, "DEJAR"),
    ],
)
def test_meli(mod, orden, envio, accion):
    assert mod.decidir_meli(orden, envio).accion.value == accion


@pytest.mark.parametrize(
    "canal, ref, nota, orden",
    [
        # Caso real S02864: la referencia es la orden y la nota trae el pack.
        (
            "MercadoLibre FBM",
            "2000019433591650 | X",
            "<p>MercadoLibre FBM | ORDER=2000015948650289 | X</p>",
            "2000019433591650",
        ),
        (
            "MercadoLibre FBM",
            "",
            "<p>MercadoLibre FBM | ORDER=2000011682284817 | X</p>",
            "2000011682284817",
        ),
        ("Amazon Flex MX", "701-5245201-0475457", "", "701-5245201-0475457"),
        (
            "Amazon FBM US",
            "AMZFBM:ATVPDKIKX0DER:114-6039747-8671463",
            "",
            "114-6039747-8671463",
        ),
        ("Amazon Easy MX", "sin id", "", None),
    ],
)
def test_orden_de_venta(mod, canal, ref, nota, orden):
    assert mod.orden_de_venta(canal, ref, nota) == orden


def test_el_listado_de_amazon_sigue_todas_las_paginas(mod):
    """En la v2026 el token viene en `pagination.nextToken`. Leerlo en la raiz
    dejaba el listado en 100 ordenes y empujaba el resto a getOrder, que se
    quedo en 429 en la primera corrida de prueba contra produccion."""

    class Resp:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    class Poll:
        AMAZON_API_BASE = "https://sp"

        def __init__(self):
            self.pedidos = []

        def _sp_api_get(self, token, url, params):
            self.pedidos.append(params.get("paginationToken"))
            if "paginationToken" not in params:
                return Resp(
                    {"orders": [{"orderId": "a"}], "pagination": {"nextToken": "t2"}}
                )
            return Resp({"orders": [{"orderId": "b"}], "pagination": {}})

    poll = Poll()
    ordenes = list(mod.listar_ordenes_amazon(poll, "tok", "MX", "2026-11-01T00:00:00Z"))
    assert [o["orderId"] for o in ordenes] == ["a", "b"]
    assert poll.pedidos == [None, "t2"]
