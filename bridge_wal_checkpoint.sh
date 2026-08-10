#!/bin/bash
# WAL checkpoint diario para bridge.db.
# Sin esto, el archivo bridge.db-wal puede crecer indefinidamente (visto 3.7GB).
set -e

DB="/mnt/data/appdata/bridge/data/bridge.db"
WAL="${DB}-wal"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

before=0
[ -f "$WAL" ] && before=$(stat -c %s "$WAL")

result=$(sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);")

after=0
[ -f "$WAL" ] && after=$(stat -c %s "$WAL")

echo "$TS wal_checkpoint busy|log|checkpointed=$result wal_bytes_before=$before wal_bytes_after=$after"
