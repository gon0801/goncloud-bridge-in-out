"""Evaluaciones puras para /v1/health.

Viven aparte de `main.py` a proposito: ese modulo monta StaticFiles y arma la
app FastAPI al importarse, asi que no se puede cargar fuera del contenedor.
Estas funciones no tocan nada — se testean directo.
"""

from datetime import datetime, timezone
from typing import Optional


def evaluar_frescura(
    ultimo_iso: Optional[str],
    warn_h: float,
    err_h: float,
    ahora: Optional[datetime] = None,
) -> tuple[str, Optional[float]]:
    """Que tan viejo es el ultimo evento de un canal inbound.

    Devuelve `(estado, edad_en_horas)` con estado en
    {"ok", "warn", "error", "empty"}. `empty` cuando no hay dato o no se puede
    interpretar: sin datos NO es una falla (instalacion nueva, canal recien
    conectado), y una fecha ilegible no debe tumbar el endpoint entero.

    Por que existe
    --------------
    El apagon del 2026-08-24 duro 19 dias sin que nada avisara. `/v1/health`
    solo miraba PROFUNDIDAD de colas, y la cola estaba en 0: no habia nada
    atascado, simplemente no llegaba nada. Un canal muerto y uno tranquilo se
    ven identicos si solo cuentas lo encolado; lo que los distingue es hace
    cuanto que no entra nada.
    """
    if not ultimo_iso:
        return "empty", None

    texto = str(ultimo_iso).strip().replace("Z", "+00:00")
    try:
        marca = datetime.fromisoformat(texto)
    except ValueError:
        try:
            marca = datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return "empty", None

    if marca.tzinfo is None:
        marca = marca.replace(tzinfo=timezone.utc)

    referencia = ahora or datetime.now(timezone.utc)
    edad_h = (referencia - marca).total_seconds() / 3600

    if edad_h > err_h:
        return "error", edad_h
    if edad_h > warn_h:
        return "warn", edad_h
    return "ok", edad_h
