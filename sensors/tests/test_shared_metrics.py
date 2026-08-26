#!/usr/bin/env python
"""The heart-rate join and the exporter must report the same aim numbers.

    python tests/test_shared_metrics.py

Before this, correlate.py counted shots and computed accuracy itself while
app/export.py computed it from the metric engine. Two implementations of the
same quantity is how a thesis ends up with two different accuracies for one
trial. They now share one implementation; this checks that they really do, by
running both over the same databases and diffing every shared column.

No Bluetooth, no pytest.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.normpath(os.path.join(ROOT, "..", "app"))
for p in (ROOT, HERE, APP):
    if p not in sys.path:
        sys.path.insert(0, p)

import correlate                                    # noqa: E402
import export                                       # noqa: E402
from test_multi_db import make_db, stamp            # noqa: E402
from polar_h10 import fileio                        # noqa: E402

_failures = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  " + detail if detail else ""))
    if not ok:
        _failures.append(label)


def all_rows(analysis_dir, filename):
    """Every row of a given grain, wherever in the tree it was routed."""
    out = []
    for root, _, files in os.walk(analysis_dir):
        if "_index" in root or filename not in files:
            continue
        with open(os.path.join(root, filename), newline="",
                  encoding="utf-8") as f:
            out.extend(csv.DictReader(f))
    return out


def close(a, b, tol=1e-9):
    if a in (None, "") or b in (None, ""):
        return a in (None, "") and b in (None, "")
    return abs(float(a) - float(b)) <= tol


def main():
    print("shared metrics")
    tmp = tempfile.mkdtemp(prefix="fpsci_shared_")
    try:
        results = os.path.join(tmp, "results")
        analysis = os.path.join(tmp, "analysis")
        os.makedirs(results)

        make_db(os.path.join(results, "gridshot_M_1.db"), "gridshot", 0, 60)
        make_db(os.path.join(results, "flicks_M_2.db"), "flicks", 100, 60, trials=5)

        dbs = sorted(os.path.join(results, f) for f in os.listdir(results))

        # Consumer 1: the exporter.
        export.run_export(results, analysis, log=lambda _m: None)
        exported = {r["trial_id"] + "/" + str(r["trial_index"]): r
                    for r in all_rows(analysis, "trials.csv")}
        check(len(exported) == 6, "exporter produced trial rows",
              "%d rows" % len(exported))

        # Consumer 2: the heart-rate join, with no strap attached. The aim
        # columns must be populated regardless; only the physiology is blank.
        fpsci = correlate.load_fpsci(dbs)
        polar = {"beats": [], "t0": fileio.parse_utc(stamp(-10)),
                 "t1": fileio.parse_utc(stamp(500))}
        joined = {r["trial_id"] + "/" + str(r["trial_index"]): r
                  for r in correlate.trial_rows(fpsci, polar, {})}
        check(len(joined) == len(exported), "join produced the same trial rows",
              "%d vs %d" % (len(joined), len(exported)))
        check(set(joined) == set(exported), "the same trials, keyed identically")

        # Every aim quantity the join carries must equal the exported one.
        pairs = [("shots", "shots_fired"), ("hits", "shots_hit"),
                 ("misses", "shots_missed"), ("kills", "kills"),
                 ("accuracy_pct", "accuracy_pct"),
                 ("mean_angular_error_deg", "mean_angular_error_deg"),
                 ("time_on_target_pct", "time_on_target_pct"),
                 ("peak_angular_velocity_dps", "peak_angular_velocity_dps")]
        mismatches = []
        for key in sorted(set(joined) & set(exported)):
            for mine, theirs in pairs:
                a, b = joined[key].get(mine), exported[key].get(theirs)
                if not close(a, b):
                    mismatches.append("%s %s: %r vs %r" % (key, mine, a, b))
        check(not mismatches, "every shared aim metric agrees exactly",
              "; ".join(mismatches[:3]) if mismatches
              else "%d metrics x %d trials" % (len(pairs), len(exported)))

        # And the run-level rollup, which recomputes rates from run totals.
        exported_runs = {r["scenario"]: r for r in all_rows(analysis, "runs.csv")}
        joined_runs = {r["scenario"]: r
                       for r in correlate.session_rows(fpsci, polar, {})}
        check(set(joined_runs) == set(exported_runs), "same runs on both sides",
              ", ".join(sorted(joined_runs)))
        run_bad = []
        for scenario in sorted(set(joined_runs) & set(exported_runs)):
            for mine, theirs in pairs:
                a = joined_runs[scenario].get(mine)
                b = exported_runs[scenario].get(theirs)
                if not close(a, b):
                    run_bad.append("%s %s: %r vs %r" % (scenario, mine, a, b))
        check(not run_bad, "run-level metrics agree exactly",
              "; ".join(run_bad[:3]) if run_bad else "")

        # The physiology side must be present but empty with no strap data.
        any_row = next(iter(joined.values()))
        check("hr_mean_bpm" in any_row, "physiology columns exist even with no strap")
        check(not any_row.get("hr_mean_bpm"),
              "and are blank rather than fabricated",
              repr(any_row.get("hr_mean_bpm")))

        print()
        if _failures:
            print("%d check(s) failed." % len(_failures))
            return 1
        print("All checks passed.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
