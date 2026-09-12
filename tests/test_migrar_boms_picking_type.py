"""La migración de recetas tiene que ser idempotente y no escribir a ciegas.

Por que existe
--------------
Modelo hibrido de PENDIENTES #3: las recetas phantom se restringen a la
operacion de entrega de EHV-MX para que FBM siga explotando el kit en
componentes mientras FULL y FBA lo mueven como unidad armada.

Son 1009 recetas vivas en produccion. Dos cosas tenian que quedar fijas:

1. **Idempotencia.** Reescribir recetas que ya apuntan al destino ensucia el
   historial de Odoo y alarga la ventana de escritura sin ganar nada. Correr el
   script dos veces debe ser inofensivo.

2. **No escribir sin respaldo.** El 2026-09-12, un purgado de `sku_mapping`
   corrio con el comando de respaldo fallado — el borrado no dependia de el y
   siguio igual. Las filas se recuperaron del backup diario, pero en otra tabla
   habria dolido. Aqui la escritura depende de que el respaldo exista y se
   pueda releer.

Sobre las recetas archivadas
----------------------------
Esta base tiene **1541 recetas phantom archivadas contra 1009 vivas** (residuo
de migraciones de componentes, como el cambio de arras de 16mm a 19mm). Ya
causaron un error de medicion: el auditor contaba SKUs bloqueados sobre recetas
muertas e inflaba el impacto de 94 a 99. La seleccion aqui filtra por `active`.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = (
    Path(__file__).resolve().parent.parent
    / "tools"
    / "odoo_migrar_boms_picking_type.py"
)
DESTINO = 2


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("migrar_boms", TOOL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["migrar_boms"] = m
    spec.loader.exec_module(m)
    return m


def test_una_receta_sin_restriccion_se_migra(mod):
    """El estado real de las 1009: `picking_type_id` vacio."""
    boms = [{"id": 101, "picking_type_id": False}]
    assert mod.boms_a_migrar(boms, DESTINO) == [101]


def test_una_receta_que_ya_apunta_al_destino_no_se_reescribe(mod):
    """Idempotencia: correr el script dos veces no debe tocar nada la segunda."""
    boms = [{"id": 101, "picking_type_id": [DESTINO, "EHV-MX: Delivery Orders"]}]
    assert mod.boms_a_migrar(boms, DESTINO) == []


def test_una_receta_apuntando_a_OTRA_operacion_si_se_migra(mod):
    """Si alguien la restringio a otro almacen, hay que corregirla."""
    boms = [{"id": 101, "picking_type_id": [99, "FBA-MX: Delivery Orders"]}]
    assert mod.boms_a_migrar(boms, DESTINO) == [101]


def test_una_corrida_mixta_solo_toca_lo_pendiente(mod):
    boms = [
        {"id": 1, "picking_type_id": False},
        {"id": 2, "picking_type_id": [DESTINO, "EHV-MX: Delivery Orders"]},
        {"id": 3, "picking_type_id": [99, "Otro"]},
        {"id": 4, "picking_type_id": False},
    ]
    assert mod.boms_a_migrar(boms, DESTINO) == [1, 3, 4]


def test_la_seleccion_filtra_recetas_archivadas():
    """1541 archivadas contra 1009 vivas: incluirlas ya causo un error de medicion."""
    fuente = TOOL.read_text(encoding="utf-8")
    consultas = [
        ln for ln in fuente.splitlines() if '"mrp.bom"' in ln and "search_read" in ln
    ]
    bloque = fuente[fuente.index('od.kw("mrp.bom", "search_read"') :][:260]
    assert '["active", "=", True]' in bloque, (
        "la seleccion de recetas a migrar no filtra por active. Esta base tiene "
        "mas recetas archivadas que vivas; tocarlas no sirve y distorsiona los "
        "conteos."
    )
    assert consultas or bloque, "no se encontro la consulta de recetas"


def test_la_escritura_depende_del_respaldo():
    """El bug de proceso del purgado de sku_mapping, ya como invariante.

    Se afirma sobre el fuente que el respaldo se relee y que un fallo aborta
    antes del primer write, no que exista una linea con la palabra backup.
    """
    fuente = TOOL.read_text(encoding="utf-8")

    i_respaldo = fuente.index("NO se toco Odoo")
    i_write = fuente.index('od.kw("mrp.bom", "write", [trozo')
    assert i_respaldo < i_write, (
        "el aborto por respaldo fallido ocurre DESPUES del primer write: "
        "el borrado no puede volver a correr sin respaldo valido"
    )
    assert "releido = json.load(fh)" in fuente, (
        "el respaldo se escribe pero no se relee; un archivo truncado pasaria por bueno"
    )


def test_hay_verificacion_de_disponibilidad_antes_y_despues():
    """El supuesto que habilita todo esto se comprueba en cada corrida."""
    fuente = TOOL.read_text(encoding="utf-8")
    assert "antes = muestrear" in fuente and "despues = muestrear" in fuente, (
        "falta el muestreo de qty_available antes/despues"
    )
    assert "--revertir" in fuente, "no hay forma de deshacer la migracion"
