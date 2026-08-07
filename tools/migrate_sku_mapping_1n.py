#!/usr/bin/env python3
"""
Migra sku_mapping de PK (channel, sku) a PK (channel, remote_item_id, remote_variation_id).

Razon: MeLi puede separar variantes en listings independientes. Un mismo SKU puede
aparecer en N listings distintos (distintos remote_item_id). El schema viejo (PK por
sku) solo permitia 1 listing por SKU -> los demas se ignoraban en el outbound.

Hace backup de bridge.db antes de cualquier cambio.
Idempotente: si ya migrada, detecta y no hace nada.

Uso:
  python3 /data/migrate_sku_mapping_1n.py [--db /data/bridge.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone


def log(msg: str) -> None:
    print(f"[migrate_sku_mapping_1n] {msg}", flush=True)


def columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def pk_of(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    pk_cols = [(r[5], r[1]) for r in rows if r[5] > 0]
    pk_cols.sort(key=lambda x: x[0])
    return [c[1] for c in pk_cols]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/bridge.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = args.db
    if not os.path.exists(db_path):
        log(f"ERROR db_not_found={db_path}")
        return 2

    conn = sqlite3.connect(db_path)
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sku_mapping'"
        ).fetchone()
        if not existing:
            log("sku_mapping no existe -> nada que migrar")
            return 0

        current_pk = pk_of(conn, "sku_mapping")
        log(f"pk_actual={current_pk}")
        if current_pk == ["channel", "remote_item_id", "remote_variation_id"]:
            log("ya migrada -> no-op")
            return 0
        if current_pk != ["channel", "sku"]:
            log(f"pk_inesperada={current_pk} (esperaba ['channel','sku']) -> abortando")
            return 3

        cols = columns_of(conn, "sku_mapping")
        log(f"columnas_actuales={cols}")
        has_site = "site" in cols

        before = conn.execute("SELECT COUNT(*) FROM sku_mapping").fetchone()[0]
        log(f"filas_antes={before}")

        if args.dry_run:
            log("DRY RUN -> no se hacen cambios")
            return 0

        # Backup
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{db_path}.BK.{ts}"
        shutil.copy2(db_path, backup)
        log(f"backup={backup}")

        # Migration
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE sku_mapping RENAME TO sku_mapping_old")

        site_col = "site TEXT DEFAULT ''," if has_site else ""
        conn.execute(
            f"""
            CREATE TABLE sku_mapping (
                channel TEXT NOT NULL,
                sku TEXT NOT NULL,
                remote_item_id TEXT NOT NULL,
                remote_variation_id TEXT NOT NULL DEFAULT '',
                {site_col}
                last_seen_at TEXT,
                PRIMARY KEY (channel, remote_item_id, remote_variation_id)
            )
            """
        )
        conn.execute("CREATE INDEX idx_sku_mapping_sku ON sku_mapping(channel, sku)")

        copy_cols = ["channel", "sku", "remote_item_id", "remote_variation_id"]
        if has_site:
            copy_cols.append("site")
        copy_cols.append("last_seen_at")
        col_list = ", ".join(copy_cols)
        # Normalize NULL/absent remote_variation_id -> '' to satisfy NOT NULL
        conn.execute(
            f"""
            INSERT OR IGNORE INTO sku_mapping ({col_list})
            SELECT
                channel,
                sku,
                remote_item_id,
                COALESCE(remote_variation_id, '') as remote_variation_id,
                {"COALESCE(site, '') as site," if has_site else ""}
                last_seen_at
            FROM sku_mapping_old
            """
        )
        after = conn.execute("SELECT COUNT(*) FROM sku_mapping").fetchone()[0]
        log(f"filas_despues={after}")

        conn.execute("DROP TABLE sku_mapping_old")
        conn.execute("COMMIT")
        log(f"OK migracion_completa filas={after} backup={backup}")
        return 0

    except Exception as e:
        conn.execute("ROLLBACK")
        log(f"ERROR {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
