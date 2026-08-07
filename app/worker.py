#!/usr/bin/env python3
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
import time

import redis

# =========================
# CONFIG
# =========================
REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
QUEUE = os.getenv("QUEUE_NAME", "stock_jobs")
DB_PATH = os.getenv("BRIDGE_DB", os.getenv("BRIDGE_SQLITE_PATH", "/data/bridge.db"))

# Tokens MELI (host: /mnt/data/appdata/bridge/data/.meli_tokens.json, contenedor: /data/.meli_tokens.json)
MELI_TOKENS_PATH = os.getenv("MELI_TOKENS_PATH", "/data/.meli_tokens.json")

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# SKU regex default (conservador, sin espacios; permite A-Z 0-9 y guión)
DEFAULT_SKU_REGEX = r"^[A-Z0-9][A-Z0-9\-]{0,63}$"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_conn():
    return sqlite3.connect(DB_PATH, timeout=10)


def sql_init_conn(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")


def setting_get(conn, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM bridge_settings WHERE key=?", (key,)
    ).fetchone()
    if not row or row[0] is None:
        return default
    return str(row[0])


def setting_is_true(conn, key: str, default: str = "0") -> bool:
    val = (setting_get(conn, key, default) or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def is_channel_enabled(conn, channel: str) -> bool:
    ch = (channel or "").strip().lower()
    if ch == "meli":
        k = "meli_enabled"
    elif ch == "amazon_fbm":
        k = "amazon_fbm_enabled"
    else:
        return False
    return setting_is_true(conn, k, "0")


def metric_upsert(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO bridge_metrics(key,value,updated_at)
        VALUES(?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
        """,
        (key, str(value)),
    )


def bump_metric(key: str, delta: int = 1) -> None:
    with closing(db_conn()) as conn:
        sql_init_conn(conn)
        conn.execute(
            """
            INSERT INTO bridge_metrics(key,value,updated_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
              value = CAST(bridge_metrics.value AS INTEGER) + ?,
              updated_at = datetime('now')
            """,
            (key, str(int(delta)), int(delta)),
        )
        conn.commit()


def set_metric(key: str, value: str) -> None:
    with closing(db_conn()) as conn:
        sql_init_conn(conn)
        metric_upsert(conn, key, str(value))
        conn.commit()


def record_rejected_sku(event_id: str, channel: str, sku: str, reason: str) -> None:
    """
    Guarda el rechazo en rejected_skus si existe la tabla.
    NO rompe el worker si la tabla no existe.
    """
    with closing(db_conn()) as conn:
        sql_init_conn(conn)
        try:
            conn.execute(
                """
                INSERT INTO rejected_skus(created_at, event_id, channel, sku, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    str(event_id),
                    str(channel),
                    str(sku),
                    str(reason)[:200],
                ),
            )
            conn.commit()
        except sqlite3.OperationalError:
            # tabla no existe o columnas no matchean -> no truena el worker
            pass


def get_sku_regex(conn, channel: str) -> str:
    """
    Permite configurar regex por canal en bridge_settings:
      sku_regex_meli
      sku_regex_amazon_fbm
    Si no existe, usa DEFAULT_SKU_REGEX.
    """
    ch = (channel or "").strip().lower()
    if ch == "meli":
        k = "sku_regex_meli"
    elif ch == "amazon_fbm":
        k = "sku_regex_amazon_fbm"
    else:
        return DEFAULT_SKU_REGEX

    rx = (setting_get(conn, k, DEFAULT_SKU_REGEX) or "").strip()
    return rx or DEFAULT_SKU_REGEX


def validate_sku(conn, channel: str, sku: str) -> tuple[bool, str]:
    """
    Devuelve (ok, reason).
    """
    s = (sku or "").strip()
    if not s:
        return (False, "empty_sku")

    # no espacios / tabs / saltos
    if any(c.isspace() for c in s):
        return (False, "whitespace_not_allowed")

    rx = get_sku_regex(conn, channel)
    try:
        if not re.match(rx, s):
            return (False, "regex_mismatch")
    except re.error:
        # regex mal configurado -> usa default
        if not re.match(DEFAULT_SKU_REGEX, s):
            return (False, "regex_mismatch_default")

    return (True, "ok")


def normalize_or_block_event_id(event_id, sku: str) -> tuple[str | None, str | None]:
    """
    Regla dura:
    - event_id debe ser 'snap-<EVENT>-<SKU>'
    - Si llega numérico (ej '1139'), lo normalizamos a 'snap-1139-<sku>'
    - Si llega algo raro (no empieza con snap-), lo bloqueamos.
    Regresa: (normalized_event_id_or_none, block_reason_or_none)
    """
    if event_id is None:
        return None, "missing_event_id"

    eid = str(event_id).strip()
    if not eid:
        return None, "missing_event_id"

    if eid.isdigit():
        # bug viejo: venía event_id numérico
        s = (sku or "").strip()
        return f"snap-{eid}-{s}", None

    if not eid.startswith("snap-"):
        return eid, f"bad_event_id:{eid[:80]}"

    return eid, None


def claim_event_idempotent(event_id: str, channel: str) -> bool:
    """
    Idempotencia: insert "claimed". Si ya existe, es duplicado.
    NOTA: NO usa created_at (tu tabla no tiene esa columna).
    """
    if not event_id:
        print(f"[{utc_now_iso()}] WARN: missing event_id; idempotency disabled")
        return True

    with closing(db_conn()) as conn:
        sql_init_conn(conn)
        try:
            conn.execute(
                "INSERT INTO processed_events(event_id, channel, status) VALUES(?, ?, ?)",
                (str(event_id), str(channel or "unknown"), "claimed"),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def mark_event_done(event_id: str, status: str, details: str = "") -> None:
    """
    Marca el event como ok/error y setea processed_at.
    """
    if not event_id:
        return
    with closing(db_conn()) as conn:
        sql_init_conn(conn)
        conn.execute(
            "UPDATE processed_events SET processed_at=datetime('now'), status=?, details=? WHERE event_id=?",
            (str(status), (details or "")[:500], str(event_id)),
        )
        conn.commit()


def _event_row_id_from_snap_event_id(event_id: str):
    """event_id = snap-<ROWID>-<SKU> → devuelve ROWID int o None."""
    if not event_id:
        return None
    parts = str(event_id).split("-", 2)
    if len(parts) < 3 or parts[0] != "snap" or not parts[1].isdigit():
        return None
    try:
        return int(parts[1])
    except Exception:
        return None


def _is_complete_snapshot_event(conn, event_id: str) -> bool:
    """Devuelve True si el snapshot tiene complete=true en su payload."""
    rid = _event_row_id_from_snap_event_id(event_id)
    if rid is None:
        return False
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE id=? LIMIT 1", (rid,)
        ).fetchone()
        if not row or not row[0]:
            return False
        import json as _json

        return bool(_json.loads(row[0]).get("complete") is True)
    except Exception:
        return False


def meli_get_token() -> str:
    with open(MELI_TOKENS_PATH, "r") as f:
        j = json.load(f)
    tok = j.get("access_token")
    if not tok:
        raise RuntimeError("MELI_TOKEN_MISSING: access_token not found")
    return tok


def meli_api_json(url: str, tok: str, method="GET", body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + tok,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), json.load(resp)
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} {e.reason} :: {err[:500]}")


def meli_set_qty_from_mapping(sku: str, qty: int) -> str:
    # Lookup TODOS los listings mapeados a este SKU.
    # (Un mismo SKU puede estar en N listings tras separacion de variantes en MeLi).
    with closing(db_conn()) as conn:
        sql_init_conn(conn)
        rows = conn.execute(
            "SELECT remote_item_id, remote_variation_id FROM sku_mapping WHERE channel='meli' AND sku=?",
            (str(sku),),
        ).fetchall()

    if not rows:
        return f"meli_skip_no_mapping sku={sku}"

    tok = meli_get_token()
    qty_int = int(qty)
    results: list[str] = []
    ok_count = 0
    err_count = 0

    for item_id, var_id in rows:
        try:
            if var_id:
                url = (
                    f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}"
                )
                code_put, resp_put = meli_api_json(
                    url, tok, method="PUT", body={"available_quantity": qty_int}
                )
                qty_put_echo = None
                if isinstance(resp_put, list) and resp_put:
                    qty_put_echo = resp_put[0].get("available_quantity")
                elif isinstance(resp_put, dict):
                    qty_put_echo = resp_put.get("available_quantity")
                code_get, resp_get = meli_api_json(url, tok, method="GET")
                qty_real = (
                    resp_get.get("available_quantity")
                    if isinstance(resp_get, dict)
                    else None
                )
                results.append(
                    f"var[{item_id}:{var_id}] put={code_put} get={code_get} "
                    f"echo={qty_put_echo} real={qty_real}"
                )
            else:
                url = f"https://api.mercadolibre.com/items/{item_id}"
                code_put, resp_put = meli_api_json(
                    url, tok, method="PUT", body={"available_quantity": qty_int}
                )
                qty_put_echo = (
                    resp_put.get("available_quantity")
                    if isinstance(resp_put, dict)
                    else None
                )
                code_get, resp_get = meli_api_json(url, tok, method="GET")
                qty_real = (
                    resp_get.get("available_quantity")
                    if isinstance(resp_get, dict)
                    else None
                )
                results.append(
                    f"item[{item_id}] put={code_put} get={code_get} "
                    f"echo={qty_put_echo} real={qty_real}"
                )
            ok_count += 1
        except Exception as e:
            err_count += 1
            results.append(
                f"FAIL[{item_id}:{var_id or '-'}] {type(e).__name__}: {str(e)[:120]}"
            )

    summary = f"meli_multi_put sku={sku} listings={len(rows)} ok={ok_count} err={err_count} qty={qty_int}"
    return summary + " | " + " | ".join(results)


# =========================
# AMAZON SP-API STOCK SYNC
# =========================


def amazon_get_credentials():
    """Lee credenciales Amazon de bridge_settings."""
    with closing(db_conn()) as conn:
        sql_init_conn(conn)

        def get(k):
            row = conn.execute(
                "SELECT value FROM bridge_settings WHERE key=?", (k,)
            ).fetchone()
            return row[0] if row else ""

        return {
            "refresh_token": get("amazon_sp_api_refresh_token"),
            "client_id": get("amazon_sp_api_client_id"),
            "client_secret": get("amazon_sp_api_client_secret"),
            "marketplace": get("amazon_marketplace_id") or "A1AM78C64UM0Y8",
            "seller_id": get("amazon_seller_id"),
        }


def amazon_set_qty(sku: str, qty: int) -> str:
    """Actualiza inventario en Amazon via SP-API directa."""
    creds = amazon_get_credentials()
    if not creds["refresh_token"] or not creds["seller_id"]:
        return "amazon_skip_no_credentials"
    with closing(db_conn()) as conn:
        sql_init_conn(conn)
        row = conn.execute(
            "SELECT seller_sku FROM amazon_sku_mapping WHERE odoo_default_code=?",
            (sku,),
        ).fetchone()
        if not row:
            return f"amazon_skip_no_mapping odoo_sku={sku}"
        amazon_sku = row[0]
    try:
        token_data = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            }
        ).encode()
        req = urllib.request.Request(
            "https://api.amazon.com/auth/o2/token", data=token_data
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            access_token = json.loads(resp.read().decode())["access_token"]
        url = f"https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/items/{creds['seller_id']}/{urllib.parse.quote(amazon_sku, safe='')}?marketplaceIds={creds['marketplace']}"
        body = json.dumps(
            {
                "productType": "PRODUCT",
                "patches": [
                    {
                        "op": "replace",
                        "path": "/attributes/fulfillment_availability",
                        "value": [
                            {
                                "fulfillment_channel_code": "DEFAULT",
                                "quantity": int(qty),
                            }
                        ],
                    }
                ],
            }
        ).encode()
        req = urllib.request.Request(url, data=body, method="PATCH")
        req.add_header("x-amz-access-token", access_token)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        return f"amazon_put_ok odoo_sku={sku} amazon_sku={amazon_sku} qty={qty} status={result.get('status')}"
    except urllib.error.HTTPError as e:
        return f"amazon_put_error sku={sku} http={e.code}"
    except Exception as e:
        return f"amazon_put_error sku={sku} error={e}"


def main():
    print(f"[{utc_now_iso()}] worker started. queue={QUEUE}")

    while True:
        try:
            item = r.blpop(QUEUE, timeout=10)
        except (redis.exceptions.BusyLoadingError, redis.exceptions.ConnectionError):
            # Redis está cargando dataset o reinició: no morir, solo esperar y reintentar
            time.sleep(2)
            continue

        if not item:
            continue

        _, payload = item
        try:
            job = json.loads(payload)
        except Exception:
            print(f"[{utc_now_iso()}] invalid job payload: {payload!r}")
            continue

        channel = (job.get("channel") or "").strip()
        sku = job.get("sku")
        qty = job.get("qty")
        raw_event_id = job.get("event_id")

        # =========================
        # HARDEN event_id
        # =========================
        event_id, bad_event_reason = normalize_or_block_event_id(
            raw_event_id, str(sku or "")
        )
        if bad_event_reason:
            # si no hay event_id válido, no intentamos idempotencia (no ensuciamos)
            print(
                f"[{utc_now_iso()}] job(blocked): reason={bad_event_reason} "
                f"channel={channel} sku={sku} qty={qty} event_id={raw_event_id}"
            )
            bump_metric("events_blocked_total", 1)
            bump_metric(f"events_blocked_total_{channel}", 1)
            bump_metric("events_processed_ok_total", 1)
            set_metric("last_success_at", utc_now_iso())
            continue

        # Idempotencia (ya con event_id normalizado)
        if not claim_event_idempotent(str(event_id), channel):
            print(
                f"[{utc_now_iso()}] skip duplicate: channel={channel} sku={sku} qty={qty} event_id={event_id}"
            )
            continue

        try:
            with closing(db_conn()) as conn:
                sql_init_conn(conn)

                dummy = setting_is_true(conn, "dummy_mode", "0")
                enabled = is_channel_enabled(conn, channel)
                meli_adapter_enabled = setting_is_true(
                    conn, "meli_adapter_enabled", "0"
                )

                # 1) dummy_mode
                if dummy:
                    reason = "dummy_mode_on"
                    mark_event_done(str(event_id), "ok", f"blocked:{reason}")
                    bump_metric("events_blocked_total", 1)
                    bump_metric(f"events_blocked_total_{channel}", 1)
                    bump_metric("events_processed_ok_total", 1)
                    set_metric("last_success_at", utc_now_iso())
                    print(
                        f"[{utc_now_iso()}] job(blocked): reason={reason} "
                        f"channel={channel} sku={sku} qty={qty} event_id={event_id}"
                    )
                    continue

                # 2) channel disabled
                if not enabled:
                    reason = "channel_disabled"
                    mark_event_done(str(event_id), "ok", f"blocked:{reason}")
                    bump_metric("events_blocked_total", 1)
                    bump_metric(f"events_blocked_total_{channel}", 1)
                    bump_metric("events_processed_ok_total", 1)
                    set_metric("last_success_at", utc_now_iso())
                    print(
                        f"[{utc_now_iso()}] job(blocked): reason={reason} "
                        f"channel={channel} sku={sku} qty={qty} event_id={event_id}"
                    )
                    continue

                # 3) SKU hardening (rechazo)
                ok_sku, reason = validate_sku(conn, channel, str(sku))
                if not ok_sku:
                    mark_event_done(str(event_id), "ok", f"blocked:{reason}")
                    record_rejected_sku(str(event_id), channel, str(sku), reason)
                    bump_metric("skus_rejected_total", 1)
                    bump_metric(f"skus_rejected_total_{channel}", 1)
                    bump_metric("events_processed_ok_total", 1)
                    set_metric("last_success_at", utc_now_iso())
                    print(
                        f"[{utc_now_iso()}] job(rejected): reason={reason} "
                        f"channel={channel} sku={sku} qty={qty} event_id={event_id}"
                    )
                    continue

                # Guardrail: NO aplicar snapshots complete=true.
                # complete=true pisó stock a 0 (ej NH-CAR-AZU-CEN-DOR → 0).
                # amazon_fbm complete=true también es ruido hasta tener mapeos.
                if channel.lower() in (
                    "meli",
                    "amazon_fbm",
                ) and _is_complete_snapshot_event(conn, str(event_id)):
                    reason = "skip_complete_snapshot"
                    mark_event_done(str(event_id), "ok", reason)
                    bump_metric("events_blocked_total", 1)
                    bump_metric(f"events_blocked_total_{channel}", 1)
                    bump_metric("events_processed_ok_total", 1)
                    set_metric("last_success_at", utc_now_iso())
                    print(
                        f"[{utc_now_iso()}] job(skipped): reason={reason} "
                        f"channel={channel} sku={sku} qty={qty} event_id={event_id}"
                    )
                    continue

            # 4) apply MELI si está habilitado
            if channel.lower() == "meli" and meli_adapter_enabled:
                detail = meli_set_qty_from_mapping(str(sku), int(qty))
                has_meli_errors = "FAIL[" in detail
                mark_event_done(str(event_id), "ok", detail)
                bump_metric("events_processed_ok_total", 1)
                bump_metric("events_meli_applied_total", 1)
                set_metric("last_success_at", utc_now_iso())
                if has_meli_errors:
                    set_metric("last_error_at", utc_now_iso())
                    set_metric("last_error", detail[:500])
                    bump_metric("events_processed_error_total", 1)
                    print(
                        f"[{utc_now_iso()}] ERROR job(meli_partial_fail): channel={channel} "
                        f"sku={sku} qty={qty} event_id={event_id} {detail}"
                    )
                else:
                    print(
                        f"[{utc_now_iso()}] job(meli_applied): channel={channel} "
                        f"sku={sku} qty={qty} event_id={event_id} {detail}"
                    )
                continue

            # 4.5) apply AMAZON si está habilitado
            if channel.lower() == "amazon_fbm" and setting_is_true(
                db_conn(), "amazon_fbm_enabled", "0"
            ):
                detail = amazon_set_qty(str(sku), int(qty))
                mark_event_done(str(event_id), "ok", detail)
                bump_metric("events_processed_ok_total", 1)
                bump_metric("events_amazon_applied_total", 1)
                set_metric("last_success_at", utc_now_iso())
                print(
                    f"[{utc_now_iso()}] job(amazon_applied): channel={channel} "
                    f"sku={sku} qty={qty} event_id={event_id} {detail}"
                )
                continue
            # 5) listo para adapter noop
            mark_event_done(str(event_id), "ok", "ready_for_adapter_noop")
            bump_metric("events_processed_ok_total", 1)
            set_metric("last_success_at", utc_now_iso())
            print(
                f"[{utc_now_iso()}] job(ready_for_adapter_noop): "
                f"channel={channel} sku={sku} qty={qty} event_id={event_id}"
            )

        except Exception as e:
            # IMPORTANTE: si algo falla, intentamos registrar error, pero sin reventar por schema mismatch
            err = repr(e)
            try:
                mark_event_done(str(event_id), "error", err)
            except Exception as e2:
                print(f"[{utc_now_iso()}] WARN: mark_event_done failed: {e2!r}")

            bump_metric("events_processed_error_total", 1)
            set_metric("last_error_at", utc_now_iso())
            set_metric("last_error", err[:500])
            print(f"[{utc_now_iso()}] ERROR processing event_id={event_id}: {err}")
            continue


if __name__ == "__main__":
    main()
