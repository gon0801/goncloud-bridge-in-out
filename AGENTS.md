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
candados, evidencia, rondas o pruebas, manda esto, salvo un plan que David aprobó para ese trabajo:
ahí manda el plan, salvo en las dos reglas sin excepción. Si una regla con criterio no se puede
cumplir, el trabajo sigue y se anota por qué en el PR. Un paso solo se detiene por secretos, por seguridad, por
algo que el plan no autoriza (borrar datos, gastar, escribir en producción, hablar con terceros en
nombre de David) o por un bloqueo de revisión que quedó sin resolver; eso espera a David. Lo demás
no espera a nadie.

Sin excepción:

- Todo cambio entra por PR; nadie sube directo a la rama principal.
- Las llaves y contraseñas quedan fuera del repo, y la revisión de secretos corre siempre (sin
  `--no-verify`). Si marca algo que no es una llave, se agrega a `.gitleaksignore` con su razón y
  se sigue.

Con criterio:

- Pruebas obligatorias en 4 casos: dinero (precios, pujas, presupuestos, cobros), escribir datos de
  producción, instalar o borrar en las máquinas, y una falla que ya pasó en producción (una prueba,
  no una batería). Fuera de esos casos, una prueba solo si es la forma más barata de comprobar que
  funciona. Por defecto no se hacen baterías, pruebas de texto ni mutantes; sí cuando son la forma
  más barata de demostrar un riesgo de dinero o de datos (por ejemplo, el texto exacto con el que
  alguien decide). Fuera de eso, el revisor no las pide.
- Las pruebas que ya existen deben pasar antes de mergear. Si una ya falla igual en la rama
  principal, no frena: se anota en el PR.
- El revisor normal es un modelo distinto del que escribió; si no está disponible, el siguiente que
  sí lo esté. La revisión no espera a una persona. Solo bloquea por seguridad, datos, el
  comportamiento pedido roto o uno de los 4 casos sin su prueba, con el comando que lo reproduce o,
  si no se puede reproducir, la línea exacta y el riesgo. Lo demás va en notas.
- Sin tope de rondas mientras cada una traiga avance. Si queda un bloqueo concreto, el implementador
  lo corrige y el revisor comprueba la corrección; cuando solo quedan notas, se integra el PR. Si el
  mismo bloqueo vuelve 3 rondas seguidas sin avance, lo revisa otro modelo; si ese tampoco lo
  resuelve, ese PR queda sin integrar y se avisa a David con el bloqueo y su evidencia. El resto del
  trabajo sigue.
- Todo cambio que toca producción dice en el PR cómo regresarlo.
- Compuertas (tamaño, lint, tipos, checklists, baterías, CI obligatorio) y revisores extra además
  del normal se agregan solo si David lo pide.
