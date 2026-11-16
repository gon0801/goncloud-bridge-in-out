"""Stock que el bridge publica en las plataformas para un producto de Odoo.

Lo usan los snapshotters de MeLi y Amazon para que las dos plataformas
reciban la misma cifra: lo fisico menos lo vendido sin enviar, nunca menos
de 0.

Kits phantom: Odoo calcula qty_available y outgoing_qty del kit cada uno
como el minimo sobre sus componentes. Restarlos daria
min(fisico_i) - min(salidas_i), que no ve un componente comprometido por la
venta de otro kit. `disponibles` calcula el kit desde sus componentes:
min sobre i de floor((fisico_i - salidas_i) / unidades_por_kit_i). Como en
Odoo, la BOM del producto gana a la de su plantilla, un componente no
almacenable no limita y una BOM anidada se abre hasta sus hojas. Tambien
como en Odoo, unidades_por_kit_i suma todas las lineas del componente i,
incluidas las de niveles anidados: dos lineas de 1 piden 2 por kit.

Unidades de medida: no se convierten. Un kit con una linea en otra unidad que
la del componente, o con la BOM en otra unidad que la del kit, publica 0 y
deja un aviso en el log. Las cantidades se operan como fracciones exactas:
1 libre con 0.1 por kit da 10 kits, no 9, y 1.5 libres con 0.5 dan 3.

Almacen apartado: sin contexto, Odoo suma en qty_available y outgoing_qty
todos los almacenes de la compania. El almacen que nombra el setting
`warehouse_amazon_flex_mx` guarda las piezas apartadas para Amazon Flex, que
no se ofrecen en MeLi ni en Amazon FBM; `contexto_publicable` lo deja fuera.
Los componentes se leen con el mismo contexto que los productos.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from math import floor

CAMPOS_ODOO = ["qty_available", "outgoing_qty", "product_tmpl_id", "uom_id"]
SETTING_APARTADO = "warehouse_amazon_flex_mx"
ALMACEN_PRINCIPAL = "EHV"
CAMPOS_ALMACEN = ["id", "name", "code"]
PROFUNDIDAD_MAXIMA = 10

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Linea:
    componente: int
    unidades: Fraction
    uom: int


@dataclass(frozen=True)
class Receta:
    uom: int
    lineas: tuple[Linea, ...]


# execute_kw sin credenciales: ejecutar(modelo, metodo, args, kwargs)
Ejecutar = Callable[[str, str, list, dict], list]


def _cantidad(valor) -> Fraction:
    return Fraction(str(valor or 0))


def _libre(producto: dict) -> Fraction:
    fisico = _cantidad(producto.get("qty_available"))
    salidas = _cantidad(producto.get("outgoing_qty"))
    return max(fisico - salidas, Fraction(0))


def disponible(producto: dict) -> int:
    return floor(_libre(producto))


def recetas_phantom(ejecutar: Ejecutar) -> Callable[[dict], Receta | None]:
    """La funcion que da la receta phantom de un producto, o None si no es kit."""
    boms = ejecutar(
        "mrp.bom",
        "search_read",
        [[("type", "=", "phantom")]],
        {
            "fields": [
                "product_id",
                "product_tmpl_id",
                "product_qty",
                "product_uom_id",
                "bom_line_ids",
            ],
            "order": "sequence, id",
        },
    )
    ids_lineas = sorted({i for b in boms for i in b["bom_line_ids"]})
    lineas = {}
    if ids_lineas:
        leidas = ejecutar(
            "mrp.bom.line",
            "read",
            [ids_lineas],
            {"fields": ["product_id", "product_qty", "product_uom_id"]},
        )
        lineas = {x["id"]: x for x in leidas}
    por_producto: dict[int, Receta] = {}
    por_plantilla: dict[int, Receta] = {}
    for b in boms:
        receta = Receta(
            uom=b["product_uom_id"][0],
            lineas=tuple(
                Linea(
                    componente=lineas[i]["product_id"][0],
                    unidades=_cantidad(lineas[i]["product_qty"]) / _cantidad(b["product_qty"]),
                    uom=lineas[i]["product_uom_id"][0],
                )
                for i in b["bom_line_ids"]
                if lineas[i]["product_qty"]
            ),
        )
        if b["product_id"]:
            por_producto.setdefault(b["product_id"][0], receta)
        else:
            por_plantilla.setdefault(b["product_tmpl_id"][0], receta)

    def receta_de(producto: dict) -> Receta | None:
        if producto["id"] in por_producto:
            return por_producto[producto["id"]]
        return por_plantilla.get(producto["product_tmpl_id"][0])

    return receta_de


def disponibles(ejecutar: Ejecutar, productos: list[dict], contexto: dict) -> dict[int, int]:
    """Stock publicable por id de producto: `disponible` para un producto
    simple, el minimo de sus componentes para un kit phantom.

    `productos` sale de search_read con CAMPOS_ODOO y el mismo `contexto`."""
    receta_de = recetas_phantom(ejecutar)
    opciones = {"fields": ["type", *CAMPOS_ODOO]}
    if contexto:
        opciones["context"] = contexto

    def hijos(producto: dict) -> set[int]:
        receta = receta_de(producto)
        return {x.componente for x in receta.lineas} if receta else set()

    componentes: dict[int, dict] = {}
    pendientes = set().union(*map(hijos, productos))
    while pendientes:
        leidos = ejecutar("product.product", "read", [sorted(pendientes)], opciones)
        componentes.update({c["id"]: c for c in leidos})
        pendientes = set().union(*map(hijos, leidos)) - componentes.keys()

    def por_kit(producto: dict, profundidad: int) -> dict[int, Fraction] | None:
        """Unidades de cada hoja almacenable por kit, sumadas sobre todas sus
        lineas y niveles; None si una unidad de medida no cuadra."""
        if profundidad > PROFUNDIDAD_MAXIMA:
            raise ValueError(f"BOM phantom ciclica en el producto {producto['id']}")
        receta = receta_de(producto)
        if receta.uom != producto["uom_id"][0]:
            log.warning("kit %s: BOM en otra unidad que el producto; publica 0", producto["id"])
            return None
        total: dict[int, Fraction] = {}
        for linea in receta.lineas:
            comp = componentes[linea.componente]
            if linea.uom != comp["uom_id"][0]:
                log.warning(
                    "kit %s: linea de %s en otra unidad que el componente; publica 0",
                    producto["id"],
                    comp["id"],
                )
                return None
            if receta_de(comp) is None:
                if comp.get("type") == "product":
                    total[comp["id"]] = total.get(comp["id"], 0) + linea.unidades
                continue
            abiertas = por_kit(comp, profundidad + 1)
            if abiertas is None:
                return None
            for hoja, unidades in abiertas.items():
                total[hoja] = total.get(hoja, 0) + unidades * linea.unidades
        return total

    def libre(producto: dict) -> int:
        if receta_de(producto) is None:
            return disponible(producto)
        hojas = por_kit(producto, 0)
        if not hojas:
            return 0
        return min(floor(_libre(componentes[h]) / u) for h, u in hojas.items())

    return {p["id"]: libre(p) for p in productos}


def almacen_apartado(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT value FROM bridge_settings WHERE key=?", (SETTING_APARTADO,)
    ).fetchone()
    return (row[0] or "").strip() if row else ""


def contexto_publicable(almacenes: list[dict], apartado: str) -> dict:
    """Contexto de Odoo con los almacenes activos menos el apartado.

    Un apartado que no existe aborta: publicar sin excluirlo ofreceria en las
    plataformas las piezas apartadas. Apartar EHV tambien: dejaria las
    plataformas solo con el stock de los almacenes de canal. Una lista vacia tambien, porque Odoo lee
    `warehouse: []` como "todos los almacenes"."""
    if not apartado:
        return {}
    if apartado not in {a["name"] for a in almacenes}:
        raise ValueError(f"almacen apartado '{apartado}' no existe en Odoo")
    if any(a["name"] == apartado and a.get("code") == ALMACEN_PRINCIPAL for a in almacenes):
        raise ValueError(f"'{apartado}' es el almacen principal {ALMACEN_PRINCIPAL}; no se aparta")
    ids = [a["id"] for a in almacenes if a["name"] != apartado]
    if not ids:
        raise ValueError(f"'{apartado}' es el unico almacen; no queda stock que publicar")
    return {"warehouse": ids}
