"""Un topic que no manejamos no es una falla; un topic desconocido si lo es.

Por que existe
--------------
`app/inbound_worker.py` acepta como `resource` valido solo `^/orders/{id}$` y
mandaba TODO lo demas a `dead` con `reason=bad_resource`.

La app esta suscrita en MeLi a cuatro topics que el worker no procesa. Medido
sobre `inbound_events` en produccion (2026-09-12):

    orders_v2   3474   procesado
    payments    1671   -> dead
    messages     267   -> dead   (resource sin barra, un hash de 32 hex)
    questions    105   -> dead
    orders        29   procesado

Resultado: la tabla de auditoria se llena de avisos de pago y mensajes de
compradores, todos inofensivos, y **una orden que realmente fallo queda
enterrada entre cientos de ellos**. En la auditoria del 2026-08-09 eran 359
`dead` en 30 dias, el 100% de esta clase.

Que verifica
------------
Que los topics conocidos-y-no-manejados se registren como `skipped`, y que un
topic DESCONOCIDO siga cayendo en `dead`.

Esa segunda mitad es la que importa de verdad. Es facil "arreglar" el ruido
silenciando todo lo que no sea una orden; eso tambien silenciaria un topic
nuevo de MeLi, o un `resource` con formato cambiado, que es exactamente lo que
si hay que ver. El valor del cambio esta en que sigue siendo ruidoso donde debe.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parent.parent / "app" / "inbound_worker.py"


@pytest.fixture(scope="module")
def worker(monkeypatch_session=None):
    spec = importlib.util.spec_from_file_location("inbound_worker_bajo_prueba", WORKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["inbound_worker_bajo_prueba"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("topic", ["payments", "messages", "questions"])
def test_los_topics_conocidos_que_no_manejamos_no_son_fallas(worker, topic):
    """Los tres que MeLi manda de verdad y el worker no procesa."""
    resultado, razon = worker.clasificar_resource_no_orden(topic)

    assert resultado == "skipped", (
        f"topic '{topic}' se registra como '{resultado}'. Es un topic al que la "
        f"app esta suscrita y que el worker no procesa: no es una falla, y "
        f"marcarlo dead entierra las fallas reales."
    )
    assert razon == "topic_not_handled"


@pytest.mark.parametrize(
    "topic",
    ["shipments", "items", "flex-handshakes", "", "orders_v3", "PAYMENTS_RARO"],
)
def test_un_topic_desconocido_sigue_siendo_dead(worker, topic):
    """La mitad que importa: no silenciar lo que todavia no entendemos.

    Un topic nuevo de MeLi, o un `resource` con formato cambiado, tiene que
    seguir siendo ruidoso. Si esto se relaja, el cambio deja de ser una mejora
    de senal y pasa a ser un silenciador.
    """
    resultado, razon = worker.clasificar_resource_no_orden(topic)

    assert resultado == "dead", (
        f"topic '{topic}' se registra como '{resultado}'. Un topic que no "
        f"reconocemos debe seguir cayendo en dead — es la unica forma de "
        f"enterarnos de que MeLi cambio algo."
    )
    assert razon == "bad_resource"


def test_la_comparacion_ignora_mayusculas_y_espacios(worker):
    """MeLi manda el topic en el payload; no dependamos de su formato exacto."""
    assert worker.clasificar_resource_no_orden("  payments  ")[0] == "skipped"
    assert worker.clasificar_resource_no_orden("Payments")[0] == "skipped"


def test_el_worker_usa_la_funcion_en_el_camino_real(worker):
    """Guardia anti-verde-falso: que la funcion no quede colgada sin llamador.

    Sin esto, los tests de arriba pasarian aunque el bucle del worker siguiera
    marcando `dead` a mano, que es justo el bug.
    """
    fuente = WORKER.read_text(encoding="utf-8")

    llamadas = fuente.count("clasificar_resource_no_orden(")
    assert llamadas >= 2, (
        f"`clasificar_resource_no_orden` aparece {llamadas} vez/veces: solo su "
        f"definicion. El bucle de procesamiento no la esta usando."
    )
    assert '"dead",\n                        {"reason": "bad_resource"' not in fuente, (
        "quedo el marcado literal a `dead` en el camino de resource invalido"
    )
