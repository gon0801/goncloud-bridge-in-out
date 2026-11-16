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
almacenable no limita y una BOM anidada se abre hasta sus hojas.

Almacen apartado: sin contexto, Odoo suma en qty_available y outgoing_qty
todos los almacenes de la compania. El almacen que nombra el setting
`warehouse_amazon_flex_mx` guarda las piezas apartadas para Amazon Flex, que
no se ofrecen en MeLi ni en Amazon FBM; `contexto_publicable` lo deja fuera.
Los componentes se leen con el mismo contexto que los productos.
"""

import sqlite3
from collections.abc import Callable

CAMPOS_ODOO = ["qty_available", "outgoing_qty", "product_tmpl_id"]
SETTING_APARTADO = "warehouse_amazon_flex_mx"
ALMACEN_PRINCIPAL = "EHV"
CAMPOS_ALMACEN = ["id", "name", "code"]
PROFUNDIDAD_MAXIMA = 10

# (id del componente en product.product, unidades por kit)
Receta = list[tuple[int, float]]
# execute_kw sin credenciales: ejecutar(modelo, metodo, args, kwargs)
Ejecutar = Callable[[str, str, list, dict], list]


def disponible(producto: dict) -> int:
    fisico = producto.get("qty_available") or 0
    salidas = producto.get("outgoing_qty") or 0
    return int(max(fisico - salidas, 0))


def recetas_phantom(ejecutar: Ejecutar) -> Callable[[dict], Receta | None]:
    """La funcion que da la receta phantom de un producto, o None si no es kit."""
    boms = ejecutar(
        "mrp.bom",
        "search_read",
        [[("type", "=", "phantom")]],
        {
            "fields": ["product_id", "product_tmpl_id", "product_qty", "bom_line_ids"],
            "order": "sequence, id",
        },
    )
    ids_lineas = sorted({i for b in boms for i in b["bom_line_ids"]})
    lineas = {}
    if ids_lineas:
        leidas = ejecutar(
            "mrp.bom.line", "read", [ids_lineas], {"fields": ["product_id", "product_qty"]}
        )
        lineas = {x["id"]: x for x in leidas}
    por_producto: dict[int, Receta] = {}
    por_plantilla: dict[int, Receta] = {}
    for b in boms:
        receta = [
            (lineas[i]["product_id"][0], lineas[i]["product_qty"] / b["product_qty"])
            for i in b["bom_line_ids"]
            if lineas[i]["product_qty"]
        ]
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
    componentes: dict[int, dict] = {}
    pendientes = {c for p in productos for c, _ in receta_de(p) or []}
    while pendientes:
        leidos = ejecutar("product.product", "read", [sorted(pendientes)], opciones)
        componentes.update({c["id"]: c for c in leidos})
        pendientes = {
            c for p in leidos for c, _ in receta_de(p) or [] if c not in componentes
        }

    def libre(producto: dict, profundidad: int) -> int:
        receta = receta_de(producto)
        if receta is None:
            return disponible(producto)
        if profundidad > PROFUNDIDAD_MAXIMA:
            raise ValueError(f"BOM phantom ciclica en el producto {producto['id']}")
        cotas = []
        for c, unidades in receta:
            comp = componentes[c]
            if receta_de(comp) is None and comp.get("type") != "product":
                continue
            cotas.append(int(libre(comp, profundidad + 1) // unidades))
        return min(cotas, default=0)

    return {p["id"]: libre(p, 0) for p in productos}


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
