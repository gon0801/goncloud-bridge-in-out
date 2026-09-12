"""Red de seguridad: la topologia de red del bridge vive en el compose, no en la mano.

Por que existe
--------------
Caso real (2026-08-24 -> 2026-09-12, ~19 dias de apagon silencioso):

  `bridge-api` quedaba expuesto a internet por `nginx-proxy-manager`, que vive
  en la red docker `proxy`. Pero el compose solo declaraba `goncloud-net` y
  `odoo_odoo_net`. La union a `proxy` estaba hecha a mano con
  `docker network connect`, que NO sobrevive a un recreate del contenedor.

  El 2026-08-24 02:00 UTC se recrearon los contenedores. `bridge-api` volvio
  sin la red `proxy`; nginx dejo de resolver el nombre del upstream
  (`getent hosts bridge-api` -> fallo) y todo POST de MercadoLibre murio en el
  proxy. Ultimo webhook: 01:44 UTC, 16 minutos antes del recreate.

  Amazon no se entero porque entra por polling, no por webhook. Nadie noto
  nada durante 19 dias: ~70-95 ordenes de MeLi nunca llegaron a Odoo.

Que verifica
------------
Que el compose declare, por si mismo, TODAS las redes que `bridge-api`
necesita para recibir trafico — incluida `proxy`. Si alguien vuelve a dejar
la union fuera del compose, esto cae en CI antes de llegar al servidor.

Sobre el verde falso
--------------------
Un test que solo hiciera `assert "proxy" in open(compose).read()` daria verde
con la palabra "proxy" en cualquier comentario. Por eso se parsea el YAML de
verdad y se afirma primero que el parseo encontro lo que esperaba
(`test_el_parseo_realmente_lee_el_compose`): si el archivo se mueve, se
renombra el servicio o cambia la forma del YAML, cae ese test primero y avisa,
en vez de mentir en verde.

Requiere PyYAML (esta en requirements.txt). El import es de modulo a proposito:
si falta, el test truena fuerte en vez de saltarse en silencio.
"""

import pathlib

import yaml

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"

# Red en la que vive nginx-proxy-manager. Sin esto, bridge-api es inalcanzable
# desde internet y los webhooks de MercadoLibre se pierden sin avisar.
RED_DEL_PROXY = "proxy"

# El unico servicio del bridge que recibe trafico entrante de internet
# (webhooks de MeLi/Amazon, OAuth, /mapper, /v1/*). Los workers no exponen nada.
SERVICIO_EXPUESTO = "bridge-api"


def cargar_compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_el_parseo_realmente_lee_el_compose():
    """Guardia anti-verde-falso: si esto pasa, los demas tests miran datos reales."""
    compose = cargar_compose()

    assert isinstance(compose, dict), f"{COMPOSE} no parseo como mapping YAML"
    assert "services" in compose, f"{COMPOSE} no tiene clave 'services'"
    assert SERVICIO_EXPUESTO in compose["services"], (
        f"'{SERVICIO_EXPUESTO}' no existe en {COMPOSE}. Si el servicio se "
        f"renombro, actualiza SERVICIO_EXPUESTO en este test."
    )
    assert compose["services"][SERVICIO_EXPUESTO].get("networks"), (
        f"'{SERVICIO_EXPUESTO}' no declara ninguna red — forma inesperada del YAML"
    )


def test_bridge_api_esta_en_la_red_del_proxy():
    """El bug de 2026-08-24: la union a `proxy` vivia fuera del compose."""
    redes = cargar_compose()["services"][SERVICIO_EXPUESTO]["networks"]

    assert RED_DEL_PROXY in redes, (
        f"'{SERVICIO_EXPUESTO}' no declara la red '{RED_DEL_PROXY}' (declara {redes}). "
        f"Sin ella nginx-proxy-manager no resuelve el upstream y TODOS los "
        f"webhooks de MercadoLibre se pierden en silencio. No la conectes a mano "
        f"con 'docker network connect': eso se borra en el proximo recreate."
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
