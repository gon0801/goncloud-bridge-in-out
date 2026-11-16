"""Stock que el bridge publica en las plataformas para un producto de Odoo.

Lo usan los snapshotters de MeLi y Amazon para que las dos plataformas
reciban la misma cifra: lo fisico menos lo vendido sin enviar, nunca menos
de 0.

Limite con kits phantom: Odoo calcula qty_available y outgoing_qty del kit
cada uno como el minimo sobre sus componentes, asi que aqui sale
min(fisico_i) - min(salidas_i), que puede quedar arriba de
min(fisico_i - salidas_i) cuando un componente tambien sale por otra venta.
tools/stock_sync_dry_run.py cuenta esos kits contra produccion.
"""

CAMPOS_ODOO = ["qty_available", "outgoing_qty"]


def disponible(producto: dict) -> int:
    fisico = producto.get("qty_available") or 0
    salidas = producto.get("outgoing_qty") or 0
    return int(max(fisico - salidas, 0))
