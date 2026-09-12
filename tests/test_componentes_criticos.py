"""La clasificacion de componentes tiene que separar dos problemas distintos.

Por que existe
--------------
Caso real (2026-09-12): 8 componentes en falta dejaban **94 de 302 SKUs
vendibles en MeLi** reportando 0 stock. Detras de los 1009 kits del catalogo hay
apenas 65 componentes, asi que uno solo en cero apaga decenas de publicaciones.

Lo importante fue el diagnostico, no el sintoma. Los 8 no eran iguales:

    SKU               entradas  salidas  neto  ultima entrada
    EST-CAR-ROJ            158      225   -67  2026-03-01
    ARR-22-PLA-VBU           0        6    -6  NUNCA

`EST-CAR-ROJ` tiene historial de compras que se corto hace seis meses: alguien
dejo de capturar las entradas. `ARR-22-PLA-VBU` salio seis veces **sin haber
entrado nunca**: no es un error de conteo, es un alta que falta.

Los dos se ven igual en el reporte (stock negativo) y se arreglan distinto. Si
a los dos les aplicas un ajuste de inventario, el segundo vuelve a pasar.

Sobre el verde falso
--------------------
`test_un_componente_sano_no_aparece` existe para que la clasificacion no pueda
volverse una que marca todo. Un reporte que grita por cada componente es tan
inutil como uno que calla — y esta sesion ya documento a donde lleva eso: el
health estuvo un mes en rojo por algo cosmetico y tapo 19 dias de ordenes
perdidas.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "odoo_componentes_criticos.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("componentes_criticos", TOOL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["componentes_criticos"] = m
    spec.loader.exec_module(m)
    return m


def test_el_que_nunca_entro_se_distingue_del_que_dejo_de_entrar(mod):
    """El corazon del reporte: mismo sintoma, causas y arreglos distintos."""
    nunca = mod.clasificar_componente(stock=-6, entradas=0, salidas_recientes=6)
    dejo = mod.clasificar_componente(stock=-67, entradas=158, salidas_recientes=67)

    assert nunca == mod.NUNCA_CARGADO, (
        "un componente que salio sin haber entrado NUNCA es un alta faltante, "
        "no un error de conteo; tratarlo igual lo deja repetirse"
    )
    assert dejo == mod.NEGATIVO
    assert nunca != dejo


def test_el_faltante_chico_pero_nunca_cargado_pesa_mas_que_el_grande(mod):
    """-6 sin historial se atiende antes que -67 con historial."""
    orden = {mod.NUNCA_CARGADO: 0, mod.NEGATIVO: 1, mod.EN_RIESGO: 2, mod.SANO: 3}
    chico = mod.clasificar_componente(stock=-1, entradas=0, salidas_recientes=1)
    grande = mod.clasificar_componente(stock=-67, entradas=158, salidas_recientes=67)
    assert orden[chico] < orden[grande]


def test_stock_bajo_con_demanda_avisa_antes_de_caer(mod):
    """REP-GD: 0 en stock y 47 salidas en 90 dias. Cae en dias."""
    assert mod.clasificar_componente(stock=0, entradas=120, salidas_recientes=47) == (
        mod.EN_RIESGO
    )
    assert mod.clasificar_componente(stock=3, entradas=200, salidas_recientes=31) == (
        mod.EN_RIESGO
    )


def test_stock_bajo_SIN_demanda_no_es_urgente(mod):
    """Un componente descontinuado en 0 no bloquea ventas: no debe hacer ruido."""
    assert (
        mod.clasificar_componente(stock=0, entradas=50, salidas_recientes=0) == mod.SANO
    )


def test_un_componente_sano_no_aparece(mod):
    """Guardia anti-alarmismo: la clasificacion tiene que discriminar de verdad."""
    assert mod.clasificar_componente(
        stock=500, entradas=900, salidas_recientes=120
    ) == (mod.SANO)
    assert (
        mod.clasificar_componente(stock=10, entradas=40, salidas_recientes=5)
        == mod.SANO
    )


def test_la_herramienta_no_escribe_en_odoo(mod):
    """Es un diagnostico. Un ajuste de inventario lo decide una persona."""
    fuente = TOOL.read_text(encoding="utf-8")
    for peligroso in ('"create"', '"write"', '"unlink"', "action_apply_inventory"):
        assert peligroso not in fuente, (
            f"la herramienta menciona {peligroso}: debe ser de solo lectura"
        )
