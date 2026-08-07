"""Pruebas de `tools/inbound_full_so_refund_and_cancel.py`.

Regresion: `die()` cerraba con `write_audit(audit)`, pero `audit` recien se
arma mucho mas abajo, despues de autenticar contra Odoo. Cualquier `die()`
temprano — falta de una env var, auth fallida — reventaba con
`NameError: name 'audit' is not defined` en vez de reportar el error real.
O sea, el camino de error del script estaba roto justo cuando mas importaba.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "inbound_full_so_refund_and_cancel.py"


def _cargar(monkeypatch, tmp_path, **env):
    """Carga el modulo con un entorno controlado, sin tocar disco real."""
    for k in ("ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_PASSWORD", "CLIENT_ORDER_REF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    spec = importlib.util.spec_from_file_location("refund_mod", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_die_temprano_no_tira_nameerror(monkeypatch, tmp_path):
    """Sin ODOO_URL el script debe salir con SystemExit, no con NameError.

    Antes del fix esto fallaba con `NameError: name 'audit' is not defined`,
    tapando el "missing_env" que el operador necesitaba ver.
    """
    with pytest.raises(SystemExit):
        _cargar(monkeypatch, tmp_path)


def test_die_temprano_reporta_la_env_que_falta(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _cargar(monkeypatch, tmp_path)
    salida = capsys.readouterr().out
    assert "missing_env" in salida, salida
    assert "ODOO_URL" in salida, salida


def test_die_temprano_sale_con_codigo_distinto_de_cero(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _cargar(monkeypatch, tmp_path)
    assert exc.value.code not in (0, None)
