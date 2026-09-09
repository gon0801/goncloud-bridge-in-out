#!/usr/bin/env python3
"""QUALITY-KIT PYTHON RUNNER v1 -- ejecuta pruebas sin fijar rutas del host."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path


def _comandos_candidatos(raiz: Path) -> Iterator[list[str]]:
    uv = shutil.which("uv")
    if uv and (raiz / "uv.lock").is_file():
        yield [uv, "run", "--frozen", "python"]

    for relativo in (
        ".venv/bin/python",
        ".venv/Scripts/python.exe",
        "venv/bin/python",
        "venv/Scripts/python.exe",
    ):
        ejecutable = raiz / relativo
        if ejecutable.is_file():
            yield [str(ejecutable)]

    for nombre in ("python3", "python", "py"):
        ejecutable = shutil.which(nombre)
        if ejecutable:
            yield [ejecutable]

    yield [sys.executable]


def _admite_modulo(comando: list[str], modulo: str, raiz: Path) -> bool:
    try:
        resultado = subprocess.run(
            [*comando, "-c", f"import {modulo}"],
            cwd=raiz,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return resultado.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=".")
    parser.add_argument("modulo", choices=("pytest", "unittest"))
    conocidos, argumentos = parser.parse_known_args(argv)

    raiz = Path.cwd().resolve()
    directorio = (raiz / conocidos.cwd).resolve()
    if not directorio.is_dir() or (
        directorio != raiz and raiz not in directorio.parents
    ):
        parser.error("--cwd debe ser un directorio dentro del repositorio")

    for comando in _comandos_candidatos(raiz):
        if _admite_modulo(comando, conocidos.modulo, raiz):
            return subprocess.call(
                [*comando, "-m", conocidos.modulo, *argumentos], cwd=directorio
            )

    print(
        f"quality-kit: no se encontro un Python capaz de importar {conocidos.modulo}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
