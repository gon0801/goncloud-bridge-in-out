"""La rotación local del respaldo debe dejar 3 copias de cada prefijo, no 7.

Por que existe
--------------
`bridge_backup.sh` guardaba 7 copias locales de `bridge_db_*.db` (~2.3 GB cada
una, 15 GB en total) y 7 de `bridge_CODE_*.tar.gz`. Esa carpeta ya sube cada
noche a OneDrive con restic, así que 7 copias duplicaban lo que ya está fuera.
Peor: las copias de `.meli_tokens.json`, `amazon_credentials.json` y `.env.meli`
del paso 2b nunca se rotaban y se acumulaban desde mayo de 2026.

David decidió el 9-nov guardar 3 días en local.

Que verifica
------------
Que una corrida real del script, con rutas temporales, deja exactamente 3
copias de cada prefijo incluida la recién hecha, que una base corrupta aborta
antes de borrar nada, y que `BRIDGE_BACKUP_KEEP` cambia el número. Usa
`BRIDGE_PATH`/`BACKUP_PATH` temporales: nunca toca `/mnt/data`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bridge_backup.sh"

CREDENCIALES = [".meli_tokens.json", "amazon_credentials.json", ".env.meli"]
PREFIJOS = ["bridge_db_", "bridge_CODE_", *CREDENCIALES]
MARCAS = [f"2020010{d}T000000Z" for d in range(1, 7)]


def _preparar(tmp_path: Path) -> tuple[Path, Path]:
    bridge = tmp_path / "bridge"
    data = bridge / "data"
    data.mkdir(parents=True)
    db = data / "bridge.db"
    subprocess.run(
        ["sqlite3", str(db), "CREATE TABLE t(x); INSERT INTO t VALUES (1);"],
        check=True,
    )
    for cred in CREDENCIALES:
        (data / cred).write_text("dummy\n", encoding="utf-8")

    backups = tmp_path / "backups"
    backups.mkdir()
    base = 1_000_000_000
    for i, marca in enumerate(MARCAS):
        nombres = [
            f"bridge_db_{marca}.db",
            f"bridge_CODE_{marca}.tar.gz",
            *[f"{cred}_{marca}" for cred in CREDENCIALES],
        ]
        for nombre in nombres:
            archivo = backups / nombre
            archivo.write_text("viejo\n", encoding="utf-8")
            os.utime(archivo, (base + i, base + i))
    return bridge, backups


def _entradas(backups: Path, prefijo: str) -> list[str]:
    return sorted(p.name for p in backups.iterdir() if p.name.startswith(prefijo))


def _correr(bridge: Path, backups: Path, extra_env: dict | None = None):
    env = dict(os.environ)
    env["BRIDGE_PATH"] = str(bridge)
    env["BACKUP_PATH"] = str(backups)
    env.pop("BRIDGE_BACKUP_KEEP", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_una_corrida_deja_tres_de_cada_prefijo(tmp_path):
    bridge, backups = _preparar(tmp_path)
    viejos = {p: set(_entradas(backups, p)) for p in PREFIJOS}

    resultado = _correr(bridge, backups)

    assert resultado.returncode == 0, resultado.stderr
    for prefijo in PREFIJOS:
        quedan = _entradas(backups, prefijo)
        assert len(quedan) == 3, f"{prefijo}: quedan {quedan}"
        nuevas = [n for n in quedan if n not in viejos[prefijo]]
        assert len(nuevas) == 1, f"{prefijo}: la copia nueva no sobrevivió {quedan}"


def test_base_corrupta_falla_y_no_borra_nada(tmp_path):
    bridge, backups = _preparar(tmp_path)
    (bridge / "data" / "bridge.db").write_bytes(b"no es una base")
    antes = {p: _entradas(backups, p) for p in PREFIJOS}

    resultado = _correr(bridge, backups)

    assert resultado.returncode != 0, "una base corrupta no puede dar rc=0"
    for prefijo in PREFIJOS:
        assert _entradas(backups, prefijo) == antes[prefijo], prefijo


def test_el_numero_de_copias_se_cambia_por_entorno(tmp_path):
    bridge, backups = _preparar(tmp_path)

    resultado = _correr(bridge, backups, {"BRIDGE_BACKUP_KEEP": "5"})

    assert resultado.returncode == 0, resultado.stderr
    for prefijo in PREFIJOS:
        quedan = _entradas(backups, prefijo)
        assert len(quedan) == 5, f"{prefijo}: quedan {quedan}"
