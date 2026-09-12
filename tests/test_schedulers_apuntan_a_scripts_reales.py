"""Un scheduler que apunta a un script inexistente falla en silencio para siempre.

Por que existe
--------------
Cron y systemd no avisan cuando el ExecStart no existe: lo intentan, fallan, y
el unico rastro queda en el journal que nadie lee. Ya paso en este repo — del
diario del 2026-05-02:

  `run_amazon_poll.sh`: corregido typo `bridge-inbound-worker` ->
  `bridge-amazon-inbound-worker`

Ese typo dejo el poll de Amazon sin correr hasta que alguien lo noto a mano.

Que verifica
------------
Que cada script referenciado por un scheduler versionado (`tools/cron.d/*` y
`tools/systemd/*.service`) exista de verdad en `tools/`, y que sea ejecutable.

Cubre el error de dedo, el renombre a medias y el borrado de un script cuyo
scheduler queda huerfano.

Sobre las lineas comentadas
---------------------------
`tools/cron.d/` tiene entradas comentadas a proposito (el sync offsite espera
un remote de rclone que todavia no existe). Se ignoran: lo que no corre no
puede romperse, y comentarlas es justamente la forma correcta de dejar algo
pendiente sin que falle cada noche.
"""

import re
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
CRON_D = TOOLS / "cron.d"
SYSTEMD = TOOLS / "systemd"

RUTA_SCRIPT = re.compile(r"/mnt/data/appdata/bridge/tools/([A-Za-z0-9_.-]+)")


def referencias_de_cron():
    """(archivo, script) por cada linea de cron ACTIVA."""
    salida = []
    for f in sorted(CRON_D.glob("*")):
        if not f.is_file():
            continue
        for linea in f.read_text(encoding="utf-8").splitlines():
            limpia = linea.strip()
            if not limpia or limpia.startswith("#"):
                continue
            for nombre in RUTA_SCRIPT.findall(limpia):
                salida.append((f.name, nombre))
    return salida


def referencias_de_systemd():
    salida = []
    for f in sorted(SYSTEMD.glob("*.service")):
        for linea in f.read_text(encoding="utf-8").splitlines():
            if linea.strip().startswith("ExecStart"):
                for nombre in RUTA_SCRIPT.findall(linea):
                    salida.append((f.name, nombre))
                # Los units usan ${BRIDGE_BASE}; resolverlo a la ruta real.
                for nombre in re.findall(
                    r"\$\{BRIDGE_BASE\}/tools/([A-Za-z0-9_.-]+)", linea
                ):
                    salida.append((f.name, nombre))
    return salida


def test_la_extraccion_encuentra_referencias():
    """Guardia anti-verde-falso: sin esto los tests de abajo pasan sobre listas vacias."""
    todas = referencias_de_cron() + referencias_de_systemd()
    assert todas, (
        f"no se extrajo ninguna referencia a script desde {CRON_D} ni {SYSTEMD}. "
        f"Si cambio el layout de los schedulers, actualiza este test."
    )


@pytest.mark.parametrize(
    "origen,script", referencias_de_cron() + referencias_de_systemd()
)
def test_el_script_referenciado_existe(origen, script):
    destino = TOOLS / script
    assert destino.is_file(), (
        f"{origen} programa '{script}' pero no existe en tools/. Cron y systemd "
        f"no avisan de esto: fallan callados en cada corrida."
    )


@pytest.mark.parametrize(
    "origen,script", referencias_de_cron() + referencias_de_systemd()
)
def test_el_script_referenciado_es_ejecutable(origen, script):
    destino = TOOLS / script
    assert destino.is_file(), f"{origen} programa '{script}' y no existe en tools/"
    modo = destino.stat().st_mode
    assert modo & stat.S_IXUSR, (
        f"{origen} programa '{script}' pero no tiene bit de ejecucion. "
        f"Corrige con: chmod +x tools/{script}"
    )
