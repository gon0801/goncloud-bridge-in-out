"""FastAPI auth dependency compartido — bridge GONCLOUD.

Provee `require_secret`, un Depends() que valida acceso autenticado a
endpoints internos (`/api/*`, `/v1/*`, `/setup/api/*`, `/oauth/refresh`).

Acepta DOS factores (cualquiera basta):
1. Header `X-Goncloud-Secret` válido contra env `GONCLOUD_API_SECRET`.
   Path para scripts ops/cron que invocan al bridge directamente.
2. Header `Cf-Access-Authenticated-User-Email` presente (lo inyecta
   Cloudflare Access tras autenticar al operador vía Google SSO).
   Path para el UI del mapper / setup wizard accedidos por el operador
   en el browser. **Solo confiable porque el origin del bridge es
   cloudflared tunnel-only**: ningún cliente puede llegar al backend
   sin pasar por Cloudflare, así que ese header no es spoofable.

Importar este módulo NO requiere que `GONCLOUD_API_SECRET` esté seteado;
el chequeo es lazy en cada request. Si la env var no está presente Y
tampoco hay Cf-Access header, se rechaza con 503 (config faltante).

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

from fastapi import Header, HTTPException


_ENV_NAME = "GONCLOUD_API_SECRET"
_CF_ACCESS_HEADER = "Cf-Access-Authenticated-User-Email"


def require_secret(
    x_goncloud_secret: str | None = Header(default=None),
    cf_access_authenticated_user_email: str | None = Header(default=None),
) -> None:
    """Dependency dual-factor: header secret VÁLIDO o Cf-Access presente.

    - Cf-Access header presente → OK (operador autenticado vía SSO).
    - Header secret válido → OK (script ops con credencial).
    - Ambos ausentes → 401.
    - Header secret presente pero mismatch → 401.
    - Ningún factor configurado en el bridge → 503.
    """
    if cf_access_authenticated_user_email:
        # Cloudflare Access ya autenticó al operador. El origin es tunnel-only,
        # así que este header no llega de un cliente externo no-Cloudflare.
        return
    expected = os.environ.get(_ENV_NAME)
    if not expected:
        # No hay header SSO ni env del secret. Decidimos fail-closed con 503
        # (config error) para que un deploy mal configurado sea visible y no
        # silencioso. Si en algún momento el bridge debe aceptar SOLO Cf-Access
        # sin nunca header, hay que setear GONCLOUD_API_SECRET aunque sea a un
        # placeholder y nunca usarlo desde clientes.
        raise HTTPException(
            status_code=503,
            detail=f"auth not configured: env var {_ENV_NAME} missing and no Cf-Access header",
        )
    if not x_goncloud_secret:
        raise HTTPException(status_code=401, detail="auth required")
    if not hmac.compare_digest(
        x_goncloud_secret.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="auth failed")
