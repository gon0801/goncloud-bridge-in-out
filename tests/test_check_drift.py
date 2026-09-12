"""Un chequeo que grita por todo equivale a un chequeo apagado.

Por que existe
--------------
`check_tools_data_drift` comparaba `tools/` con `data/` usando `diff` de texto.
El 2026-08-07 paso un `ruff format` masivo por el repo y `tools/` se actualizo;
`data/` no. Desde entonces el chequeo reportaba 19 archivos en drift.

De esos 19, 17 eran solo formato. Las 2 diferencias que si cambiaban el
comportamiento — una guarda faltante en `die()` y dos `except:` pelados —
estuvieron ahi, visibles, dentro de una lista que nadie podia revisar.

El bug no fue el drift. Fue un reporte con tanto ruido que se volvio invisible.

Que verifica
------------
Que la clasificacion distinga formato de comportamiento:

- reformatear (comillas, saltos de linea, f-string sin placeholders, docstrings)
  NO es drift real
- quitar una guarda, cambiar un `except Exception:` por `except:`, agregar o
  quitar una llamada SI lo es

El caso del `except:` pelado esta explicito porque es el que se escapo de
verdad, dos veces, en dos archivos distintos.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from check_tools_data_drift import (  # noqa: E402
    FORMATO,
    IDENTICO,
    ILEGIBLE,
    REAL,
    clasificar,
    comparar,
    diferencias,
)

ORIGINAL = '''
"""Docstring del modulo."""
import os


def cobrar(monto):
    """Cobra."""
    try:
        return os.environ["X"], monto
    except Exception:
        raise
'''


def test_identico_es_identico():
    assert comparar(ORIGINAL, ORIGINAL) == IDENTICO


SIN_GUARDA = '''
"""Docstring del modulo."""
import os


def cobrar(monto):
    """Cobra."""
    return os.environ["X"], monto
'''


@pytest.mark.parametrize(
    "descripcion, izq, der",
    [
        (
            "comillas",
            ORIGINAL,
            ORIGINAL.replace('os.environ["X"]', "os.environ['X']"),
        ),
        (
            "firma partida en varias lineas",
            ORIGINAL,
            ORIGINAL.replace("def cobrar(monto):", "def cobrar(\n    monto,\n):"),
        ),
        (
            "f-string sin placeholders",
            ORIGINAL.replace('os.environ["X"]', '"literal"'),
            ORIGINAL.replace('os.environ["X"]', 'f"literal"'),
        ),
        (
            "docstring reescrito",
            ORIGINAL,
            ORIGINAL.replace("Cobra.", "Cobra el monto."),
        ),
        (
            "import multiple partido en uno por linea",
            "import os, sys\nprint(os, sys)\n",
            "import os\nimport sys\nprint(os, sys)\n",
        ),
        (
            "docstring de modulo borrado",
            ORIGINAL,
            ORIGINAL.replace('"""Docstring del modulo."""\n', ""),
        ),
    ],
)
def test_reformatear_no_es_drift_real(descripcion, izq, der):
    assert comparar(izq, der) == FORMATO, descripcion


@pytest.mark.parametrize(
    "descripcion, modificado",
    [
        (
            "except Exception: -> except: pelado",
            ORIGINAL.replace("except Exception:", "except:"),
        ),
        ("se quita el try/except entero", SIN_GUARDA),
        (
            "se agrega una llamada",
            ORIGINAL.replace("    try:", "    os.system('rm -rf /')\n    try:"),
        ),
        (
            "cambia un literal de texto",
            ORIGINAL.replace('os.environ["X"]', 'os.environ["Y"]'),
        ),
        # Normalizamos f-strings SIN placeholders porque son equivalentes a la
        # constante. Aplanar tambien los que SI interpolan seria un falso
        # negativo: escondería drift real detras de "solo formato", que es la
        # direccion peligrosa del error.
        (
            "f-string con placeholder que interpola otra cosa",
            ORIGINAL.replace('os.environ["X"], monto', 'f"{monto:.2f}", monto'),
        ),
    ],
)
def test_cambiar_comportamiento_si_es_drift_real(descripcion, modificado):
    assert comparar(ORIGINAL, modificado) == REAL, descripcion


def test_agregar_un_import_si_es_drift_real():
    """Partir un import es formato; agregar uno no — puede tener efectos."""
    assert comparar("import os\n", "import os\nimport socket\n") == REAL


def test_f_string_con_placeholder_no_se_aplana():
    """Aplanar un f-string que interpola esconderia drift real como formato."""
    izq = ORIGINAL.replace('os.environ["X"]', 'f"total {monto}"')
    der = ORIGINAL.replace('os.environ["X"]', 'f"total {monto + 1}"')
    assert comparar(izq, der) == REAL


def test_archivo_ilegible_no_revienta():
    assert comparar(ORIGINAL, "def roto(:\n") == ILEGIBLE


def test_las_diferencias_nombran_la_sentencia_que_cambio():
    """Sin esto el reporte vuelve a ser 'DRIFT: archivo.py' y a nadie le sirve."""
    modificado = ORIGINAL.replace("    try:", "    os.system('rm -rf /')\n    try:")
    pares = diferencias(ORIGINAL, modificado)
    texto = " ".join(a + b for a, b in pares)
    assert "os.system" in texto


def test_clasificar_recorre_los_dos_directorios(tmp_path):
    tools, data = tmp_path / "tools", tmp_path / "data"
    tools.mkdir()
    data.mkdir()

    (tools / "igual.py").write_text(ORIGINAL)
    (data / "igual.py").write_text(ORIGINAL)

    (tools / "solo_formato.py").write_text(ORIGINAL)
    (data / "solo_formato.py").write_text(ORIGINAL.replace("Cobra.", "Cobra bien."))

    (tools / "real.py").write_text(ORIGINAL)
    (data / "real.py").write_text(ORIGINAL.replace("except Exception:", "except:"))

    # Sin pareja en data/: se ignora, no es drift.
    (tools / "huerfano.py").write_text(ORIGINAL)

    cubetas = clasificar(tools, data)
    assert cubetas[IDENTICO] == ["igual.py"]
    assert cubetas[FORMATO] == ["solo_formato.py"]
    assert cubetas[REAL] == ["real.py"]
    assert "huerfano.py" not in sum(cubetas.values(), [])
