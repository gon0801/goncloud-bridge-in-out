# GONCLOUD Bridge: contexto para agentes

Este repo integra MercadoLibre y Amazon con Odoo 17. Las órdenes entran a Odoo; los cambios de stock salen a los marketplaces. El código y la configuración desplegada determinan el comportamiento vigente.

## Antes de cambiar o diagnosticar

- Lee [`AGENTS.md`](AGENTS.md) para las reglas del repo.
- Si la tarea trata sobre backlog o prioridades, consulta [`PENDIENTES.md`](PENDIENTES.md). No copies su estado aquí.
- Lee el worker y la herramienta afectados. Comprueba `bridge.db` y los logs antes de atribuir una falla a una causa pasada.
- Para un cambio en producción, compara el checkout, `docker-compose.yml`, los scripts en `tools/` y las copias que ejecuta el host. `bash tools/check_tools_data_drift.sh` muestra diferencias entre `tools/` y `/data/`.

## Documentación por tarea

- [`docs/BRIDGE_REFERENCE.md`](docs/BRIDGE_REFERENCE.md): arquitectura, flujos, reglas de negocio y precedencia de herramientas.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md): síntomas conocidos, consultas, recuperación de órdenes y despliegue de scripts.
- [`docs/ANEXO_C_FULL_ML_STATUS.md`](docs/ANEXO_C_FULL_ML_STATUS.md): regla canónica de estados FULL de MercadoLibre.
- [`docs/HISTORY.md`](docs/HISTORY.md): regresiones, estados y decisiones históricos. Comprueba cualquier afirmación operativa antes de reutilizarla.
- [`MASTER_RUNBOOK.md`](MASTER_RUNBOOK.md) y [`docs/RUNBOOK.md`](docs/RUNBOOK.md): instalación y operación. Incluyen datos históricos que pueden diferir de la configuración actual.

## Invariantes que requieren atención

- El inbound de webhooks llega por Cloudflare Tunnel a `127.0.0.1:8099`. La interfaz del mapper usa la red Docker `proxy`. Declara ambos caminos en `docker-compose.yml`; una configuración manual desaparece al recrear el contenedor.
- Los workers buscan herramientas primero en `/data/` y después en `tools/`. Algunos timers del host ejecutan `tools/` directamente; otros llaman `/data/`. Verifica qué copia llama cada servicio antes de desplegar.
- Una orden FULL de MercadoLibre en `returned` no prueba que se haya emitido un refund. La reversa contable exige una señal explícita según el anexo C.
- Amazon Flex MX puede llegar en `Pending` y requiere el flujo con picking descrito en la referencia. Comprueba el tratamiento en el poll y en el worker al cambiar ese flujo.

## Reglas de trabajo

Iguales en todos los repos de David. Si otro documento de este repo dice otra cosa sobre calidad,
candados, evidencia, rondas o pruebas, manda esto, salvo un plan que David aprobó para ese trabajo:
ahí manda el plan. Si una regla no se puede cumplir, el trabajo sigue y se anota por qué en el PR.
Solo se detiene ese paso, hasta resolverlo, por secretos, por seguridad o por algo que el plan no
autoriza (borrar datos, gastar, escribir en producción, hablar con terceros en nombre de David),
que espera su aprobación. Lo demás no espera a nadie.

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
  resuelve, se escala a David con el bloqueo y su evidencia.
- Todo cambio que toca producción dice en el PR cómo regresarlo.
- Compuertas (tamaño, lint, tipos, checklists, baterías, CI obligatorio) y revisores extra además
  del normal se agregan solo si David lo pide.
