"""Que receta usa `stock_disponible` para un kit y cuando lo deja en 0.

Odoo 17 elige la BOM phantom de un producto con `mrp.bom._bom_find`, que
ordena por "sequence, product_id, id": la de menor sequence gana aunque sea
de plantilla y, con la misma sequence, la de plantilla gana a la de producto
(product_id ordena por "priority desc" de product.product y la plantilla
tiene priority NULL, que PostgreSQL pone primero en DESC). Las lineas
"Apply on Variants" las salta Odoo segun la variante; el bridge no evalua
atributos y deja el kit en 0. Una linea de
componente no almacenable se salta antes de mirar su unidad, como en Odoo.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from stock_disponible import disponibles  # noqa: E402

UNIDAD, DOCENA = 1, 2


def _odoo(boms, lineas, productos):
    """execute_kw falso con las BOMs, lineas y productos dados."""

    def ejecutar(modelo, metodo, args, kwargs):
        if (modelo, metodo) == ("mrp.bom", "search_read"):
            return sorted(boms, key=lambda b: (b["sequence"], b["id"]))
        if (modelo, metodo) == ("mrp.bom.line", "read"):
            return [lineas[i] for i in args[0]]
        if (modelo, metodo) == ("product.product", "read"):
            return [productos[i] for i in args[0]]
        raise ValueError(f"llamada inesperada: {modelo}.{metodo}")

    return ejecutar


def _bom(bid, sequence, lineas, producto=None, plantilla=1000):
    return {
        "id": bid,
        "sequence": sequence,
        "product_id": [producto, "x"] if producto else False,
        "product_tmpl_id": [plantilla, "x"],
        "product_qty": 1.0,
        "product_uom_id": [UNIDAD, "x"],
        "bom_line_ids": lineas,
    }


def _linea(lid, componente, uom=UNIDAD, variantes=()):
    return {
        "id": lid,
        "product_id": [componente, "x"],
        "product_qty": 1.0,
        "product_uom_id": [uom, "x"],
        "bom_product_template_attribute_value_ids": list(variantes),
    }


def _producto(pid, libre=0, tipo="product", plantilla=None):
    return {
        "id": pid,
        "type": tipo,
        "qty_available": libre,
        "outgoing_qty": 0,
        "product_tmpl_id": [plantilla or 2000 + pid, "x"],
        "uom_id": [UNIDAD, "x"],
    }


KIT = _producto(1, tipo="consu", plantilla=1000)
COMPONENTES = {10: _producto(10, libre=10), 11: _producto(11, libre=3)}
LINEAS = {100: _linea(100, 10), 101: _linea(101, 11)}


def test_bom_de_plantilla_con_menor_sequence_gana_a_la_de_producto():
    """La de producto (sequence 5) pide el 10, con 10 libres; la de plantilla
    (sequence 1) pide el 11, con 3. Odoo usa la de plantilla: 3."""
    boms = [_bom(7, 5, [100], producto=1), _bom(8, 1, [101])]
    assert disponibles(_odoo(boms, LINEAS, COMPONENTES), [KIT], {}) == {1: 3}


def test_con_la_misma_sequence_la_bom_de_plantilla_gana():
    """La de plantilla (id 8) pide el 11, con 3; la de producto (id 7, menor)
    pide el 10, con 10. Con la misma sequence Odoo usa la de plantilla: 3."""
    boms = [_bom(7, 1, [100], producto=1), _bom(8, 1, [101])]
    assert disponibles(_odoo(boms, LINEAS, COMPONENTES), [KIT], {}) == {1: 3}


def test_entre_dos_boms_de_producto_con_la_misma_sequence_gana_la_de_menor_id():
    boms = [_bom(9, 1, [101], producto=1), _bom(8, 1, [100], producto=1)]
    assert disponibles(_odoo(boms, LINEAS, COMPONENTES), [KIT], {}) == {1: 10}


def test_linea_con_filtro_de_variante_deja_el_kit_en_cero(caplog):
    lineas = {100: _linea(100, 10), 101: _linea(101, 11, variantes=[55])}
    ejecutar = _odoo([_bom(7, 1, [100, 101], producto=1)], lineas, COMPONENTES)
    with caplog.at_level(logging.WARNING):
        assert disponibles(ejecutar, [KIT], {}) == {1: 0}
    assert "kit 1: linea de 11 con filtro de variante" in caplog.text


def test_filtro_de_variante_en_un_subkit_deja_el_kit_en_cero():
    componentes = {**COMPONENTES, 2: _producto(2, tipo="consu", plantilla=1002)}
    lineas = {100: _linea(100, 10), 101: _linea(101, 11, variantes=[55]), 102: _linea(102, 2)}
    boms = [_bom(7, 1, [102, 100], producto=1), _bom(8, 1, [101], producto=2, plantilla=1002)]
    assert disponibles(_odoo(boms, lineas, componentes), [KIT], {}) == {1: 0}


def test_componente_no_almacenable_en_otra_unidad_no_limita():
    """Odoo salta la linea no almacenable antes de mirar su unidad."""
    componentes = {**COMPONENTES, 12: _producto(12, tipo="consu")}
    lineas = {100: _linea(100, 10), 102: _linea(102, 12, uom=DOCENA)}
    ejecutar = _odoo([_bom(7, 1, [100, 102], producto=1)], lineas, componentes)
    assert disponibles(ejecutar, [KIT], {}) == {1: 10}


def test_componente_almacenable_en_otra_unidad_sigue_dejando_el_kit_en_cero():
    lineas = {100: _linea(100, 10), 101: _linea(101, 11, uom=DOCENA)}
    ejecutar = _odoo([_bom(7, 1, [100, 101], producto=1)], lineas, COMPONENTES)
    assert disponibles(ejecutar, [KIT], {}) == {1: 0}
