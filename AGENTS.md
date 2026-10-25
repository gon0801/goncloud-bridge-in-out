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

## Pruebas y revisiones
- Pruebas nuevas SOLO en 4 casos: dinero (pujas, precios, presupuestos, cobros), escribir datos de produccion
  (base del servidor, Amazon, Mercado Libre, Shopify, Odoo), instalar/borrar/deshacer en las maquinas, y una
  falla que ya paso en produccion (una prueba, no una bateria). Lo demas: basta comprobar que corre. Nunca
  pruebas de texto, de las pruebas ni mutantes. Un revisor no pide pruebas fuera de eso.
- Un revisor solo bloquea por seguridad, datos, el comportamiento pedido roto o un caso de los 4 sin su
  prueba, siempre con el comando que lo reproduce. Lo demas va en notas y se corrige en la misma ronda.
- Nunca subir contrasenas ni llaves a un repo.
