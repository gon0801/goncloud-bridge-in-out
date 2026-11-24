"""La nota del SO de MeLi FBM lleva el numero de envio.

ML imprime el numero de envio en la etiqueta; el ticket del picking
(x_ticket.report_picking_80mm) imprime `sale.order.note`, asi que el numero
tiene que estar en la nota para que el empacador lo cruce con la etiqueta.

Lo que se verifica: el formato `ENVIO=<id> | ORDER=<id>` cuando la orden trae
envio, la nota de siempre cuando no, y que el lector de la nota
(`orden_de_venta`) siga sacando el id de ORDER= con ENVIO= delante.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def worker():
    return _cargar("worker_nota_envio", RAIZ / "app" / "inbound_worker.py")


@pytest.fixture(scope="module")
def cerrar():
    return _cargar("cerrar_nota_envio", RAIZ / "tools" / "odoo_cerrar_entregas.py")


def test_la_nota_trae_el_envio_antes_de_order(worker):
    nota = worker.fbm_so_note(
        {"shipping": {"id": 46789012345}}, "2000011682284817", "Juan Perez"
    )
    assert nota == (
        "MercadoLibre FBM | ENVIO=46789012345 | ORDER=2000011682284817 | Juan Perez"
    )


@pytest.mark.parametrize("order", [{}, {"shipping": None}, {"shipping": {}}])
def test_sin_envio_la_nota_queda_como_antes(worker, order):
    nota = worker.fbm_so_note(order, "2000011682284817", "Juan Perez")
    assert nota == "MercadoLibre FBM | ORDER=2000011682284817 | Juan Perez"


def test_el_nombre_vacio_cae_a_na(worker):
    nota = worker.fbm_so_note({"shipping": {"id": 1}}, "20", "")
    assert nota == "MercadoLibre FBM | ENVIO=1 | ORDER=20 | N/A"


def test_el_lector_saca_el_order_de_la_nota_nueva(worker, cerrar):
    nota = worker.fbm_so_note(
        {"shipping": {"id": 46789012345}}, "2000011682284817", "Juan Perez"
    )
    assert (
        cerrar.orden_de_venta("MercadoLibre FBM", "", f"<p>{nota}</p>")
        == "2000011682284817"
    )


def test_process_fbm_manda_la_nota_con_el_envio_al_tool(worker, monkeypatch):
    """La conexion: `process_fbm` tiene que usar la nota nueva. Sin esto, revertir
    la linea que arma `so_note` dejaba pasar todas las pruebas."""
    llamadas = []
    monkeypatch.setattr(worker, "is_enabled", lambda key: True)
    monkeypatch.setattr(worker, "parse_items", lambda order: [{"sku": "X", "qty": 1}])
    monkeypatch.setattr(
        worker,
        "run_tool",
        lambda nombre, env, **kw: llamadas.append((nombre, env)) or (0, "", ""),
    )
    monkeypatch.setattr(worker, "mark_completed", lambda *a, **k: None)
    orden = {
        "shipping": {"id": 46789012345},
        "buyer": {"nickname": "COMPRADOR1", "last_name": "Perez"},
    }

    worker.process_fbm(
        orden, "2000011682284817", "MLM", "paid", "dk", "2000011682284817", "lk"
    )

    assert [n for n, _ in llamadas] == ["inbound_fbm_so_apply_paid_one_shot"]
    assert llamadas[0][1]["SO_NOTE"] == (
        "MercadoLibre FBM | ENVIO=46789012345 | ORDER=2000011682284817 | COMPRADOR1 Perez"
    )
