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

# Productos simples: id -> (codigo, tipo, fisico por almacen, salidas sin enviar).
# -102 replica el caso real de NH-CAR-ROJ-*; 134 es el control sano.
# TEST-VENDIDO y TEST-SOBREVENDIDO tienen ventas sin enviar (outgoing_qty):
# MeLi debe recibir lo mismo que Amazon, fisico menos salidas.
# TEST-FLEX tiene 5 en EHV-MX y 49 apartadas en Flex. Odoo sin contexto suma
# todos los almacenes; con `warehouse` suma solo los que recibe.
SIMPLES = {
    1: ("TEST-ROJ-NEG", "product", {1: -102}, 0),
    2: ("TEST-AZU-OK", "product", {1: 134}, 0),
    3: ("TEST-VENDIDO", "product", {1: 10}, 3),
    4: ("TEST-SOBREVENDIDO", "product", {1: 4}, 6),
    5: ("TEST-FLEX", "product", {1: 5, 6: 49}, 0),
    # Estuche Italia: 3 en EHV, las 3 vendidas en ventas de otros kits, y 40
    # apartadas en Flex.
    101: ("4405-BG", "product", {1: 3, 6: 40}, 3),
    102: ("MON-CEN", "product", {1: 10}, 0),
    103: ("MON-VCO", "product", {1: 9}, 0),
    104: ("ARR-22", "product", {1: 5}, 0),
    105: ("COF-AGOTADO", "product", {1: 0}, 0),
    106: ("BOLSA", "consu", {}, 0),
    107: ("CINTA-M", "product", {1: 1.5}, 0),
    108: ("PERLA", "product", {1: 1}, 0),
}
UNIDAD, DOCENA = 1, 2
# Kits phantom: id -> (codigo, [(componente, unidades por kit[, unidad de la linea])])
KITS = {
    20: ("SET-ITA-CEN", [(101, 1), (102, 1)]),
    21: ("SET-ITA-VCO", [(101, 1), (103, 1)]),
    22: ("SET-ARR-DOBLE", [(104, 2), (106, 1)]),
    23: ("SET-AGOTADO", [(102, 1), (105, 1)]),
    24: ("SET-ANIDADO", [(22, 1), (103, 1)]),
    25: ("SET-REPETIDO", [(102, 1), (102, 1)]),
    26: ("SUB-CEN-VCO", [(102, 1), (103, 1)]),
    27: ("SET-REPETIDO-ANIDADO", [(26, 1), (102, 1)]),
    28: ("SET-LINEA-DOCENA", [(102, 1, DOCENA)]),
    29: ("SET-CINTA", [(107, 0.5)]),
    30: ("SET-PERLA", [(108, 0.1)]),
    31: ("SET-BOM-DOCENA", [(103, 1)]),
}
UNIDAD_BOM = {31: DOCENA}
EN_MELI = [1, 2, 3, 4, 5, 20, 21, 22, 23, 24, 25, 27, 28, 29, 30, 31]

ALMACENES = [
    {"id": 1, "name": "EHV-MX", "code": "EHV"},
    {"id": 6, "name": "Flex - MX", "code": "FLEX"},
]


class _HandlerCualquierPath(SimpleXMLRPCRequestHandler):
    # El script pega contra /xmlrpc/2/common y /xmlrpc/2/object; con
    # rpc_paths vacio el handler de la stdlib acepta cualquier path.
    rpc_paths = ()


def _authenticate(db, user, password, extra):
    return 2


def _producto(pid: int, almacenes: list[int]) -> dict:
    """Como Odoo 17: a un kit le da qty_available y outgoing_qty como el
    minimo de cada campo sobre sus componentes almacenables, por separado."""
    if pid in SIMPLES:
        codigo, tipo, fisico, salidas = SIMPLES[pid]
        qa = sum(q for a, q in fisico.items() if a in almacenes)
        out = salidas
    else:
        codigo, receta = KITS[pid]
        tipo = "consu"
        comps = [(_producto(c, almacenes), u) for c, u, *_ in receta]
        comps = [(c, u) for c, u in comps if c["type"] == "product" or c["id"] in KITS]
        qa = min(c["qty_available"] // u for c, u in comps)
        out = min(c["outgoing_qty"] // u for c, u in comps)
    return {
        "id": pid,
        "default_code": codigo,
        "type": tipo,
        "qty_available": qa,
        "outgoing_qty": out,
        "product_tmpl_id": [1000 + pid, codigo],
        "uom_id": [UNIDAD, "Units"],
    }


def _execute_kw(db, uid, pwd, model, method, args, kwargs):
    almacenes = (kwargs.get("context") or {}).get("warehouse") or [1, 6]
    campos = ["id", *(kwargs.get("fields") or [])]
    if (model, method) == ("stock.warehouse", "search_read"):
        return ALMACENES
    if (model, method) == ("mrp.bom", "search_read"):
        return [
            {
                "id": k,
                "product_id": [k, codigo],
                "product_tmpl_id": [1000 + k, codigo],
                "product_qty": 1.0,
                "product_uom_id": [UNIDAD_BOM.get(k, UNIDAD), "x"],
                "bom_line_ids": [k * 10 + i for i in range(len(receta))],
                "sequence": 0,
            }
            for k, (codigo, receta) in KITS.items()
        ]
    if (model, method) == ("mrp.bom.line", "read"):
        lineas = {
            k * 10 + i: {
                "id": k * 10 + i,
                "product_id": [linea[0], "x"],
                "product_qty": float(linea[1]),
                "product_uom_id": [(linea[2:] or (UNIDAD,))[0], "x"],
                "bom_product_template_attribute_value_ids": [],
            }
            for k, (_, receta) in KITS.items()
            for i, linea in enumerate(receta)
        }
        return [lineas[i] for i in args[0]]
    if model == "product.product" and method in ("search_read", "read"):
        ids = EN_MELI if method == "search_read" else args[0]
        return [
            {k: v for k, v in _producto(i, almacenes).items() if k in campos} for i in ids
        ]
    raise ValueError(f"llamada inesperada: {model}.{method}")


def _crear_bridge_db(db_path: Path, apartado: str | None) -> None:
    """Schema minimo: solo las tablas y columnas que el script toca."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sku_mapping(channel TEXT, sku TEXT)")
    conn.execute(
        "INSERT INTO sku_mapping(channel, sku) VALUES "
        + ",".join(f"('meli','{_producto(i, [1])['default_code']}')" for i in EN_MELI)
    )
    conn.execute("CREATE TABLE bridge_settings(key TEXT PRIMARY KEY, value TEXT)")
    if apartado is not None:
        conn.execute(
            "INSERT INTO bridge_settings VALUES ('warehouse_amazon_flex_mx', ?)", (apartado,)
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


def _correr(tmp_path_factory, apartado=None):
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
    _crear_bridge_db(db_path, apartado)

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


@pytest.fixture(scope="module")
def corrida(tmp_path_factory):
    return _correr(tmp_path_factory)


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


def test_descuenta_lo_vendido_sin_enviar(corrida):
    """MeLi publicaba qty_available y sobrevendia lo ya vendido sin enviar."""
    _, db_path = corrida
    assert _qty_de(db_path, "TEST-VENDIDO") == 7


def test_mas_salidas_que_fisico_queda_en_cero(corrida):
    _, db_path = corrida
    assert _qty_de(db_path, "TEST-SOBREVENDIDO") == 0


def test_kit_con_2_unidades_en_receta_publica_la_mitad(corrida):
    """5 aros alcanzan para 2 sets; la bolsa no almacenable no limita."""
    _, db_path = corrida
    assert _qty_de(db_path, "SET-ARR-DOBLE") == 2


def test_kit_con_un_componente_en_cero_queda_en_cero(corrida):
    _, db_path = corrida
    assert _qty_de(db_path, "SET-AGOTADO") == 0


def test_kit_anidado_se_calcula_hasta_sus_hojas(corrida):
    _, db_path = corrida
    assert _qty_de(db_path, "SET-ANIDADO") == 2


def test_componente_repetido_en_dos_lineas_suma_lo_que_pide_el_kit(corrida):
    """10 piezas con dos lineas de 1 por kit dan 5, no 10."""
    _, db_path = corrida
    assert _qty_de(db_path, "SET-REPETIDO") == 5


def test_componente_repetido_en_dos_niveles_suma_lo_que_pide_el_kit(corrida):
    """El subkit pide 1 MON-CEN y el kit otro: 2 por kit, 10 piezas dan 5."""
    _, db_path = corrida
    assert _qty_de(db_path, "SET-REPETIDO-ANIDADO") == 5


def test_linea_en_otra_unidad_que_el_componente_publica_cero(corrida):
    proc, db_path = corrida
    assert _qty_de(db_path, "SET-LINEA-DOCENA") == 0
    assert "kit 28: linea de 102 en otra unidad que el componente" in proc.stderr


def test_bom_en_otra_unidad_que_el_kit_publica_cero(corrida):
    proc, db_path = corrida
    assert _qty_de(db_path, "SET-BOM-DOCENA") == 0
    assert "kit 31: BOM en otra unidad que el producto" in proc.stderr


def test_lo_libre_fraccionario_no_se_trunca_antes_de_dividir(corrida):
    """1.5 m de cinta con 0.5 por kit dan 3 kits."""
    _, db_path = corrida
    assert _qty_de(db_path, "SET-CINTA") == 3


def test_decimal_por_kit_no_pierde_un_kit_por_redondeo_binario(corrida):
    """1 perla con 0.1 por kit da 10; en float 1 // 0.1 es 9."""
    _, db_path = corrida
    assert _qty_de(db_path, "SET-PERLA") == 10


def test_sin_almacen_apartado_publica_todos_los_almacenes(corrida):
    _, db_path = corrida
    assert _qty_de(db_path, "TEST-FLEX") == 54


def test_lo_apartado_para_flex_no_se_publica(tmp_path_factory):
    proc, db_path = _correr(tmp_path_factory, "Flex - MX")
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert _qty_de(db_path, "TEST-FLEX") == 5


def test_kit_no_publica_un_componente_vendido_en_otro_kit(tmp_path_factory):
    """Caso Italia: el estuche tiene 3 en EHV y las 3 salen en ventas de
    otros kits. Odoo da al kit qty_available 3 y outgoing_qty 0, asi que la
    resta publicaba 3. Lo apartado en Flex tampoco cuenta."""
    proc, db_path = _correr(tmp_path_factory, "Flex - MX")
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert _producto(20, [1])["qty_available"] - _producto(20, [1])["outgoing_qty"] == 3
    assert _qty_de(db_path, "SET-ITA-CEN") == 0
    assert _qty_de(db_path, "SET-ITA-VCO") == 0


def test_un_almacen_apartado_que_no_existe_no_publica_nada(tmp_path_factory):
    proc, db_path = _correr(tmp_path_factory, "Flex MX")
    assert proc.returncode == 1
    assert "almacen apartado 'Flex MX' no existe" in proc.stdout
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM snapshot_items").fetchone() == (0,)
    conn.close()


def test_apartar_el_almacen_ehv_no_publica_nada(tmp_path_factory):
    proc, db_path = _correr(tmp_path_factory, "EHV-MX")
    assert proc.returncode == 1
    assert "'EHV-MX' es el almacen principal EHV" in proc.stdout
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM snapshot_items").fetchone() == (0,)
    conn.close()
