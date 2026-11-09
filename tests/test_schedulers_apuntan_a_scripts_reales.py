"""Un scheduler que apunta a un script inexistente o no ejecutable falla en silencio.

Por que existe
--------------
Cron y systemd no avisan cuando el ExecStart no existe: lo intentan, fallan, y
el unico rastro queda en el journal que nadie lee. Ya paso en este repo — del
diario del 2026-05-02:

  `run_amazon_poll.sh`: corregido typo `bridge-inbound-worker` ->
  `bridge-amazon-inbound-worker`

Ese typo dejo el poll de Amazon sin correr hasta que alguien lo noto a mano.

El 2026-08-10 paso lo mismo con el bit de ejecucion: `bridge_wal_checkpoint.sh`
quedo versionado como `100644` y el cron lo llamaba directo, asi que fallo con
"Permission denied" cada noche. Sin checkpoint, `bridge.db-wal` llego a 2.6 GB.

Que verifica
------------
Que cada script referenciado por un scheduler versionado (`tools/cron/*`,
`tools/cron.d/*` y `tools/systemd/*.service`) exista de verdad y tenga el bit de
ejecucion en el indice de git. Se lee el modo del indice (`git ls-files -s`) y
no el del disco: el checkout puede cambiar el permiso del archivo de trabajo,
pero lo que se instala en el servidor es lo que dice el indice.

Cubre el error de dedo, el renombre a medias, el borrado de un script cuyo
scheduler queda huerfano, y el script que pierde el permiso al commitearlo.

Sobre las lineas comentadas
---------------------------
`tools/cron.d/` tiene entradas comentadas a proposito (el sync offsite espera
un remote de rclone que todavia no existe). Se ignoran: lo que no corre no
puede romperse, y comentarlas es justamente la forma correcta de dejar algo
pendiente sin que falle cada noche.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
CRON = TOOLS / "cron"
CRON_D = TOOLS / "cron.d"
SYSTEMD = TOOLS / "systemd"

BASE = "/mnt/data/appdata/bridge"
# Captura `tools/<archivo>` y tambien `<archivo>` directo bajo BASE. Exige
# extension para no confundir directorios como `data/` con scripts.
RUTA_SCRIPT = re.compile(
    re.escape(BASE) + r"/(?:(tools)/)?([A-Za-z0-9_.-]+\.[A-Za-z0-9]+)"
)
RUTA_BRIDGE_BASE = re.compile(
    r"\$\{BRIDGE_BASE\}/tools/([A-Za-z0-9_.-]+\.[A-Za-z0-9]+)"
)


def _scripts_de_linea(linea):
    """Rutas repo-relativas de los scripts que ejecuta una linea de scheduler."""
    salida = []
    for prefijo, nombre in RUTA_SCRIPT.findall(linea):
        salida.append(f"tools/{nombre}" if prefijo else nombre)
    for nombre in RUTA_BRIDGE_BASE.findall(linea):
        salida.append(f"tools/{nombre}")
    return salida


def _referencias_de_crontab(directorio):
    salida = []
    for f in sorted(directorio.glob("*")):
        if not f.is_file():
            continue
        for linea in f.read_text(encoding="utf-8").splitlines():
            limpia = linea.strip()
            if not limpia or limpia.startswith("#"):
                continue
            for ruta in _scripts_de_linea(limpia):
                salida.append((f.name, ruta))
    return salida


def referencias_de_cron():
    """(archivo, ruta_repo_relativa) por cada linea de cron ACTIVA."""
    return _referencias_de_crontab(CRON) + _referencias_de_crontab(CRON_D)


def referencias_de_systemd():
    salida = []
    for f in sorted(SYSTEMD.glob("*.service")):
        for linea in f.read_text(encoding="utf-8").splitlines():
            if linea.strip().startswith("ExecStart"):
                for ruta in _scripts_de_linea(linea):
                    salida.append((f.name, ruta))
    return salida


def modo_en_git(ruta):
    """Modo del archivo en el indice de git, o None si no esta versionado."""
    salida = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-s", "--", ruta],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not salida:
        return None
    return salida.split()[0]


def test_la_extraccion_encuentra_referencias():
    """Guardia anti-verde-falso: sin esto los tests de abajo pasan sobre listas vacias."""
    todas = referencias_de_cron() + referencias_de_systemd()
    assert todas, (
        f"no se extrajo ninguna referencia a script desde {CRON}, {CRON_D} ni "
        f"{SYSTEMD}. Si cambio el layout de los schedulers, actualiza este test."
    )


def test_la_extraccion_encuentra_scripts_en_la_raiz():
    """El WAL vive en la raiz, no en tools/: sin esto su caso quedaria sin cubrir."""
    rutas = [ruta for _, ruta in referencias_de_cron() + referencias_de_systemd()]
    assert any("/" not in ruta for ruta in rutas), (
        "no se extrajo ningun script de la raiz del repo. El cron del WAL apunta a "
        "bridge_wal_checkpoint.sh en la raiz: revisa RUTA_SCRIPT."
    )


@pytest.mark.parametrize(
    "origen,ruta", referencias_de_cron() + referencias_de_systemd()
)
def test_el_script_referenciado_existe(origen, ruta):
    destino = REPO / ruta
    assert destino.is_file(), (
        f"{origen} programa '{ruta}' pero no existe en el repo. Cron y systemd "
        f"no avisan de esto: fallan callados en cada corrida."
    )


@pytest.mark.parametrize(
    "origen,ruta", referencias_de_cron() + referencias_de_systemd()
)
def test_el_script_referenciado_es_ejecutable_en_git(origen, ruta):
    modo = modo_en_git(ruta)
    assert modo is not None, (
        f"{origen} programa '{ruta}' pero no esta versionado en git"
    )
    assert modo == "100755", (
        f"{origen} ejecuta '{ruta}' pero git lo versiona como {modo}, sin bit de "
        f"ejecucion. El checkout lo deja sin permiso y el cron falla con "
        f"'Permission denied'. Corrige con: git update-index --chmod=+x {ruta} "
        f"(o llamalo con /bin/bash en el scheduler)."
    )
