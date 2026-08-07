# Audit Checklist — GONCLOUD Bridge

Use this checklist during each section of the audit. Tick items as they are verified.

---

## S1 — Infra & Configuration

- [ ] `docker-compose.yml` services all have `restart: unless-stopped`
- [ ] Networks `goncloud-net` and `odoo_odoo_net` are declared as external
- [ ] `bridge-api` port 8099 does not conflict with host services
- [ ] `Dockerfile` uses pinned base image tag (not `latest`)
- [ ] No `EXPOSE` port mismatches with `docker-compose.yml`
- [ ] Environment variables referenced in code have defaults or are validated at startup
- [ ] `REDIS_URL` format validated (redis://host:port/db)
- [ ] `BRIDGE_DB` path exists inside container and is on a persistent volume
- [ ] No hardcoded paths to `/mnt/data/` inside Python code (use env or settings)
- [ ] Shell scripts use `set -euo pipefail` or equivalent error handling

## S2 — API & Webhooks

- [ ] All FastAPI routes have exception handlers (not raw 500s)
- [ ] `auth_middleware.py`: `x_goncloud_secret` is validated with `hmac.compare_digest`
- [ ] Cloudflare Access SSO fallback does not bypass webhook routes
- [ ] MeLi webhook: `rawsha` dedupe works for split-variant orders
- [ ] Amazon SNS: signature verified with cached cert, SubscribeURL allowlisted
- [ ] AP-5 path-secret routes still accept old URLs for backward compat — log exposure risk noted
- [ ] CSRF double-submit cookie matches on browser routes
- [ ] SQLite connections use `PRAGMA busy_timeout=60000`
- [ ] WAL mode enabled (`PRAGMA journal_mode=WAL`)
- [ ] No `connection.close()` leaks in FastAPI dependency injection

## S3 — Workers & Job Queues

- [ ] Reaper loop interval is <= lock timeout / 2
- [ ] Heartbeat stale threshold accounts for GC pauses
- [ ] Zombie jobs (heartbeat dead, not in manual_review) are re-enqueued or logged
- [ ] Redis `blpop` timeout is not infinite (allows shutdown)
- [ ] Subprocess calls use list args, not shell strings
- [ ] Subprocess timeout kills hung tools (e.g., Odoo timeout)
- [ ] `processed_inbound_events` dedupe key includes channel + order_id + status
- [ ] `manual_review` records have TTL or cleanup policy
- [ ] Worker SIGTERM handler flushes logs and releases locks
- [ ] Queue backlog monitored (Redis `LLEN` check documented)

## S4 — Database & Schema

- [ ] All tables referenced in code exist in `migrate_rev4_schema.py` or `db_init.py`
- [ ] `sku_mapping` has index on `(channel, remote_item_id, remote_variation_id)`
- [ ] `processed_inbound_events` has index on `(order_id, channel)`
- [ ] `amazon_processed_events` has index on `(amazon_order_id)`
- [ ] `bridge_settings` key lookups are fast (small table, ok without index)
- [ ] `events` table has index on `(sku, channel, snapshot_id)`
- [ ] `PRAGMA foreign_keys` status checked and documented
- [ ] WAL checkpoint does not block during high write load
- [ ] Migration script is idempotent (CREATE TABLE IF NOT EXISTS, ALTER ADD COLUMN guards)
- [ ] No ALTER TABLE DROP COLUMN in migrations (SQLite limitation)

## S5 — Amazon SP-API

- [ ] `tools/amazon_orders_poll.py` uses `/orders/2026-01-01/orders` endpoint
- [ ] Normalization covers all fields used by `app/amazon_inbound_worker.py`
- [ ] Rate limit 429 response triggers exponential backoff
- [ ] `x-amzn-RateLimit-Limit` header parsed and logged
- [ ] LWA token refresh handles 401 correctly (refresh token used, not re-auth)
- [ ] Profile detection (FLEX/FBM/FBA) does not misclassify Easy Ship
- [ ] `IS_USD_ORDER` logic handles MXN vs USD correctly for all marketplaces
- [ ] Zero-amount order does not create $0 invoice (or is handled explicitly)
- [ ] `amazon_sku_mapping` fallback to `sku_mapping` documented

## S6 — MercadoLibre

- [ ] `meli_refresh_tokens.sh` exits non-zero on refresh failure
- [ ] Cron runs every 6h and logs to persistent file
- [ ] Token expiry detected before 401 (preemptive refresh)
- [ ] Webhook `rawsha` includes `variation_id` for split variants
- [ ] `SELLER_SKU` attribute read correctly (not deprecated `seller_custom_field`)
- [ ] FULL (MeLi Fulfillment) orders skip picking creation
- [ ] FBM orders create picking from correct warehouse
- [ ] Stock sync push does not race with inbound order creation
- [ ] `backfill_meli_mappings.py` handles pagination and rate limits

## S7 — Odoo Connector

- [ ] XML-RPC `socket.setdefaulttimeout` is set
- [ ] Connection error distinguishes: DNS, timeout, 403, Odoo constraint violation
- [ ] `client_order_ref` uniqueness enforced (Amazon order_id, MeLi order_id)
- [ ] SO line price uses correct decimal precision (Odoo currency rounding)
- [ ] Phantom BOM logic does not create phantom pickings
- [ ] Warehouse resupply rules configured for new warehouses (Task 1)
- [ ] Credit note creation reverses correct invoice, not all open invoices
- [ ] Refund amount matches original order total (currency, rounding)

## S8 — Stock Outbound

- [ ] Snapshot timestamp is before qty read from Odoo (not after)
- [ ] `processed_events` dedupe uses `(snapshot_id, sku, channel)`
- [ ] Zero-stock push enabled only when `ENABLE_MISSING_ZERO_CHANNELS=1`
- [ ] Amazon FBA inventory cached separately from FBM stock
- [ ] MeLi stock push handles 1:N SKU → listings correctly
- [ ] Failed stock pushes are retried with backoff
- [ ] Odoo qty read uses correct warehouse/lot context

## S9 — SKU Mapping Integrity

- [ ] `sku_mapping` 1:N schema supports split variants
- [ ] `amazon_sku_mapping` is kept in sync or deprecated
- [ ] Orphaned mappings (no active listing) are flagged
- [ ] `backfill_meli_mappings.py` last run timestamp logged
- [ ] `sync_split_variants_stock.py` covers all split SKUs
- [ ] Manual mapping UI (`/mapper`) validates SKU regex
- [ ] Duplicate `(channel, remote_item_id, remote_variation_id)` entries rejected

## S10 — Security & Secrets

- [ ] No API keys, passwords, or tokens in source code (grep for `password=`, `token=`, `secret=`)
- [ ] Webhook secrets not logged by uvicorn/nginx (AP-5 migration plan)
- [ ] SQLite DB file permissions are 600 or 640
- [ ] `bridge.db` backup encrypted or access-controlled
- [ ] PII (buyer name, address, email) not logged to plaintext files
- [ ] `auth_middleware.py` does not leak secret in error messages
- [ ] Cloudflare Access JWT validation uses proper signature check

## S11 — Operations & Crons

- [ ] All cron jobs log output to timestamped files
- [ ] Cron failures trigger alert (or at minimum persist stderr)
- [ ] `bridge_backup.sh` includes `bridge.db`, `.meli_tokens.json`, `amazon_credentials.json`
- [ ] Backup retention policy defined and enforced
- [ ] `cleanup_old_records.py` dry-run mode tested before production run
- [ ] `check_tools_data_drift.sh` runs without false positives
- [ ] `start_inbound.sh` checks preconditions (Redis up, DB writable)

## S12 — Tests & Validation

- [ ] `test_v2026_normalize.py` passes against current SP-API
- [ ] `test_odoo_sku.py` connects and returns real product data
- [ ] Dry-run modes do not write to DB or external APIs
- [ ] At least one integration test path exists for each inbound channel
- [ ] No broken imports or syntax errors in any `.py` file

## S13 — Documentation Drift

- [ ] `PENDIENTES.md` task count matches `STATUS.html`
- [ ] `CLAUDE.md` server info matches current VPS (IP, user, paths)
- [ ] `MASTER_RUNBOOK.md` deploy instructions reference correct paths
- [ ] `SETUP_WIZARD_CANONICAL_v1.md` URLs match production domain
- [ ] Code comments describe current behavior (not outdated logic)

## S14 — App vs Tools Drift

- [ ] `app/amazon_fba_paid_one_shot.py` ≡ `tools/amazon_fba_paid_one_shot.py`
- [ ] `app/amazon_fbm_paid_one_shot.py` ≡ `tools/amazon_fbm_paid_one_shot.py`
- [ ] `app/apply_one_delta.py` ≡ `tools/apply_one_delta.py`
- [ ] Any divergence is documented and justified
- [ ] Deploy script copies from `tools/` to `/data/` (not from `app/`)
