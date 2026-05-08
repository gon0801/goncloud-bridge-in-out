#!/usr/bin/env python3
"""
Mark one or more audit findings as ✅ Corregido in status-bridge.html.

Usage:
  python mark_done.py --file status-bridge.html --ids D3.1 D4.1 D4.7 \
    --note "redis pinned; LREM fixed; ml_orders_processing cleaned"
"""
import argparse, re, sys
from datetime import datetime, timezone

BLOCKER_BADGE = '<span class="badge badge-blocker">🔴 Pendiente</span>'
DONE_BADGE    = '<span class="badge badge-done">✅ Corregido</span>'


def mark_finding(content: str, finding_id: str) -> tuple[str, bool]:
    """Find the <tr> row for finding_id and flip its badge to done."""
    # Match the row that starts with this ID in a <td class="sub"> or <td>
    pattern = re.compile(
        r'(<tr>.*?<td[^>]*>\s*' + re.escape(finding_id) + r'\s*</td>.*?)'
        + re.escape(BLOCKER_BADGE),
        re.DOTALL
    )
    new_content, count = pattern.subn(lambda m: m.group(1) + DONE_BADGE, content, count=1)
    return new_content, count > 0


def update_counter(content: str, pending_delta: int) -> str:
    """Decrement the 🔴 Pendiente counter in the summary header."""
    def replace_count(m):
        n = int(m.group(1)) + pending_delta
        return m.group(0).replace(m.group(1), str(max(0, n)))
    return re.sub(r'(\d+)\s*(?:🔴\s*Pendientes?|pendientes?)', replace_count, content, flags=re.IGNORECASE)


def add_closed_entry(content: str, ids: list[str], note: str) -> str:
    """Prepend a <li> entry in the cerrados section."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ids_str = ", ".join(ids)
    note_html = f" — {note}" if note else ""
    entry = f'      <li><strong>{date}</strong> — {ids_str}{note_html}</li>\n'
    marker = '<ul class="closed-list">\n'
    pos = content.find(marker)
    if pos == -1:
        return content
    insert = pos + len(marker)
    return content[:insert] + entry + content[insert:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--ids", nargs="+", required=True, help="Finding IDs to mark done")
    parser.add_argument("--note", default="", help="Short fix description for the closed entry")
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        content = f.read()

    fixed = []
    not_found = []
    for fid in args.ids:
        content, ok = mark_finding(content, fid)
        (fixed if ok else not_found).append(fid)

    if not_found:
        print(f"WARNING: IDs not found or already done: {not_found}", file=sys.stderr)

    if fixed:
        content = update_counter(content, -len(fixed))
        content = add_closed_entry(content, fixed, args.note)

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Marked done: {fixed}" + (f" | Not found: {not_found}" if not_found else ""))


if __name__ == "__main__":
    main()
