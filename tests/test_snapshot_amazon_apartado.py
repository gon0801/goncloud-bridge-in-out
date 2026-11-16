"""El snapshotter de Amazon FBM no publica lo apartado para Amazon Flex.

Corre `app/snapshotter_odoo_amazon_run_once.py` como lo corre cron, en un
subproceso, contra un Odoo falso por XML-RPC y una SQLite temporal. Mismo
contrato que el de MeLi: sin setting cuenta todos los almacenes, con el
setting excluye el apartado, y con un nombre que no existe sale 1 sin
publicar nada.
"""

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "app" / "snapshotter_odoo_amazon_run_once.py"

ALMACENES = [
    {"id": 1, "name": "EHV-MX", "code": "EHV"},
    {"id": 6, "name": "Flex - MX", "code": "FLEX"},
]
POR_ALMACEN = {1: 5, 6: 49}


class _HandlerCualquierPath(SimpleXMLRPCRequestHandler):
    rpc_paths = ()


def _execute_kw(db, uid, pwd, model, method, args, kwargs):
    if (model, method) == ("stock.warehouse", "search_read"):
        return ALMACENES
    if (model, method) != ("product.product", "search_read"):
        raise ValueError(f"llamada inesperada: {model}.{method}")
    almacenes = (kwargs.get("context") or {}).get("warehouse") or list(POR_ALMACEN)
    return [
        {
            "id": 77,
            "default_code": "TEST-FLEX",
            "qty_available": sum(POR_ALMACEN[a] for a in almacenes),
            "outgoing_qty": 1,
        }
    ]


def _correr(tmp_path, apartado=None):
    server = SimpleXMLRPCServer(
        ("127.0.0.1", 0),
        requestHandler=_HandlerCualquierPath,
        allow_none=True,
        logRequests=False,
    )
    server.register_function(lambda *a: 2, "authenticate")
    server.register_function(_execute_kw, "execute_kw")
    hilo = threading.Thread(target=server.serve_forever, daemon=True)
    hilo.start()

    db = tmp_path / "bridge.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bridge_settings(key TEXT PRIMARY KEY, value TEXT)")
    if apartado is not None:
        con.execute(
            "INSERT INTO bridge_settings VALUES ('warehouse_amazon_flex_mx', ?)", (apartado,)
        )
    con.execute(
        "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT, channel TEXT, item_count INTEGER NOT NULL, payload TEXT)"
    )
    con.execute(
        "CREATE TABLE snapshot_items(event_id TEXT, channel TEXT, sku TEXT,"
        " qty INTEGER, derived_zero INTEGER)"
    )
    con.commit()
    con.close()

    env = {
        **os.environ,
        "ODOO_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "ODOO_DB": "x",
        "ODOO_USER": "x",
        "ODOO_PASSWORD": "x",
        "BRIDGE_DB": str(db),
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        hilo.join(timeout=5)

    con = sqlite3.connect(db)
    filas = con.execute("SELECT sku, qty FROM snapshot_items").fetchall()
    eventos = con.execute("SELECT count(*) FROM events").fetchone()[0]
    con.close()
    return proc, filas, eventos


def test_sin_setting_cuenta_todos_los_almacenes(tmp_path):
    proc, filas, _ = _correr(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert filas == [("TEST-FLEX", 53)]


def test_con_el_setting_lo_apartado_no_se_publica(tmp_path):
    proc, filas, _ = _correr(tmp_path, "Flex - MX")
    assert proc.returncode == 0, proc.stderr
    assert filas == [("TEST-FLEX", 4)]


def test_un_apartado_que_no_existe_sale_1_sin_publicar(tmp_path):
    proc, filas, eventos = _correr(tmp_path, "Flex MX")
    assert proc.returncode == 1
    assert "[FATAL] almacen apartado 'Flex MX' no existe en Odoo" in proc.stdout
    assert (filas, eventos) == ([], 0)


def test_apartar_el_almacen_ehv_sale_1_sin_publicar(tmp_path):
    proc, filas, eventos = _correr(tmp_path, "EHV-MX")
    assert proc.returncode == 1
    assert "[FATAL] 'EHV-MX' es el almacen principal EHV; no se aparta" in proc.stdout
    assert (filas, eventos) == ([], 0)
