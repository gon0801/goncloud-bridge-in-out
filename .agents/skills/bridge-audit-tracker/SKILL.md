---
name: bridge-audit-tracker
description: Track and document bridge security/ops audit findings in status-bridge.html. Use whenever completing or updating an audit (D1-D5, S1-S14, CVE scans, data integrity checks, frontend security reviews, redis/queue health checks, or any production deep-dive). Ensures every finding gets a unique ID, priority, file reference, and pendiente/corregido status in the living dashboard.
---

# Bridge Audit Tracker

## Trigger

Activate this skill after **any** audit or deep-dive on the GONCLOUD bridge project completes. Examples: D1 Data Integrity, D2 Frontend Security, D3 Supply Chain/CVE, D4 Redis & Queue Health, D5 Observability, S1-S14 code audits, penetration tests, infrastructure reviews.

## Workflow

### 1. Gather findings

Collect every actionable finding from the audit. Each finding must have:
- **Unique ID**: Use audit prefix + number (e.g., `D4.1`, `F1.1`, `B411`, `S3.2`)
- **Location**: File and line number when available (e.g., `setup.html:487`, `main.py:509`)
- **Description**: One-line actionable description
- **Priority**: `Crítica`, `Alta`, `Media`, or `Baja`
- **Status**: Always `🔴 Pendiente` on first entry; update to `✅ Corregido` when fixed

### 2. Update `status-bridge.html`

The file lives in **repo root** (`status-bridge.html`) and is served from `/app/static/status-bridge.html` on the bridge-api container.

Three sections to update:

#### A. Audit summary row
Add or update the row in the "Auditorías programadas" table with:
- Badge: `✅ Completada`
- Short summary with counts (e.g., "12 hallazgos: 4 Críticos, 4 Altos, 3 Medios, 1 Bajo")
- Top 3 most critical findings in the description cell

#### B. Actionable items table
Add a `group-header` row for the audit under "Puntos a corregir — Auditorías D1-D4" (or create a new D5/D6 section if needed). Then add one `<tr>` per finding with the ID, description, status badge, and priority badge.

Use this exact HTML row template:

```html
<tr>
  <td class="sub">ID</td>
  <td colspan="2"><code>file:line</code> — description with <code>inline code</code> for vars</td>
  <td><span class="badge badge-blocker">🔴 Pendiente</span></td>
  <td><span class="pri pri-critica">Crítica</span></td>
</tr>
```

Priority CSS classes: `pri-critica`, `pri-alta`, `pri-media`, `pri-baja`.

#### C. Closed entry
Add a dated `<li>` in the "Cerrados recientemente" section summarizing:
- Date
- Audit name and total counts
- Root cause of the top critical finding
- Timeline if relevant (e.g., token expiry chain)

### 3. Deploy

```bash
git add status-bridge.html
git commit -m "update status-bridge.html: D<X> findings (N items)"
git push origin main
ssh gonserver "cd /mnt/data/appdata/bridge && git pull origin main"
```

No container rebuild is needed because `/mnt/data/appdata/bridge/app:/app` is a bind mount.

## Helper script

For bulk insertions, use `scripts/add_audit_finding.py`:

```bash
python .agents/skills/bridge-audit-tracker/scripts/add_audit_finding.py \
  --file status-bridge.html \
  --audit D4 \
  --id D4.1 \
  --desc "LREM mismatch in inbound_worker.py" \
  --loc "inbound_worker.py" \
  --priority critica
```

## Rules

- Never skip updating `status-bridge.html` after an audit.
- Never mark a finding as `✅ Corregido` unless the fix is committed, pushed, and deployed.
- If a finding spans multiple files, list the primary file in the location cell.
- Keep descriptions under 140 characters so the table renders cleanly.
