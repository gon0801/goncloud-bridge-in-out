---
name: audit-repo-status
description: Audit the GONCLOUD Bridge Python repository for bugs, inconsistencies, security issues, data drift, and technical debt. Use when the user asks to audit, review, scan, or health-check the codebase, any specific module (app/, tools/, bridge_connector/), or when they want to update STATUS.html with current findings. Also trigger when the user mentions bugs, code review, drift between app/ and tools/, schema issues, worker problems, or webhook/auth concerns.
---

# Audit Repo Status

Audit the GONCLOUD Bridge repository systematically and update `STATUS.html` with findings.

## Workflow

1. **Load audit checklist** from `references/audit-checklist.md`.
2. **Run section-by-section audit** in the order defined below.
3. **Never skip STATUS.html update** — append a new audit section with date, findings, severity, and action items.
4. **If fixing bugs** during audit, mark them in STATUS.html under the audit section.
5. **Commit** changes with message: `audit(YYYY-MM-DD): <section> — N findings, N fixed`.

## Audit Sections (run in this order)

Run each section as a focused subagent or direct exploration. Record findings per section.

### S1 — Infra & Configuration
- `docker-compose.yml`: service definitions, networks, volumes, env vars, restart policies
- `Dockerfile*` layers, base images, exposed ports, COPY vs mount conflicts
- `.env`/settings: hardcoded secrets, missing defaults, path mismatches between host and container

### S2 — API & Webhooks (`app/main.py`, `auth_middleware.py`)
- Route handlers: missing error handling, unhandled exceptions leaking stack traces
- Auth: `auth_middleware.py` bypass paths, Cloudflare Access SSO fallback, secret rotation status
- Webhooks: HMAC verification, SNS signature validation, path-secret deprecation (AP-5), CSRF on browser routes
- Rate limiting: none? document if missing
- SQLite concurrency: `PRAGMA busy_timeout` usage, WAL mode, connection leaks

### S3 — Workers & Job Queues (`app/inbound_worker.py`, `app/amazon_inbound_worker.py`, `app/worker.py`)
- Reaper loops: lock timeout logic, heartbeat dead detection, zombie job recovery
- Redis queue: job serialization, encoding errors, queue growth (unprocessed backlog)
- Subprocess spawning: shell injection risks, argument passing, timeout handling
- Idempotency: dedupe keys, `processed_events` / `processed_inbound_events` collision domains
- Error handling: `manual_review` classification, retry logic, dead letter behavior

### S4 — Database & Schema (`bridge.db`, `migrate_rev4_schema.py`, `app/db_init.py`)
- Schema drift: tables in code vs migration scripts vs actual deployed schema
- Indexes: missing indexes on high-cardinality lookup columns (`order_id`, `client_order_ref`, `rawsha`)
- Foreign keys: SQLite FK enforcement (`PRAGMA foreign_keys`) — usually OFF, document risks
- WAL mode: checkpointing, `busy_timeout`, long-running reads blocking writers
- Migrations: `migrate_rev4_schema.py` idempotency, rollback absence

### S5 — Amazon SP-API (`tools/amazon_*.py`, `app/amazon_*.py`)
- v0 vs v2026-01-01 drift: `tools/amazon_orders_poll.py` normalization completeness
- Rate limit handling: backoff, 429 retry, header parsing (`x-amzn-RateLimit-Limit`)
- Profile detection: FLEX vs FBM vs FBA logic, edge cases (mixed-profile orders)
- Price parsing: `ItemPrice`, `Proceeds`, currency handling, zero-amount invoices
- Token refresh: LWA token expiry, refresh token persistence

### S6 — MercadoLibre (`tools/inbound_*.py`, `app/inbound_worker.py`, `tools/meli_refresh_tokens.sh`)
- OAuth: token refresh cron, expiry detection, `meli_refresh_tokens.sh` error handling
- Webhook payload: `rawsha` dedupe, schema changes, null fields causing KeyError
- SKU mapping: 1:N split variant support, `seller_custom_field` vs `SELLER_SKU` attributes
- Stock sync: race condition between inbound order and outbound stock push

### S7 — Odoo Connector (`bridge_connector/`, `tools/odoo_*.py`)
- XML-RPC: connection pooling, timeout, error classification (Odoo down vs bad credentials vs constraint violation)
- Model drift: fields referenced in code that may not exist in target Odoo version
- Phantom BOM: references to phantom BOM logic, warehouse routing, resupply rules
- Credit notes / refunds: SO cancellation flow, picking reversal, invoice unlink

### S8 — Stock Outbound (`app/worker.py`, `tools/apply_one_delta.py`, `snapshotter_*.py`)
- Race conditions: snapshot qty vs actual Odoo qty at moment of push
- Idempotency: `processed_events` dedupe, duplicate push detection
- Channel mapping: `ENABLE_MISSING_ZERO_CHANNELS` behavior, zero-stock push to unavailable channels
- FBA inventory: cached `amazon_fba_inventory` staleness

### S9 — SKU Mapping Integrity (`sku_mapping`, `amazon_sku_mapping`, `backfill_meli_mappings.py`)
- Orphaned mappings: SKUs in mapping without active listings
- Split variants: parent-child consistency, `sync_split_variants_stock.py` completeness
- Drift between `sku_mapping` and `amazon_sku_mapping` tables
- Backfill cron: last run success, coverage gaps

### S10 — Security & Secrets
- Secrets in logs: webhook path-secret visible in uvicorn/nginx logs (AP-5)
- Hardcoded credentials: any API keys, passwords, tokens in source code
- PII handling: buyer name/address/email logging, RDT requirements (now role-based in v2026)
- File permissions: SQLite DB world-readable? shell scripts executable?

### S11 — Operations & Crons (shell scripts, cron jobs)
- `check_tools_data_drift.sh`: does it detect real drift?
- Cron definitions: paths, user permissions, logging, failure alerting
- Backup: `bridge_backup.sh` completeness, offsite storage
- Cleanup: `cleanup_old_records.py` dry-run vs actual, retention policy

### S12 — Tests & Validation
- `test_v2026_normalize.py`: coverage, stale test data
- `test_odoo_sku.py`: connectivity only or functional?
- Dry-run modes: `inbound_so_dry_run.py`, `cleanup_old_records.py --dry-run`
- Missing tests: no pytest suite, no CI pipeline

### S13 — Documentation Drift
- `CLAUDE.md` vs `MASTER_RUNBOOK.md`: duplicate info, contradictions
- `PENDIENTES.md` vs `STATUS.html`: task count mismatch
- Code comments vs actual behavior
- Deploy instructions: `tools/` vs `app/` copy targets, `/data/` deployment path

### S14 — App vs Tools Drift
- File pairs: `app/amazon_fba_paid_one_shot.py` vs `tools/amazon_fba_paid_one_shot.py`
- Check: `diff` or semantic comparison for each pair
- Record: which files are out of sync, what fields differ, risk level

## STATUS.html Update Rules

Always update `STATUS.html` after any audit. Use the template in `assets/status-template.html`.

### Append a new audit block at the top of the `<tbody>` (before existing task groups)

Structure:
```html
<!-- AUDIT 2026-05-07 -->
<tr class="group-header">
  <td>🔍</td>
  <td>Auditoría <span class="sub">2026-05-07</span></td>
  <td><strong>S1–S3</strong> — Infra, API, Workers</td>
  <td><span class="badge badge-done">✅ Completada</span></td>
  <td>—</td>
</tr>
<tr>
  <td class="sub">S2.1</td>
  <td></td>
  <td><code>auth_middleware.py</code> línea 44 — secret header opcional, recomendar required</td>
  <td><span class="badge badge-pending">⏳ Pendiente</span></td>
  <td><span class="pri pri-media">Media</span></td>
</tr>
<tr>
  <td class="sub">S3.2</td>
  <td></td>
  <td><code>inbound_worker.py</code> — reaper no maneja SIGTERM graceful</td>
  <td><span class="badge badge-blocker">🔴 Crítico</span></td>
  <td><span class="pri pri-critica">Crítica</span></td>
</tr>
<!-- /AUDIT -->
```

### Rules
- Badge states: `badge-done` (fixed), `badge-pending` (to fix), `badge-blocker` (critical/blocking), `badge-info` (info/observation).
- Priority: `pri-critica`, `pri-alta`, `pri-media`, `pri-baja`.
- If a finding is fixed during the same session, mark it `badge-done` and add note "Fixeado en sesión".
- Update the progress bar and subtitle counts to reflect new findings.
- Add a closed entry in `.section-closed` summarizing: date, sections audited, total findings, critical count, fixed count.

## Severity Definitions

- **Crítica**: Data loss, security breach, production outage, unrecoverable state.
- **Alta**: Incorrect business logic, order processing failure, auth bypass.
- **Media**: Performance issue, missing validation, operational friction.
- **Baja**: Code style, documentation drift, missing index, cosmetic.

## Exit Criteria

An audit session is complete when:
1. All requested sections have been explored.
2. `STATUS.html` has been updated with findings.
3. A summary has been presented to the user: findings by severity, quick wins, blockers.
4. If fixes were applied, they are noted in STATUS.html and a commit message is suggested.
