"""FastAPI auth dependency compartido — bridge GONCLOUD.

Provee `require_secret`, un Depends() que valida acceso autenticado a
endpoints internos (`/api/*`, `/v1/*`, `/setup/api/*`, `/oauth/refresh`).

Acepta TRES factores (cualquiera basta):
1. Header `X-Goncloud-Secret` válido contra env `GONCLOUD_API_SECRET`.
   Path para scripts ops/cron que invocan al bridge directamente.
2. Header `Cf-Access-Authenticated-User-Email` presente (lo inyecta
   Cloudflare Access tras autenticar al operador vía Google SSO).
   Path para el UI del mapper / setup wizard accedidos por el operador
   en el browser via dominio público.
3. Request desde red Tailscale (IP 100.x.x.x) o WireGuard (10.13.13.x).
   Path para acceso directo interno vía goncloud:8099.

Importar este módulo NO requiere que `GONCLOUD_API_SECRET` esté seteado;
el chequeo es lazy en cada request.

Uso en main.py:
    from auth_middleware import require_secret
    from fastapi import Depends

    @app.get("/api/something", dependencies=[Depends(require_secret)])
    def my_endpoint(): ...

NO se aplica a webhooks / oauth/callback (tienen su propio mecanismo
de validación basado en secret-de-app o firma SNS).
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Request


_ENV_NAME = "GONCLOUD_API_SECRET"

# Tailscale asigna IPs en el rango 100.64.0.0/10 (RFC 6598)
_TAILSCALE_PREFIX = "100."

# WireGuard: subred del tunel propio del operador (clientes VPN goncloud)
_WIREGUARD_PREFIX = "10.13.13."

# Docker userland-proxy enmascara el trafico VPN que entra por el puerto
# publicado: el contenedor lo ve como el gateway del bridge docker.
# DOCKER-USER ya DROPea el internet publico hacia 8099, los webhooks publicos
# NO usan require_secret, y los endpoints require_secret estan detras de
# Cloudflare Access en el dominio publico -> confiar en este gateway ==
# confiar en VPN/operador ya filtrado.
_DOCKER_GW = "172.18.0.1"


def require_secret(
    request: Request,
    x_goncloud_secret: str | None = Header(default=None),
    cf_access_authenticated_user_email: str | None = Header(default=None),
) -> None:
    """Dependency tri-factor: Cf-Access, Tailscale IP, o header secret válido."""
    # Factor 1: Cloudflare Access SSO
    if cf_access_authenticated_user_email:
        return

    # Factor 2: red Tailscale o WireGuard (acceso interno directo por VPN)
    client_ip = request.client.host if request.client else ""
    if (
        client_ip.startswith(_TAILSCALE_PREFIX)
        or client_ip.startswith(_WIREGUARD_PREFIX)
        or client_ip == _DOCKER_GW
    ):
        return

    # Factor 3: secret header explícito (scripts/cron)
    expected = os.environ.get(_ENV_NAME)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"auth not configured: env var {_ENV_NAME} missing",
        )
    if not x_goncloud_secret:
        raise HTTPException(status_code=401, detail="auth required")
    if not hmac.compare_digest(
        x_goncloud_secret.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="auth failed")
