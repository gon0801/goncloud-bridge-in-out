#!/usr/bin/env python3
import json
import os
import sqlite3
from datetime import datetime, timezone

import redis

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
QUEUE = os.getenv("QUEUE_NAME", "stock_jobs")
CHANNEL = "meli"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pick_last_consistent_event(conn) -> int | None:
    """
    Elige el último evento 'meli' que sea consistente:
    - item_count > 0
    - snapshot_items count == item_count
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.id, e.item_count
        FROM events e
        WHERE e.channel=?
        ORDER BY e.id DESC
        LIMIT 50
    """,
        (CHANNEL,),
    )
    candidates = cur.fetchall()

    for eid, item_count in candidates:
        if not item_count or int(item_count) <= 0:
            continue
        cnt = cur.execute(
            "SELECT COUNT(*) FROM snapshot_items WHERE channel=? AND event_id=?",
            (CHANNEL, str(eid)),
        ).fetchone()[0]
        if int(cnt) == int(item_count):
            return int(eid)
    return None


def main():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    event_id = pick_last_consistent_event(conn)
    if not event_id:
        conn.close()
        print(
            json.dumps(
                {"SNAPSHOT_ENQUEUED": False, "error": "no_consistent_events_meli"},
                ensure_ascii=False,
            )
        )
        return

    cur = conn.cursor()
    cur.execute(
        """
        SELECT sku, qty
        FROM snapshot_items
        WHERE channel=? AND event_id=?
        ORDER BY sku
    """,
        (CHANNEL, str(event_id)),
    )
    items = cur.fetchall()
    conn.close()

    pushed = 0
    created_at = utc_now_iso()

    for sku, qty in items:
        sku = (sku or "").strip()
        if not sku:
            continue
        job = {
            "event_id": f"snap-{event_id}-{sku}",  # <- SIEMPRE ESTE FORMATO
            "channel": CHANNEL,
            "sku": sku,
            "qty": int(qty),
            "created_at": created_at,
        }
        r.rpush(QUEUE, json.dumps(job, ensure_ascii=False))
        pushed += 1

    print(
        json.dumps(
            {
                "SNAPSHOT_ENQUEUED": True,
                "channel": CHANNEL,
                "snapshot_event_id": str(event_id),
                "items": len(items),
                "pushed": pushed,
                "queue": QUEUE,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
