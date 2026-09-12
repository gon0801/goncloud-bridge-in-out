#!/usr/bin/env python3
"""Recupera ordenes de MercadoLibre que nunca llegaron por webhook.

Por que existe
--------------
El inbound de MeLi es 100% webhook: si el ingress se cae, MeLi reintenta unas
horas y despues descarta. No habia forma de recuperar esa ventana — a
diferencia de Amazon, que entra por polling (`amazon_orders_poll.py`) y se
autocura solo.

Caso real: 2026-08-24 -> 2026-09-12, `bridge-api` quedo fuera de la red docker
del proxy tras un recreate. 19 dias sin un solo webhook y ~70-95 ordenes que
nunca llegaron a Odoo. Nadie se entero.

Que hace
--------
Lista las ordenes del seller en un rango de fechas via `/orders/search` y las
encola en `ml_orders_jobs` con la MISMA forma que produce el webhook, para que
`inbound_worker.py` las procese por el camino normal.

Por que es seguro correrlo de mas
---------------------------------
NO pasa `order_json`: encola solo el `resource`, igual que el webhook, y deja
que el worker haga el GET autoritativo de `/orders/{id}`. La idempotencia la
garantiza el worker aguas abajo: calcula `ml:{order_id}:{action}` DESPUES de
leer el estado real de la orden y `is_already_completed()` corta si ya hay un
`success` o `dead` en `processed_inbound_events`. Reprocesar un rango que se
solapa con ordenes ya en Odoo no duplica nada.

Uso
---
    # Ver que haria, sin tocar nada (SIEMPRE correr esto primero)
    docker exec bridge-api python3 /data/meli_orders_backfill.py \\
        --from 2026-08-24 --dry-run

    # Ejecutar
    docker exec bridge-api python3 /data/meli_orders_backfill.py \\
        --from 2026-08-24

`--to` por defecto es ahora. Las fechas se interpretan en UTC.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import redis
import requests

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
TOKEN_FILE = os.getenv("MELI_TOKEN_FILE", "/data/.meli_tokens.json")

QUEUE = "ml_orders_jobs"
API = "https://api.mercadolibre.com"

# MeLi topea /orders/search en 50 por pagina.
PAGE_SIZE = 50

# Tope de seguridad: sin esto, un rango mal tecleado (--from 2020-01-01) barre
# años de historia y llena la cola. 5000 ordenes es ~3 años del volumen actual.
MAX_ORDERS = 5000

# Contrato con app/inbound_worker.py: el worker hace
#   re.match(r"^/orders/([A-Z0-9\-]+)$", job["resource"])
# y manda a `dead` con reason=bad_resource todo lo que no matchee. Si esto se
# desincroniza, el backfill encola basura silenciosamente.
RESOURCE_RE = re.compile(r"^/orders/([A-Z0-9\-]+)$")


def log(msg: str) -> None:
    print(f"[BACKFILL] {msg}", flush=True)


def get_setting(key: str, default: str = "") -> str:
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT value FROM bridge_settings WHERE key=?", (key,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] is not None else default


def ml_token() -> str:
    try:
        with open(TOKEN_FILE) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"ERROR: no existe {TOKEN_FILE}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: {TOKEN_FILE} corrupto: {exc}")
    token = data.get("access_token")
    if not token:
        raise SystemExit(f"ERROR: {TOKEN_FILE} sin access_token")
    return token


def build_job(order_id: str, received_at: str) -> dict:
    """Arma el job con la misma forma que produce el webhook de main.py.

    NO incluye `dedupe_key` a proposito: el worker lo deriva del resource como
    `ml:{order_id}` y luego lo reemplaza por el action-aware `ml:{id}:{action}`.
    NO incluye `order_json` a proposito: que el worker haga el GET autoritativo.
    """
    return {
        "topic": "orders_v2",
        "resource": f"/orders/{order_id}",
        "received_at": received_at,
        "source": "backfill",
    }


def iter_orders(token: str, seller_id: str, date_from: str, date_to: str):
    """Pagina /orders/search. Devuelve los order id como string."""
    offset = 0
    total_reported = None

    while True:
        resp = requests.get(
            f"{API}/orders/search",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "seller": seller_id,
                "order.date_created.from": date_from,
                "order.date_created.to": date_to,
                "sort": "date_asc",
                "offset": offset,
                "limit": PAGE_SIZE,
            },
            timeout=30,
        )

        if resp.status_code == 401:
            raise SystemExit(
                "ERROR: 401 de MeLi — access_token invalido o expirado.\n"
                "  El cron /etc/cron.d/goncloud_meli_refresh lo renueva c/6h.\n"
                "  Forzar: /mnt/data/appdata/bridge/tools/meli_refresh_tokens.sh"
            )
        if resp.status_code != 200:
            raise SystemExit(
                f"ERROR: /orders/search devolvio HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        data = resp.json()
        results = data.get("results") or []
        if total_reported is None:
            total_reported = (data.get("paging") or {}).get("total")
            log(f"MeLi reporta {total_reported} ordenes en el rango")

        if not results:
            return

        for order in results:
            order_id = order.get("id")
            if order_id is not None:
                yield str(order_id)

        offset += len(results)
        if total_reported is not None and offset >= total_reported:
            return
        if offset >= MAX_ORDERS:
            log(f"AVISO: corte en MAX_ORDERS={MAX_ORDERS}. Acota el rango.")
            return


def audit(order_id: str, received_at: str, job: dict) -> None:
    """Deja rastro en inbound_events. dedupe_key propio para no pisar webhooks."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute(
            """
            INSERT OR IGNORE INTO inbound_events
            (received_at, topic, resource, user_id, payload_json, dedupe_key, status)
            VALUES (?, ?, ?, NULL, ?, ?, 'queued')
            """,
            (
                received_at,
                job["topic"],
                job["resource"],
                json.dumps(job, ensure_ascii=False),
                f"backfill:{order_id}",
            ),
        )
        con.commit()
    finally:
        con.close()


def parse_day(value: str, end_of_day: bool = False) -> str:
    """YYYY-MM-DD -> ISO con offset, como espera /orders/search."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"ERROR: fecha invalida '{value}', se espera YYYY-MM-DD")
    if end_of_day:
        day = day + timedelta(days=1) - timedelta(milliseconds=1)
    return day.strftime("%Y-%m-%dT%H:%M:%S.000-00:00")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recupera ordenes MeLi perdidas por caida de ingress"
    )
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--to", dest="date_to", default=None, help="YYYY-MM-DD (default: hoy)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="lista lo que encolaria, sin tocar Redis ni la DB",
    )
    args = parser.parse_args()

    seller_id = get_setting("meli_seller_id")
    if not seller_id:
        raise SystemExit("ERROR: falta meli_seller_id en bridge_settings")

    date_from = parse_day(args.date_from)
    date_to = parse_day(
        args.date_to or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        end_of_day=True,
    )

    log(f"seller={seller_id}  desde={date_from}  hasta={date_to}")
    if args.dry_run:
        log("DRY-RUN: no se encola nada")

    token = ml_token()
    rds = None if args.dry_run else redis.from_url(REDIS_URL, decode_responses=True)

    encoladas = 0
    rechazadas = 0

    for order_id in iter_orders(token, seller_id, date_from, date_to):
        received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        job = build_job(order_id, received_at)

        # Guardia: no encolar nada que el worker vaya a mandar a `dead`.
        if not RESOURCE_RE.match(job["resource"]):
            log(f"RECHAZADA {order_id}: resource no matchea el contrato del worker")
            rechazadas += 1
            continue

        if args.dry_run:
            log(f"encolaria {job['resource']}")
        else:
            audit(order_id, received_at, job)
            rds.rpush(QUEUE, json.dumps(job, ensure_ascii=False))
        encoladas += 1

    log(f"LISTO: {encoladas} encoladas, {rechazadas} rechazadas")
    if not args.dry_run and encoladas:
        log(f"cola {QUEUE} = {rds.llen(QUEUE)} pendientes")
        log("seguimiento: docker logs bridge-inbound-worker --tail 50 -f")
    return 0


if __name__ == "__main__":
    sys.exit(main())
