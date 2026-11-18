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

## Reglas de trabajo

Iguales en todos los repos de David. Si otro documento de este repo dice otra cosa sobre calidad,
candados, evidencia, rondas o pruebas, manda esto.

- Todo cambio entra por PR; nadie sube directo a la rama principal.
- Pruebas nuevas solo en 4 casos: dinero (precios, pujas, presupuestos, cobros), escribir datos de
  producción, instalar o borrar en las máquinas, y una falla que ya pasó en producción (una prueba,
  no una batería). Lo demás: basta comprobar que corre.
- Las pruebas que ya existen deben pasar antes de mergear: en CI si el repo lo tiene; si no, en
  local.
- Revisa uno solo, distinto de quien escribió. Solo bloquea por seguridad, datos, el comportamiento
  pedido roto o uno de los 4 casos sin su prueba, siempre con el comando que lo reproduce. Lo demás
  va en notas.
- DeepSeek opina en el PR; el revisor decide. Un hallazgo Low nunca abre otra vuelta.
- Máximo 2 rondas de revisión por PR; una 3.ª solo por algo grave.
- Los cambios grandes van detrás de un interruptor apagado y se despliegan poco a poco.
- El tamaño de archivos y funciones es una meta, no un freno.
- Los encargos o contratos para otro agente son cortos: qué debe pasar y cómo se comprueba.
- Revisión cruzada con otro modelo solo cuando David la pide.
- Nunca subas llaves ni contraseñas; no uses `--no-verify` sobre la revisión de secretos.
- No agregues compuertas, checklists, revisores, baterías ni CI obligatorios sin que David lo pida.
