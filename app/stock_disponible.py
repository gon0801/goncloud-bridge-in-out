"""Stock que el bridge publica en las plataformas para un producto de Odoo.

Lo usan los snapshotters de MeLi y Amazon para que las dos plataformas
reciban la misma cifra: lo fisico menos lo vendido sin enviar, nunca menos
de 0.

Limite con kits phantom: Odoo calcula qty_available y outgoing_qty del kit
cada uno como el minimo sobre sus componentes, asi que aqui sale
min(fisico_i) - min(salidas_i), que puede quedar arriba de
min(fisico_i - salidas_i) cuando un componente tambien sale por otra venta.
tools/stock_sync_dry_run.py cuenta esos kits contra produccion.

Almacen apartado: sin contexto, Odoo suma en qty_available y outgoing_qty
todos los almacenes de la compania. El almacen que nombra el setting
`warehouse_amazon_flex_mx` guarda las piezas apartadas para Amazon Flex, que
no se ofrecen en MeLi ni en Amazon FBM; `contexto_publicable` lo deja fuera.
"""

import sqlite3

CAMPOS_ODOO = ["qty_available", "outgoing_qty"]
SETTING_APARTADO = "warehouse_amazon_flex_mx"


def disponible(producto: dict) -> int:
    fisico = producto.get("qty_available") or 0
    salidas = producto.get("outgoing_qty") or 0
    return int(max(fisico - salidas, 0))


def almacen_apartado(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT value FROM bridge_settings WHERE key=?", (SETTING_APARTADO,)
    ).fetchone()
    return (row[0] or "").strip() if row else ""


def contexto_publicable(almacenes: list[dict], apartado: str) -> dict:
    """Contexto de Odoo con los almacenes activos menos el apartado.

    Un apartado que no existe aborta: publicar sin excluirlo ofreceria en las
    plataformas las piezas apartadas. Una lista vacia tambien, porque Odoo lee
    `warehouse: []` como "todos los almacenes"."""
    if not apartado:
        return {}
    if apartado not in {a["name"] for a in almacenes}:
        raise ValueError(f"almacen apartado '{apartado}' no existe en Odoo")
    ids = [a["id"] for a in almacenes if a["name"] != apartado]
    if not ids:
        raise ValueError(f"'{apartado}' es el unico almacen; no queda stock que publicar")
    return {"warehouse": ids}
