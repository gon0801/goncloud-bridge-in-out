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

<!-- >>> QUALITY-KIT CALIDAD SECTION START -- managed by quality-kit's init-repo.ps1. Do not hand-edit between these markers; re-running init-repo.ps1 will refresh this block cleanly. -->
## Calidad (quality-kit)

Candados de commit instalados (pre-commit):
- base (limpieza de archivos)
- ruff (lint + formato Python)

Comandos para correr los candados a mano:
- `pre-commit run --all-files` (todos los candados de commit)

Reglas de hierro:
1. Si un candado falla, se arregla el problema real -- JAMAS se usa `--no-verify` ni se saltea un candado.
2. Cada bug arreglado incluye, en el mismo cambio, una prueba que lo habria atrapado.
<!-- >>> QUALITY-KIT CALIDAD SECTION END -->
