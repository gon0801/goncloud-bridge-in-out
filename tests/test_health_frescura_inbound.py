"""Un inbound en silencio total tiene que disparar alarma.

Por que existe
--------------
Caso real (2026-08-24 -> 2026-09-12): el ingress de MercadoLibre estuvo caido
19 dias. `/v1/health` no lo detecto porque solo vigilaba PROFUNDIDAD de colas
(`ml_orders_dead`, `stock_jobs`) y la cola estaba en **0**: no habia nada
atascado, simplemente no llegaba nada. Un canal muerto se ve exactamente igual
que un canal tranquilo si solo miras cuanto hay encolado.

Umbrales elegidos con datos, no a ojo
-------------------------------------
Distribucion real de huecos entre webhooks, jun -> 23-ago (1966 eventos, 83
dias, antes del apagon):

    p50 0.0h · p90 2.3h · p95 5.6h · p99 16.9h · max 31.8h

    umbral 12h -> 49 falsos positivos (17.7/mes)
    umbral 18h -> 16 falsos positivos  (5.8/mes)
    umbral 24h ->  2 falsos positivos  (0.7/mes)
    umbral 30h ->  1 falso positivo    (0.4/mes)
    umbral 36h ->  0 falsos positivos

Amazon (polling, 1058 eventos en 97 dias): 30h -> 0 falsos positivos.

Por eso ERROR va en 36h (MeLi) y 30h (Amazon): cero falsos positivos
historicos. La tentacion es apretar mas para enterarse antes, pero una alarma
injustificada al mes vuelve a entrenar a todos a ignorar el semaforo — que es
literalmente como se perdieron estos 19 dias (el health ya estaba en rojo desde
el 10-ago por el cache de Amazon, ver PR #35). Con 36h el apagon se detecta el
dia 2 en vez del dia 19, y sin gritar en falso.

El WARN (18h) es informativo y NO tumba `ok`.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
MAIN = APP / "main.py"
CHECKS = APP / "health_checks.py"


@pytest.fixture(scope="module")
def main_mod():
    """`evaluar_frescura` vive en app/health_checks.py.

    Se testea ese modulo y no `main.py` porque main monta StaticFiles y arma la
    app FastAPI al importarse: fuera del contenedor truena en
    `Directory '/app/static' does not exist`.
    """
    spec = importlib.util.spec_from_file_location("health_checks_bajo_prueba", CHECKS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["health_checks_bajo_prueba"] = mod
    spec.loader.exec_module(mod)
    return mod


def hace(horas: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()


def test_un_canal_al_dia_esta_ok(main_mod):
    estado, edad = main_mod.evaluar_frescura(hace(2), warn_h=18, err_h=36)
    assert estado == "ok"
    assert 1.5 < edad < 2.5


def test_a_las_20h_avisa_pero_no_tumba_el_semaforo(main_mod):
    """El warn es informativo: si tumbara `ok`, volveriamos al rojo cronico."""
    estado, _ = main_mod.evaluar_frescura(hace(20), warn_h=18, err_h=36)
    assert estado == "warn", (
        "20h esta sobre el p99 (16.9h) pero debajo del max observado (31.8h): "
        "merece un aviso, no un rojo"
    )


def test_a_las_40h_el_canal_esta_muerto(main_mod):
    """36h no se supero NUNCA en 83 dias de operacion normal."""
    estado, _ = main_mod.evaluar_frescura(hace(40), warn_h=18, err_h=36)
    assert estado == "error"


def test_el_apagon_real_se_habria_detectado_el_dia_2(main_mod):
    """La prueba que le da sentido a todo esto.

    El silencio duro 19 dias. Con este chequeo, al segundo dia ya estaba rojo.
    """
    estado, _ = main_mod.evaluar_frescura(hace(19 * 24), warn_h=18, err_h=36)
    assert estado == "error"

    estado_dia2, _ = main_mod.evaluar_frescura(hace(48), warn_h=18, err_h=36)
    assert estado_dia2 == "error"


def test_el_maximo_historico_normal_no_dispara(main_mod):
    """31.8h fue el hueco mas grande en 83 dias sanos. No puede ser alarma."""
    estado, _ = main_mod.evaluar_frescura(hace(31.8), warn_h=18, err_h=36)
    assert estado != "error", (
        "31.8h es trafico normal medido en produccion; marcarlo error seria "
        "un falso positivo garantizado"
    )


def test_una_tabla_vacia_no_es_una_falla(main_mod):
    """Instalacion nueva o canal recien conectado: sin datos != roto."""
    estado, edad = main_mod.evaluar_frescura(None, warn_h=18, err_h=36)
    assert estado == "empty"
    assert edad is None


def test_una_fecha_ilegible_no_tumba_el_health(main_mod):
    """Preferimos ciego a que una excepcion rompa todo el endpoint."""
    estado, edad = main_mod.evaluar_frescura("no-es-fecha", warn_h=18, err_h=36)
    assert estado == "empty"
    assert edad is None


def test_el_health_ignora_las_filas_del_backfill(main_mod):
    """Detalle sutil y peligroso.

    `tools/meli_orders_backfill.py` inserta filas en `inbound_events` con
    dedupe_key `backfill:%`. Si el chequeo de frescura las contara, correr un
    rescate a mano dejaria el semaforo en verde con el ingress muerto — justo
    lo que paso hoy con el vigia de esta sesion, que se disparo con las filas
    del propio backfill creyendo que era un webhook real.
    """
    fuente = MAIN.read_text(encoding="utf-8")
    consultas = [
        ln
        for ln in fuente.splitlines()
        if "MAX(received_at)" in ln and "inbound_events" in ln
    ]
    assert consultas, "no se encontro la consulta de frescura de MeLi en main.py"
    assert any("rawsha" in ln for ln in consultas), (
        "la consulta de frescura debe filtrar `dedupe_key LIKE 'rawsha:%'` para "
        "contar SOLO webhooks reales; si no, un backfill manual enmascara un "
        "ingress muerto"
    )
