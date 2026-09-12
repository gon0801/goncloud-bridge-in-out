"""El job que encola el backfill debe pasar el contrato real del worker.

Por que existe
--------------
`tools/meli_orders_backfill.py` inyecta trabajos en `ml_orders_jobs` a mano,
saltandose el webhook. Si la forma del job se desvia de lo que espera
`app/inbound_worker.py`, el worker manda TODO a `dead` con
`reason=bad_resource` y el rescate falla en silencio — justo el sintoma que ya
ensucia la DLQ (359 dead/30d, ver PENDIENTES.md #1).

Sobre el verde falso
--------------------
Un test que copiara el regex del worker seria una tautologia: si alguien cambia
el worker, las dos copias quedan mal y el test sigue verde. Por eso el regex se
EXTRAE del fuente de `app/inbound_worker.py` en tiempo de test. Si el contrato
del worker cambia, esto lo ve.

`test_el_regex_extraido_discrimina` cubre el otro lado: prueba que el regex
recuperado efectivamente RECHAZA formas invalidas. Sin eso, una extraccion que
devolviera algo permisivo (por ejemplo `.*`) daria verde con cualquier basura.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKER_SRC = REPO / "app" / "inbound_worker.py"
BACKFILL_PATH = REPO / "tools" / "meli_orders_backfill.py"


@pytest.fixture(scope="module")
def backfill():
    spec = importlib.util.spec_from_file_location("meli_orders_backfill", BACKFILL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meli_orders_backfill"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def regex_del_worker():
    """Saca el regex de resource del fuente del worker, no de una copia."""
    fuente = WORKER_SRC.read_text(encoding="utf-8")
    encontrados = re.findall(r're\.match\(\s*r"(\^/orders/[^"]+)"', fuente)

    assert encontrados, (
        f"no se encontro el regex de resource en {WORKER_SRC}. Si el worker "
        f"cambio de forma, actualiza la extraccion de este test — no lo "
        f"desactives: sin el, el backfill puede encolar basura sin que nadie vea."
    )
    assert len(set(encontrados)) == 1, (
        f"el worker usa mas de un regex de resource distinto: {set(encontrados)}"
    )
    return re.compile(encontrados[0])


def test_el_regex_extraido_discrimina(regex_del_worker):
    """Guardia anti-verde-falso: el contrato recuperado debe rechazar de verdad."""
    assert not regex_del_worker.match("orders/2000018393906916"), "falta la barra"
    assert not regex_del_worker.match("/orders/2000018393906916/"), "barra de mas"
    assert not regex_del_worker.match("/collections/173753815883"), "topic payments"
    assert not regex_del_worker.match("/questions/123"), "topic questions"
    assert regex_del_worker.match("/orders/2000018393906916"), "forma valida rechazada"


def test_el_job_del_backfill_pasa_el_contrato_del_worker(backfill, regex_del_worker):
    """La orden real que destapo el apagon de 19 dias."""
    job = backfill.build_job("2000018393906916", "2026-09-12T07:00:00+00:00")

    assert regex_del_worker.match(job["resource"]), (
        f"el worker mandaria este job a dead: resource={job['resource']!r}"
    )


def test_el_job_no_trae_order_json_ni_dedupe_key(backfill):
    """De esto depende que reprocesar un rango ya cargado NO duplique en Odoo.

    Con `order_json` el worker se saltea el GET autoritativo; con `dedupe_key`
    propio se saltea el action-aware `ml:{id}:{action}` que es el que consulta
    `is_already_completed()`. En ambos casos se pierde la idempotencia.
    """
    job = backfill.build_job("2000018393906916", "2026-09-12T07:00:00+00:00")

    assert "order_json" not in job, (
        "pasar order_json saltea el GET autoritativo de /orders/{id}"
    )
    assert "dedupe_key" not in job, (
        "pasar dedupe_key rompe la idempotencia action-aware del worker"
    )


def test_las_fechas_se_mandan_en_el_formato_que_espera_meli(backfill):
    desde = backfill.parse_day("2026-08-24")
    hasta = backfill.parse_day("2026-09-12", end_of_day=True)

    assert desde == "2026-08-24T00:00:00.000-00:00"
    assert hasta.startswith("2026-09-12T23:59:59"), hasta
    assert desde < hasta


def test_una_fecha_invalida_aborta_en_vez_de_barrer_todo(backfill):
    """Sin esto, un typo manda un rango vacio o gigante a /orders/search."""
    with pytest.raises(SystemExit):
        backfill.parse_day("24-08-2026")
