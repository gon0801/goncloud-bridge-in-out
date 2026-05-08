---
name: deploy-bridge
description: >
  Full deployment pipeline for GONCLOUD Bridge: commit staged changes, push to main,
  SSH to VPS and deploy to production. ALWAYS invoke after completing any code fix
  that needs to go live. Handles file routing (which files go where, which containers restart).
  Trigger words: "deploy", "push", "sube", "manda al server", "aplica en producción",
  "haz el deploy", or at the end of any fix session.
---

# Deploy Bridge

## When to Invoke

After any code fix is ready to go live. This skill covers the full pipeline:
commit → push → SSH pull on VPS → copy files → restart containers.

## Required Inputs

- `COMMIT_MSG` — commit message (skip if already committed and pushed)
- `FILES` — list of changed files (skip if already committed)

If all changes are already committed and pushed, skip to Step 3.

## Deployment Map

| File(s) | Production path | Container restart |
|---------|----------------|-------------------|
| `app/main.py` | `/mnt/data/appdata/bridge/app/main.py` | `bridge-api` |
| `app/static/**` | `/mnt/data/appdata/bridge/app/static/` | `bridge-api` |
| `app/*.html` | `/mnt/data/appdata/bridge/app/` | `bridge-api` |
| `app/inbound_worker.py` | `/mnt/data/appdata/bridge/app/inbound_worker.py` | `bridge-inbound-worker` |
| `app/amazon_inbound_worker.py` | `/mnt/data/appdata/bridge/app/amazon_inbound_worker.py` | `bridge-amazon-inbound-worker` |
| `app/worker.py` | `/mnt/data/appdata/bridge/app/worker.py` | `bridge-worker` |
| `app/auth_middleware.py` | `/mnt/data/appdata/bridge/app/auth_middleware.py` | `bridge-api` |
| `tools/*.py` | `/mnt/data/appdata/bridge/data/` | *(none — hot-reload)* |
| `tools/*.sh` | `/mnt/data/appdata/bridge/tools/` | *(none)* |
| `tools/cron/*` | `/etc/cron.d/` | *(none)* |
| `status-bridge.html` | `/mnt/data/appdata/bridge/app/static/status-bridge.html` | *(none)* |
| `docker-compose.yml` | `/mnt/data/appdata/bridge/docker-compose.yml` | `docker compose up -d` |
| `RECOVERY_CHECKLIST.txt` | *(docs only — no deploy needed)* | *(none)* |

## Workflow

### Step 1 — Commit (if not already done)

```bash
git add <FILES>
git commit -m "<COMMIT_MSG>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

### Step 2 — Push

```bash
git push origin main
```

### Step 3 — SSH to VPS and pull

```bash
ssh goncloud "cd /tmp/goncloud-bridge-in-out && git pull origin main"
```

If `/tmp/goncloud-bridge-in-out` doesn't exist (reboots wipe /tmp):
```bash
ssh goncloud "git clone https://github.com/gon0801/goncloud-bridge-in-out.git /tmp/goncloud-bridge-in-out"
```

### Step 4 — Copy files and restart containers

Run only the lines relevant to the files that changed (see Deployment Map above).

```bash
ssh goncloud "
# app/ files
sudo cp /tmp/goncloud-bridge-in-out/app/main.py /mnt/data/appdata/bridge/app/
sudo cp /tmp/goncloud-bridge-in-out/app/inbound_worker.py /mnt/data/appdata/bridge/app/
sudo cp /tmp/goncloud-bridge-in-out/app/amazon_inbound_worker.py /mnt/data/appdata/bridge/app/
sudo cp /tmp/goncloud-bridge-in-out/app/worker.py /mnt/data/appdata/bridge/app/
sudo cp /tmp/goncloud-bridge-in-out/app/auth_middleware.py /mnt/data/appdata/bridge/app/
sudo cp /tmp/goncloud-bridge-in-out/app/static/* /mnt/data/appdata/bridge/app/static/

# tools/ → /data/ (hot-reload, no restart needed)
sudo cp /tmp/goncloud-bridge-in-out/tools/*.py /mnt/data/appdata/bridge/data/

# status-bridge.html → static
sudo cp /tmp/goncloud-bridge-in-out/status-bridge.html /mnt/data/appdata/bridge/app/static/

# container restarts
sudo docker restart bridge-api
sudo docker restart bridge-inbound-worker
sudo docker restart bridge-amazon-inbound-worker
sudo docker restart bridge-worker
"
```

Only restart containers whose files actually changed.

### Step 5 — Verify

```bash
ssh goncloud "sudo docker ps --format '{{.Names}}\t{{.Status}}' | grep bridge"
ssh goncloud "curl -s -H 'X-Goncloud-Secret: \$(cat /mnt/data/appdata/bridge/data/.bridge_secret 2>/dev/null || echo none)' http://127.0.0.1:8099/v1/health"
```

Expected: all 5 containers `Up X seconds`, health `{"ok": true}`.

### Step 6 — Check logs for errors

```bash
ssh goncloud "sudo docker logs bridge-api --tail 20"
ssh goncloud "sudo docker logs bridge-inbound-worker --tail 20"
```

## Rules

- **Always run Step 5 (verify)** — confirm health before reporting done.
- If a container fails to start, check logs immediately and fix before closing session.
- If `/tmp/goncloud-bridge-in-out` is missing on the VPS (happens after reboot), clone first.
- `tools/*.py` go to `/data/` AND stay in `tools/` in repo. The `/data/` copy is what the worker actually runs.
- Never restart `bridge-redis` unless specifically required — it clears all queues.
- Update `mark-finding-done` badge **after** deploy completes (badge-done = live in production).
