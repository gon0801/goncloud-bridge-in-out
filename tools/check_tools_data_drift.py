#!/usr/bin/env python3
"""Compara `tools/` contra `data/` por lo que el codigo HACE, no por como se ve.

Por que existe
--------------
La version anterior comparaba con `diff` y reportaba 19 archivos. De esos 19,
17 eran el `ruff format` masivo del 2026-08-07: comillas, imports partidos en
varias lineas, espacios dentro de un literal SQL. Nadie puede revisar 19
"alertas" que en su mayoria no son nada, asi que el reporte se volvio ruido y
las 2 diferencias reales vivieron ahi meses sin que nadie las viera.

Un chequeo que grita por todo equivale a un chequeo apagado.

Que hace distinto
-----------------
Clasifica en tres cubetas y, para las reales, IMPRIME las sentencias que
difieren. Ver el cambio en el reporte es lo que permite decidir en segundos en
vez de abrir un diff de 900 lineas.

Solo las reales devuelven codigo de salida 1.

Que se normaliza antes de comparar (y por que es seguro)
--------------------------------------------------------
- Docstrings: no afectan el comportamiento del script.
- f-strings sin placeholders (`f"abc"` -> `"abc"`): exactamente equivalentes.
  Son la salida tipica del `ruff` al saneear un f-string inutil.
- `import os, sys` partido en una linea por modulo: la misma operacion. El
  ORDEN de los imports no se toca — ahi si puede haber efectos al importar.

NO se normaliza nada mas. Un import de mas, una variable sin usar o un cambio
dentro de un literal de texto salen como reales: pueden ser inofensivos, pero
esa decision es del operador, no del script.

Los dos directorios estan vivos, con llamadores distintos
---------------------------------------------------------
No hay un lado "bueno" y uno "viejo":

  - Los workers resuelven `/data/` primero via `run_tool()`, con `tools/` de
    fallback.
  - Los timers de systemd del host ejecutan `tools/` directo
    (`ExecStart=...${BRIDGE_BASE}/tools/...`).
  - `app/main.py` invoca algunos scripts con ruta fija `/data/`.

Por eso el script no promueve nada solo: decir cual lado gana depende de quien
llama a ese archivo en particular.

Uso:
    python3 tools/check_tools_data_drift.py [raiz]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

IDENTICO = "identico"
FORMATO = "formato"
REAL = "real"
ILEGIBLE = "ilegible"


class _Normalizador(ast.NodeTransformer):
    """Quita las diferencias que no cambian lo que el script hace."""

    def visit_Import(self, node: ast.Import) -> ast.AST | list[ast.AST]:
        # `import os, sys` y dos lineas `import os` / `import sys` son la misma
        # operacion; partirlas es lo que hace `ruff`. No se toca el ORDEN, que
        # si puede importar cuando un modulo tiene efectos al importarse.
        if len(node.names) > 1:
            return [
                ast.copy_location(ast.Import(names=[alias]), node)
                for alias in node.names
            ]
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        self.generic_visit(node)
        # f"abc" sin placeholders es identico a "abc".
        if all(isinstance(v, ast.Constant) for v in node.values):
            texto = "".join(v.value for v in node.values)
            return ast.copy_location(ast.Constant(value=texto), node)
        return node


def _sin_docstring(cuerpo: list[ast.stmt]) -> list[ast.stmt]:
    if (
        cuerpo
        and isinstance(cuerpo[0], ast.Expr)
        and isinstance(cuerpo[0].value, ast.Constant)
        and isinstance(cuerpo[0].value.value, str)
    ):
        return cuerpo[1:]
    return cuerpo


def normalizar(fuente: str) -> ast.Module:
    arbol = ast.parse(fuente)
    arbol.body = _sin_docstring(arbol.body)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodo.body = _sin_docstring(nodo.body)
    return _Normalizador().visit(arbol)


def comparar(fuente_a: str, fuente_b: str) -> str:
    """Clasifica el par sin tocar disco. Es la parte que los tests ejercitan."""
    if fuente_a == fuente_b:
        return IDENTICO
    try:
        a, b = normalizar(fuente_a), normalizar(fuente_b)
    except SyntaxError:
        return ILEGIBLE
    return FORMATO if ast.dump(a) == ast.dump(b) else REAL


def sentencias(fuente: str) -> list[str]:
    """Una linea por sentencia, sin el cuerpo anidado, para mostrar el cambio."""
    anidados = {"body", "orelse", "finalbody", "handlers"}
    salida = []
    for nodo in ast.walk(normalizar(fuente)):
        if not isinstance(nodo, ast.stmt):
            continue
        plano = type(nodo)(
            **{
                campo: ([] if campo in anidados else getattr(nodo, campo, None))
                for campo in nodo._fields
            }
        )
        try:
            salida.append(ast.unparse(plano).split("\n")[0])
        except Exception:
            salida.append(type(nodo).__name__)
    return salida


def diferencias(fuente_a: str, fuente_b: str) -> list[tuple[str, str]]:
    """Las sentencias que difieren, como (lado_a, lado_b). '' = ausente."""
    import difflib

    a, b = sentencias(fuente_a), sentencias(fuente_b)
    fuera = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        for linea in a[i1:i2]:
            fuera.append((linea, ""))
        for linea in b[j1:j2]:
            fuera.append(("", linea))
    return fuera


def clasificar(dir_tools: Path, dir_data: Path) -> dict[str, list[str]]:
    cubetas: dict[str, list[str]] = {IDENTICO: [], FORMATO: [], REAL: [], ILEGIBLE: []}
    for archivo in sorted(dir_tools.glob("*.py")):
        pareja = dir_data / archivo.name
        if not pareja.is_file():
            continue
        veredicto = comparar(
            archivo.read_text(encoding="utf-8", errors="replace"),
            pareja.read_text(encoding="utf-8", errors="replace"),
        )
        cubetas[veredicto].append(archivo.name)
    return cubetas


def main(argv: list[str]) -> int:
    raiz = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    dir_tools, dir_data = raiz / "tools", raiz / "data"
    if not dir_data.is_dir():
        print(f"No existe {dir_data} — este chequeo corre en el servidor.")
        return 0

    cubetas = clasificar(dir_tools, dir_data)
    reales, formato = cubetas[REAL], cubetas[FORMATO]

    if formato:
        print(f"Solo formato ({len(formato)}) — sin efecto, no urge:")
        print("  " + ", ".join(formato))
        print()
    if cubetas[ILEGIBLE]:
        print(f"No se pudieron parsear ({len(cubetas[ILEGIBLE])}):")
        print("  " + ", ".join(cubetas[ILEGIBLE]))
        print()

    if not reales:
        print("OK: ninguna diferencia real de comportamiento entre tools/ y data/.")
        return 0

    print(f"DIFERENCIA REAL DE COMPORTAMIENTO ({len(reales)}):")
    for nombre in reales:
        print(f"\n  === {nombre}")
        pares = diferencias(
            (dir_tools / nombre).read_text(encoding="utf-8", errors="replace"),
            (dir_data / nombre).read_text(encoding="utf-8", errors="replace"),
        )
        for lado_tools, lado_data in pares[:12]:
            if lado_tools:
                print(f"     tools/ : {lado_tools[:92]}")
            if lado_data:
                print(f"     data/  : {lado_data[:92]}")
        if len(pares) > 12:
            print(f"     ... y {len(pares) - 12} mas")
    print(
        "\nQuien corre cada archivo decide cual lado gana:\n"
        "  workers  -> /data/ primero (run_tool), tools/ de fallback\n"
        "  systemd  -> tools/ directo\n"
        "  main.py  -> algunos con ruta fija /data/"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
