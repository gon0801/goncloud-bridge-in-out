"""Red de seguridad: como se alcanza a bridge-api vive en el compose, no en la mano.

Por que existe
--------------
Caso real (2026-08-24 -> 2026-09-12, ~19 dias de apagon silencioso):

  Los webhooks de MercadoLibre NO entran por nginx-proxy-manager. Entran por un
  Cloudflare Tunnel que corre como servicio del host, con la config en el
  dashboard de Cloudflare (no en disco), y que enruta
  `meli-webhooks.goncloud.cc` -> `http://localhost:8099`.

  El compose del servidor publicaba el puerto SOLO en la interfaz WireGuard
  (`10.13.13.1:8099:8099`). Al recrearse los contenedores el 2026-08-24 02:00
  UTC, cloudflared empezo a resolver `localhost` a `[::1]` y a comerse un
  `connection refused` en cada entrega:

    ERR Unable to reach the origin service ... dial tcp [::1]:8099:
        connect: connection refused
        originService=http://localhost:8099
        dest=https://meli-webhooks.goncloud.cc/webhooks/meli/orders/***

  138 intentos rechazados por dia, 19 dias, ~70-95 ordenes que nunca llegaron a
  Odoo. Amazon no se vio afectado: entra por polling. Los otros tres servicios
  del tunnel (8080/8081/8082) sí publican en 127.0.0.1 — bridge-api era la
  unica anomalia.

Que verifica
------------
1. Que el puerto se publique en loopback, que es lo que el tunnel necesita.
2. Que se declare la red `proxy`, que es lo que nginx-proxy-manager necesita
   para servir mapper.goncloud.cc (la UI del SKU mapper). Esa ruta tambien
   estaba rota, por una causa distinta y con consecuencias mucho menores.

Son dos caminos independientes hacia el mismo contenedor. Romper cualquiera de
los dos es invisible hasta que alguien nota que faltan ordenes.

Sobre el verde falso
--------------------
Un test que solo hiciera `assert "127.0.0.1" in open(compose).read()` daria
verde con esa cadena en cualquier comentario — y este archivo tiene varios. Por
eso se parsea el YAML de verdad y se afirma primero que el parseo encontro lo
que esperaba (`test_el_parseo_realmente_lee_el_compose`): si el archivo se
mueve, se renombra el servicio o cambia la forma del YAML, cae ese test primero
y avisa, en vez de mentir en verde.

Requiere PyYAML (esta en requirements.txt). El import es de modulo a proposito:
si falta, el test truena fuerte en vez de saltarse en silencio.
"""

import pathlib

import yaml

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"

# El unico servicio del bridge que recibe trafico entrante (webhooks de
# MeLi/Amazon, OAuth, /mapper, /v1/*). Los workers no exponen nada.
SERVICIO_EXPUESTO = "bridge-api"

PUERTO = "8099"

# Origen que espera el Cloudflare Tunnel: `http://localhost:8099`.
LOOPBACK = "127.0.0.1"

# Red en la que vive nginx-proxy-manager, que sirve mapper.goncloud.cc.
RED_DEL_PROXY = "proxy"


def cargar_compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def puertos_publicados() -> list[str]:
    return [str(p) for p in cargar_compose()["services"][SERVICIO_EXPUESTO]["ports"]]


def test_el_parseo_realmente_lee_el_compose():
    """Guardia anti-verde-falso: si esto pasa, los demas tests miran datos reales."""
    compose = cargar_compose()

    assert isinstance(compose, dict), f"{COMPOSE} no parseo como mapping YAML"
    assert "services" in compose, f"{COMPOSE} no tiene clave 'services'"
    assert SERVICIO_EXPUESTO in compose["services"], (
        f"'{SERVICIO_EXPUESTO}' no existe en {COMPOSE}. Si el servicio se "
        f"renombro, actualiza SERVICIO_EXPUESTO en este test."
    )

    servicio = compose["services"][SERVICIO_EXPUESTO]
    assert servicio.get("ports"), f"'{SERVICIO_EXPUESTO}' no declara puertos"
    assert servicio.get("networks"), f"'{SERVICIO_EXPUESTO}' no declara redes"


def test_bridge_api_publica_en_loopback_para_el_tunnel():
    """El bug de 2026-08-24: el puerto solo estaba en la interfaz WireGuard."""
    publicados = puertos_publicados()
    esperado = f"{LOOPBACK}:{PUERTO}:{PUERTO}"

    assert esperado in publicados, (
        f"'{SERVICIO_EXPUESTO}' no publica {esperado} (publica {publicados}). "
        f"El Cloudflare Tunnel enruta meli-webhooks.goncloud.cc a "
        f"http://localhost:{PUERTO}; sin el binding en loopback, cloudflared "
        f"resuelve localhost a [::1] y TODO webhook de MercadoLibre se pierde "
        f"en silencio."
    )


def test_el_puerto_no_queda_expuesto_a_todas_las_interfaces():
    """`- "8099:8099"` bindea 0.0.0.0. El firewall hoy lo tapa; no dependamos de eso."""
    publicados = puertos_publicados()

    sin_interfaz = [p for p in publicados if p.count(":") < 2]
    assert not sin_interfaz, (
        f"{sin_interfaz} publica en todas las interfaces (0.0.0.0). Fija la "
        f"interfaz explicitamente: '{LOOPBACK}:{PUERTO}:{PUERTO}'."
    )


def test_bridge_api_esta_en_la_red_del_proxy():
    """Ruta separada: nginx-proxy-manager -> mapper.goncloud.cc (UI del mapper)."""
    redes = cargar_compose()["services"][SERVICIO_EXPUESTO]["networks"]

    assert RED_DEL_PROXY in redes, (
        f"'{SERVICIO_EXPUESTO}' no declara la red '{RED_DEL_PROXY}' (declara {redes}). "
        f"Sin ella nginx no resuelve el upstream y mapper.goncloud.cc da 502. "
        f"No la conectes a mano con 'docker network connect': eso se borra en "
        f"el proximo recreate."
    )


def test_la_red_del_proxy_se_declara_externa():
    """`proxy` la crea el compose de nginx-proxy-manager; aca solo se referencia."""
    redes_top = cargar_compose().get("networks") or {}

    assert RED_DEL_PROXY in redes_top, (
        f"falta declarar '{RED_DEL_PROXY}' en el bloque 'networks' de nivel raiz"
    )
    assert (redes_top[RED_DEL_PROXY] or {}).get("external") is True, (
        f"'{RED_DEL_PROXY}' debe ser 'external: true' — la crea el compose de "
        f"nginx-proxy-manager, este compose no debe intentar crearla."
    )
