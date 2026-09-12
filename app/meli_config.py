"""Configuracion de MercadoLibre que no debe vivir hardcodeada en el codigo.

Aparte de `main.py` porque ese modulo monta StaticFiles al importarse y no se
puede cargar fuera del contenedor; esto se testea directo.
"""

from typing import Optional

# Host servido por el Cloudflare Tunnel (`cloudflared.service`), el mismo por
# donde entran los webhooks. Es el unico camino a bridge-api verificado
# end-to-end el 2026-09-12.
#
# El valor anterior era `https://meli.goncloud.cc/oauth/callback`, hardcodeado
# en main.py:156. Ese host es del servidor viejo: no tiene proxy host, ni
# certificado, ni entrada en el tunnel. Dejo de existir en la migracion del
# 2026-05-02 y nadie lo noto durante meses porque el refresh diario usa el
# `refresh_token` y no necesita redirect. Una re-auth desde cero habria
# fallado — justo cuando mas urge.
REDIRECT_URI_DEFAULT = "https://meli-webhooks.goncloud.cc/oauth/callback"


def resolver_redirect_uri(
    desde_settings: Optional[str] = None,
    desde_env: Optional[str] = None,
) -> str:
    """Precedencia: bridge_settings -> env -> default.

    `bridge_settings` va primero para poder corregirlo en caliente, sin
    redeploy. Es la leccion de CLAUDE.md PROBLEMA 10: la config que solo se lee
    al arrancar deja el sistema roto hasta que alguien reinicia, y nadie
    relaciona una cosa con la otra.

    OJO: este valor DEBE coincidir EXACTO con el registrado en el DevCenter de
    MeLi. Lo valida dos veces — al pedir la autorizacion y al canjear el code.
    Cambiar uno sin el otro rompe OAuth.
    """
    for candidato in (desde_settings, desde_env):
        if candidato and candidato.strip():
            return candidato.strip()
    return REDIRECT_URI_DEFAULT
