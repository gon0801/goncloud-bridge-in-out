---
name: mark-finding-done
description: >
  Mark audit findings as ✅ Corregido in status-bridge.html and deploy.
  ALWAYS invoke this skill immediately after completing any fix from the bridge audit
  (D1–D6, S1–S14, F1, or any finding listed in status-bridge.html).
  Trigger words: "done", "fixed", "arreglado", "resuelto", "corregido", "deploied",
  or when completing a numbered audit task (e.g., "D4.1 done", "finished D6.2").
---

# Mark Finding Done

## When to Invoke

**Immediately after any audit finding is fixed, committed, pushed, and deployed.**

Do NOT invoke before deploy — badge-done means it is live in production.

## Required Inputs

Before running, identify:
- `IDS` — list of finding IDs fixed this session (e.g., `D3.1 D4.1 D6.2`)
- `NOTE` — one-line description of what was done (e.g., `"redis pinned; LREM fixed with _proc_entry"`)

## Workflow

### Step 1 — Run the helper script

```bash
python .agents/skills/mark-finding-done/scripts/mark_done.py \
  --file status-bridge.html \
  --ids <IDS> \
  --note "<NOTE>"
```

This will:
- Find each `<tr>` row with that ID and flip `badge-blocker` → `badge-done` (`🔴 Pendiente` → `✅ Corregido`)
- Decrement the pending counter in the summary header
- Prepend a dated `<li>` in the "Cerrados recientemente" section

### Step 2 — Verify the change

```bash
grep -A3 "<ID>" status-bridge.html
```

Confirm the badge shows `badge-done`.

### Step 3 — Deploy

Invoke the `deploy-bridge` skill to commit, push, and deploy to production.
The `status-bridge.html` in repo root is served at `/static/status-bridge.html` on bridge-api.

## Rules

- **Never skip this skill** after completing a fix. Every resolved finding must be reflected in status-bridge.html.
- **Never mark done** if fix is only local (not committed + pushed + deployed).
- If a finding was already `badge-done`, the script warns but does not fail.
- Keep `--note` under 120 chars so the closed-list `<li>` renders cleanly.
- If no `cerrados recientemente` `<ul class="closed-list">` section exists, add it manually before running the script.
