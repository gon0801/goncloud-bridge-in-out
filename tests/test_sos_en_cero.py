"""Una venta en $0 recién creada es normal; una de hace meses es dinero perdido.

Por que existe
--------------
Amazon devuelve `OrderTotal = 0` cuando una orden Flex MX esta en `Pending`, y
lo llena despues **sin cambiar de estado**. El SO nace en cero y se corrige
cuando llega el segundo evento.

Funciona en 103 de 107 casos. Las que fallan quedan invisibles: el poll filtra
por fecha, asi que a los pocos dias la orden sale de la ventana y nadie la
vuelve a mirar. El 2026-09-12 habia dos de junio — **tres meses** facturadas en
cero, con Amazon ya devolviendo 768.90 y 1188.00 MXN sobre ordenes todavia en
`Pending`. Se encontraron de casualidad.

El umbral sale de los datos
---------------------------
Demora entre el evento `Pending` y el siguiente, sobre las 103 que si se
corrigieron:

    p50 1.2d · p90 3.0d · p95 3.2d · p99 4.1d · max 16.6d

    umbral 2d -> 23 falsos positivos de 103
    umbral 3d -> 10
    umbral 4d ->  2
    umbral 7d ->  1

7 dias deja un solo falso positivo, y ese caso — una semana en cero — merece
mirarse igual. La tentacion de apretarlo a 2 o 3 dias es la misma que ya costo
19 dias de ordenes en este repo: una alarma que grita seguido entrena a todos
a ignorarla.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "odoo_sos_en_cero.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("sos_en_cero", TOOL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["sos_en_cero"] = m
    spec.loader.exec_module(m)
    return m


def test_las_de_junio_se_detectan(mod):
    """El caso real: S01609, tres meses en $0."""
    assert mod.es_sospechosa(monto=0, estado="sale", dias=89) is True


def test_una_recien_creada_no_hace_ruido(mod):
    """p50 de la espera = 1.2 dias. Avisar aqui seria gritar en falso."""
    assert mod.es_sospechosa(monto=0, estado="sale", dias=0.3) is False
    assert mod.es_sospechosa(monto=0, estado="sale", dias=1.2) is False


def test_el_p99_de_la_espera_normal_no_dispara(mod):
    """4.1 dias fue el p99 de las 103 que se corrigieron solas."""
    assert mod.es_sospechosa(monto=0, estado="sale", dias=4.1) is False


def test_a_los_7_dias_ya_no_es_normal(mod):
    assert mod.es_sospechosa(monto=0, estado="sale", dias=7) is True


def test_una_cancelada_no_es_dinero_perdido(mod):
    """Una orden cancelada en $0 es correcta, no un ingreso sin registrar."""
    assert mod.es_sospechosa(monto=0, estado="cancel", dias=200) is False


def test_una_con_precio_no_es_sospechosa(mod):
    assert mod.es_sospechosa(monto=768.90, estado="sale", dias=200) is False


def test_el_reproceso_descarta_el_payload_sin_precio():
    """La compuerta que faltaba en el primer intento del 2026-09-12.

    Sin ella, re-encolar una orden que Amazon todavia reporta en cero reescribe
    el mismo $0 y el reporte se "arregla" sin arreglar nada. Peor: ese dia se
    encolaron 4 jobs con el payload mal normalizado (la API v2026 anida la
    orden bajo `order`, no bajo `payload`) y el worker los consumio sin dejar
    rastro.
    """
    # Con los espacios colapsados: `ruff format` parte las llamadas largas y un
    # test atado al formato se rompe solo. Ya paso hoy — dejo main en rojo.
    plano = "".join(TOOL.read_text(encoding="utf-8").split())

    assert '(resp.json()or{}).get("order")' in plano, (
        "la v2026 anida la orden bajo `order`; tomar `payload` devuelve un "
        "objeto vacio que normaliza a total 0"
    )
    i_compuerta = plano.index("total<=0oritems==0")
    i_push = plano.index("poll.push_to_redis")
    assert i_compuerta < i_push, (
        "se encola antes de validar el payload: es exactamente el error que "
        "metio 4 jobs vacios en la cola"
    )
    i_delete = plano.index("DELETEFROMamazon_processed_events")
    assert i_compuerta < i_delete, (
        "se borra la auditoria antes de saber si el reproceso va a servir"
    )
