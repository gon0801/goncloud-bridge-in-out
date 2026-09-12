"""Buscar el payload por la clave exacta nunca lo encuentra.

Por que existe
--------------
El payload se persiste con la clave INICIAL (`ml:{order_id}`), pero la auditoria
guarda la clave ACTION-AWARE (`ml:{order_id}:paid`), que el worker calcula
despues de leer el estado real de la orden.

`recover_manual_review.py` buscaba por igualdad exacta contra la clave de la
auditoria. Medido en produccion el 2026-09-12: el join exacto entre
`processed_inbound_events` e `inbound_job_payloads` devolvia **1 fila de
todas** — la unica con clave `legacy:`, que no lleva sufijo de accion.

    payloads:   ml:2000017618465684
    auditoria:  ml:2000017618465684:paid

Eso es lo que hay detras de la nota de CLAUDE.md sobre que las ordenes MeLi con
clave `rawsha:` son irrecuperables y hay que pedirle a MeLi que reenvie el
webhook. No es que el payload no se guarde: se busca mal, y la herramienta
reporta "sin payload persistido" para todas.

Sobre el orden de las candidatas
--------------------------------
La clave exacta va primero. Las `legacy:` no llevan sufijo y ahi coincide
directo; recortar siempre romperia ese caso, que es el unico que hoy funciona.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "recover_manual_review.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("recover_manual_review", TOOL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["recover_manual_review"] = m
    spec.loader.exec_module(m)
    return m


def test_una_clave_action_aware_busca_tambien_la_inicial(mod):
    """El caso real: la auditoria tiene `:paid`, el payload no."""
    assert mod.claves_de_payload("ml:2000017618465684:paid") == [
        "ml:2000017618465684:paid",
        "ml:2000017618465684",
    ]


def test_la_clave_exacta_va_primero(mod):
    """Las `legacy:` no llevan sufijo: recortar siempre romperia el unico caso
    que hoy funciona."""
    assert mod.claves_de_payload("legacy:24a8e2f59b09e80d")[0] == (
        "legacy:24a8e2f59b09e80d"
    )


def test_una_clave_sin_sufijo_no_se_recorta(mod):
    """`legacy:hash` tiene un solo `:`; recortarlo daria `legacy`, que no existe."""
    assert mod.claves_de_payload("legacy:24a8e2f59b09e80d") == [
        "legacy:24a8e2f59b09e80d"
    ]


def test_el_formato_rawsha_viejo_tambien_se_cubre(mod):
    """Los webhooks anteriores al PR #42 dejaron claves `rawsha:{sha}:{accion}`."""
    assert mod.claves_de_payload("rawsha:abc123:paid") == [
        "rawsha:abc123:paid",
        "rawsha:abc123",
    ]


def test_una_clave_vacia_no_revienta(mod):
    assert mod.claves_de_payload("") == []
    assert mod.claves_de_payload(None) == []


def test_la_herramienta_usa_el_helper():
    """Guardia anti-verde-falso: que la funcion no quede sin llamador."""
    plano = "".join(TOOL.read_text(encoding="utf-8").split())
    assert plano.count("buscar_payload(") >= 2, (
        "`buscar_payload` solo aparece en su definicion: el flujo de recuperacion "
        "sigue buscando por igualdad exacta"
    )
