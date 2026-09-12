"""Ningún camino de venta puede emitir una factura en $0.

Por que existe
--------------
Amazon devuelve `OrderTotal = 0` mientras la orden esta en `Pending` y lo llena
despues **sin cambiar de estado**. Si el tool factura con ese dato, queda una
factura en cero que ya no se corrige: se emite, se paga y se queda.

Paso de verdad. El 18-19 de febrero de 2026 quedaron **nueve facturas `posted`
y `paid` en $0.00** por 9,194 MXN reales:

    S00476  701-6546898-4975422    980.00  INV/2026/00132
    S00487  701-2189652-3183433  1,889.00  INV/2026/00138
    S00497  701-1360539-2557013  1,288.00  INV/2026/00148   (y seis mas)

El operador decidio dejarlas como estan — una factura publicada y pagada se
corrige con nota de credito, no con un script. Lo que si habia que cerrar es
que vuelva a pasar.

Que verifica
------------
Que **los cuatro** caminos de venta difieran la factura cuando todas las lineas
estan en cero. Solo `amazon_fbm_paid_one_shot.py` tenia la guarda; los otros
tres la tenian abierta:

    amazon_fba_paid_one_shot.py             sin guarda
    inbound_fbm_so_apply_paid_one_shot.py   sin guarda
    inbound_full_paid_one_shot_no_stock.py  sin guarda

MeLi hoy siempre manda precio, asi que su riesgo es menor — pero el costo de la
guarda es nulo y el de la falla es irreversible.

Sobre el verde falso
--------------------
Se afirma que la guarda esta ANTES de crear la factura, no solo que exista el
texto. Una guarda despues del `account.move create` no impide nada. Y se
compara con los espacios colapsados: `ruff format` parte las llamadas largas, y
un test atado al formato ya dejo main en rojo una vez hoy.
"""

from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"

CAMINOS = [
    "amazon_fbm_paid_one_shot.py",
    "amazon_fba_paid_one_shot.py",
    "inbound_fbm_so_apply_paid_one_shot.py",
    "inbound_full_paid_one_shot_no_stock.py",
]


def plano(nombre: str) -> str:
    ruta = TOOLS / nombre
    assert ruta.is_file(), f"no existe {ruta}"
    return "".join(ruta.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("herramienta", CAMINOS)
def test_difiere_la_factura_si_todo_esta_en_cero(herramienta):
    src = plano(herramienta)
    assert 'all(linea["price_unit"]==0forlineaincurrent_line_prices)' in src, (
        f"{herramienta} no difiere la factura cuando todas las lineas estan en "
        f"$0. Una factura en cero no se corrige despues: en febrero de 2026 "
        f"quedaron nueve `posted` y `paid` por 9,194 MXN reales."
    )


@pytest.mark.parametrize("herramienta", CAMINOS)
def test_la_guarda_corta_antes_de_facturar(herramienta):
    """Guardia anti-verde-falso: una guarda despues del create no impide nada."""
    src = plano(herramienta)
    i_guarda = src.index('all(linea["price_unit"]==0')

    # El primer punto donde se crea o busca la factura, segun el tool.
    marcas = [
        m
        for m in (
            '"account.move","create"',
            '"account.move","search"',
            "invoice_origin",
        )
        if m in src
    ]
    assert marcas, f"{herramienta}: no se encontro el paso de facturacion"
    i_factura = min(src.index(m) for m in marcas)

    assert i_guarda < i_factura, (
        f"{herramienta}: la guarda de precio cero aparece DESPUES del paso de "
        f"facturacion. Ahi ya no impide nada."
    )


@pytest.mark.parametrize("herramienta", CAMINOS)
def test_la_guarda_termina_el_proceso(herramienta):
    """Diferir significa salir, no seguir de largo hacia la factura."""
    src = plano(herramienta)
    i_guarda = src.index('all(linea["price_unit"]==0')
    despues = src[i_guarda : i_guarda + 400]
    assert "sys.exit(0)" in despues, (
        f"{herramienta}: la guarda detecta el caso pero no corta el flujo"
    )
