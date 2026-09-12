"""La clave de deduplicación debe identificar la ORDEN, no el payload.

Por que existe
--------------
El webhook fijaba `dedupe_key = rawsha:{sha256}` en el job y el worker lo
respetaba. Como cada re-entrega de MeLi produce un cuerpo distinto, la misma
orden generaba una clave nueva cada vez y volvia a pasar por todo el flujo.
Medido en produccion el 2026-09-12:

    ordenes MeLi distintas ......... 157
    procesadas mas de una vez ...... 147
    promedio de pasadas por orden .. 7.4

No duplicaba en Odoo — los tools buscan la SO por `client_order_ref` antes de
crear, y se verifico que hay 0 SOs duplicadas. Los dos costos reales eran:

1. **La auditoria mentia.** Contar `success:paid` daba ~6x las ventas reales.
   Eso ya causo un error de diagnostico documentado en esta misma sesion.
2. **El lock no serializaba.** `lock_key` sale de esta misma clave, asi que dos
   notificaciones de la misma orden tomaban locks distintos y podian procesarla
   en paralelo. El codigo ya documentaba la intencion contraria —
   `inbound_worker.py`: "lock_key es el initial key (`ml:{id}`)" — pero el
   webhook la anulaba.

Por que se puede deduplicar (y por que no era obvio)
----------------------------------------------------
El riesgo era el enriquecimiento tardio: que una segunda notificacion traiga
datos que la primera no tenia. Pasa de verdad con Amazon Flex MX (CLAUDE.md
PROBLEMA 7: en `Pending` no llega `BuyerName`, llega en `Unshipped`, y el
segundo pase corrige el `client_order_ref`). Deduplicar ahi se saltaria la
correccion.

Medido sobre MeLi antes de tocar nada — cuatro dimensiones, cero casos:

    cambio de prefijo FBM<->FULL ......... 0 ordenes
    cambio en el conteo de items ......... 0 ordenes
    mas de una accion por orden .......... 0 ordenes
    SOs en Odoo sin el comprador ......... 0 de 166

MeLi manda el `nickname` en la primera notificacion. No tiene el patron.

Compatibilidad
--------------
Un job que YA traiga `dedupe_key` lo conserva. Importa por dos motivos: los
jobs encolados antes del deploy siguen funcionando, y `recover_manual_review.py`
re-encola el payload persistido tal cual.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
WORKER = APP / "inbound_worker.py"
MAIN = APP / "main.py"


@pytest.fixture(scope="module")
def worker():
    spec = importlib.util.spec_from_file_location("worker_dedupe_bajo_prueba", WORKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["worker_dedupe_bajo_prueba"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_una_orden_da_siempre_la_misma_clave(worker):
    """El corazon del fix: dos entregas distintas de la MISMA orden."""
    entrega1 = {
        "topic": "orders_v2",
        "resource": "/orders/2000018393906916",
        "received_at": "2026-09-10T19:56:43+00:00",
    }
    entrega2 = {
        "topic": "orders_v2",
        "resource": "/orders/2000018393906916",
        "received_at": "2026-09-11T04:12:08+00:00",
        "attempts": 3,
    }

    k1 = worker.clave_inicial_del_job(entrega1, json.dumps(entrega1))
    k2 = worker.clave_inicial_del_job(entrega2, json.dumps(entrega2))

    assert k1 == k2 == "ml:2000018393906916", (
        f"las dos entregas dieron claves distintas ({k1} vs {k2}): la orden se "
        f"reprocesaria y el lock no serializaria"
    )


def test_ordenes_distintas_no_colisionan(worker):
    a = {"resource": "/orders/2000018393906916"}
    b = {"resource": "/orders/2000018415573782"}
    assert worker.clave_inicial_del_job(a, "{}") != worker.clave_inicial_del_job(
        b, "{}"
    )


def test_un_dedupe_key_explicito_se_respeta(worker):
    """Compatibilidad: jobs viejos en cola y los que re-encola el recovery."""
    job = {"resource": "/orders/2000018393906916", "dedupe_key": "rawsha:abc123"}
    assert worker.clave_inicial_del_job(job, "{}") == "rawsha:abc123"


def test_un_resource_que_no_es_orden_cae_a_legacy(worker):
    """payments/messages/questions: sin orden que identificar."""
    job = {"topic": "payments", "resource": "/collections/173753815883"}
    clave = worker.clave_inicial_del_job(job, json.dumps(job))
    assert clave.startswith("legacy:"), clave


def test_el_webhook_ya_no_fija_la_clave_en_el_job():
    """Guardia sobre el fuente real: es el origen del bug.

    Si `main.py` vuelve a poner `dedupe_key` en el job de `ml_orders_jobs`, el
    worker lo respeta y volvemos a 7.4 pasadas por orden — en silencio, porque
    todo sigue dando `success`.
    """
    fuente = MAIN.read_text(encoding="utf-8")

    # El sitio real de encolado, no las menciones en docstrings ni en el
    # endpoint de metricas (que solo lista nombres de cola).
    marca = 'r.rpush(\n            "ml_orders_jobs",'
    assert marca in fuente, (
        "no se encontro el rpush a ml_orders_jobs; si cambio de forma, "
        "actualiza este test en vez de borrarlo"
    )

    inicio = fuente.index(marca)
    bloque = fuente[max(0, inicio - 900) : inicio]
    assert '"topic": topic' in bloque, (
        "no se encontro la construccion del job antes del rpush; forma inesperada"
    )
    assert '"dedupe_key"' not in bloque, (
        "main.py vuelve a fijar dedupe_key en el job de ml_orders_jobs. El "
        "worker lo respeta y cada re-entrega reprocesa la orden entera."
    )


def test_el_worker_usa_la_funcion_en_el_camino_real(worker):
    """Guardia anti-verde-falso: que la funcion no quede sin llamador."""
    fuente = WORKER.read_text(encoding="utf-8")
    assert fuente.count("clave_inicial_del_job(") >= 2, (
        "`clave_inicial_del_job` solo aparece en su definicion: el bucle de "
        "procesamiento no la esta usando"
    )
