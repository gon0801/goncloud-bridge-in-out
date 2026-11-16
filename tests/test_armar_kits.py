"""Armar kits no puede consumir componentes si alguno no alcanza.

Por que existe
--------------
Segundo paso del modelo hibrido de PENDIENTES #3. Un producto con receta
phantom no acumula stock propio, asi que para que Meli-Full o FBA tengan kits
armados hay que moverlos por `Virtual Locations/Production`: los componentes
salen de EHV, el kit entra al almacen de canal.

El riesgo es el armado a medias. Si se consumen los componentes de a uno y el
ultimo no alcanza, quedan materiales destruidos sin kit que los justifique — y
en un catalogo donde 65 componentes sostienen 1009 kits, eso desarma decenas de
publicaciones de golpe.

Por eso la verificacion es previa y total: se calculan TODAS las necesidades,
se comparan contra lo disponible, y solo si no falta nada se mueve algo.

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


LOCS = {"EHV/Stock": 8, "Virtual Locations/Production": 15, "FLEX/Stock": 60, "Full/Stock": 61}
KIT = 900


class FakeOdoo:
    """Moves con estados y quants por ubicacion. `cortar_en` corta la
    conexion en la llamada numero N a ese metodo de stock.move."""

    def __init__(self, stock, cortar_en=None, recetas=1):
        self.quants = dict(stock)
        self.recetas = recetas
        self.moves = {}
        self.cortar_en = cortar_en or {}
        self.llamadas = {}

    def loc(self, nombre):
        return LOCS[nombre]

    def kw(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        if model == "stock.move" and method in self.cortar_en:
            self.llamadas[method] = self.llamadas.get(method, 0) + 1
            if self.llamadas[method] == self.cortar_en[method]:
                raise ConnectionError("corte simulado")
        if (model, method) == ("product.product", "search_read"):
            return [{"id": KIT, "default_code": "SET-X", "qty_available": 0}]
        if (model, method) == ("product.product", "read"):
            return [{"id": KIT, "qty_available": 0}]
        if (model, method) == ("mrp.bom", "search_read"):
            return [{"id": i, "bom_line_ids": [1, 2, 3], "product_qty": 1} for i in range(self.recetas)]
        if (model, method) == ("mrp.bom.line", "read"):
            return lineas()
        if (model, method) == ("stock.quant", "search_read"):
            ids, loc = args[0][0][2], args[0][1][2]
            return [
                {"product_id": [pid, ""], "quantity": self.quants.get((loc, pid), 0), "reserved_quantity": 0}
                for pid in ids
            ]
        if (model, method) == ("stock.move", "search_read"):
            origin = args[0][0][2]
            return [
                {
                    **m,
                    "product_id": [m["product_id"], ""],
                    "location_id": [m["location_id"], ""],
                    "location_dest_id": [m["location_dest_id"], ""],
                }
                for m in self.moves.values()
                if m["origin"] == origin
            ]
        if (model, method) == ("stock.move", "create"):
            mid = 1000 + len(self.moves)
            self.moves[mid] = {"id": mid, "state": "draft", **args[0]}
            return mid
        m = self.moves[args[0][0]]
        if method == "read":
            return [{"state": m["state"]}]
        if method == "_action_confirm":
            m["state"] = "confirmed"
        elif method == "_action_assign":
            m["state"] = "assigned"
        elif method == "write":
            m.update(args[1])
        elif method == "_action_done":
            assert m["state"] == "assigned" and m["quantity"] == m["product_uom_qty"]
            m["state"] = "done"
            origen = (m["location_id"], m["product_id"])
            destino = (m["location_dest_id"], m["product_id"])
            self.quants[origen] = self.quants.get(origen, 0) - m["quantity"]
            self.quants[destino] = self.quants.get(destino, 0) + m["quantity"]
        else:
            raise AssertionError(f"llamada inesperada {model}.{method}")
        return True


EHV, PROD, FLEX = LOCS["EHV/Stock"], LOCS["Virtual Locations/Production"], LOCS["FLEX/Stock"]
STOCK_EHV = {(EHV, 101): 10, (EHV, 102): 10, (EHV, 103): 10}


def _correr(mod, monkeypatch, od, *args):
    monkeypatch.setattr(mod, "Odoo", lambda: od)
    return mod.main(["--sku", "SET-X", "--cantidad", "2", "--destino", "FLEX", *args])


def _stock(od):
    return {k: v for k, v in od.quants.items() if k[0] != PROD}


def test_flex_consume_de_ehv_y_da_de_alta_en_flex_stock(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert _stock(od) == {(EHV, 101): 8, (EHV, 102): 8, (EHV, 103): 6, (FLEX, KIT): 2}
    alta = [m for m in od.moves.values() if m["product_id"] == KIT]
    assert [(m["location_id"], m["location_dest_id"], m["origin"]) for m in alta] == [
        (PROD, FLEX, "Armado F1")
    ]


def test_un_corte_a_mitad_se_reanuda_sin_duplicar(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV, cortar_en={"_action_done": 3})
    with pytest.raises(ConnectionError):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert _stock(od) == {(EHV, 101): 8, (EHV, 102): 8, (EHV, 103): 10}
    od.cortar_en = {}
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert _stock(od) == {(EHV, 101): 8, (EHV, 102): 8, (EHV, 103): 6, (FLEX, KIT): 2}
    assert len(od.moves) == 4
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 0
    assert len(od.moves) == 4


def test_desarmar_regresa_el_kit_de_flex_a_componentes_en_ehv(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert _correr(mod, monkeypatch, od, "--desarmar", "--aplicar", "--folio", "F1") == 0
    assert _stock(od) == {**STOCK_EHV, (FLEX, KIT): 0}
    primero = min(m["id"] for m in od.moves.values() if m["origin"] == "Desarmado F1")
    assert od.moves[primero]["product_id"] == KIT


def test_desarmar_sin_kits_en_flex_no_mueve_nada(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    assert _correr(mod, monkeypatch, od, "--desarmar", "--aplicar", "--folio", "D1") == 1
    assert od.moves == {}


def test_si_un_componente_no_alcanza_no_se_mueve_nada(mod, monkeypatch):
    od = FakeOdoo({**STOCK_EHV, (EHV, 103): 3})
    assert _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1") == 1
    assert od.moves == {}


def test_la_simulacion_no_escribe(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    assert _correr(mod, monkeypatch, od) == 0
    assert od.moves == {}


def test_aplicar_sin_folio_aborta(mod, monkeypatch):
    with pytest.raises(SystemExit, match="requiere --folio"):
        _correr(mod, monkeypatch, FakeOdoo(STOCK_EHV), "--aplicar")


def test_un_folio_reusado_con_otra_cantidad_aborta(mod, monkeypatch):
    od = FakeOdoo(STOCK_EHV)
    _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    monkeypatch.setattr(mod, "Odoo", lambda: od)
    with pytest.raises(SystemExit, match="usa otro folio"):
        mod.main(["--sku", "SET-X", "--cantidad", "3", "--destino", "FLEX", "--aplicar", "--folio", "F1"])
    assert len(od.moves) == 4


def test_con_dos_recetas_activas_no_arma(mod, monkeypatch):
    """1541 recetas archivadas contra 1009 vivas: agarrar la equivocada arma mal."""
    od = FakeOdoo(STOCK_EHV, recetas=2)
    with pytest.raises(SystemExit, match="2 recetas activas"):
        _correr(mod, monkeypatch, od, "--aplicar", "--folio", "F1")
    assert od.moves == {}
