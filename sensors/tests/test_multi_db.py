#!/usr/bin/env python
"""One sitting is now several FPSci databases. Prove the join still sees it all.

    python tests/test_multi_db.py

FPSci moved to one database per run, so picking the single newest file -- which
is what correlate.py used to do -- would join a whole sitting against its last
run alone. These tests build small synthetic databases with known time spans
and check the selection and the merge.

No Bluetooth, no pytest.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import correlate                                      # noqa: E402
from polar_h10 import fileio                          # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S.%f"
BASE = datetime(2026, 8, 21, 12, 0, 0)

_failures = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  " + detail if detail else ""))
    if not ok:
        _failures.append(label)


def stamp(offset_s):
    return (BASE + timedelta(seconds=offset_s)).strftime(FMT)


def make_db(path, scenario, start_s, duration_s, trials=1):
    """A results database with the columns correlate.py actually reads."""
    conn = sqlite3.connect(path)
    conn.execute("create table Sessions (session_id text, start_time text, "
                 "end_time text, subject_id text, description text, "
                 "complete boolean, tasks_complete int, trials_complete int)")
    conn.execute("create table Trials (session_id text, block_id int, "
                 "task_id text, task_index int, trial_id text, trial_index int, "
                 "start_time text, end_time text, pretrial_duration real, "
                 "task_execution_time real, destroyed_targets int, "
                 "total_targets int)")
    conn.execute("create table Player_Action (time text, position_az real, "
                 "position_el real, state text, event text, target_id text)")

    conn.execute("insert into Sessions values (?,?,?,?,?,?,?,?)",
                 (scenario, stamp(start_s), stamp(start_s + duration_s),
                  "Mokshagna", "aimtrain/" + scenario, 1, 1, trials))
    for i in range(trials):
        t0 = start_s + i * (duration_s / max(trials, 1))
        t1 = t0 + (duration_s / max(trials, 1))
        conn.execute("insert into Trials values (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (scenario, 0, scenario + "_task", 0, scenario + "_trial", i,
                      stamp(t0), stamp(t1), 0.4, t1 - t0, 3, 3))
        conn.execute("insert into Player_Action values (?,?,?,?,?,?)",
                     (stamp(t0 + 1), 0.0, 0.0, "trialTask", "hit", "t0"))
    conn.commit()
    conn.close()


def make_stub(path, scenario, start_s):
    """A menu stub: a session row, no trials, no gameplay."""
    make_db(path, scenario, start_s, 0, trials=0)


def main():
    print("multi-database join")
    tmp = tempfile.mkdtemp(prefix="fpsci_multi_")
    try:
        results = os.path.join(tmp, "results")
        os.makedirs(results)

        # A sitting: four runs back to back, plus a stub from menu browsing.
        make_db(os.path.join(results, "gridshot_M_1.db"), "gridshot", 0, 60)
        make_db(os.path.join(results, "flicks_M_2.db"), "flicks", 100, 60, trials=5)
        make_db(os.path.join(results, "microadjust_M_3.db"), "microadjust", 200, 60)
        make_db(os.path.join(results, "tracking_M_4.db"), "tracking", 300, 60)
        make_stub(os.path.join(results, "gridshot_M_stub.db"), "gridshot", 90)
        # An unrelated run from a different day, well outside the window.
        make_db(os.path.join(results, "gridshot_M_old.db"), "gridshot",
                -86400, 60)

        t0 = fileio.parse_utc(stamp(-30))
        t1 = fileio.parse_utc(stamp(400))

        picked = correlate.dbs_for_window(results, t0, t1)
        names = sorted(os.path.basename(p) for p in picked)
        check(len(picked) == 4, "all four runs of the sitting are picked up",
              "%d picked" % len(picked))
        check("gridshot_M_stub.db" not in names,
              "a menu stub with no trials is skipped")
        check("gridshot_M_old.db" not in names,
              "a run from outside the window is skipped")

        fpsci = correlate.load_fpsci(picked)
        check(len(fpsci["trials"]) == 8, "trials from every database are merged",
              "%d trials" % len(fpsci["trials"]))
        starts = [t["start_unix"] for t in fpsci["trials"]]
        check(starts == sorted(starts), "merged trials are in time order")
        check(len({t["session_id"] for t in fpsci["trials"]}) == 4,
              "all four scenarios present",
              ", ".join(sorted({t["session_id"] for t in fpsci["trials"]})))
        check(all(t.get("db") for t in fpsci["trials"]),
              "every trial knows which database it came from")
        check(len(fpsci["events"]) == 8, "shot events are merged too",
              "%d events" % len(fpsci["events"]))

        # The old behaviour, for contrast: newest file only.
        newest = max(
            (os.path.join(results, f) for f in os.listdir(results)
             if f.endswith(".db")), key=os.path.getmtime)
        only_newest = correlate.load_fpsci(newest)
        check(len(only_newest["trials"]) < len(fpsci["trials"]),
              "picking one database alone would have lost trials",
              "%d vs %d" % (len(only_newest["trials"]), len(fpsci["trials"])))

        # A single path must still work -- the end-to-end test passes one.
        one = correlate.load_fpsci(os.path.join(results, "gridshot_M_1.db"))
        check(len(one["trials"]) == 1, "a single database path still works")

        empty = correlate.dbs_for_window(results, fileio.parse_utc(stamp(9000)),
                                         fileio.parse_utc(stamp(9100)))
        check(empty == [], "a window with no gameplay selects nothing")

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
