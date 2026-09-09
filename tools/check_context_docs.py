#!/usr/bin/env python3
# QUALITY-KIT REPO-HYGIENE v1 -- managed by install-repo-hygiene.ps1
# Re-correr el instalador refresca este archivo (solo si conserva esta marca).
"""Candado de higiene de repo: capa de contexto + basura acumulada.

Por que existe: los docs de IA (CLAUDE.md, AGENTS.md) se cargan en CADA
sesion de agente. Si acumulan diarios de sesion / changelog crecen sin tope
(caso real: AGENTS.md de goncloud-MCP-2 llego a 1,124 lineas, 83% diarios;
CLAUDE.md de goncloud-accounting a 666). La politica global del usuario:
nada fechado/narrativo en estos archivos — eso vive en git log, el tracker
del repo, o docs/.

Modo candado (default, para pre-commit):
  - CLAUDE.md raiz <= 200 lineas; CLAUDE.md anidados <= 80.
  - AGENTS.md / AGENT.md raiz <= 400 lineas.
  - Ninguno acepta secciones "## Estado anterior" ni mas de UNA seccion
    "## Estado actual" (la vigente se REEMPLAZA; la historia va a docs/).
  Exit 1 con mensaje accionable si algo se paso.

Modo sweep (--sweep, manual/agendable): reporta basura candidata SIN borrar
nada — untracked en la raiz del repo, *.tmp/*.bak/*.diff/*.orig, archivos
'null'/'nul', __pycache__/*.pyc untracked, y one-offs _*.py/_*.sh untracked.
Exit 0 siempre (con --strict, exit 1 si encontro algo).

Uso: python tools/check_context_docs.py [raiz] [--sweep] [--strict]
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_CLAUDE_LIMIT = 200
NESTED_CLAUDE_LIMIT = 80
AGENTS_LIMIT = 400
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    "vendor",
}
SWEEP_PATTERNS = ("*.tmp", "*.bak", "*.bak-*", "*.diff", "*.orig", "*.rej", "*.pyc")
SWEEP_NAMES = {"null", "nul", "NUL"}


def _lines(p: Path) -> int:
    return len(p.read_text(encoding="utf-8", errors="replace").splitlines())


def _walk_docs(root: Path):
    for p in root.rglob("CLAUDE.md"):
        if not SKIP_DIRS.intersection(p.relative_to(root).parts):
            yield p


def check_budgets(root: Path) -> list:
    errors = []
    for p in _walk_docs(root):
        n = _lines(p)
        rel = p.relative_to(root).as_posix()
        limit = ROOT_CLAUDE_LIMIT if p.parent == root else NESTED_CLAUDE_LIMIT
        if n > limit:
            errors.append(
                f"{rel}: {n} lineas (limite {limit}). Lo fechado/narrativo va "
                f"a docs/ o al tracker del repo, no aqui."
            )
    docs = list(_walk_docs(root))
    for name in ("AGENTS.md", "AGENT.md"):
        p = root / name
        if p.exists():
            docs.append(p)
            n = _lines(p)
            if n > AGENTS_LIMIT:
                errors.append(
                    f"{name}: {n} lineas (limite {AGENTS_LIMIT}). Diarios de "
                    f"sesion a docs/ (archivo de estados), no aqui."
                )
    for p in docs:
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        prev = len(re.findall(r"^## Estado anterior", text, re.MULTILINE))
        cur = len(re.findall(r"^## Estado actual", text, re.MULTILINE))
        if prev:
            errors.append(
                f"{rel}: {prev} seccion(es) '## Estado anterior' — la seccion "
                f"'Estado actual' se REEMPLAZA; la desplazada va a docs/."
            )
        if cur > 1:
            errors.append(
                f"{rel}: {cur} secciones '## Estado actual' — solo puede "
                f"haber UNA (la vigente)."
            )
    return errors


def sweep(root: Path) -> list:
    # Ruta absoluta de git (S607) y solo los errores que git puede dar (BLE001):
    # sin git no hay sweep, y se dice, no se disfraza de "limpio".
    git = shutil.which("git")
    if git is None:
        return ["(sweep no disponible: no se encontro git en PATH)"]
    try:
        out = subprocess.run(
            [git, "ls-files", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        return [f"(sweep no disponible: git fallo: {e})"]
    findings = []
    for line in out.splitlines():
        p = Path(line)
        name = p.name
        top_level = len(p.parts) == 1
        junky = (
            name in SWEEP_NAMES
            or any(p.match(pat) for pat in SWEEP_PATTERNS)
            or "__pycache__" in p.parts
            or (name.startswith("_") and p.suffix in (".py", ".sh"))
        )
        if junky:
            findings.append(f"{line}  <- patron de basura")
        elif top_level:
            findings.append(f"{line}  <- untracked en la raiz (revisar)")
    return findings


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    root = Path(args[0]) if args else Path(".")
    # Una raiz inexistente o que es un archivo no tiene docs que medir: sin
    # esto el candado decia "dentro de presupuesto" sobre nada (falso verde).
    if not root.is_dir():
        print(f"[repo-hygiene] raiz invalida (no es un directorio): {root}")
        return 1

    if "--sweep" in flags:
        findings = sweep(root)
        if findings:
            print(
                f"[repo-hygiene sweep] {len(findings)} candidato(s) a "
                f"basura/desorden (NO se borro nada):"
            )
            for f in findings:
                print(f"  - {f}")
            return 1 if "--strict" in flags else 0
        print("[repo-hygiene sweep] limpio: sin basura candidata.")
        return 0

    errors = check_budgets(root)
    if errors:
        print("[repo-hygiene] La capa de contexto se paso de presupuesto:")
        for e in errors:
            print(f"  - {e}")
        print(
            "  Estos archivos cargan en cada sesion de agente: podar AHORA "
            "(mover diarios a docs/), no usar --no-verify."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
