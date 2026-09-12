"""Toda tabla que pueda poner el health en rojo necesita un refresco automatico.

Por que existe
--------------
Caso real (2026-08-10 -> 2026-09-12):

  `/v1/health` mide la edad de `amazon_inventory_cache` y con mas de
  AMAZON_CACHE_ERROR_HOURS (168h) pone `ok: false`. Pero esa tabla solo se
  escribia a mano, llamando `POST /api/amazon/refresh-inventory`. Nadie la
  llamaba desde el 10-ago, asi que el health quedo en rojo permanente.

  La tabla en si es cosmetica: alimenta /api/amazon/amazon-skus, la lista de
  SKUs del mapper UI. Ninguna orden depende de ella.

  El dano no fue el mapper desactualizado. Fue que el 24-ago se cayo el ingress
  de MercadoLibre — 19 dias, ~138 entregas rechazadas por dia, 18 ordenes que
  nunca llegaron a Odoo — y el semaforo llevaba dos semanas en rojo por algo
  inofensivo. La senal existia; ya no significaba nada.

Que verifica
------------
La cadena completa, leyendo los archivos de verdad:

  1. El health mide la edad de una tabla y eso puede volver `ok` falso.
  2. Existe un endpoint que ESCRIBE esa tabla.
  3. Un script de `tools/` llama a ese endpoint.
  4. Un `.service` de systemd ejecuta ese script.
  5. Un `.timer` dispara ese `.service`.

Si alguien agrega otro chequeo de vejez sin su refresco, o borra el timer, la
cadena se corta y esto cae en CI — en vez de descubrirlo un mes despues, cuando
el rojo cronico ya tapo algo grave.

Sobre el verde falso
--------------------
Cada paso afirma primero que ENCONTRO lo que buscaba
(`test_la_extraccion_encuentra_la_cadena_completa`). Sin eso, un cambio de
formato haria que los regex no matchearan nada y todos los `assert ... in`
pasarian sobre conjuntos vacios.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = REPO / "app" / "main.py"
TOOLS = REPO / "tools"
SYSTEMD = TOOLS / "systemd"


@pytest.fixture(scope="module")
def fuente_main() -> str:
    return MAIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tabla_vigilada(fuente_main: str) -> str:
    """La tabla cuya vejez mide el health."""
    m = re.search(r"SELECT\s+MAX\(updated_at\)\s+FROM\s+(\w+)", fuente_main)
    assert m, (
        f"no se encontro el chequeo de vejez en {MAIN}. Si cambio de forma, "
        f"actualiza este test — no lo borres: sin el, una tabla puede volver a "
        f"poner el health en rojo sin que nada la refresque."
    )
    return m.group(1)


@pytest.fixture(scope="module")
def endpoint_que_refresca(fuente_main: str, tabla_vigilada: str) -> str:
    """La ruta del endpoint que escribe esa tabla."""
    escritura = re.search(rf"INSERT\s+INTO\s+{tabla_vigilada}\b", fuente_main)
    assert escritura, f"nada escribe {tabla_vigilada} en {MAIN}"

    # La ruta es el ultimo decorador @app.<verbo> antes de la escritura.
    previo = fuente_main[: escritura.start()]
    rutas = re.findall(r'@app\.(?:get|post|put)\(\s*"([^"]+)"', previo)
    assert rutas, f"no hay decorador @app.* antes de la escritura de {tabla_vigilada}"
    return rutas[-1]


def test_la_extraccion_encuentra_la_cadena_completa(
    tabla_vigilada, endpoint_que_refresca
):
    """Guardia anti-verde-falso: si esto pasa, los demas tests miran datos reales."""
    assert tabla_vigilada, "no se identifico la tabla vigilada"
    assert endpoint_que_refresca.startswith("/"), (
        f"ruta con forma inesperada: {endpoint_que_refresca!r}"
    )
    assert SYSTEMD.is_dir(), f"falta el directorio {SYSTEMD}"


def test_un_script_del_repo_llama_al_endpoint_de_refresco(endpoint_que_refresca):
    """Paso 3: el refresco no puede depender de que alguien lo dispare a mano."""
    llaman = [
        f
        for f in TOOLS.glob("*.sh")
        if endpoint_que_refresca in f.read_text(encoding="utf-8")
    ]

    assert llaman, (
        f"ningun script de tools/ llama a {endpoint_que_refresca}. El health "
        f"vigila una tabla que solo se refresca a mano: va a quedar en rojo "
        f"cronico y a tapar fallas reales."
    )


def test_un_timer_de_systemd_dispara_ese_script(endpoint_que_refresca):
    """Pasos 4 y 5: script -> .service -> .timer, todo versionado en el repo."""
    script = next(
        f
        for f in TOOLS.glob("*.sh")
        if endpoint_que_refresca in f.read_text(encoding="utf-8")
    )

    servicios = [
        u
        for u in SYSTEMD.glob("*.service")
        if script.name in u.read_text(encoding="utf-8")
    ]
    assert servicios, (
        f"ningun .service de {SYSTEMD} ejecuta {script.name}. Un script que "
        f"nadie dispara es igual de inutil que no tenerlo."
    )

    servicio = servicios[0]
    timers = [
        t
        for t in SYSTEMD.glob("*.timer")
        if servicio.name in t.read_text(encoding="utf-8")
    ]
    assert timers, (
        f"ningun .timer dispara {servicio.name}. Los units van versionados en "
        f"el repo a proposito: la infra que vive solo en el servidor se pierde "
        f"en el siguiente recreate y falla en silencio."
    )

    contenido = timers[0].read_text(encoding="utf-8")
    assert "OnCalendar=" in contenido, f"{timers[0].name} no declara OnCalendar"
    assert "Persistent=true" in contenido, (
        f"{timers[0].name} sin Persistent=true: si el server esta apagado a la "
        f"hora programada, se saltea la corrida y el cache envejece igual"
    )
