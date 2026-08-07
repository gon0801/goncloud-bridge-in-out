"""Pruebas de `tools/inbound_full_so_apply.py`.

Regresion: `utc_now()` usaba `timezone.utc` con solo `import datetime` en el
modulo, o sea `timezone` nunca estaba definido. La funcion se llama al
persistir el resultado (linea del UPDATE de la tabla de inbound), asi que
cualquier corrida real explotaba con NameError. Ruff lo marcaba como F821.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "inbound_full_so_apply.py"


@pytest.fixture(scope="module")
def modulo():
    spec = importlib.util.spec_from_file_location("inbound_full_so_apply", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_utc_now_no_explota(modulo):
    """Antes del fix esto tiraba NameError: name 'timezone' is not defined."""
    assert modulo.utc_now()


def test_utc_now_devuelve_iso_utc_con_sufijo_z(modulo):
    valor = modulo.utc_now()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", valor), valor


def test_utc_now_sin_microsegundos(modulo):
    assert "." not in modulo.utc_now()
