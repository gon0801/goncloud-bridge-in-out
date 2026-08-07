from odoo import models, api
import requests
import logging

_logger = logging.getLogger("BridgeSync")


class BridgeConnector(models.TransientModel):
    _name = "bridge.connector"
    _description = "Lógica de sincronización stock"

    @api.model
    def sync_stock_fbm(self):
        BRIDGE_URL = "http://bridge-api:8099/v1/stock/snapshot"
        CHANNELS = ["amazon_fbm", "meli"]
        TIMEOUT_SECONDS = 3

        try:
            # 1) Ubicación principal
            stock_location = self.env.ref("stock.stock_location_stock")

            # 2) Buscamos Quants
            quants = self.env["stock.quant"].search(
                [
                    ("location_id", "child_of", stock_location.id),
                    ("product_id.type", "=", "product"),
                ]
            )

            # 3) Consolidamos (Quantity - Reserved)
            items = {}
            for q in quants:
                prod = q.product_id
                if not prod or not prod.default_code:
                    continue

                sku = str(prod.default_code).strip()
                if not sku:
                    continue

                available = (q.quantity or 0.0) - (q.reserved_quantity or 0.0)
                available_int = int(round(available))
                items[sku] = items.get(sku, 0) + available_int

            # 4) Limpieza y formateo
            clean_items = [
                {"sku": sku, "qty": max(0, qty)} for sku, qty in items.items()
            ]

            base_payload = {
                "complete": True,
                "items": clean_items,
            }

            # 5) Envío
            for ch in CHANNELS:
                payload = dict(base_payload)
                payload["channel"] = ch
                try:
                    resp = requests.post(
                        BRIDGE_URL, json=payload, timeout=TIMEOUT_SECONDS
                    )
                    _logger.info(
                        f"Bridge sent: {ch} | SKUs: {len(clean_items)} | Status: {resp.status_code}"
                    )
                except Exception as e:
                    _logger.error(f"Bridge failed: {ch} | Error: {str(e)}")

        except Exception as e:
            _logger.error(f"Bridge fatal error: {str(e)}")
