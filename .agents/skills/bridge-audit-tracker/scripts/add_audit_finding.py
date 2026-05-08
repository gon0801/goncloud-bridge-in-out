#!/usr/bin/env python3
"""
Add an audit finding row to status-bridge.html.
Usage:
  python add_audit_finding.py --file status-bridge.html --audit D4 --id D4.1 \
    --desc "LREM mismatch" --loc "inbound_worker.py" --priority critica
"""
import argparse, re, sys

def priority_class(p: str) -> str:
    return {"critica": "pri-critica", "alta": "pri-alta",
            "media": "pri-media", "baja": "pri-baja"}.get(p.lower(), "pri-media")

def priority_badge(p: str) -> str:
    return {"critica": "Crítica", "alta": "Alta",
            "media": "Media", "baja": "Baja"}.get(p.lower(), "Media")

def main():
    parser = argparse.ArgumentParser(description="Insert audit finding into status-bridge.html")
    parser.add_argument("--file", required=True)
    parser.add_argument("--audit", required=True, help="Audit prefix, e.g. D4")
    parser.add_argument("--id", required=True)
    parser.add_argument("--desc", required=True)
    parser.add_argument("--loc", default="", help="File/line reference")
    parser.add_argument("--priority", required=True, choices=["critica","alta","media","baja"])
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        content = f.read()

    # Build row HTML
    loc_html = f"<code>{args.loc}</code> — " if args.loc else ""
    row = (
        f'    <tr>\n'
        f'      <td class="sub">{args.id}</td>\n'
        f'      <td colspan="2">{loc_html}{args.desc}</td>\n'
        f'      <td><span class="badge badge-blocker">🔴 Pendiente</span></td>\n'
        f'      <td><span class="pri {priority_class(args.priority)}">{priority_badge(args.priority)}</span></td>\n'
        f'    </tr>\n'
    )

    # Find the audit section under Puntos a corregir
    marker = f'<!-- {args.audit.upper()} '
    sec_start = content.find(marker)
    if sec_start == -1:
        print(f"ERROR: Audit section {args.audit.upper()} not found.", file=sys.stderr)
        sys.exit(1)

    # Insert after the last </tr> before the next group-header or </tbody>
    next_group = content.find('<tr class="group-header">', sec_start + 1)
    next_tbody = content.find('  </tbody>', sec_start + 1)
    insert_pos = min(x for x in [next_group, next_tbody] if x != -1)

    new_content = content[:insert_pos] + row + "\n" + content[insert_pos:]

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Inserted {args.id} into {args.file}")

if __name__ == "__main__":
    main()
