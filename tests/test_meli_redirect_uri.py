"""El redirect de OAuth no puede estar hardcodeado ni apuntar a un host muerto.

Por que existe
--------------
`app/main.py:156` tenia literal:

    MELI_REDIRECT_URI = "https://meli.goncloud.cc/oauth/callback"

Ese host es del servidor viejo. Verificado contra produccion el 2026-09-12: no
tiene proxy host en nginx-proxy-manager (revisada la tabla completa, incluidos
los borrados), no tiene certificado (ni siquiera uno eliminado) y no esta en el
ingress del Cloudflare Tunnel.

Nadie lo noto durante meses porque el refresh diario usa el `refresh_token` y
no necesita redirect. Confirmado contra la API de MeLi:

    GET /applications/{app_id}
      callback_url               = https://meli.goncloud.cc/oauth/callback   <- muerto
      notifications_callback_url = https://meli-webhooks.goncloud.cc/...     <- vivo

O sea: **una re-auth desde cero habria fallado**, justo en el escenario donde
mas urge (token irrecuperable, hay que reconectar ya).

Que verifica
------------
1. Que el valor sea configurable, no un literal.
2. Que el host muerto no vuelva a aparecer en el codigo.
3. Que los dos puntos del flujo OAuth usen la MISMA fuente. Si divergen, MeLi
   rechaza el canje del code con un error que no dice nada util: valida el
   redirect_uri al pedir autorizacion y otra vez al canjear.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
MAIN = APP / "main.py"
CONFIG = APP / "meli_config.py"

# El host del servidor viejo. No debe volver al codigo.
HOST_MUERTO = "meli.goncloud.cc"


@pytest.fixture(scope="module")
def cfg():
    spec = importlib.util.spec_from_file_location("meli_config_bajo_prueba", CONFIG)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meli_config_bajo_prueba"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_settings_gana_sobre_env_y_default(cfg):
    """bridge_settings primero: permite corregirlo sin redeploy."""
    valor = cfg.resolver_redirect_uri(
        desde_settings="https://desde-settings/oauth/callback",
        desde_env="https://desde-env/oauth/callback",
    )
    assert valor == "https://desde-settings/oauth/callback"


def test_env_gana_sobre_el_default(cfg):
    valor = cfg.resolver_redirect_uri(
        desde_settings=None, desde_env="https://desde-env/oauth/callback"
    )
    assert valor == "https://desde-env/oauth/callback"


def test_un_valor_vacio_no_cuenta_como_configurado(cfg):
    """Un setting en blanco es el caso real: la fila existe con value=''."""
    assert cfg.resolver_redirect_uri(desde_settings="   ", desde_env=None) == (
        cfg.REDIRECT_URI_DEFAULT
    )
    assert cfg.resolver_redirect_uri(desde_settings="", desde_env="") == (
        cfg.REDIRECT_URI_DEFAULT
    )


def test_el_default_no_apunta_al_host_muerto(cfg):
    """El bug original. `meli-webhooks.goncloud.cc` si esta servido por el tunnel."""
    assert HOST_MUERTO not in cfg.REDIRECT_URI_DEFAULT, (
        f"el default sigue apuntando a {HOST_MUERTO}, que no tiene proxy host, "
        f"ni certificado, ni entrada en el tunnel"
    )
    assert cfg.REDIRECT_URI_DEFAULT.startswith("https://")
    assert cfg.REDIRECT_URI_DEFAULT.endswith("/oauth/callback")


def test_el_host_muerto_no_quedo_en_el_codigo():
    """Guardia de regresion sobre el fuente real, no sobre una constante."""
    fuente = MAIN.read_text(encoding="utf-8")
    lineas = [
        (i, ln)
        for i, ln in enumerate(fuente.splitlines(), 1)
        if HOST_MUERTO in ln and not ln.lstrip().startswith("#")
    ]
    assert not lineas, (
        f"{MAIN.name} menciona {HOST_MUERTO} fuera de un comentario: "
        f"{[f'linea {i}' for i, _ in lineas]}"
    )


def test_los_dos_puntos_del_flujo_usan_la_misma_fuente():
    """Si /oauth/start y /oauth/callback difieren, MeLi rechaza el canje.

    Guardia anti-verde-falso incluido: se afirma primero que se encontraron DOS
    usos. Si el flujo cambia de forma y la busqueda no matchea nada, este test
    cae en vez de pasar sobre una lista vacia.
    """
    fuente = MAIN.read_text(encoding="utf-8")
    usos = [
        ln.strip()
        for ln in fuente.splitlines()
        if '"redirect_uri"' in ln and not ln.lstrip().startswith("#")
    ]

    assert len(usos) == 2, (
        f"se esperaban 2 usos de redirect_uri (authorize y canje del code), "
        f"se encontraron {len(usos)}: {usos}"
    )
    assert usos[0] == usos[1], (
        f"los dos puntos del flujo OAuth arman el redirect_uri distinto:\n"
        f"  {usos[0]}\n  {usos[1]}\n"
        f"MeLi lo valida en ambos pasos; si difieren, el canje falla con un "
        f"error que no explica nada."
    )
