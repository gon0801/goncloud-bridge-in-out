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
