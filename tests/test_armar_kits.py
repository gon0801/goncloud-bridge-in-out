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


def test_la_verificacion_ocurre_antes_de_mover():
    """Guardia sobre el fuente, con espacios colapsados para no atarse al formato."""
    plano = "".join(TOOL.read_text(encoding="utf-8").split())
    i_check = plano.index("falta=faltantes(plan,disponible)")
    i_mover = plano.index("movs.append(mover(")
    assert i_check < i_mover, (
        "se mueven componentes antes de verificar que todos alcancen: un armado "
        "a medias deja materiales consumidos sin kit"
    )
    assert "ifnotargs.aplicar:" in plano, "no hay modo simulacion"


def test_exige_una_sola_receta_activa():
    """1541 recetas archivadas contra 1009 vivas: agarrar la equivocada arma mal."""
    plano = "".join(TOOL.read_text(encoding="utf-8").split())
    assert '["active","=",True]' in plano, "la busqueda de receta no filtra archivadas"
    assert "iflen(boms)!=1:" in plano, (
        "no se exige exactamente una receta activa; con varias no se sabe cual usar"
    )
