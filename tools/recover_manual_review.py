#!/usr/bin/env python3
"""
GONCLOUD Bridge — Recuperación de órdenes bloqueadas en manual_review

Busca órdenes MeLi y Amazon que quedaron en manual_review/dead en las últimas N horas
y las re-encola en Redis para que el worker las reintente.

IMPORTANTE: Solo re-encola si el job original tiene payload persistido.
Para MeLi webhook-based (sin payload), se necesita re-notificar manualmente.

Uso:
    # Ver qué hay bloqueado sin re-encolar:
    python3 tools/recover_manual_review.py --dry-run

    # Re-encolar MeLi manual_review de las últimas 24h:
    python3 tools/recover_manual_review.py --channel meli --hours 24

    # Re-encolar Amazon manual_review y dead:
    python3 tools/recover_manual_review.py --channel amazon --include-dead

    # Re-encolar todo:
    python3 tools/recover_manual_review.py --channel all

    BRIDGE_DB=/mnt/data/appdata/bridge/data/bridge.db \\
    REDIS_URL=redis://bridge-redis:6379/0 \\
    python3 tools/recover_manual_review.py --dry-run
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

DB_PATH = (
    os.getenv("BRIDGE_DB")
    or os.getenv("BRIDGE_DB_PATH")
    or "/data/bridge.db"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    return c


def get_redis():
    import redis as _redis
    r = _redis.Redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    return r


# ─────────────────────────────────────────────────────
# MELI recovery
# ─────────────────────────────────────────────────────
def recover_meli(db, r, since_iso: str, include_dead: bool, dry_run: bool) -> int:
    """
    Re-encola órdenes MeLi bloqueadas.

    Flujo:
    1. Busca en processed_inbound_events: result IN ('manual_review', 'dead')
    2. Para cada una, busca payload en inbound_job_payloads
    3. Si tiene payload → re-encola en ml_orders_jobs y borra el audit record
       para que el worker la procese fresca
    4. Si NO tiene payload → muestra advertencia (webhooks sin payload guardado)
    """
    results = ["manual_review"]
    if include_dead:
        results.append("dead")
    placeholders = ",".join("?" * len(results))

    rows = db.execute(
        f"""
        SELECT dedupe_key, processed_at, result, detail_json
        FROM processed_inbound_events
        WHERE processed_at >= ?
          AND result IN ({placeholders})
        ORDER BY processed_at
        """,
        (since_iso, *results),
    ).fetchall()

    if not rows:
        print("[meli] Sin órdenes bloqueadas en el período.")
        return 0

    print(f"\n[meli] Encontradas {len(rows)} órdenes bloqueadas:")
    recovered = 0
    no_payload = 0

    for row in rows:
        dk = row["dedupe_key"]
        result = row["result"]
        detail = {}
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except Exception:
            pass

        reason = detail.get("reason") or detail.get("error", "")
        ref = detail.get("ref", "")
        print(f"\n  {dk[:55]}")
        print(f"    result={result}  ref={ref}  reason={reason[:80]}")

        # Buscar payload
        payload_row = db.execute(
            "SELECT payload_json FROM inbound_job_payloads WHERE dedupe_key=?",
            (dk,),
        ).fetchone()

        if not payload_row:
            print(f"    ⚠️  Sin payload persistido — no se puede re-encolar automáticamente")
            print(f"       Acción: re-enviar webhook desde MeLi o re-notificar manualmente")
            no_payload += 1
            continue

        job = json.loads(payload_row["payload_json"])
        job["_recovered"] = True
        job["_recovered_at"] = datetime.now(timezone.utc).isoformat()
        job["_recovered_from"] = result
        # Resetear contadores de retry para que no se bloquee inmediatamente
        job.pop("_retry_count", None)
        job.pop("_retry_delay", None)
        job.pop("_reap_count", None)

        if dry_run:
            print(f"    [DRY-RUN] Se re-encolaría en ml_orders_jobs")
        else:
            # Borrar el registro de audit para que is_already_completed() pase
            db.execute("DELETE FROM processed_inbound_events WHERE dedupe_key=?", (dk,))
            # Borrar el lock si existe
            db.execute("DELETE FROM inbound_processing_locks WHERE dedupe_key=?", (dk,))
            db.execute("COMMIT")
            # Re-encolar
            r.rpush("ml_orders_jobs", json.dumps(job, ensure_ascii=False))
            print(f"    ✅ Re-encolada en ml_orders_jobs")

        recovered += 1

    print(f"\n[meli] Resumen: {recovered} re-encoladas, {no_payload} sin payload (requieren acción manual)")
    return recovered


# ─────────────────────────────────────────────────────
# AMAZON recovery
# ─────────────────────────────────────────────────────
def recover_amazon(db, r, since_iso: str, include_dead: bool, dry_run: bool) -> int:
    """
    Re-encola órdenes Amazon bloqueadas.
    Amazon tiene payload persistido en amazon_job_payloads, así que siempre hay payload.
    """
    results = ["manual_review"]
    if include_dead:
        results.append("dead")
    placeholders = ",".join("?" * len(results))

    rows = db.execute(
        f"""
        SELECT dedupe_key, processed_at, result, detail_json
        FROM amazon_processed_events
        WHERE processed_at >= ?
          AND result IN ({placeholders})
        ORDER BY processed_at
        """,
        (since_iso, *results),
    ).fetchall()

    if not rows:
        print("[amazon] Sin órdenes bloqueadas en el período.")
        return 0

    print(f"\n[amazon] Encontradas {len(rows)} órdenes bloqueadas:")
    recovered = 0
    no_payload = 0

    for row in rows:
        dk = row["dedupe_key"]
        result = row["result"]
        detail = {}
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except Exception:
            pass

        reason = detail.get("reason", "")
        print(f"\n  {dk[:60]}")
        print(f"    result={result}  reason={reason[:80]}")

        payload_row = db.execute(
            "SELECT payload_json FROM amazon_job_payloads WHERE dedupe_key=?",
            (dk,),
        ).fetchone()

        if not payload_row:
            print(f"    ⚠️  Sin payload — probablemente expirado (7 días TTL)")
            no_payload += 1
            continue

        job = json.loads(payload_row["payload_json"])
        job["_recovered"] = True
        job["_recovered_at"] = datetime.now(timezone.utc).isoformat()
        job["_recovered_from"] = result
        job.pop("_retry_count", None)
        job.pop("_lock_busy", None)
        job.pop("_deferred_count", None)
        job.pop("_reap_count", None)

        if dry_run:
            order_id = (job.get("order_json") or {}).get("AmazonOrderId", "?")
            status = (job.get("order_json") or {}).get("OrderStatus", "?")
            print(f"    [DRY-RUN] Se re-encolaría  order_id={order_id}  status={status}")
        else:
            db.execute("DELETE FROM amazon_processed_events WHERE dedupe_key=?", (dk,))
            db.execute("DELETE FROM amazon_processing_locks WHERE dedupe_key=?", (dk,))
            db.execute("COMMIT")
            r.rpush("amazon_orders_jobs", json.dumps(job, ensure_ascii=False))
            print(f"    ✅ Re-encolada en amazon_orders_jobs")

        recovered += 1

    print(f"\n[amazon] Resumen: {recovered} re-encoladas, {no_payload} sin payload")
    return recovered


# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Re-encola órdenes bloqueadas en manual_review/dead"
    )
    parser.add_argument(
        "--channel", choices=["meli", "amazon", "all"], default="all",
        help="Canal a recuperar (default: all)",
    )
    parser.add_argument(
        "--hours", type=int, default=24,
        help="Buscar órdenes bloqueadas en las últimas N horas (default: 24)",
    )
    parser.add_argument(
        "--include-dead", action="store_true",
        help="Incluir también órdenes en estado 'dead'",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Solo mostrar qué se haría, sin modificar nada",
    )
    args = parser.parse_args()

    since_iso = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

    print(f"{'='*60}")
    print(f"  GONCLOUD — Recuperación de órdenes bloqueadas")
    print(f"  DB:    {DB_PATH}")
    print(f"  Desde: {since_iso[:19]}")
    print(f"  Canal: {args.channel}  |  dry_run={args.dry_run}")
    print(f"{'='*60}")

    db = conn()

    # Atomicidad POR ORDEN: recover_meli()/recover_amazon() hacen COMMIT por
    # cada orden recuperada (DELETE audit + DELETE lock). Es lo correcto para
    # una tool de recuperación: el progreso parcial es durable y re-ejecutar
    # es idempotente. NO envolver en un BEGIN/COMMIT externo — chocaba con los
    # COMMIT por-fila y hacía fallar el cierre con "cannot commit - no
    # transaction is active" (incidente 2026-05-19).
    try:
        if not args.dry_run:
            r = get_redis()
        else:
            r = None

        total = 0
        if args.channel in ("meli", "all"):
            total += recover_meli(db, r, since_iso, args.include_dead, args.dry_run)

        if args.channel in ("amazon", "all"):
            total += recover_amazon(db, r, since_iso, args.include_dead, args.dry_run)

        print(f"\n{'='*60}")
        if args.dry_run:
            print(f"  [DRY-RUN] {total} órdenes serían re-encoladas.")
            print(f"  Ejecuta sin --dry-run para aplicar.")
        else:
            print(f"  ✅ {total} órdenes re-encoladas exitosamente.")
        print(f"{'='*60}\n")

    except Exception as e:
        # Sin ROLLBACK externo: cada orden ya commiteó por su cuenta. Un
        # ROLLBACK acá fallaba con "no transaction is active" y enmascaraba
        # el error real.
        print(f"\nERROR: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
