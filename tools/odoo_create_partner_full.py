#!/usr/bin/env python3
import os, sys, json, requests

ODOO_URL = os.getenv("ODOO_URL","").rstrip("/")
DB       = os.getenv("ODOO_DB","")
USER     = os.getenv("ODOO_USER","")
PW       = os.getenv("ODOO_PASSWORD","")
NAME     = os.getenv("FULL_PARTNER_NAME","MercadoLibre FULL").strip()

def die(m, code=2):
    print("ERROR:", m, file=sys.stderr)
    sys.exit(code)

for k,v in [("ODOO_URL",ODOO_URL),("ODOO_DB",DB),("ODOO_USER",USER),("ODOO_PASSWORD",PW)]:
    if not v:
        die(f"missing env {k}")

def jcall(service, method, args):
    payload={"jsonrpc":"2.0","method":"call","params":{"service":service,"method":method,"args":args},"id":1}
    r=requests.post(ODOO_URL+"/jsonrpc", json=payload, timeout=30)
    r.raise_for_status()
    d=r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]

def exec_kw(uid, model, method, args=None, kwargs=None):
    if args is None: args=[]
    if kwargs is None: kwargs={}
    return jcall("object","execute_kw",[DB,uid,PW,model,method,args,kwargs])

uid = jcall("common","authenticate",[DB,USER,PW,{}])
if not uid:
    die("AUTH_FAIL uid=null")
print("AUTH_OK uid=", uid)

rows = exec_kw(uid, "res.partner", "search_read",
    [[["name","=",NAME]]],
    {"fields":["id","name","active"],"limit":5}
)

if rows:
    # idempotente: si ya existe, no duplica
    if len(rows) > 1:
        die(f'partner_not_unique name="{NAME}" ids={[r["id"] for r in rows]}')
    print("ALREADY_EXISTS")
    print(json.dumps(rows[0], ensure_ascii=False, indent=2))
    print("OK_DONE")
    sys.exit(0)

vals = {
    "name": NAME,
    "company_type": "company",   # ayuda para reportes
    "is_company": True,
    "active": True,
    "comment": "AUTO CREATED for MercadoLibre FULL inbound (LEVEL1 SO only)"
}

pid = exec_kw(uid, "res.partner", "create", [vals])
print("CREATED partner_id=", pid)

p = exec_kw(uid, "res.partner", "read", [[pid], ["id","name","is_company","company_type","active"]])
print(json.dumps(p, ensure_ascii=False, indent=2))
print("OK_DONE")
