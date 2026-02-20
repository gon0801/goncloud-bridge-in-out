#!/usr/bin/env python3
import os
import sys
import re
import argparse
import subprocess

APPLY_TOOL = "/mnt/data/appdata/bridge/tools/inbound_full_so_apply.py"
CONFIRM_TOOL = "/mnt/data/appdata/bridge/tools/inbound_full_so_confirm_no_picking.py"

# match: ref=MLFULL:MLM:SIM-FULL-1
REF_RE = re.compile(r"\bref=(MLFULL:[A-Z0-9_]+:[A-Za-z0-9\-_]+)\b")

# fallback: any MLFULL:... token in output
ALT_REF_RE = re.compile(r"\b(MLFULL:[A-Z0-9_]+:[A-Za-z0-9\-_]+)\b")

# match: so_id=257 (covers: "already_created so_id=257 name=...")
SO_ID_RE = re.compile(r"\bso_id=(\d+)\b")

REQUIRED_ODOO_ENVS = ["ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_PASSWORD"]


def die(msg: str, code: int = 2):
    print(f"[FULL_ONE_SHOT] ERROR {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return p.returncode, p.stdout


def extract_ref(output: str) -> str | None:
    m = REF_RE.search(output)
    if m:
        return m.group(1)
    m2 = ALT_REF_RE.search(output)
    if m2:
        return m2.group(1)
    return None


def extract_so_id(output: str) -> str | None:
    m = SO_ID_RE.search(output)
    if m:
        return m.group(1)
    return None


def require_odoo_envs():
    missing = [k for k in REQUIRED_ODOO_ENVS if not os.getenv(k)]
    if missing:
        die(
            "missing env(s): " + ",".join(missing) +
            " (export ODOO_URL/ODOO_DB/ODOO_USER/ODOO_PASSWORD antes de correr one-shot)"
        )


def main():
    ap = argparse.ArgumentParser(
        description="FULL one-shot: create FULL SO (draft) + force state=sale without picking/stock."
    )
    ap.add_argument("--ml-order-id", required=True, help="ML order id (numeric) or SIM id")
    ap.add_argument("--order-json-file", help="SIM only: path to order JSON")
    args = ap.parse_args()

    # Sanity: required scripts exist
    if not os.path.exists(APPLY_TOOL):
        die(f"missing {APPLY_TOOL}")
    if not os.path.exists(CONFIRM_TOOL):
        die(f"missing {CONFIRM_TOOL}")

    # Step 1: apply (create draft SO; may return already_created)
    apply_cmd = ["python3", APPLY_TOOL, "--ml-order-id", args.ml_order_id]
    if args.order_json_file:
        apply_cmd += ["--order-json-file", args.order_json_file]

    rc, out = run(apply_cmd)
    print(out, end="")

    if rc != 0:
        die("apply step failed (see output above)", code=rc)

    # Step 2 needs Odoo envs (confirm tool uses JSON-RPC env)
    require_odoo_envs()

    # Step 2: confirm-no-picking (force state=sale WITHOUT procurement)
    ref = extract_ref(out)
    if ref:
        confirm_cmd = ["python3", CONFIRM_TOOL, "--client-order-ref", ref]
        rc2, out2 = run(confirm_cmd)
        print(out2, end="")
        if rc2 != 0:
            die("confirm step failed (see output above)", code=rc2)
        print(f"[FULL_ONE_SHOT] OK done ref={ref}")
        return

    so_id = extract_so_id(out)
    if so_id:
        print(f"[FULL_ONE_SHOT] INFO fallback_confirm_by_so_id so_id={so_id}", flush=True)
        confirm_cmd = ["python3", CONFIRM_TOOL, "--so-id", so_id]
        rc2, out2 = run(confirm_cmd)
        print(out2, end="")
        if rc2 != 0:
            die("confirm step failed (see output above)", code=rc2)
        print(f"[FULL_ONE_SHOT] OK done so_id={so_id}")
        return

    die("could not extract client_order_ref nor so_id from apply output")


if __name__ == "__main__":
    main()
