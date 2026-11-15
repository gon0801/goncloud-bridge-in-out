#!/usr/bin/env python3
"""Cierra en Odoo las entregas de venta de EHV-MX que la plataforma ya envio.

El bridge confirma la venta y deja la entrega (WH/OUT) sin validar. Este job
pregunta a la plataforma el estado real de cada entrega abierta y actua:

    enviada, en camino, entregada o recolectada  -> valida (hecho = demanda)
    cancelada sin envio                          -> cancela la entrega
    orden MeLi cancelada o con reembolso parcial
    y envio vivo o ya fuera                      -> no la toca y avisa
    cualquier otro estado                        -> no la toca

Por defecto solo muestra lo que haria, entrega por entrega. Con `--aplicar`
escribe en Odoo y registra cada accion, cada error y un resumen por corrida;
las entregas que no toca solo se cuentan, para que el log no crezca 48 veces
al dia con lo mismo.

Sale 1 si algo fallo, si alguna entrega pide revision o si queda alguna con mas
de 5 dias abierta que no se pudo cerrar. Corre envuelto en `cron-avisa`, que con
ese codigo avisa por Telegram una sola vez, y otra cuando vuelve a salir 0.

Uso
---
    docker exec bridge-api python3 /data/odoo_cerrar_entregas.py
    docker exec bridge-api python3 /data/odoo_cerrar_entregas.py --aplicar
    docker exec bridge-api python3 /data/odoo_cerrar_entregas.py --aplicar --limite 1
"""

from __future__ import annotations

import argparse
import enum
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
POLL_PATH = os.getenv("AMAZON_POLL", "/data/amazon_orders_poll.py")
MELI_TOKEN_FILE = os.getenv("MELI_TOKEN_FILE", "/data/.meli_tokens.json")
MELI_API = "https://api.mercadolibre.com"

ALMACEN = "EHV"
DIAS_VENCIDA = 5
# El poll crea la venta con ordenes actualizadas en los ultimos 2 dias, asi que
# la orden de cualquier entrega se actualizo como muy pronto 2 dias antes de que
# naciera la entrega. Listar desde 3 dias antes de la mas vieja las cubre todas.
MARGEN_LISTADO = timedelta(days=3)
TOPE_LISTADO = timedelta(days=30)
PAUSA_GET_ORDER_S = 2.0
MARKETPLACES = {"MX": "A1AM78C64UM0Y8", "US": "ATVPDKIKX0DER"}

CANALES_AMAZON = {
    "Amazon Flex MX": "MX",
    "Amazon Easy MX": "MX",
    "Amazon FBM MX": "MX",
    "Amazon FBM US": "US",
}
CANAL_MELI = "MercadoLibre FBM"

AMAZON_ENVIADA = {"Shipped", "InvoiceUnconfirmed"}
AMAZON_CANCELADA = {"Canceled"}
MELI_ENVIADA = {"shipped", "delivered"}
# Con el envio ya fuera, validar la entrega completa descontaria unidades que
# la orden cancelo o reembolso; eso lo resuelve una persona.
MELI_ORDEN_A_REVISAR = {"cancelled", "partially_refunded", "pending_cancel"}

CONTEXTO_VALIDAR = {"skip_backorder": True, "skip_sms": True}


class Accion(enum.Enum):
    VALIDAR = "VALIDAR"
    CANCELAR = "CANCELAR"
    DEJAR = "DEJAR"


@dataclass(frozen=True)
class Veredicto:
    accion: Accion
    estado: str
    avisar: bool = False


@dataclass(frozen=True)
class Entrega:
    picking_id: int
    picking: str
    so: str
    canal: str
    orden: str
    marketplace: str | None
    creada: datetime

    @property
    def plataforma(self) -> str:
        return "meli" if self.canal == CANAL_MELI else "amazon"


class ErrorCierre(Exception):
    pass


def decidir_amazon(estado: str | None) -> Veredicto:
    estado = estado or "?"
    if estado in AMAZON_ENVIADA:
        return Veredicto(Accion.VALIDAR, estado)
    if estado in AMAZON_CANCELADA:
        return Veredicto(Accion.CANCELAR, estado)
    return Veredicto(Accion.DEJAR, estado)


def decidir_meli(orden: dict | None, envio: dict | None) -> Veredicto:
    """En un pack el envio es uno solo para todas sus ordenes, asi que su
    estado no dice si salio la mercancia de esta orden: una orden cancelada
    dentro de un pack enviado no se valida. Solo se cancela si su envio no
    existe o tambien se cancelo; si no, se deja y se avisa."""
    if orden is None:
        return Veredicto(Accion.DEJAR, "orden no encontrada")
    orden_st = orden.get("status")
    pack = f" pack {orden['pack_id']}" if orden.get("pack_id") else ""
    status = sub = None
    if envio:
        status, sub = envio.get("status"), envio.get("substatus")
    envio_st = "sin envio" if not envio else f"{status}/{sub}" if sub else str(status)
    estado = f"orden {orden_st}{pack}, envio {envio_st}"
    salio = status in MELI_ENVIADA or (status == "ready_to_ship" and sub == "picked_up")

    if orden_st == "cancelled":
        if status in (None, "cancelled"):
            return Veredicto(Accion.CANCELAR, estado)
        return Veredicto(Accion.DEJAR, estado, avisar=True)
    if status == "cancelled":
        return Veredicto(Accion.CANCELAR, estado)
    if salio and orden_st in MELI_ORDEN_A_REVISAR:
        return Veredicto(Accion.DEJAR, estado, avisar=True)
    if salio:
        return Veredicto(Accion.VALIDAR, estado)
    return Veredicto(Accion.DEJAR, estado)


def orden_de_venta(canal: str, ref: str, nota: str) -> str | None:
    """Id de la orden en la plataforma.

    En MeLi el `client_order_ref` empieza con el id de la orden. La nota
    (`ORDER=<id>`) trae el id del pack cuando la orden es parte de uno, y
    /orders/<pack> da 404, asi que la nota solo sirve si falta la referencia.
    """
    if canal == CANAL_MELI:
        m = re.match(r"\s*(\d+)", ref or "") or re.search(r"ORDER=(\d+)", nota or "")
    else:
        m = re.search(r"\b(\d{3}-\d{7}-\d{7})\b", ref or "")
    return m.group(1) if m else None


def fecha_odoo(texto: str) -> datetime:
    return datetime.fromisoformat(texto).replace(tzinfo=UTC)


def listar_entregas(od, almacen: str = ALMACEN) -> tuple[list[Entrega], int]:
    """Entregas de venta abiertas del almacen, y cuantas se ignoraron por no
    ser de un canal de marketplace conocido."""
    picks = (
        od.kw(
            "stock.picking",
            "search_read",
            [
                [
                    ["picking_type_code", "=", "outgoing"],
                    ["picking_type_id.warehouse_id.code", "=", almacen],
                    ["state", "in", ["waiting", "confirmed", "assigned"]],
                    ["sale_id", "!=", False],
                ]
            ],
            {"fields": ["id", "name", "sale_id", "create_date"], "order": "id"},
        )
        or []
    )
    sids = sorted({p["sale_id"][0] for p in picks})
    sos = {}
    if sids:
        for s in od.kw(
            "sale.order",
            "read",
            [sids, ["name", "partner_id", "client_order_ref", "note"]],
        ):
            sos[s["id"]] = s
    entregas, ignoradas = [], 0
    for p in picks:
        so = sos.get(p["sale_id"][0]) or {}
        canal = (so.get("partner_id") or [None, ""])[1]
        orden = None
        if canal == CANAL_MELI or canal in CANALES_AMAZON:
            orden = orden_de_venta(
                canal, so.get("client_order_ref") or "", so.get("note") or ""
            )
        if not orden:
            ignoradas += 1
            continue
        entregas.append(
            Entrega(
                picking_id=p["id"],
                picking=p["name"],
                so=so["name"],
                canal=canal,
                orden=orden,
                marketplace=CANALES_AMAZON.get(canal),
                creada=fecha_odoo(p["create_date"]),
            )
        )
    return entregas, ignoradas


def cerrar(od, picking_id: int, accion: Accion, aplicar: bool) -> str:
    """Valida o cancela una entrega. La vuelve a leer antes: otro pudo cerrarla."""
    p = od.kw("stock.picking", "read", [[picking_id], ["state", "move_ids"]])[0]
    if p["state"] in ("done", "cancel"):
        return f"ya estaba {p['state']}"
    if accion is Accion.CANCELAR:
        if not aplicar:
            return "cancelaria"
        od.kw("stock.picking", "action_cancel", [[picking_id]])
        return _confirmar_estado(od, picking_id, "cancel", "cancelada")

    moves = od.kw(
        "stock.move",
        "read",
        [p["move_ids"], ["state", "product_uom_qty", "quantity", "has_tracking"]],
    )
    activos = [m for m in moves if m["state"] not in ("done", "cancel")]
    if not activos:
        raise ErrorCierre("sin movimientos activos")
    if any((m.get("has_tracking") or "none") != "none" for m in activos):
        raise ErrorCierre("requiere lote o serie")
    if not aplicar:
        return "validaria"
    for m in activos:
        od.kw(
            "stock.move",
            "write",
            [[m["id"]], {"quantity": m["product_uom_qty"], "picked": True}],
        )
    res = od.kw(
        "stock.picking",
        "button_validate",
        [[picking_id]],
        {"context": CONTEXTO_VALIDAR},
    )
    if isinstance(res, dict):
        raise ErrorCierre(f"Odoo pidio confirmacion ({res.get('res_model')})")
    return _confirmar_estado(od, picking_id, "done", "validada")


def _confirmar_estado(od, picking_id: int, esperado: str, resultado: str) -> str:
    estado = od.kw("stock.picking", "read", [[picking_id], ["state"]])[0]["state"]
    if estado != esperado:
        raise ErrorCierre(f"quedo en {estado}")
    return resultado


@dataclass
class Resumen:
    revisadas: int = 0
    validadas: int = 0
    canceladas: int = 0
    dejadas: int = 0
    errores: int = 0
    vencidas: list = field(default_factory=list)
    avisos: list = field(default_factory=list)

    def fallo(self) -> bool:
        return bool(self.errores or self.vencidas or self.avisos)


def correr(od, consultar, aplicar: bool, ahora: datetime, limite=None, log=print):
    """Recorre las entregas abiertas. `consultar(entregas)` devuelve, por
    picking_id, un Veredicto o la excepcion que impidio obtenerlo."""
    entregas, ignoradas = listar_entregas(od)
    veredictos = consultar(entregas)
    r = Resumen(revisadas=len(entregas))
    escrituras = 0
    for e in entregas:
        vencida = ahora - e.creada > timedelta(days=DIAS_VENCIDA)
        cabeza = f"{e.picking} {e.so} {e.canal} orden={e.orden}"
        v = veredictos.get(e.picking_id)
        if not isinstance(v, Veredicto):
            r.errores += 1
            log(f"ERROR     {cabeza} consulta: {v}")
            if vencida:
                r.vencidas.append(e)
            continue
        if v.accion is Accion.DEJAR:
            r.dejadas += 1
            if not aplicar or vencida or v.avisar:
                log(f"DEJAR     {cabeza} estado={v.estado}")
            if v.avisar:
                r.avisos.append((e, v.estado))
            if vencida:
                r.vencidas.append(e)
            continue
        if aplicar and limite is not None and escrituras >= limite:
            r.dejadas += 1
            log(f"{v.accion.value:<9} {cabeza} estado={v.estado} -> pospuesta (limite)")
            continue
        try:
            resultado = cerrar(od, e.picking_id, v.accion, aplicar)
        except Exception as ex:  # noqa: BLE001 - una entrega no frena las demas
            r.errores += 1
            log(f"ERROR     {cabeza} estado={v.estado} {v.accion.value}: {ex}")
            if vencida:
                r.vencidas.append(e)
            continue
        if resultado in ("validada", "cancelada"):
            escrituras += 1
        if v.accion is Accion.VALIDAR:
            r.validadas += 1
        else:
            r.canceladas += 1
        log(f"{v.accion.value:<9} {cabeza} estado={v.estado} -> {resultado}")

    modo = "aplicado" if aplicar else "prueba (sin escribir)"
    log(
        f"RESUMEN   modo={modo} revisadas={r.revisadas} validar={r.validadas} "
        f"cancelar={r.canceladas} dejar={r.dejadas} errores={r.errores} "
        f"vencidas={len(r.vencidas)} avisos={len(r.avisos)} ignoradas={ignoradas}"
    )
    for e in r.vencidas:
        dias = (ahora - e.creada).days
        log(f"VENCIDA   {e.picking} {e.so} {e.canal} orden={e.orden} {dias} dias")
    for e, estado in r.avisos:
        log(f"REVISAR   {e.picking} {e.so} {e.canal} orden={e.orden} estado={estado}")
    return r


def get_setting(key: str) -> str:
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT value FROM bridge_settings WHERE key=?", (key,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else ""


class Odoo:
    def __init__(self):
        self.url = get_setting("odoo_url").rstrip("/")
        self.db = get_setting("odoo_db")
        self.pw = get_setting("odoo_password")
        user = get_setting("odoo_user")
        if not all((self.url, self.db, user, self.pw)):
            raise SystemExit("ERROR: faltan credenciales de Odoo en bridge_settings")
        self.uid = self._call("common", "authenticate", [self.db, user, self.pw, {}])
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
        # Validar una entrega dispara el envio de stock de meli_oerp_stock
        # dentro de Odoo; se han visto llamadas de mas de 2 minutos.
        raw = json.loads(urllib.request.urlopen(req, timeout=300).read())
        if "error" in raw:
            err = raw["error"]
            msg = (err.get("data") or {}).get("message") or err.get("message")
            raise ErrorCierre(f"Odoo: {str(msg)[:200]}")
        return raw.get("result")

    def kw(self, model, method, args, kwargs=None):
        return self._call(
            "object",
            "execute_kw",
            [self.db, self.uid, self.pw, model, method, args, kwargs or {}],
        )


def _cargar_poll():
    spec = importlib.util.spec_from_file_location("poll", POLL_PATH)
    poll = importlib.util.module_from_spec(spec)
    sys.modules["poll"] = poll
    spec.loader.exec_module(poll)
    return poll


def listar_ordenes_amazon(poll, token: str, mkt_id: str, desde: str):
    """Ordenes actualizadas desde `desde`, todas las paginas.

    La v2026 devuelve el token de la siguiente pagina en `pagination.nextToken`;
    `poll.get_orders` lo busca en la raiz y se queda en las primeras 100.
    """
    params = {"marketplaceIds": mkt_id, "lastUpdatedAfter": desde}
    params["includedData"] = "FULFILLMENT"
    while True:
        resp = poll._sp_api_get(
            token, f"{poll.AMAZON_API_BASE}/orders/2026-01-01/orders", params
        )
        if resp.status_code != 200:
            raise ErrorCierre(f"SP-API HTTP {resp.status_code} al listar ordenes")
        data = resp.json() or {}
        yield from data.get("orders") or []
        siguiente = (data.get("pagination") or {}).get("nextToken")
        if not siguiente:
            return
        params = {**params, "paginationToken": siguiente}


def estados_amazon(entregas: list[Entrega], ahora: datetime) -> dict:
    """OrderStatus por orden. Lista por fecha de actualizacion y pide una por
    una, con pausa, solo las que no salieron en la lista."""
    poll = _cargar_poll()
    token = poll.get_access_token(poll.get_credentials())
    estados: dict = {}
    for mkt, mkt_id in MARKETPLACES.items():
        grupo = [e for e in entregas if e.marketplace == mkt]
        if not grupo:
            continue
        desde = max(min(e.creada for e in grupo) - MARGEN_LISTADO, ahora - TOPE_LISTADO)
        buscadas = {e.orden for e in grupo}
        desde_txt = desde.strftime("%Y-%m-%dT%H:%M:%SZ")
        for o in listar_ordenes_amazon(poll, token, mkt_id, desde_txt):
            v0 = poll.normalize_to_v0(o)
            if v0["AmazonOrderId"] in buscadas:
                estados[v0["AmazonOrderId"]] = v0["OrderStatus"]
        for oid in sorted(buscadas - estados.keys()):
            time.sleep(PAUSA_GET_ORDER_S)
            resp = poll._sp_api_get(
                token,
                f"{poll.AMAZON_API_BASE}/orders/2026-01-01/orders/{oid}",
                {"marketplaceIds": mkt_id, "includedData": "FULFILLMENT"},
            )
            if resp.status_code == 404:
                estados[oid] = "no encontrada"
            elif resp.status_code != 200:
                estados[oid] = ErrorCierre(f"SP-API HTTP {resp.status_code}")
            else:
                orden = (resp.json() or {}).get("order") or {}
                estados[oid] = poll.normalize_to_v0(orden)["OrderStatus"]
    return estados


def meli_get(path: str, token: str):
    req = urllib.request.Request(
        MELI_API + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise ErrorCierre(f"MeLi HTTP {e.code} en {path}") from None


def veredicto_meli(e: Entrega, token: str) -> Veredicto:
    orden = meli_get(f"/orders/{e.orden}", token)
    envio_id = ((orden or {}).get("shipping") or {}).get("id")
    envio = meli_get(f"/shipments/{envio_id}", token) if envio_id else None
    return decidir_meli(orden, envio)


def consultar_plataformas(entregas: list[Entrega], ahora: datetime) -> dict:
    resultado: dict = {}
    amazon = [e for e in entregas if e.plataforma == "amazon"]
    meli = [e for e in entregas if e.plataforma == "meli"]
    if amazon:
        try:
            estados = estados_amazon(amazon, ahora)
        except Exception as ex:  # noqa: BLE001
            falla = ErrorCierre(f"SP-API: {type(ex).__name__}")
            estados = {e.orden: falla for e in amazon}
        for e in amazon:
            st = estados.get(e.orden)
            resultado[e.picking_id] = (
                st if isinstance(st, Exception) else decidir_amazon(st)
            )
    if meli:
        try:
            with open(MELI_TOKEN_FILE) as f:
                token = json.load(f).get("access_token") or ""
        except (OSError, ValueError):
            token = ""
        for e in meli:
            if not token:
                resultado[e.picking_id] = ErrorCierre("sin token de MeLi")
                continue
            try:
                resultado[e.picking_id] = veredicto_meli(e, token)
            except Exception as ex:  # noqa: BLE001
                resultado[e.picking_id] = ex
    return resultado


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aplicar", action="store_true", help="escribir en Odoo")
    ap.add_argument("--limite", type=int, help="maximo de entregas a cerrar")
    args = ap.parse_args(argv)

    ahora = datetime.now(UTC)
    sello = ahora.strftime("%Y-%m-%dT%H:%M:%SZ")

    def log(linea: str) -> None:
        print(f"{sello} {linea}", flush=True)

    r = correr(
        Odoo(),
        lambda entregas: consultar_plataformas(entregas, ahora),
        args.aplicar,
        ahora,
        args.limite,
        log,
    )
    return 1 if r.fallo() else 0


if __name__ == "__main__":
    sys.exit(main())
