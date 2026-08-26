#!/usr/bin/env python3
"""Regression tests for two bugs found the first time a real sitting was played.

    python app/tests/test_warmup_anchor.py

**The label came from the wrong moment.** FPSci opens a database when a
scenario is *selected*, and the run can start a minute later. The warm-up was
stamped from the session start, so changing the condition while the player was
still on the click-to-start screen recorded the previous one. Observed live:
a run played entirely under Breathing was filed as Control.

**--rebuild destroyed the sitting log.** It lived in analysis/_index/ and
rebuild did rmtree(analysis). The warm-up condition is the one thing here that
cannot be re-derived from the databases, so that was unrecoverable data loss.

No pytest needed -- run it directly.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
for _p in (APP, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export                                          # noqa: E402
import synthetic                                       # noqa: E402
from synthetic import stamp                            # noqa: E402

_failures = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  " + detail if detail else ""))
    if not ok:
        _failures.append(label)


def make_db(path, scenario, selected_s, played_s, duration_s=60):
    """A run where the scenario was selected well before it was played.

    That gap is the whole point: it is where the condition can change.
    """
    return synthetic.make_run(path, scenario=scenario, subject="Mokshagna",
                              selected_s=selected_s, played_s=played_s,
                              duration_s=duration_s)


def write_sittings(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "name", "warmup", "start_utc", "end_utc"])
        w.writerows(rows)


def ledger(analysis):
    path = os.path.join(analysis, export.INDEX_DIR, "runs.csv")
    return {r["run_id"]: r for r in export.read_csv(path)}


def test_anchor():
    print("\nwarm-up is stamped from when the run was played")
    tmp = tempfile.mkdtemp(prefix="fpsci_anchor_")
    try:
        results = os.path.join(tmp, "results")
        analysis = os.path.join(tmp, "analysis")
        sittings = os.path.join(tmp, "study")
        os.makedirs(results)

        # Selected at +0, played at +90. The condition changes at +30, i.e.
        # after the database exists but well before the run starts.
        make_db(os.path.join(results, "gridshot_M_late.db"), "gridshot",
                selected_s=0, played_s=90)
        # And one played entirely inside the first stretch.
        make_db(os.path.join(results, "gridshot_M_early.db"), "gridshot",
                selected_s=-300, played_s=-280, duration_s=10)

        write_sittings(os.path.join(sittings, "sittings.csv"), [
            ["s1", "Mokshagna", "Control", stamp(-600), stamp(30)],
            ["s2", "Mokshagna", "Breathing", stamp(30), stamp(600)],
        ])

        export.run_export(results, analysis, sittings_dir=sittings,
                          log=lambda _m: None)
        rows = ledger(analysis)

        late = rows.get("gridshot_M_late", {})
        early = rows.get("gridshot_M_early", {})
        check(late.get("warmup") == "Breathing",
              "a run played after the switch gets the new condition",
              "got %r (selected under Control, played under Breathing)"
              % late.get("warmup"))
        check(early.get("warmup") == "Control",
              "a run played before the switch keeps the old one",
              "got %r" % early.get("warmup"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rebuild_preserves_sittings():
    print("\nrebuild does not destroy what it did not create")
    tmp = tempfile.mkdtemp(prefix="fpsci_rebuild_")
    try:
        results = os.path.join(tmp, "results")
        analysis = os.path.join(tmp, "analysis")
        sittings = os.path.join(tmp, "study")
        os.makedirs(results)
        make_db(os.path.join(results, "gridshot_M_1.db"), "gridshot", 0, 10, 10)
        sit_path = os.path.join(sittings, "sittings.csv")
        write_sittings(sit_path, [["s1", "Mokshagna", "Music",
                                   stamp(-60), stamp(600)]])

        export.run_export(results, analysis, sittings_dir=sittings,
                          log=lambda _m: None)
        # Something a person dropped into the derived tree by hand.
        note = os.path.join(analysis, "notes.txt")
        with open(note, "w", encoding="utf-8") as f:
            f.write("do not delete me")

        export.run_export(results, analysis, rebuild=True,
                          sittings_dir=sittings, log=lambda _m: None)

        check(os.path.exists(sit_path), "the sitting log survives a rebuild")
        check(os.path.exists(note), "an unrecognised file survives a rebuild")
        check(ledger(analysis).get("gridshot_M_1", {}).get("warmup") == "Music",
              "and the condition still lands on the run after rebuilding")

        derived = os.path.join(analysis, "gridshot")
        check(os.path.isdir(derived), "derived output was rebuilt, not just kept")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("warm-up anchor and rebuild safety")
    test_anchor()
    test_rebuild_preserves_sittings()
    print()
    if _failures:
        print("%d check(s) failed." % len(_failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
