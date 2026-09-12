#!/usr/bin/env python3
"""Detecta ventas de marketplace que quedaron facturadas en $0.

Por que existe
--------------
Amazon devuelve `OrderTotal = 0` cuando una orden Flex MX esta en `Pending`, y
lo llena despues **sin cambiar de estado**. El bridge la captura en el primer
momento y crea el SO en cero; cuando llega el segundo evento, el tool actualiza
el precio de las lineas.

Eso funciona en 103 de 107 casos. Las que fallan quedan invisibles: el poll
filtra por fecha, asi que a los pocos dias la orden sale de la ventana y nadie
la vuelve a mirar.

Caso real (2026-09-12): cuatro SOs en $0. Dos eran de junio — **tres meses**
facturadas en cero, con Amazon ya devolviendo el precio real (768.90 y 1188.00
MXN) sobre ordenes todavia en `Pending`. Se encontraron de casualidad,
investigando otra cosa.

Umbral medido, no estimado
--------------------------
Demora entre el evento `Pending` y el siguiente, sobre las 103 ordenes que si
se corrigieron:

    p50 1.2d · p90 3.0d · p95 3.2d · p99 4.1d · max 16.6d

    umbral 2d -> 23 falsos positivos de 103
    umbral 3d -> 10
    umbral 4d ->  2
    umbral 7d ->  1   (el unico caso de 16.6 dias)

Por eso 7 dias. Una alarma que grita seguido vuelve a entrenar a todos a
ignorarla — y este repo ya perdio 19 dias de ordenes por un semaforo en rojo
cronico que nadie miraba.

Uso
---
    # solo reportar
    docker exec bridge-api python3 /data/odoo_sos_en_cero.py

    # reportar y re-encolar para que el bridge las corrija
    docker exec bridge-api python3 /data/odoo_sos_en_cero.py --reprocesar
"""

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
POLL_PATH = os.getenv("AMAZON_POLL", "/data/amazon_orders_poll.py")
UMBRAL_DIAS = 7
# Entregado y nunca facturado es otro modo de falla, y mas silencioso: no hay
# factura en $0 que mirar, no hay nada. El bridge factura en la misma corrida
# en que valida la entrega, asi que un hueco de dias solo aparece cuando la
# venta se armo a mano. Medido el 2026-09-12 sobre toda la base: 3 ventas en
# ese estado, las 3 reales. No hay poblacion "normal" de la que distinguirlas,
# asi que 2 dias es margen de sobra para una entrega legitimamente en vuelo.
UMBRAL_SIN_FACTURAR = 2
CLAVE_ESTADO = "sos_en_cero_ultimo_chequeo"


CLAVE_ACEPTADAS = "sos_en_cero_aceptadas"
CLAVE_ACEPTADAS_SF = "sos_sin_facturar_aceptadas"


def filtrar_aceptadas(hallazgos: list, aceptadas: set) -> list:
    """Saca de la lista las ventas que el operador ya decidio dejar asi.

    Sin esto el reporte deja de servir. Las nueve facturas de febrero de 2026 no
    se van a corregir — son `posted` y `paid`, se arreglan con nota de credito y
    el operador decidio dejarlas. Si salen en cada corrida, el dia que aparezca
    una NUEVA va a estar enterrada entre diez que ya nadie lee.

    Es el mismo patron que costo 19 dias de ordenes perdidas en agosto: un
    semaforo en rojo permanente por algo conocido, y una falla real invisible
    detras.
    """
    return [h for h in hallazgos if h.get("so") not in aceptadas]


def extraer_order_id_amazon(ref: str):
    """Saca el AmazonOrderId de un `client_order_ref`, sea cual sea su formato.

    Conviven tres formas en la base, porque el formato cambio el 2026-02-23:

        "701-1234567-1234567 | Juan Garcia"          formato actual
        "AMZFBA:A1AM78C64UM0Y8:701-1234567-1234567"  formato crudo, anterior
        "2000018393906916 | comprador"               MercadoLibre, no aplica

    Buscar por posicion fallaba con el formato viejo: `split("|")[0]` devolvia
    la cadena entera con el prefijo, y el filtro la descartaba. Nueve SOs de
    febrero por 9,174 MXN quedaban fuera del reproceso justo por eso.

    El id de Amazon tiene una forma fija (3-7-7 digitos), asi que se busca por
    patron y no por posicion.
    """
    if not ref:
        return None
    m = re.search(r"\b\d{3}-\d{7}-\d{7}\b", ref)
    return m.group(0) if m else None


def es_sospechosa(
    monto: float, estado: str, dias: float, umbral: float = UMBRAL_DIAS
) -> bool:
    """Si un SO de marketplace en cero ya dejo de ser normal.

    Una cancelada no es un ingreso perdido. Una recien creada todavia puede
    estar esperando que Amazon llene el precio: el p50 de esa espera es 1.2
    dias.
    """
    if (estado or "").lower() == "cancel":
        return False
    if (monto or 0) > 0:
        return False
    return dias >= umbral


def es_entregada_sin_facturar(
    estado: str,
    invoice_status: str,
    entregado: float,
    facturado: float,
    dias: float,
    umbral: float = UMBRAL_SIN_FACTURAR,
) -> bool:
    """Si una venta ya entregada lleva demasiado sin que exista su factura.

    Distinto de `es_sospechosa`: ahi hay factura y esta en cero. Aqui la
    mercancia salio y no hay factura ninguna, que no dispara ninguna alarma
    contable porque no hay documento que revisar.

    Solo cuenta si de verdad se entrego: un SO confirmado y sin entregar
    todavia no tiene por que estar facturado.
    """
    if (estado or "").lower() != "sale":
        return False
    if (invoice_status or "") != "to invoice":
        return False
    if (entregado or 0) <= 0:
        return False
    if (facturado or 0) > 0:
        return False
    return dias >= umbral


def get_setting(key: str) -> str:
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT value FROM bridge_settings WHERE key=?", (key,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else ""


def set_setting(key: str, value: str) -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute(
            "INSERT INTO bridge_settings(key, value, updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


class Odoo:
    def __init__(self):
        self.url = get_setting("odoo_url").rstrip("/")
        self.db = get_setting("odoo_db")
        self.user = get_setting("odoo_user")
        self.pw = get_setting("odoo_password")
        if not all((self.url, self.db, self.user, self.pw)):
            raise SystemExit("ERROR: faltan credenciales de Odoo en bridge_settings")
        self.uid = self._call(
            "common", "authenticate", [self.db, self.user, self.pw, {}]
        )
        if not self.uid:
            raise SystemExit("ERROR: no se pudo autenticar contra Odoo")

    def _call(self, service, method, args):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": 1,
            "params": {"service": service, "method": method, "args": args},
        }
        req = urllib.request.Request(
            self.url + "/jsonrpc",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        raw = json.loads(urllib.request.urlopen(req, timeout=120).read())
        if "error" in raw:
            raise SystemExit(f"ERROR Odoo: {json.dumps(raw['error'])[:300]}")
        return raw.get("result")

    def kw(self, model, method, args, kwargs=None):
        return self._call(
            "object",
            "execute_kw",
            [self.db, self.uid, self.pw, model, method, args, kwargs or {}],
        )


def buscar_sin_facturar(od, umbral: float, ahora) -> list:
    """Ventas entregadas cuya factura nunca se creo.

    Se pide `invoice_status = to invoice` a Odoo y despues se confirma linea
    por linea que haya entrega real y cero facturado. El campo por si solo no
    alcanza: tambien lo tiene un SO confirmado que aun no sale del almacen.
    """
    sos = (
        od.kw(
            "sale.order",
            "search_read",
            [[["state", "=", "sale"], ["invoice_status", "=", "to invoice"]]],
            {
                "fields": [
                    "id",
                    "name",
                    "client_order_ref",
                    "amount_total",
                    "state",
                    "invoice_status",
                    "date_order",
                ],
                "limit": 500,
            },
        )
        or []
    )
    hallazgos = []
    for s_ in sos:
        lineas = (
            od.kw(
                "sale.order.line",
                "search_read",
                [[["order_id", "=", s_["id"]]]],
                {"fields": ["qty_delivered", "qty_invoiced", "display_type"]},
            )
            or []
        )
        reales = [x for x in lineas if not x.get("display_type")]
        if not reales:
            continue
        entregado = min(x.get("qty_delivered") or 0 for x in reales)
        facturado = max(x.get("qty_invoiced") or 0 for x in reales)
        try:
            fecha = datetime.fromisoformat(s_["date_order"]).replace(
                tzinfo=timezone.utc
            )
        except Exception:
            continue
        dias = (ahora - fecha).total_seconds() / 86400
        if es_entregada_sin_facturar(
            s_.get("state"),
            s_.get("invoice_status"),
            entregado,
            facturado,
            dias,
            umbral,
        ):
            hallazgos.append(
                {
                    "so": s_["name"],
                    "ref": s_.get("client_order_ref") or "",
                    "dias": round(dias, 1),
                    "monto": s_.get("amount_total") or 0,
                }
            )
    return hallazgos


def reprocesar(oids: list) -> int:
    """Re-encola las ordenes con el payload FRESCO de Amazon.

    La compuerta importa: se descarta el payload que no traiga id, total > 0 e
    items. Sin ella, re-encolar una orden que Amazon todavia reporta en cero
    reescribe el mismo $0 y el reporte se "arregla" sin arreglar nada.
    """
    import redis

    spec = importlib.util.spec_from_file_location("poll", POLL_PATH)
    poll = importlib.util.module_from_spec(spec)
    sys.modules["poll"] = poll
    spec.loader.exec_module(poll)

    mx = get_setting("amazon_marketplace_id") or "A1AM78C64UM0Y8"
    token = poll.get_access_token(poll.get_credentials())
    con = sqlite3.connect(DB_PATH, timeout=30)
    r = redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://bridge-redis:6379/0"), decode_responses=True
    )
    encoladas = 0
    for oid in oids:
        resp = poll._sp_api_get(
            token,
            f"{poll.AMAZON_API_BASE}/orders/2026-01-01/orders/{oid}",
            {"marketplaceIds": mx, "includedData": poll.INCLUDED_DATA},
        )
        if resp.status_code != 200:
            print(f"   {oid}: HTTP {resp.status_code}, se salta")
            continue
        # v2026 anida la orden bajo "order" (no "payload", como el listado).
        orden = (resp.json() or {}).get("order")
        if not isinstance(orden, dict):
            print(f"   {oid}: respuesta inesperada, se salta")
            continue
        o = poll.normalize_to_v0(orden)
        total = float((o.get("OrderTotal") or {}).get("Amount") or 0)
        items = len(o.get("OrderItems") or [])
        if o.get("AmazonOrderId") != oid or total <= 0 or items == 0:
            print(f"   {oid}: Amazon aun no da precio (total={total}), se salta")
            continue
        con.execute(
            "DELETE FROM amazon_processed_events WHERE dedupe_key LIKE ?", (f"%{oid}%",)
        )
        con.commit()
        poll.push_to_redis(r, o, poll.make_dedupe_key(o, mx))
        encoladas += 1
        print(f"   {oid}: encolada con total {total:.2f}")
    con.close()
    return encoladas


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ventas de marketplace en $0, o entregadas sin facturar"
    )
    ap.add_argument("--dias", type=float, default=UMBRAL_DIAS)
    ap.add_argument(
        "--reprocesar", action="store_true", help="re-encolar las encontradas"
    )
    ap.add_argument("--dias-sin-facturar", type=float, default=UMBRAL_SIN_FACTURAR)
    ap.add_argument(
        "--aceptar",
        action="store_true",
        help="marcar las encontradas como conocidas: dejan de reportarse",
    )
    args = ap.parse_args()

    od = Odoo()
    sos = (
        od.kw(
            "sale.order",
            "search_read",
            [
                [
                    ["amount_total", "=", 0],
                    ["state", "!=", "cancel"],
                    "|",
                    ["client_order_ref", "like", "70"],
                    ["client_order_ref", "like", "2000"],
                ]
            ],
            {
                "fields": [
                    "name",
                    "client_order_ref",
                    "amount_total",
                    "state",
                    "create_date",
                ],
                "limit": 500,
            },
        )
        or []
    )

    ahora = datetime.now(timezone.utc)
    hallazgos = []
    for s in sos:
        try:
            creada = datetime.fromisoformat(s["create_date"]).replace(
                tzinfo=timezone.utc
            )
        except Exception:
            continue
        dias = (ahora - creada).total_seconds() / 86400
        if es_sospechosa(s.get("amount_total"), s.get("state"), dias, args.dias):
            hallazgos.append(
                {
                    "so": s["name"],
                    "ref": s.get("client_order_ref") or "",
                    "dias": round(dias, 1),
                }
            )

    aceptadas = set(json.loads(get_setting(CLAVE_ACEPTADAS) or "[]"))
    hallazgos = filtrar_aceptadas(hallazgos, aceptadas)

    aceptadas_sf = set(json.loads(get_setting(CLAVE_ACEPTADAS_SF) or "[]"))
    sin_facturar = filtrar_aceptadas(
        buscar_sin_facturar(od, args.dias_sin_facturar, ahora), aceptadas_sf
    )

    if args.aceptar:
        nuevas = sorted(aceptadas | {h["so"] for h in hallazgos})
        set_setting(CLAVE_ACEPTADAS, json.dumps(nuevas))
        nuevas_sf = sorted(aceptadas_sf | {h["so"] for h in sin_facturar})
        set_setting(CLAVE_ACEPTADAS_SF, json.dumps(nuevas_sf))
        print(f"Aceptadas {len(hallazgos)} ventas en $0. Total: {len(nuevas)}")
        print(
            f"Aceptadas {len(sin_facturar)} entregadas sin facturar. "
            f"Total: {len(nuevas_sf)}"
        )
        return 0

    set_setting(
        CLAVE_ESTADO,
        json.dumps(
            {
                "pendientes": len(hallazgos),
                "sin_facturar": len(sin_facturar),
                "revisado": ahora.isoformat(timespec="seconds"),
            }
        ),
    )

    if not hallazgos:
        print(
            f"Sin ventas en $0 con mas de {args.dias} dias sin aceptar. "
            f"({len(sos)} en cero; {len(aceptadas)} aceptadas previamente)"
        )
    else:
        print(
            f"Ventas de marketplace en $0 con mas de {args.dias} dias: "
            f"{len(hallazgos)}\n"
        )
        print("%-10s %-9s %s" % ("SO", "DIAS", "ORDEN"))
        for h in sorted(hallazgos, key=lambda x: -x["dias"]):
            print("%-10s %-9s %s" % (h["so"], h["dias"], h["ref"][:40]))

        if args.reprocesar:
            oids = [
                x for x in (extraer_order_id_amazon(h["ref"]) for h in hallazgos) if x
            ]
            print(f"\nReprocesando {len(oids)} ordenes de Amazon:")
            n = reprocesar(oids)
            print(f"\n{n} encoladas. El worker las corrige en segundos.")
        else:
            print("\nCon --reprocesar se re-encolan para que el bridge las corrija.")

    if sin_facturar:
        perdido = sum(h["monto"] for h in sin_facturar)
        print(
            f"\nEntregadas y NUNCA facturadas, con mas de "
            f"{args.dias_sin_facturar} dias: {len(sin_facturar)}\n"
        )
        print("%-10s %-9s %12s  %s" % ("SO", "DIAS", "MONTO", "ORDEN"))
        for h in sorted(sin_facturar, key=lambda x: -x["dias"]):
            print(
                "%-10s %-9s %12.2f  %s"
                % (h["so"], h["dias"], h["monto"], h["ref"][:40])
            )
        print(f"\n  sin facturar: {perdido:.2f} MXN")
        print(
            "  Estas NO se reprocesan solas: la mercancia ya salio y el SO ya "
            "existe.\n  Revisar a mano, o --aceptar si se decide dejarlas asi."
        )

    return 1 if (hallazgos or sin_facturar) else 0


if __name__ == "__main__":
    sys.exit(main())
