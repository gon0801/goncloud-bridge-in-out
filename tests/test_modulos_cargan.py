"""Red de seguridad: ningun modulo del repo debe tener errores de codigo.

Por que existe
--------------
El bridge son scripts que corren desatendidos (systemd timers, worker de
cola). Sin pruebas, un nombre mal escrito vivia en produccion hasta que
alguien lo ejecutaba. Dos casos reales encontrados asi:

  * `utc_now()` usaba `timezone.utc` sin importarlo -> NameError en cada
    corrida del apply de FULL.
  * `die()` referenciaba `audit` antes de que existiera -> NameError que
    tapaba el "missing_env" que el operador necesitaba ver.

Que verifica
------------
Carga cada modulo en un proceso aparte, con las env vars del bridge
borradas, y exige que NO aparezca un error de codigo.

Que tolera a proposito
----------------------
Fallar por falta de ENTORNO esta bien: estos scripts abortan adrede sin
ODOO_URL, sin DB o sin red. Eso se acepta. Lo que no se acepta es que el
modulo se rompa por si mismo.

Sobre el verde falso
--------------------
La primera version de este archivo vaciaba PATH en el subproceso. Resultado:
el interprete no encontraba site-packages, todos los modulos morian en el
primer `import requests` y el test daba verde sin haber ejecutado una sola
linea del codigo bajo prueba — pasaba incluso con los dos bugs reales
reintroducidos a mano.

De ahi `test_el_detector_atrapa_un_nameerror_deliberado`: siembra un modulo
roto y exige que el detector lo marque. Si la maquinaria se vuelve a romper,
ese test cae primero y avisa, en vez de mentir en verde.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _pytest.outcomes import Failed

REPO_ROOT = Path(__file__).resolve().parents[1]

# bridge_connector/ es un addon que corre dentro del runtime de Odoo: importa
# `odoo`, que no se instala por pip. Queda fuera a proposito.
EXCLUIDOS = {"bridge_connector", "tests", ".git", "__pycache__", ".agents", ".github"}

# Errores que significan "el modulo esta mal escrito".
ERRORES_DE_CODIGO = (
    "NameError",
    "UnboundLocalError",
    "SyntaxError",
    "IndentationError",
    "AttributeError",
    "ImportError: cannot import name",
)

# Env vars del bridge: se borran para ejercitar el camino de error temprano,
# que es justo donde vivian los dos bugs.
ENV_DEL_BRIDGE = (
    "ODOO_URL",
    "ODOO_DB",
    "ODOO_USER",
    "ODOO_PASS",
    "ODOO_PASSWORD",
    "CLIENT_ORDER_REF",
    "ORDER_JSON",
    "SITE_ID",
    "BRIDGE_DB",
    "BRIDGE_DB_PATH",
    "BRIDGE_SQLITE_PATH",
    "SNAPSHOT_CHANNEL",
    "REDIS_URL",
)


def _entorno(tmp_path):
    """Entorno real (con PATH/site-packages) pero sin la config del bridge.

    Ojo: NO vaciar PATH. Si el interprete no encuentra site-packages, todo
    modulo muere en su primer import de terceros y el test pasa sin haber
    probado nada.
    """
    env = os.environ.copy()
    for k in ENV_DEL_BRIDGE:
        env.pop(k, None)
    env["AUDIT_DIR"] = str(tmp_path / "audit")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _cargar(ruta, tmp_path):
    """Ejecuta el modulo en un subproceso. Devuelve (returncode, stderr)."""
    codigo = textwrap.dedent(f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location('bajo_prueba', r'{ruta}')
        mod = importlib.util.module_from_spec(spec)
        sys.modules['bajo_prueba'] = mod
        spec.loader.exec_module(mod)
    """)
    r = subprocess.run(
        [sys.executable, "-c", codigo],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=REPO_ROOT,
        env=_entorno(tmp_path),
    )
    return r.returncode, (r.stderr or "")


# APIs POSIX que no existen en Windows. El bridge se despliega en Linux
# (systemd, /mnt/data, /data/bridge.db), asi que un modulo que las use esta
# BIEN: es el entorno local el que no da la talla, no el codigo. En CI, que
# corre ubuntu-latest, no se tolera nada de esto y la revision es completa.
SOLO_POSIX = (
    "has no attribute 'uname'",
    "has no attribute 'fork'",
    "has no attribute 'getuid'",
    "has no attribute 'geteuid'",
    "has no attribute 'setsid'",
    "has no attribute 'SIGHUP'",
    "has no attribute 'SIGKILL'",
)


def _revisar(rel, stderr):
    """Falla si el stderr delata un error de codigo."""
    for marca in ERRORES_DE_CODIGO:
        if marca not in stderr:
            continue
        if sys.platform == "win32" and any(p in stderr for p in SOLO_POSIX):
            pytest.skip(
                f"{rel}: usa una API POSIX que Windows no tiene. Se revisa de "
                "verdad en CI (ubuntu-latest)."
            )
        pytest.fail(
            f"{rel}: error de codigo al cargar el modulo -> {marca}\n\n"
            f"--- stderr ---\n{stderr[-2500:]}"
        )


def _modulos():
    return [
        p.relative_to(REPO_ROOT)
        for p in sorted(REPO_ROOT.rglob("*.py"))
        if not any(parte in EXCLUIDOS for parte in p.relative_to(REPO_ROOT).parts)
    ]


MODULOS = _modulos()


def test_hay_modulos_para_revisar():
    """Guardia: si el glob se rompe, no queremos verde por lista vacia."""
    assert len(MODULOS) > 30, f"solo se encontraron {len(MODULOS)} modulos"


def test_las_dependencias_estan_instaladas():
    """Sin las deps de requirements.txt esta suite no prueba nada real.

    Se declara fuerte a proposito: preferimos rojo ruidoso antes que el verde
    silencioso que ya nos engano una vez.
    """
    faltan = []
    for mod in ("requests", "redis", "defusedxml", "fastapi", "pydantic"):
        r = subprocess.run([sys.executable, "-c", f"import {mod}"], capture_output=True)
        if r.returncode != 0:
            faltan.append(mod)
    assert not faltan, (
        f"faltan dependencias {faltan}: instala `pip install -r requirements.txt`. "
        "Sin ellas los modulos mueren en su primer import y esta suite no "
        "verifica nada."
    )


def test_el_detector_atrapa_un_nameerror_deliberado(tmp_path):
    """Meta-test: siembra un modulo roto y exige que el detector lo marque."""
    roto = tmp_path / "modulo_roto.py"
    roto.write_text(
        "import requests\n"  # dep real primero, como los modulos de verdad
        "valor = nombre_que_no_existe\n",
        encoding="utf-8",
    )
    _, stderr = _cargar(roto, tmp_path)
    assert "NameError" in stderr, (
        "el detector no vio un NameError sembrado a proposito; la maquinaria "
        f"de carga esta rota. stderr:\n{stderr[-1500:]}"
    )
    with pytest.raises(Failed):
        _revisar("modulo_roto.py", stderr)


@pytest.mark.parametrize("rel", MODULOS, ids=lambda p: str(p).replace("\\", "/"))
def test_modulo_no_tiene_errores_de_codigo(rel, tmp_path):
    try:
        _, stderr = _cargar(REPO_ROOT / rel, tmp_path)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{rel}: se colgo al importarse (>90s). Algo bloquea a nivel modulo."
        )
    _revisar(rel, stderr)
