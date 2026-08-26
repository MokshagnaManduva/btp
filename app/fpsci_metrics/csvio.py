"""CSV output, with the same column ordering and lock handling as the original.

Column order matters: the golden test diffs files byte-for-byte, so ``row_id``
first and then keys in first-seen order has to be preserved exactly.
"""

from __future__ import annotations

import csv
import sys


def write_csv(path, rows, add_row_id=True):
    if not rows:
        return 0
    # "row_id" is a plain 1..N serial number for the file, seeded into keys
    # first so it lands in column 1 regardless of dict insertion order.
    keys = []
    if add_row_id:
        for n, r in enumerate(rows, start=1):
            r["row_id"] = n
        keys.append("row_id")
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    # Excel takes an exclusive lock on an open .csv, so a file the user is
    # looking at cannot be rewritten. Since this runs unattended after every
    # session, one open file must not abort the whole export: divert to a
    # sidecar and carry on with the remaining tables.
    try:
        f = open(path, "w", newline="", encoding="utf-8")
    except PermissionError:
        alt = path[:-4] + ".locked.csv" if path.endswith(".csv") else path + ".locked"
        print(f"  !! {path} is open in another program (Excel?) -- wrote {alt} "
              f"instead; close it and re-run to refresh the original.",
              file=sys.stderr)
        try:
            f = open(alt, "w", newline="", encoding="utf-8")
        except PermissionError:
            print(f"  !! could not write {alt} either; skipping this table.",
                  file=sys.stderr)
            return 0
    with f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})
    return len(rows)
