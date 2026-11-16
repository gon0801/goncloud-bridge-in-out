"""Stock que el bridge publica en las plataformas para un producto de Odoo.

Lo usan los snapshotters de MeLi y Amazon para que las dos plataformas
reciban la misma cifra: lo fisico menos lo vendido sin enviar, nunca menos
de 0. Para kits phantom Odoo ya calcula ambos campos desde la BOM.
"""

CAMPOS_ODOO = ["qty_available", "outgoing_qty"]


def disponible(producto: dict) -> int:
    fisico = producto.get("qty_available") or 0
    salidas = producto.get("outgoing_qty") or 0
    return int(max(fisico - salidas, 0))
