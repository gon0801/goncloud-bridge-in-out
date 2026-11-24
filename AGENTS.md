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

## Cómo trabajar con David

Habla conmigo en español claro. Toma mi petición y el plan que haya aprobado como guía para el trabajo. Usa los modelos, cuentas y destinos acordados para ese encargo.

Hazte cargo del resultado. Decide por tu cuenta lo que te haya delegado y comprueba que el resultado funciona donde voy a usarlo. Elige comprobaciones proporcionales al cambio.

Si algo falla, averigua por qué, corrígelo y continúa. Si un intento repite el mismo fallo, cambia de enfoque usando lo que aprendiste. El número de intentos, por sí solo, no decide cuándo parar.

Si falta algo que solo yo pueda aportar, o hace falta una decisión fuera del encargo, avanza con el resto y dime qué ocurrió, qué intentaste y qué decisión concreta necesitas. Al cerrar, dime qué quedó hecho, cómo lo comprobaste y qué sigue pendiente.

## Revisión de PRs, tope de rondas y continuidad nocturna

Esta política aplica en todos los repositorios de David y prevalece sobre cualquier plan, diseño, runbook o criterio previo que pida rondas infinitas o «cero esquinas reproducibles».

- **Revisores:** `ai-review` en GitHub (informativo) más el revisor asignado al encargo (cualquier modelo distinto al implementador; por ejemplo Astra u otro). Devin no cuenta como compuerta ni abre rondas.
- **Qué sí bloquea (P0/P1):** rompe lo pedido o la aceptación del bloque en uso real, falla de seguridad o fuga de secretos, o corrupción/pérdida de dinero o datos en operación normal o ante un fallo simple común.
- **Qué NO bloquea (P2 — va a notas o seguimiento, nunca frena el merge):** esquinas que requieren 2 o más fallos o carreras encadenadas, defensas hipotéticas fuera de alcance, o preferencias de estilo/estructura.
- **Tope de 3 rondas + 1 relevo fuerte:** los hallazgos de `ai-review` y del revisor asignado se atienden juntos en las mismas rondas (máximo 3).
  - Si al cerrar la ronda 3 **no quedan P0/P1 reales**, se anotan los P2 restantes y se integra.
  - Si tras la ronda 3 **sigue abierto un P0/P1 real**, se lanza automáticamente **1 solo relevo con contexto limpio y el modelo más fuerte disponible distinto al implementador y al revisor** (prioridad orientativa: Opus / Neon / Astra según disponibilidad), con un tope de **2 rondas** y aplicando `principle-attack-the-premise` para atacar la raíz en lugar de parchar.
  - Si incluso el relevo deja un **P0/P1 real abierto**, **NO se mergea roto ni se abren más rondas**: se estaciona el PR en **Draft** con resumen para David y el coordinador continúa automáticamente con el siguiente bloque o tarea independiente para no frenar la noche.
- **Skills de `pstack` por disparador (cuando estén disponibles en el entorno, nunca en cambios triviales):**
  - `architect`: antes de codear cambios complejos (dinero, concurrencia, migraciones, máquinas de estado o varios módulos).
  - `principle-attack-the-premise`: en el relevo o si un mismo P0/P1 se repite en ronda 2.
  - `fix-ci` y `fix-merge-conflicts`: cuando un PR tenga CI rojo o conflictos.
  - `show-me-your-work` / `ketchup`: en corridas nocturnas o desatendidas para dejar resumen claro de lo integrado y lo estacionado en Draft.
  - No crear loops tipo `babysit` ni watchers de LLM: la vigilancia externa la realiza Timbre + Agent Orchestrator.
