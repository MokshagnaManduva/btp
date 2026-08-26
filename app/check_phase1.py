#!/usr/bin/env python3
"""Verify the Phase 1 config flip actually took, by inspecting what FPSci wrote.

Play at least two runs, answering the "Submit this run?" prompt, then:

    python app/check_phase1.py

Every check reads a results database rather than the config, so it reports what
the game really did, not what we asked it to do. Databases are opened read-only.

Not every .db is a run. With logToSingleDb false FPSci opens a fresh database
the moment a scenario is SELECTED, so browsing the menu leaves small files
behind with no gameplay in them. They are classified and skipped, not failed.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import sys
from urllib.request import pathname2url

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fpsci_metrics.questions import clean_answer          # noqa: E402

RESULTS = os.path.join(ROOT, "FPSci-bin", "results")
SESSIONS_LOG = os.path.join(ROOT, "FPSci-bin", "userstatus.sessions.csv")

SCENARIOS = ("gridshot", "flicks", "microadjust", "tracking")

# <scenario>_<user>_<YYYY_MM_DD-HH_MM_SS>, written when logToSingleDb is false.
PER_RUN_NAME = re.compile(
    r"^(" + "|".join(SCENARIOS) + r")_(.+)_(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})$")

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"
_results = []


def record(status, label, detail=""):
    _results.append((status, label, detail))
    print("  [%s] %-44s %s" % (status, label, detail))


def open_ro(path):
    uri = "file:" + pathname2url(os.path.abspath(path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def tables(con):
    return {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")}


def classify(con, tabs):
    trials = con.execute("select count(*) from Trials").fetchone()[0] \
        if "Trials" in tabs else 0
    if trials:
        return "run", trials
    task_frames = con.execute(
        "select count(*) from Player_Action where state='trialTask'"
    ).fetchone()[0] if "Player_Action" in tabs else 0
    return ("abandoned" if task_frames else "stub"), 0


def check_run(path, con, tabs, trials):
    name = os.path.splitext(os.path.basename(path))[0]

    m = PER_RUN_NAME.match(name)
    if m:
        record(PASS, "filename is per-run", "%s / %s" % (m.group(1), m.group(2)))
    else:
        record(FAIL, "filename is per-run",
               "got %r -- logToSingleDb may still be true" % name)

    sess = [dict(r) for r in con.execute("select * from Sessions")]
    if len(sess) != 1:
        record(FAIL, "one session per file", "found %d" % len(sess))
        if not sess:
            return
    s = sess[-1]

    if s["complete"]:
        record(PASS, "Sessions.complete", "1 -- session finished normally")
    else:
        record(FAIL, "Sessions.complete",
               "0 -- the end-of-session block did not run")

    if s["start_time"] != s["end_time"]:
        record(PASS, "Sessions.end_time advanced", str(s["end_time"]))
    else:
        record(FAIL, "Sessions.end_time advanced", "equal to start_time")

    record(PASS, "trials recorded", str(trials))

    subs = [dict(r) for r in con.execute(
        "select * from Questions where question like '%Submit%'")] \
        if "Questions" in tabs else []
    if not subs:
        record(FAIL, "submit answer recorded", "no Submit question row")
    else:
        ans = clean_answer(subs[-1])
        if ans in ("Yes", "No"):
            record(PASS, "submit answer recorded",
                   "%s   (stored as %r)" % (ans, subs[-1]["response"]))
        else:
            record(FAIL, "submit answer recorded", "unexpected answer %r" % ans)

    if "Experiments" in tabs:
        n = con.execute("select count(*) from Experiments").fetchone()[0]
        if n:
            row = con.execute(
                "select description, hash, length(config) as n from Experiments"
            ).fetchone()
            record(PASS, "Experiments populated",
                   "%s, hash %s, %d bytes of config"
                   % (row["description"], row["hash"], row["n"]))
        else:
            record(FAIL, "Experiments populated",
                   "empty -- an apostrophe is back in experimentconfig.Any")
    else:
        record(FAIL, "Experiments populated", "table missing")

    fr = str(s["frameRate"]).strip() if "frameRate" in s.keys() else "?"
    record(INFO, "frameRate logged",
           "%s  (no override in config; ~477 Hz measured)" % fr)


def main():
    print("Phase 1 verification")
    print("results: %s\n" % RESULTS)

    dbs = sorted(glob.glob(os.path.join(RESULTS, "*.db")), key=os.path.getmtime)
    if not dbs:
        print("  No databases yet. Play two runs first.")
        return 1

    runs, stubs, abandoned = [], [], []
    for path in dbs:
        con = open_ro(path)
        kind, trials = classify(con, tables(con))
        (runs if kind == "run" else
         abandoned if kind == "abandoned" else stubs).append((path, trials))
        con.close()

    mb = sum(os.path.getsize(p) for p in dbs) / 1e6
    print("%d database(s), %.1f MB: %d run(s), %d menu stub(s), %d abandoned"
          % (len(dbs), mb, len(runs), len(stubs), len(abandoned)))

    for path, _ in stubs:
        print("  skip (menu stub)  %s" % os.path.basename(path))
    for path, _ in abandoned:
        print("  skip (abandoned mid-trial, no Trials row)  %s"
              % os.path.basename(path))

    if len(runs) < 2:
        print("\n  Only %d completed run(s). Play at least two, answering the"
              "\n  'Submit this run?' prompt each time, then re-run this."
              % len(runs))
        return 1

    for path, trials in runs:
        print("\n%s" % os.path.basename(path))
        con = open_ro(path)
        check_run(path, con, tables(con), trials)
        con.close()

    print("\nuserstatus.sessions.csv")
    if os.path.exists(SESSIONS_LOG):
        lines = [l for l in open(SESSIONS_LOG, encoding="utf-8").read().splitlines()
                 if l.strip()]
        if len(lines) >= len(runs):
            record(PASS, "completions logged",
                   "%d line(s), last: %s" % (len(lines), lines[-1]))
        elif lines:
            record(FAIL, "completions logged",
                   "%d line(s) for %d run(s)" % (len(lines), len(runs)))
        else:
            record(FAIL, "completions logged",
                   "empty -- markSessComplete not firing, or file is read-only")
    else:
        record(FAIL, "completions logged", "file missing")

    fails = [r for r in _results if r[0] == FAIL]
    checks = [r for r in _results if r[0] != INFO]
    print("\n%d checks over %d run(s): %d passed, %d failed"
          % (len(checks), len(runs), len(checks) - len(fails), len(fails)))
    if fails:
        print("\nFailed:")
        for _, label, detail in fails:
            print("  - %s (%s)" % (label, detail))
    else:
        print("\nPhase 1 verified.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
