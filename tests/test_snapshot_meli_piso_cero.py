"""El snapshotter de MeLi debe pisar a 0 las cantidades negativas de Odoo.

Por que existe
--------------
`app/snapshotter_odoo_meli_run_once.py` corre por cron dentro de bridge-api y
vuelca `qty_available` de Odoo en `snapshot_items` tal cual. Cuando un kit
tiene un componente de su BOM en negativo, Odoo reporta disponibilidad
negativa (medido en produccion: -102 para los SKUs NH-CAR-ROJ-*) y ese numero
crudo viaja al worker, que se lo manda a MercadoLibre. MeLi rechaza stock
negativo con HTTP 400 (`item.variations.total_stock...`) y la publicacion
queda congelada reintentando cada 5 minutos (FAIL en `processed_events`).

El snapshotter hermano de Amazon ya tiene el piso
(`app/snapshotter_odoo_amazon_run_once.py`: `max(... - outgoing_qty, 0)`).

Como se prueba
--------------
El script es de nivel modulo y corre como proceso aparte; el test lo ejecuta
igual que cron (subproceso con env) contra:

- un Odoo falso: SimpleXMLRPCServer en 127.0.0.1 que responde `authenticate`
  y `execute_kw` con dos productos, uno en -102 y otro en 134;
- una SQLite temporal con el schema minimo que el script toca.

Las aserciones son sobre el estado observable en la base, no sobre el fuente.
"""

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "app" / "snapshotter_odoo_meli_run_once.py"

# -102 replica el caso real de NH-CAR-ROJ-*; 134 es el control sano.
PRODUCTOS_ODOO = [
    {"default_code": "TEST-ROJ-NEG", "qty_available": -102},
    {"default_code": "TEST-AZU-OK", "qty_available": 134},
]


class _HandlerCualquierPath(SimpleXMLRPCRequestHandler):
    # El script pega contra /xmlrpc/2/common y /xmlrpc/2/object; con
    # rpc_paths vacio el handler de la stdlib acepta cualquier path.
    rpc_paths = ()


def _authenticate(db, user, password, extra):
    return 2


def _execute_kw(db, uid, pwd, model, method, args, kwargs):
    if (model, method) != ("product.product", "search_read"):
        raise ValueError(f"llamada inesperada: {model}.{method}")
    return [dict(p) for p in PRODUCTOS_ODOO]


def _crear_bridge_db(db_path: Path) -> None:
    """Schema minimo: solo las tablas y columnas que el script toca."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sku_mapping(channel TEXT, sku TEXT)")
    conn.execute(
        "INSERT INTO sku_mapping(channel, sku) VALUES"
        " ('meli','TEST-ROJ-NEG'),('meli','TEST-AZU-OK')"
    )
    conn.execute(
        "CREATE TABLE events("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "created_at TEXT, channel TEXT,"
        "item_count INTEGER NOT NULL, payload TEXT)"
    )
    conn.execute(
        "CREATE TABLE snapshot_items(event_id TEXT, channel TEXT, sku TEXT, qty INTEGER)"
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def corrida(tmp_path_factory):
    """Corre el snapshotter real una vez, como lo hace cron, contra el stub."""
    server = SimpleXMLRPCServer(
        ("127.0.0.1", 0),
        requestHandler=_HandlerCualquierPath,
        allow_none=True,
        logRequests=False,
    )
    server.register_function(_authenticate, "authenticate")
    server.register_function(_execute_kw, "execute_kw")
    hilo = threading.Thread(target=server.serve_forever, daemon=True)
    hilo.start()

    db_path = tmp_path_factory.mktemp("bridge") / "bridge.db"
    _crear_bridge_db(db_path)

    env = {
        **os.environ,
        "ODOO_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "ODOO_DB": "x",
        "ODOO_USER": "x",
        "ODOO_PASSWORD": "x",
        "BRIDGE_DB": str(db_path),
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,  # el test mismo afirma el exit code con contexto
        )
    finally:
        server.shutdown()
        server.server_close()
        hilo.join(timeout=5)

    return proc, db_path


def _qty_de(db_path: Path, sku: str) -> int:
    conn = sqlite3.connect(db_path)
    fila = conn.execute("SELECT qty FROM snapshot_items WHERE sku=?", (sku,)).fetchone()
    conn.close()
    assert fila is not None, f"{sku} no llego a snapshot_items"
    return fila[0]


def test_el_script_termina_ok(corrida):
    proc, _ = corrida
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert '"SNAPSHOT_OK": true' in proc.stdout, proc.stdout


def test_qty_negativa_se_pisa_a_cero(corrida):
    """El corazon del fix: -102 de Odoo no puede quedar persistido como -102."""
    _, db_path = corrida
    qty = _qty_de(db_path, "TEST-ROJ-NEG")
    assert qty == 0, (
        f"qty_available=-102 de Odoo quedo persistido como {qty}: ese valor "
        "negativo viaja a MeLi (HTTP 400) y congela la publicacion"
    )


def test_qty_positiva_pasa_intacta(corrida):
    """Control: el piso no aplana las cantidades sanas."""
    _, db_path = corrida
    assert _qty_de(db_path, "TEST-AZU-OK") == 134
