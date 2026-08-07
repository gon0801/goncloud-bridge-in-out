import os
import json
import sqlite3
import redis

DB = os.getenv("BRIDGE_DB", os.getenv("BRIDGE_SQLITE_PATH", "/data/bridge.db"))
Q = os.getenv("QUEUE_NAME", "stock_jobs")
R = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
CH = os.getenv("SNAPSHOT_CHANNEL", "amazon_fbm")

con = sqlite3.connect(DB)
cur = con.cursor()
row = cur.execute(
    "SELECT event_id FROM snapshot_items WHERE channel=? ORDER BY created_at DESC LIMIT 1",
    (CH,),
).fetchone()
if not row:
    print("NO_SNAPSHOT", {"channel": CH})
    raise SystemExit(0)
eid = str(row[0])
items = cur.execute(
    "SELECT sku, qty FROM snapshot_items WHERE channel=? AND event_id=? ORDER BY sku",
    (CH, eid),
).fetchall()

r = redis.Redis.from_url(R, decode_responses=True)
n = 0
for sku, qty in items:
    job = {"channel": CH, "sku": sku, "qty": int(qty), "event_id": f"snap-{eid}-{sku}"}
    r.lpush(Q, json.dumps(job))
    n += 1

print(
    "SNAPSHOT_ENQUEUED",
    {
        "channel": CH,
        "snapshot_event_id": eid,
        "items": len(items),
        "pushed": n,
        "queue": Q,
    },
)
