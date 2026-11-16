"""Apartar en un almacen de canal los componentes de N kits, sin mover nada a medias.

Por que existe
--------------
Para vender un kit desde FBA - MX (Flex) ese almacen necesita las piezas. Odoo
17 explota la receta phantom en cualquier picking, asi que lo que se aparta son
los componentes, en un solo picking EHV/Stock -> <destino>/Stock.

El riesgo es mover a medias: por eso la verificacion es previa y total, y el
folio permite reanudar un corte sin duplicar.

Sobre el redondeo
-----------------
`plan_de_componentes` multiplica sin redondear. Si una receta pidiera 0.5 de un
componente, armar 3 kits pide 1.5 — y ese faltante tiene que verse como tal.
Redondear hacia abajo esconderia un consumo real.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "odoo_armar_kits.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("armar_kits", TOOL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["armar_kits"] = m
    spec.loader.exec_module(m)
    return m


def lineas():
    """Receta real: SET-ARR-COF-22-VCO-DOR = arras + cofre + charola."""
    return [
        {
            "product_id": [101, "[ARR-22-DOR-VCO] Arras Virgen Completa"],
            "product_qty": 1.0,
        },
        {"product_id": [102, "[COF-22-DOR] Cofre 22 Dorado"], "product_qty": 1.0},
        {"product_id": [103, "[CHA-OVA-VIR-DOR] Charola Ovalada"], "product_qty": 2.0},
    ]


def test_la_receta_se_multiplica_por_la_cantidad(mod):
    plan = mod.plan_de_componentes(lineas(), 20)
    por_id = {p["product_id"]: p["necesario"] for p in plan}
    assert por_id[101] == 20
    assert por_id[102] == 20
    assert por_id[103] == 40, "la charola va x2 por kit: 20 kits piden 40"


def test_si_todo_alcanza_no_hay_faltantes(mod):
    plan = mod.plan_de_componentes(lineas(), 20)
    assert mod.faltantes(plan, {101: 100, 102: 50, 103: 103}) == []


def test_un_solo_componente_corto_frena_el_armado(mod):
    """El corazon: no se consume nada si falta uno.

    CHA-OVA-VIR-DOR estaba en 3 el 2026-09-12. Pedir 20 kits necesita 40.
    """
    plan = mod.plan_de_componentes(lineas(), 20)
    falta = mod.faltantes(plan, {101: 100, 102: 50, 103: 3})
    assert len(falta) == 1
    assert falta[0]["product_id"] == 103
    assert falta[0]["falta"] == 37


def test_un_componente_ausente_cuenta_como_cero(mod):
    """Sin quant, el componente no existe en esa ubicacion: no es 'infinito'."""
    plan = mod.plan_de_componentes(lineas(), 5)
    falta = mod.faltantes(plan, {101: 100, 102: 50})
    assert [f["product_id"] for f in falta] == [103]
    assert falta[0]["hay"] == 0


def test_las_fracciones_no_se_redondean(mod):
    """Media pieza faltante tiene que verse, no esconderse en un redondeo."""
    receta = [{"product_id": [201, "Medio componente"], "product_qty": 0.5}]
    plan = mod.plan_de_componentes(receta, 3)
    assert plan[0]["necesario"] == 1.5
    assert mod.faltantes(plan, {201: 1}) != [], "1 < 1.5 debe reportarse como faltante"


EHV, FBAMX, FULL = 8, 49, 41
ALMACENES = [
    {"id": 1, "name": "EHV-MX", "code": "EHV", "lot_stock_id": [EHV, "EHV/Stock"], "int_type_id": [5, "EHV-MX: Internal Transfers"]},
    {"id": 2, "name": "Meli - Full", "code": "Full", "lot_stock_id": [FULL, "Full/Stock"], "int_type_id": [13, "Meli - Full: Internal Transfers"]},
    {"id": 3, "name": "FBA - MX", "code": "FBAMX", "lot_stock_id": [FBAMX, "FBAMX/Stock"], "int_type_id": [21, "FBA - MX: Internal Transfers"]},
]
INTERNO_EHV, INTERNO_FBAMX, INTERNO_FULL = 5, 21, 13
PROD = 15
KIT = 900
COMPONENTES = (101, 102, 103)
SETTINGS_FBA = {"warehouse_amazon_fba_mx": "FBA - MX"}


def _cumple(reg, dominio):
    for campo, op, valor in dominio:
        actual = reg.get(campo)
        actual = actual[0] if isinstance(actual, (list, tuple)) else actual
        if op == "=" and actual != valor:
            return False
        if op == "in" and actual not in valor:
            return False
    return True


class FakeOdoo:
    """Odoo 17. Como el real, rechaza por RPC cualquier metodo que empiece con
    guion bajo, y al confirmar un picking explota en componentes cualquier
    producto con receta phantom (`kits`), sin mirar el tipo de operacion.
    `cortar_en` corta la conexion en la llamada numero N a ese metodo de
    stock.picking."""

    def __init__(
        self, stock, cortar_en=None, recetas=1, tipo_receta="phantom", kits=(), asistente=None,
        accion_al_validar=None, tope_quantity=None,
    ):
        self.quants = dict(stock)
        self.recetas = recetas
        self.tipo_receta = tipo_receta
        self.kits = {KIT, *kits}
        self.asistente = asistente
        self.accion_al_validar = accion_al_validar
        self.tope_quantity = tope_quantity
        self.pickings = {}
        self.moves = {}
        self.cortar_en = cortar_en or {}
        self.llamadas = {}

    def _move(self, vals, picking_id=False):
        mid = 9000 + len(self.moves)
        self.moves[mid] = {"id": mid, "state": "draft", "quantity": 0, "picked": False, "picking_id": picking_id, **vals}
        return mid

    def _de_picking(self, pid):
        return [m for m in self.moves.values() if m["picking_id"] == pid and m["state"] != "cancel"]

    def _vista(self, reg):
        return {
            k: [v, f"nombre {v}"] if k in ("product_id", "location_id", "location_dest_id") or (k == "picking_id" and v) else v
            for k, v in reg.items()
        }

    def kw(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        if method.startswith("_"):
            raise SystemExit(
                f"ERROR Odoo: AccessError: Private methods (such as '{model}.{method}') cannot be called remotely."
            )
        if model == "stock.picking":
            self.llamadas[method] = self.llamadas.get(method, 0) + 1
            if self.llamadas[method] == self.cortar_en.get(method):
                raise ConnectionError("corte simulado")
        if (model, method) == ("stock.warehouse", "search_read"):
            return [a for a in ALMACENES if _cumple(a, args[0])]
        if (model, method) == ("product.product", "search_read"):
            return [{"id": KIT, "default_code": "SET-X", "product_tmpl_id": [77, "SET-X"]}]
        if (model, method) == ("mrp.bom", "search_read"):
            return [{"id": i, "bom_line_ids": [1, 2, 3], "type": self.tipo_receta} for i in range(self.recetas)]
        if (model, method) == ("mrp.bom.line", "read"):
            return lineas()
        if (model, method) == ("stock.quant", "search_read"):
            ids, loc = args[0][0][2], args[0][1][2]
            return [
                {"product_id": [pid, ""], "quantity": self.quants.get((loc, pid), 0), "reserved_quantity": 0}
                for pid in ids
            ]
        if model == "stock.move":
            if method == "search_read":
                return [self._vista(m) for m in self.moves.values() if _cumple(m, args[0])]
            if method == "write":
                for i in args[0]:
                    vals = dict(args[1])
                    if self.tope_quantity is not None and "quantity" in vals:
                        vals["quantity"] = min(vals["quantity"], self.tope_quantity)
                    self.moves[i].update(vals)
                return True
        if model == "stock.picking":
            if method == "create":
                vals = dict(args[0])
                pid = 500 + len(self.pickings)
                self.pickings[pid] = {"id": pid, "state": "draft", **{k: v for k, v in vals.items() if k != "move_ids"}}
                for _, _, mv in vals["move_ids"]:
                    self._move(mv, pid)
                return pid
            if method == "search_read":
                return [self._vista(p) for p in self.pickings.values() if _cumple(p, args[0])]
            pid = args[0][0]
            p = self.pickings[pid]
            if method == "read":
                return [{"state": p["state"]}]
            if method == "action_confirm":
                for m in self._de_picking(pid):
                    if m["product_id"] in self.kits:
                        m["state"] = "cancel"
                        for c in COMPONENTES:
                            self._move({**m, "product_id": c, "state": "draft"}, pid)
                for m in self._de_picking(pid):
                    m["state"] = "confirmed"
                p["state"] = "confirmed"
                return True
            if method == "action_assign":
                for m in self._de_picking(pid):
                    if self.quants.get((m["location_id"], m["product_id"]), 0) >= m["product_uom_qty"]:
                        m["state"], m["quantity"] = "assigned", m["product_uom_qty"]
                return True
            if method == "button_validate":
                movs = self._de_picking(pid)
                if self.asistente or any(not m["picked"] or m["quantity"] < m["product_uom_qty"] for m in movs):
                    return {"type": "ir.actions.act_window", "res_model": self.asistente or "stock.backorder.confirmation"}
                for m in movs:
                    m["state"] = "done"
                    origen = (m["location_id"], m["product_id"])
                    destino = (m["location_dest_id"], m["product_id"])
                    self.quants[origen] = self.quants.get(origen, 0) - m["quantity"]
                    self.quants[destino] = self.quants.get(destino, 0) + m["quantity"]
                p["state"] = "done"
                return self.accion_al_validar or True
            if method == "action_cancel":
                for m in self._de_picking(pid):
                    m["state"] = "cancel"
                p["state"] = "cancel"
                return True
        raise AssertionError(f"llamada inesperada {model}.{method}")


STOCK_EHV = {(EHV, 101): 10, (EHV, 102): 10, (EHV, 103): 10}
APARTADO_2 = {(EHV, 101): 8, (EHV, 102): 8, (EHV, 103): 6, (FBAMX, 101): 2, (FBAMX, 102): 2, (FBAMX, 103): 4}


def _correr(mod, monkeypatch, od, *args, cantidad="2", destino="FLEX", settings=SETTINGS_FBA):
    monkeypatch.setattr(mod, "Odoo", lambda: od)
    monkeypatch.setattr(mod, "get_setting", lambda k: settings.get(k, ""))
    return mod.main(["--sku", "SET-X", "--cantidad", cantidad, "--destino", destino, *args])


def _resumen(od):
    """(tipo, origen, destino, estado, origin) de cada picking, en orden."""
    return [
        (p["picking_type_id"], p["location_id"], p["location_dest_id"], p["state"], p["origin"])
        for p in od.pickings.values()
    ]


def test_un_metodo_privado_rechazado_como_en_odoo_17(mod):
    """El falso reproduce la falla de produccion con el move 9173."""
    with pytest.raises(SystemExit, match="Private methods"):
        FakeOdoo(STOCK_EHV).kw("stock.move", "_action_confirm", [[9173]])


def test_flex_sin_setting_propio_aparta_en_fba_mx_con_un_picking_interno(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert od.quants == APARTADO_2
    assert _resumen(od) == [(INTERNO_EHV, EHV, FBAMX, "done", "Armado F1")]


def test_flex_usa_el_setting_warehouse_amazon_flex_mx_cuando_existe(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    settings = {**SETTINGS_FBA, "warehouse_amazon_flex_mx": "Meli - Full"}
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1", settings=settings) == 0
    assert _resumen(od) == [(INTERNO_EHV, EHV, FULL, "done", "Armado F1")]


def test_el_destino_acepta_el_nombre_del_almacen(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1", destino="FBA - MX", settings={}) == 0
    assert od.quants == APARTADO_2


def test_flex_sin_settings_aborta(mod, monkeypatch):
    with pytest.raises(SystemExit, match="warehouse_amazon_flex_mx"):
        _correr(mod, monkeypatch, FakeOdoo(STOCK_EHV), settings={})


def test_un_almacen_inexistente_aborta(mod, monkeypatch):
    with pytest.raises(SystemExit, match="llamado 'Flex - MX'"):
        _correr(mod, monkeypatch, FakeOdoo(STOCK_EHV), destino="Flex - MX")


def test_ehv_no_es_destino(mod, monkeypatch):
    with pytest.raises(SystemExit, match="almacen principal"):
        _correr(mod, monkeypatch, FakeOdoo(STOCK_EHV), destino="EHV-MX")


def test_un_corte_al_validar_se_reanuda_sin_duplicar(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, cortar_en={"button_validate": 1})
    with pytest.raises(ConnectionError):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert od.quants == STOCK_EHV
    od.cortar_en = {}
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert od.quants == APARTADO_2
    assert len(od.pickings) == 1 and len(od.moves) == 3
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert len(od.pickings) == 1 and len(od.moves) == 3
    assert od.quants == APARTADO_2


def test_un_picking_creado_y_sin_confirmar_se_reanuda(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, cortar_en={"action_confirm": 1})
    with pytest.raises(ConnectionError):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    od.cortar_en = {}
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert [p[3] for p in _resumen(od)] == ["done"]
    assert len(od.moves) == 3


def _suelto_9173(od, estado="draft"):
    """Lo que dejo en produccion la version que llamaba `_action_confirm`."""
    return od._move(
        {"origin": "Armado F1", "product_id": 101, "product_uom_qty": 2.0, "location_id": EHV, "location_dest_id": PROD, "state": estado}
    )


def test_el_move_suelto_en_draft_del_folio_se_cancela_al_aplicar(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    suelto = _suelto_9173(od)
    assert _correr(mod, monkeypatch, od, "--folio", "F1") == 0
    assert od.moves[suelto]["state"] == "draft", "la simulacion no lo toca"
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert od.moves[suelto]["state"] == "cancel"
    assert od.quants == APARTADO_2
    assert _resumen(od) == [(INTERNO_EHV, EHV, FBAMX, "done", "Armado F1")]


def test_un_move_suelto_del_folio_fuera_de_draft_aborta_sin_escribir(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    _suelto_9173(od, estado="confirmed")
    with pytest.raises(SystemExit, match="ya no estan en draft"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert od.pickings == {}


def test_desarmar_regresa_los_componentes_de_fba_mx_a_ehv(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert _correr(mod, monkeypatch, od, "--desarmar", "--aplicar", "--folio", "F1") == 0
    assert od.quants == {**STOCK_EHV, (FBAMX, 101): 0, (FBAMX, 102): 0, (FBAMX, 103): 0}
    assert _resumen(od)[1:] == [(INTERNO_FBAMX, FBAMX, EHV, "done", "Desarmado F1")]


def test_desarmar_sin_componentes_en_el_destino_no_crea_pickings(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    assert _correr(mod, monkeypatch, od, "--desarmar", "--aplicar", "--folio", "D1") == 1
    assert od.pickings == {}


def test_si_un_componente_no_alcanza_no_se_crea_nada(mod, monkeypatch):
    od = FakeOdoo({**STOCK_EHV, (EHV, 103): 3})
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 1
    assert od.pickings == {}


def test_la_simulacion_no_escribe(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    assert _correr(mod, monkeypatch, od, "--folio", "F1") == 0
    assert od.pickings == {} and od.moves == {}


def test_aplicar_sin_folio_aborta(mod, monkeypatch):
    with pytest.raises(SystemExit, match="requiere --folio"):
        _correr(mod, monkeypatch, FakeOdoo(STOCK_EHV), "--aplicar")


def test_un_folio_reusado_con_otra_cantidad_aborta(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    with pytest.raises(SystemExit, match="usa otro folio"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1", cantidad="3")
    assert len(od.pickings) == 1


def test_con_dos_recetas_activas_no_mueve(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, recetas=2)
    with pytest.raises(SystemExit, match="2 recetas activas"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert od.pickings == {}


def test_una_receta_que_no_es_phantom_aborta(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, tipo_receta="normal")
    with pytest.raises(SystemExit, match="no phantom"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert od.pickings == {}


def test_un_componente_que_es_kit_se_explota_y_el_picking_se_cancela_sin_mover_stock(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, kits=(103,))
    with pytest.raises(SystemExit, match="phantom se exploto"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert _resumen(od)[0][3] == "cancel"
    assert od.quants == STOCK_EHV


def test_el_asistente_de_backorder_cancela_el_picking_sin_mover_stock(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, asistente="stock.backorder.confirmation")
    with pytest.raises(SystemExit, match="stock.backorder.confirmation"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert _resumen(od)[0][3] == "cancel"
    assert od.quants == STOCK_EHV


def test_una_cantidad_que_no_iguala_lo_pedido_no_se_valida(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, tope_quantity=1)
    with pytest.raises(SystemExit, match="no igualan lo pedido"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert "button_validate" not in od.llamadas
    assert _resumen(od)[0][3] == "cancel"
    assert od.quants == STOCK_EHV


def test_una_accion_de_impresion_con_el_picking_en_done_es_exito(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, accion_al_validar={"type": "ir.actions.report", "report_name": "stock.report_picking"})
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert od.quants == APARTADO_2
    assert _resumen(od) == [(INTERNO_EHV, EHV, FBAMX, "done", "Armado F1")]
