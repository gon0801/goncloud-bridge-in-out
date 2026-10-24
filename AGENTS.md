# GONCLOUD Bridge — Agent Instructions

## Always update `status-bridge.html`

After **every audit** (D1, D2, D3, D4, D5, etc.) or deep-dive (S1–S14), you **must** update `status-bridge.html` before finishing the session.

### What to add
1. **Audit summary** in the "Auditorías programadas" section (or update the existing row if re-auditing).
2. **Actionable findings** in the "Puntos a corregir" section with:
   - Unique ID (e.g., `D4.1`, `F1.1`, `B411`)
   - One-line description with file/line reference when possible
   - Priority badge (`Crítica`, `Alta`, `Media`, `Baja`)
   - Status badge (`🔴 Pendiente` until fixed, then `✅ Corregido`)
3. **Closed entry** in the "Cerrados recientemente" section with date, scope, and top 3 findings.

### Where the file lives
- **Repo:** `status-bridge.html` (root of `goncloud-bridge-in-out`)
- **Production:** served from `/app/static/status-bridge.html` inside `bridge-api` container (mounted volume)
- **URL:** `https://<bridge-host>/static/status-bridge.html`

### How to deploy
```bash
# 1. Commit and push locally
git add status-bridge.html
git commit -m "update status-bridge.html: D<X> findings"
git push origin main

# 2. Sync to server (already mounted, but pull to keep repo clean)
ssh gonserver "cd /mnt/data/appdata/bridge && git pull origin main"
```

No rebuild required — the container mounts `/mnt/data/appdata/bridge/app:/app`.
