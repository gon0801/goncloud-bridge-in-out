"""Los pedidos AFN de MX salen de Flex (bodega EHV) o de FBA MX según un setting.

Por que existe
--------------
`detect_order_profile` trataba todo AFN de MX como Flex, y el poll dejaba pasar
los `Pending` de AFN MX por la misma suposición. El perfil `FBA_MX` existía en
`_PROFILE_MAP` pero no se alcanzaba. Cuando Flex se mueva a FBA México, cada
venta debe descontarse del almacén de su canal. El setting
`amazon_mx_afn_profile` (FLEX_MX | FBA_MX) elige el perfil; sin setting o con
un valor inválido queda FLEX_MX, el comportamiento anterior.

Worker y poll deben coincidir: si el worker cree que es FBA_MX y el poll sigue
encolando `Pending`, o al revés, el pedido se procesa con el perfil equivocado.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MX = "A1AM78C64UM0Y8"
US = "ATVPDKIKX0DER"


def _cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "bridge.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE bridge_settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO bridge_settings VALUES ('amazon_inbound_enabled', '1')")
    con.commit()
    con.close()
    return path


def _fijar_setting(db_path, valor):
    con = sqlite3.connect(db_path)
    con.execute("DELETE FROM bridge_settings WHERE key='amazon_mx_afn_profile'")
    if valor is not None:
        con.execute(
            "INSERT INTO bridge_settings VALUES ('amazon_mx_afn_profile', ?)", (valor,)
        )
    con.commit()
    con.close()


@pytest.fixture
def worker(db_path):
    mod = _cargar("amazon_inbound_worker_perfil", ROOT / "app" / "amazon_inbound_worker.py")
    mod.db = sqlite3.connect(db_path)
    yield mod
    mod.db.close()


@pytest.fixture
def poll(db_path, monkeypatch):
    mod = _cargar("amazon_orders_poll_perfil", ROOT / "tools" / "amazon_orders_poll.py")
    monkeypatch.setattr(mod, "DB_PATH", str(db_path))
    return mod


def _perfil(worker, db_path, valor, orden):
    _fijar_setting(db_path, valor)
    worker._settings_cache.clear()
    return worker.detect_order_profile(orden)


EASY_MX = {"FulfillmentChannel": "MFN", "MarketplaceId": MX, "EasyShipShipmentStatus": "PendingPickUp"}
FBM_MX = {"FulfillmentChannel": "MFN", "MarketplaceId": MX}
FBM_US = {"FulfillmentChannel": "MFN", "MarketplaceId": US}
AFN_MX = {"FulfillmentChannel": "AFN", "MarketplaceId": MX}
AFN_US = {"FulfillmentChannel": "AFN", "MarketplaceId": US}

FLEX = ("FBM", "Amazon Flex MX", "FLEX_MX")
FBA_MX = ("FBA", "Amazon FBA MX", "FBA_MX")


@pytest.mark.parametrize("valor", [None, "FLEX_MX", "FBA_MX", "valor-raro"])
@pytest.mark.parametrize(
    "orden,esperado",
    [
        (EASY_MX, ("FBM", "Amazon Easy MX", "EASY_MX")),
        (FBM_MX, ("FBM", "Amazon FBM MX", "FBM_MX")),
        (FBM_US, ("FBM", "Amazon FBM US", "FBM_US")),
        (AFN_US, ("FBA", "Amazon FBA US", "FBA_US")),
    ],
    ids=["easy_mx", "fbm_mx", "fbm_us", "fba_us"],
)
def test_el_setting_no_cambia_los_perfiles_que_no_son_afn_mx(
    worker, db_path, valor, orden, esperado
):
    assert _perfil(worker, db_path, valor, orden) == esperado


@pytest.mark.parametrize("valor", [None, "FLEX_MX"])
def test_afn_mx_es_flex_por_omision_y_con_flex_mx(worker, db_path, valor):
    assert _perfil(worker, db_path, valor, AFN_MX) == FLEX


def test_afn_mx_es_fba_mx_con_fba_mx(worker, db_path):
    assert _perfil(worker, db_path, "FBA_MX", AFN_MX) == FBA_MX


def test_el_setting_ignora_mayusculas_y_espacios(worker, db_path):
    assert _perfil(worker, db_path, " fba_mx ", AFN_MX) == FBA_MX


@pytest.mark.parametrize("valor", ["FBA_US", "flex", "0", "FBA MX"])
def test_valor_invalido_cae_a_flex_mx_con_aviso(worker, db_path, capsys, valor):
    assert _perfil(worker, db_path, valor, AFN_MX) == FLEX
    avisos = [
        json.loads(linea)
        for linea in capsys.readouterr().out.splitlines()
        if linea.startswith("{")
    ]
    assert [a["level"] for a in avisos if "inválido" in a["msg"]] == ["WARN"]


def test_valor_valido_o_ausente_no_avisa(worker, db_path, capsys):
    for valor in (None, "FLEX_MX", "FBA_MX"):
        _perfil(worker, db_path, valor, AFN_MX)
    assert "inválido" not in capsys.readouterr().out


def _orden_v2026(order_id, mp_id, fulfilled_by, status):
    return {
        "orderId": order_id,
        "salesChannel": {"marketplaceId": mp_id},
        "fulfillment": {"fulfilledBy": fulfilled_by, "fulfillmentStatus": status},
    }


def _correr_poll(poll, monkeypatch, capsys, ordenes):
    empujados = []

    class FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr(sys, "argv", ["amazon_orders_poll.py", "--marketplace", "MX"])
    monkeypatch.setattr(poll, "get_credentials", lambda: {"id": "x"})
    monkeypatch.setattr(poll, "get_access_token", lambda creds: "token")
    monkeypatch.setattr(poll.redis.Redis, "from_url", lambda *a, **k: FakeRedis())
    monkeypatch.setattr(poll, "get_orders", lambda *a, **k: ordenes)
    monkeypatch.setattr(
        poll,
        "push_to_redis",
        lambda r, order, key: empujados.append(order["AmazonOrderId"]),
    )
    assert poll.main() == 0
    return empujados, capsys.readouterr()


POLL_ORDENES = [
    _orden_v2026("AFN-PENDING", MX, "AMAZON", "PENDING"),
    _orden_v2026("AFN-UNSHIPPED", MX, "AMAZON", "UNSHIPPED"),
    _orden_v2026("MFN-PENDING", MX, "MERCHANT", "PENDING"),
]


@pytest.mark.parametrize("valor", [None, "FLEX_MX", "valor-raro", ""])
def test_poll_encola_el_pending_de_afn_mx_si_es_flex(
    poll, db_path, monkeypatch, capsys, valor
):
    _fijar_setting(db_path, valor)
    empujados, salida = _correr_poll(poll, monkeypatch, capsys, POLL_ORDENES)
    assert empujados == ["AFN-PENDING", "AFN-UNSHIPPED"]
    assert "AFN-PENDING: status=Pending (Flex MX), encolando" in salida.out


def test_poll_salta_el_pending_de_afn_mx_si_es_fba_mx(
    poll, db_path, monkeypatch, capsys
):
    _fijar_setting(db_path, "FBA_MX")
    empujados, salida = _correr_poll(poll, monkeypatch, capsys, POLL_ORDENES)
    assert empujados == ["AFN-UNSHIPPED"]
    assert "AFN-PENDING: status=Pending, skipping" in salida.out


def test_poll_avisa_con_valor_invalido(poll, db_path, monkeypatch, capsys):
    _fijar_setting(db_path, "valor-raro")
    _, salida = _correr_poll(poll, monkeypatch, capsys, POLL_ORDENES)
    assert "amazon_mx_afn_profile='VALOR-RARO' inválido" in salida.err
