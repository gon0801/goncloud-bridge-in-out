#!/usr/bin/env python3
"""
push_fx_to_odoo.py — Copia el tipo de cambio USD/MXN de accounting.db a Odoo.

Uso:
  python3 push_fx_to_odoo.py             # Última tasa disponible
  python3 push_fx_to_odoo.py 2026-02-23  # Fecha específica

En Odoo 17, res.currency.rate.rate = inverse_company_rate = USD por 1 MXN
(ej. si 1 USD = 17.197 MXN → rate = 1/17.197 = 0.058149)
"""
import sys
import sqlite3
import requests

BRIDGE_DB  = "/mnt/data/appdata/bridge/data/bridge.db"
ACCOUNTING_DB = "/mnt/data/appdata/accounting/data/accounting.db"


def get_settings():
    conn = sqlite3.connect(BRIDGE_DB)
    def gs(k):
        return (conn.execute("SELECT value FROM bridge_settings WHERE key=?", (k,)).fetchone() or [None])[0]
    cfg = {
        "url":  gs("odoo_url").rstrip("/"),
        "db":   gs("odoo_db"),
        "user": gs("odoo_user"),
        "pw":   gs("odoo_password"),
    }
    conn.close()
    return cfg


def get_rate_from_accounting(rate_date=None):
    """Lee el rate (MXN por 1 USD) de accounting.db."""
    conn = sqlite3.connect(ACCOUNTING_DB)
    if rate_date:
        row = conn.execute(
            "SELECT rate_date, rate FROM currency_rates "
            "WHERE base_currency='MXN' AND quote_currency='USD' AND rate_date=? LIMIT 1",
            (rate_date,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT rate_date, rate FROM currency_rates "
            "WHERE base_currency='MXN' AND quote_currency='USD' "
            "ORDER BY rate_date DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if not row:
        raise RuntimeError(f"No hay rate en accounting.db{' para ' + rate_date if rate_date else ''}")
    return row[0], float(row[1])  # (rate_date, mxn_per_usd)


def push_to_odoo(rate_date, mxn_per_usd):
    cfg = get_settings()

    def jcall(service, method, args):
        r = requests.post(
            f"{cfg['url']}/jsonrpc",
            json={"jsonrpc": "2.0", "method": "call",
                  "params": {"service": service, "method": method, "args": args}, "id": 1},
            timeout=30,
        )
        res = r.json()
        if "error" in res:
            raise RuntimeError(str(res["error"]))
        return res["result"]

    uid = jcall("common", "authenticate", [cfg["db"], cfg["user"], cfg["pw"], {}])
    if not uid:
        raise RuntimeError("Odoo authentication failed")

    # ID de la divisa USD
    currencies = jcall("object", "execute_kw", [cfg["db"], uid, cfg["pw"],
        "res.currency", "search_read", [[["name", "=", "USD"]]],
        {"fields": ["id", "active"], "limit": 1}])
    if not currencies:
        raise RuntimeError("Divisa USD no existe en Odoo")
    currency_id = currencies[0]["id"]

    # En Odoo 17: rate = inverse_company_rate = 1 / mxn_per_usd
    odoo_rate = round(1.0 / mxn_per_usd, 8)

    # Verificar sanity del rate antes de escribir
    check = round(1.0 / odoo_rate, 4)
    if check < 5.0 or check > 500.0:
        raise RuntimeError(f"Rate sospechoso: {mxn_per_usd} MXN/USD — no se escribe en Odoo")

    # Buscar si ya existe registro para esta fecha
    existing = jcall("object", "execute_kw", [cfg["db"], uid, cfg["pw"],
        "res.currency.rate", "search_read",
        [[["currency_id", "=", currency_id], ["name", "=", rate_date]]],
        {"fields": ["id"], "limit": 1}])

    if existing:
        jcall("object", "execute_kw", [cfg["db"], uid, cfg["pw"],
            "res.currency.rate", "write",
            [[existing[0]["id"]], {"rate": odoo_rate}]])
        print(f"✅ Odoo rate UPDATED: USD {rate_date}  rate={odoo_rate}  (1 USD = {mxn_per_usd} MXN)")
    else:
        jcall("object", "execute_kw", [cfg["db"], uid, cfg["pw"],
            "res.currency.rate", "create",
            [{"currency_id": currency_id, "name": rate_date, "rate": odoo_rate}]])
        print(f"✅ Odoo rate CREATED: USD {rate_date}  rate={odoo_rate}  (1 USD = {mxn_per_usd} MXN)")


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    rate_date, mxn_per_usd = get_rate_from_accounting(date_arg)
    print(f"[push_fx] accounting.db → {rate_date}: 1 USD = {mxn_per_usd} MXN")
    push_to_odoo(rate_date, mxn_per_usd)
