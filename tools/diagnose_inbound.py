#!/usr/bin/env python3
"""
GONCLOUD Bridge — Diagnóstico de Inbound
Muestra qué pasó con las órdenes de hoy en MeLi y Amazon.

Uso:
    python3 tools/diagnose_inbound.py
    python3 tools/diagnose_inbound.py --hours 24
    python3 tools/diagnose_inbound.py --date 2026-02-22

Apunta al DB de producción:
    BRIDGE_DB=/mnt/data/appdata/bridge/data/bridge.db python3 tools/diagnose_inbound.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

DB_PATH = os.getenv("BRIDGE_DB") or os.getenv("BRIDGE_DB_PATH") or "/data/bridge.db"

HOURS = int(os.getenv("HOURS", "24"))
for arg in sys.argv[1:]:
    if arg.startswith("--hours="):
        HOURS = int(arg.split("=")[1])
    elif arg.startswith("--hours"):
        idx = sys.argv.index("--hours")
        HOURS = int(sys.argv[idx + 1])
        break


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    return c


def sep(title="", char="─", width=72):
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n{'─' * pad} {title} {'─' * (width - pad - len(title) - 2)}")
    else:
        print("─" * width)


def banner(msg):
    print(f"\n{'═' * 72}")
    print(f"  {msg}")
    print(f"{'═' * 72}")


# ──────────────────────────────────────────────
# 1. KILL-SWITCHES
# ──────────────────────────────────────────────
def show_kill_switches(db):
    sep("KILL-SWITCHES (bridge_settings)")
    keys = [
        "meli_inbound_enabled",
        "meli_webhook_enabled",
        "meli_inbound_full_paid_enabled",
        "meli_inbound_fbm_paid_enabled",
        "meli_inbound_full_refunds_enabled",
        "meli_inbound_fbm_refunds_enabled",
        "amazon_inbound_enabled",
        "amazon_webhook_enabled",
        "amazon_inbound_fba_paid_enabled",
        "amazon_inbound_fbm_paid_enabled",
        "amazon_inbound_fba_refunds_enabled",
        "amazon_inbound_fbm_refunds_enabled",
    ]
    try:
        rows = db.execute(
            "SELECT key, value FROM bridge_settings WHERE key IN ({})".format(
                ",".join("?" * len(keys))
            ),
            keys,
        ).fetchall()
        found = {r["key"]: r["value"] for r in rows}
        for k in keys:
            v = found.get(k, "NOT SET")
            status = "✅ ON " if v == "1" else "❌ OFF" if v == "0" else f"⚠️  {v}"
            print(f"  {status}  {k}")
    except Exception as e:
        print(f"  ERROR reading settings: {e}")


# ──────────────────────────────────────────────
# 2. MELI — processed_inbound_events HOY
# ──────────────────────────────────────────────
def show_meli_today(db, since_iso: str):
    sep(f"MELI — órdenes desde {since_iso[:16]}")
    try:
        summary = db.execute(
            """
            SELECT result, COUNT(*) as cnt
            FROM processed_inbound_events
            WHERE processed_at >= ?
            GROUP BY result
            ORDER BY cnt DESC
            """,
            (since_iso,),
        ).fetchall()

        if not summary:
            print("  Sin eventos MeLi en el período.")
        else:
            total = sum(r["cnt"] for r in summary)
            print(f"  Total: {total}")
            for r in summary:
                icon = {
                    "success": "✅",
                    "manual_review": "⚠️ ",
                    "dead": "❌",
                    "skipped": "⏭️ ",
                    "error": "🔴",
                }.get(r["result"], "  ")
                print(f"    {icon} {r['result']:<20} {r['cnt']:>4}")

        # Detalle de errores/manual_review
        errors = db.execute(
            """
            SELECT dedupe_key, processed_at, result, detail_json
            FROM processed_inbound_events
            WHERE processed_at >= ?
              AND result IN ('manual_review', 'dead', 'error')
            ORDER BY processed_at DESC
            LIMIT 30
            """,
            (since_iso,),
        ).fetchall()

        if errors:
            sep("MELI — detalle errores/manual_review")
            for r in errors:
                detail = {}
                try:
                    detail = json.loads(r["detail_json"] or "{}")
                except Exception:
                    pass
                reason = detail.get("reason") or detail.get("error", "")
                ref = detail.get("ref", "")
                rc = detail.get("rc", "")
                err_tail = str(detail.get("error", ""))[:120]
                print(
                    f"  [{r['processed_at'][:19]}] {r['result']:<14} key={r['dedupe_key'][:40]}"
                )
                if ref:
                    print(f"    ref={ref}")
                if reason:
                    print(f"    reason={reason}")
                if rc != "":
                    print(f"    rc={rc}")
                if err_tail and err_tail != reason:
                    print(f"    error={err_tail[:100]}")
    except Exception as e:
        print(f"  ERROR: {e}")


# ──────────────────────────────────────────────
# 3. AMAZON — amazon_processed_events HOY
# ──────────────────────────────────────────────
def show_amazon_today(db, since_iso: str):
    sep(f"AMAZON — órdenes desde {since_iso[:16]}")
    try:
        summary = db.execute(
            """
            SELECT result, COUNT(*) as cnt
            FROM amazon_processed_events
            WHERE processed_at >= ?
            GROUP BY result
            ORDER BY cnt DESC
            """,
            (since_iso,),
        ).fetchall()

        if not summary:
            print("  Sin eventos Amazon en el período.")
        else:
            total = sum(r["cnt"] for r in summary)
            print(f"  Total: {total}")
            for r in summary:
                icon = {
                    "success": "✅",
                    "manual_review": "⚠️ ",
                    "dead": "❌",
                    "skipped": "⏭️ ",
                    "deferred": "⏳",
                    "error": "🔴",
                }.get(r["result"], "  ")
                print(f"    {icon} {r['result']:<20} {r['cnt']:>4}")

        errors = db.execute(
            """
            SELECT dedupe_key, processed_at, result, detail_json
            FROM amazon_processed_events
            WHERE processed_at >= ?
              AND result IN ('manual_review', 'dead', 'error', 'deferred')
            ORDER BY processed_at DESC
            LIMIT 30
            """,
            (since_iso,),
        ).fetchall()

        if errors:
            sep("AMAZON — detalle errores")
            for r in errors:
                detail = {}
                try:
                    detail = json.loads(r["detail_json"] or "{}")
                except Exception:
                    pass
                reason = detail.get("reason", "")
                err_tail = str(detail.get("error", ""))[:120]
                print(
                    f"  [{r['processed_at'][:19]}] {r['result']:<14} key={r['dedupe_key'][:50]}"
                )
                if reason:
                    print(f"    reason={reason}")
                if err_tail and err_tail != reason:
                    print(f"    error={err_tail[:100]}")
    except Exception as e:
        print(f"  ERROR: {e}")


# ──────────────────────────────────────────────
# 4. REDIS QUEUE STATUS (opcional)
# ──────────────────────────────────────────────
def show_redis_status():
    sep("REDIS — estado de colas")
    try:
        import redis as _redis

        REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
        r = _redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        for q in [
            "ml_orders_jobs",
            "ml_orders_processing",
            "ml_orders_dead",
            "amazon_orders_jobs",
            "amazon_orders_dead",
        ]:
            n = r.llen(q)
            icon = "⚠️ " if n > 0 else "  "
            print(f"  {icon} {q:<35} {n:>4} items")
    except ImportError:
        print("  redis no disponible, saltando")
    except Exception as e:
        print(f"  ERROR Redis: {e}")


# ──────────────────────────────────────────────
# 5. LOCKS ACTIVOS
# ──────────────────────────────────────────────
def show_locks(db):
    sep("LOCKS ACTIVOS — inbound_processing_locks")
    try:
        locks = db.execute(
            "SELECT dedupe_key, claimed_at, worker_id, heartbeat_at FROM inbound_processing_locks ORDER BY claimed_at"
        ).fetchall()
        if not locks:
            print("  Sin locks activos.")
        else:
            now = datetime.now(timezone.utc)
            for lk in locks:
                age_s = "?"
                try:
                    claimed = datetime.fromisoformat(
                        lk["claimed_at"].replace("Z", "+00:00")
                    )
                    age_s = int((now - claimed).total_seconds())
                except Exception:
                    pass
                print(
                    f"  {lk['dedupe_key'][:50]}  age={age_s}s  worker={lk['worker_id'][:30]}"
                )
    except Exception as e:
        print(f"  ERROR: {e}")

    sep("LOCKS ACTIVOS — amazon_processing_locks")
    try:
        locks = db.execute(
            "SELECT dedupe_key, claimed_at, worker_id FROM amazon_processing_locks ORDER BY claimed_at"
        ).fetchall()
        if not locks:
            print("  Sin locks activos.")
        else:
            now = datetime.now(timezone.utc)
            for lk in locks:
                age_s = "?"
                try:
                    claimed = datetime.fromisoformat(
                        lk["claimed_at"].replace("Z", "+00:00")
                    )
                    age_s = int((now - claimed).total_seconds())
                except Exception:
                    pass
                print(f"  {lk['dedupe_key'][:50]}  age={age_s}s")
    except Exception as e:
        print(f"  ERROR: {e}")


# ──────────────────────────────────────────────
# 6. INBOUND EVENTS (webhooks recibidos hoy)
# ──────────────────────────────────────────────
def show_inbound_events(db, since_iso: str):
    sep(f"WEBHOOKS RECIBIDOS — inbound_events desde {since_iso[:16]}")
    try:
        rows = db.execute(
            """
            SELECT status, COUNT(*) as cnt
            FROM inbound_events
            WHERE received_at >= ?
            GROUP BY status
            """,
            (since_iso,),
        ).fetchall()
        if not rows:
            print("  Sin webhooks registrados.")
        else:
            for r in rows:
                print(f"    {r['status']:<20} {r['cnt']:>4}")
    except Exception as e:
        print(f"  ERROR (tabla puede no existir): {e}")

    sep(f"WEBHOOKS AMAZON — amazon_inbound_events desde {since_iso[:16]}")
    try:
        rows = db.execute(
            """
            SELECT status, COUNT(*) as cnt
            FROM amazon_inbound_events
            WHERE received_at >= ?
            GROUP BY status
            """,
            (since_iso,),
        ).fetchall()
        if not rows:
            print("  Sin webhooks Amazon registrados.")
        else:
            for r in rows:
                print(f"    {r['status']:<20} {r['cnt']:>4}")
    except Exception as e:
        print(f"  ERROR (tabla puede no existir): {e}")


# ──────────────────────────────────────────────
# 7. MÉTRICAS HORARIAS
# ──────────────────────────────────────────────
def show_metrics(db):
    sep("MÉTRICAS HORARIAS — MeLi (inbound_metrics)")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        rows = db.execute(
            "SELECT hour, processed, manual_review, dead, errors, retries FROM inbound_metrics WHERE hour LIKE ? ORDER BY hour",
            (f"{today}%",),
        ).fetchall()
        if not rows:
            print("  Sin métricas para hoy.")
        else:
            print(
                f"  {'Hora':<16} {'OK':>5} {'manual':>7} {'dead':>6} {'err':>5} {'retry':>6}"
            )
            for r in rows:
                print(
                    f"  {r['hour']:<16} {r['processed'] or 0:>5} {r['manual_review'] or 0:>7} {r['dead'] or 0:>6} {r['errors'] or 0:>5} {r['retries'] or 0:>6}"
                )
    except Exception as e:
        print(f"  ERROR: {e}")

    sep("MÉTRICAS HORARIAS — Amazon (amazon_metrics)")
    try:
        rows = db.execute(
            "SELECT hour, processed, manual_review, dead, errors, deferred, skipped FROM amazon_metrics WHERE hour LIKE ? ORDER BY hour",
            (f"{today}%",),
        ).fetchall()
        if not rows:
            print("  Sin métricas Amazon para hoy.")
        else:
            print(
                f"  {'Hora':<16} {'OK':>5} {'manual':>7} {'dead':>6} {'err':>5} {'defer':>6} {'skip':>5}"
            )
            for r in rows:
                print(
                    f"  {r['hour']:<16} {r['processed'] or 0:>5} {r['manual_review'] or 0:>7} {r['dead'] or 0:>6} {r['errors'] or 0:>5} {r['deferred'] or 0:>6} {r['skipped'] or 0:>5}"
                )
    except Exception as e:
        print(f"  ERROR: {e}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    banner(
        f"GONCLOUD Bridge — Diagnóstico Inbound  ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    )
    print(f"  DB: {DB_PATH}")
    print(f"  Ventana: últimas {HOURS}h")

    since = (datetime.now(timezone.utc) - timedelta(hours=HOURS)).isoformat()

    db = conn()
    try:
        show_kill_switches(db)
        show_redis_status()
        show_inbound_events(db, since)
        show_metrics(db)
        show_meli_today(db, since)
        show_amazon_today(db, since)
        show_locks(db)
    finally:
        db.close()

    sep()
    print("\nPARA RECUPERAR ÓRDENES BLOQUEADAS:")
    print("  python3 tools/recover_manual_review.py --dry-run")
    print("  python3 tools/recover_manual_review.py  # re-encola")
    sep()


if __name__ == "__main__":
    main()
