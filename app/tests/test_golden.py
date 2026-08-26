#!/usr/bin/env python3
"""Prove the refactor changed no numbers.

Runs the original FPSci/scripts/analysis/fpsci_parse.py and the refactored
app/fpsci_metrics over the same databases, writes both through the same CSV
writer, and diffs the results byte-for-byte.

    python app/tests/test_golden.py                 # uses FPSci-bin/results
    python app/tests/test_golden.py path/to/dbs

Byte-for-byte rather than value-by-value on purpose: it catches a column that
moved or a float that formats differently, not just one that changed value.
No pytest needed -- run it directly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ROOT = os.path.dirname(APP)

OLD_SCRIPT = os.path.join(ROOT, "FPSci", "scripts", "analysis", "fpsci_parse.py")
DEFAULT_DBS = os.path.join(ROOT, "FPSci-bin", "results")

TABLES = ("runs.csv", "trials.csv", "shots.csv", "targets.csv", "sessions.csv")

if APP not in sys.path:
    sys.path.insert(0, APP)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import synthetic                                      # noqa: E402
from fpsci_metrics import analyse, write_csv          # noqa: E402


def run_old(db_dir, outdir):
    """The original script, unmodified, as the reference implementation."""
    proc = subprocess.run(
        [sys.executable, OLD_SCRIPT, db_dir, "-o", outdir, "--no-raw", "-q"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("reference parser failed (exit %d)" % proc.returncode)


def run_new(db_dir, outdir):
    os.makedirs(outdir, exist_ok=True)
    result = analyse(db_dir)
    write_csv(os.path.join(outdir, "runs.csv"), result.runs)
    write_csv(os.path.join(outdir, "trials.csv"), result.trials)
    write_csv(os.path.join(outdir, "shots.csv"), result.shots)
    write_csv(os.path.join(outdir, "targets.csv"), result.targets)
    write_csv(os.path.join(outdir, "sessions.csv"), result.sessions)
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"sessions": result.sessions, "trials": result.trials},
                  f, indent=2, default=str)
    for res in result.databases:
        res.close()
    return result


def first_difference(a_path, b_path):
    """Line number and both texts at the first differing line, or None."""
    with open(a_path, encoding="utf-8") as f:
        a = f.read().splitlines()
    with open(b_path, encoding="utf-8") as f:
        b = f.read().splitlines()
    for i, (x, y) in enumerate(zip(a, b), start=1):
        if x != y:
            return i, x, y
    if len(a) != len(b):
        return min(len(a), len(b)) + 1, "<%d lines>" % len(a), "<%d lines>" % len(b)
    return None


def main(argv):
    db_dir = argv[1] if len(argv) > 1 else DEFAULT_DBS
    dbs = ([f for f in os.listdir(db_dir) if f.lower().endswith(".db")]
           if os.path.isdir(db_dir) else [])

    print("golden test")

    tmp = tempfile.mkdtemp(prefix="fpsci_golden_")
    try:
        # The reference parser opens databases read-write, so give both sides
        # copies and leave the originals alone.
        snap = os.path.join(tmp, "snap")
        os.makedirs(snap)
        if dbs:
            print("  databases: %d real, from %s" % (len(dbs), db_dir))
            for f in dbs:
                shutil.copy2(os.path.join(db_dir, f), snap)
        else:
            # No recorded data: generate some. Real databases are the better
            # test, but the suite has to stay runnable on a clean checkout.
            synthetic.populate(snap, runs=2)
            print("  databases: none recorded, using %d synthetic"
                  % len(os.listdir(snap)))

        old_dir, new_dir = os.path.join(tmp, "old"), os.path.join(tmp, "new")
        run_old(snap, old_dir)
        result = run_new(snap, new_dir)
        print("  %r" % result)

        failures = 0
        for table in TABLES:
            a, b = os.path.join(old_dir, table), os.path.join(new_dir, table)
            if not os.path.exists(a) and not os.path.exists(b):
                print("  [SKIP] %-14s neither side produced it" % table)
                continue
            if not os.path.exists(a) or not os.path.exists(b):
                print("  [FAIL] %-14s only one side produced it" % table)
                failures += 1
                continue
            diff = first_difference(a, b)
            if diff is None:
                rows = sum(1 for _ in open(a, encoding="utf-8")) - 1
                print("  [OK]   %-14s identical (%d rows)" % (table, rows))
            else:
                ln, x, y = diff
                print("  [FAIL] %-14s first difference at line %d" % (table, ln))
                print("           reference: %s" % x[:110])
                print("           refactor : %s" % y[:110])
                failures += 1

        print()
        if failures:
            print("%d of %d tables differ -- the refactor changed behaviour."
                  % (failures, len(TABLES)))
            return 1
        print("All %d tables identical. The refactor changed no numbers." % len(TABLES))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
