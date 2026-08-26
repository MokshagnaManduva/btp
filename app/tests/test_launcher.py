#!/usr/bin/env python3
"""Test the launcher's non-interactive parts.

    python app/tests/test_launcher.py

Covers the three things that can silently ruin a sitting: a preflight check
that never fires, a generated config that FPSci cannot parse, and a warm-up
label that does not reach the exported rows. The GUI itself is only smoke
tested -- it is built and torn down without ever being shown.

No pytest needed -- run it directly.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ROOT = os.path.dirname(APP)

if APP not in sys.path:
    sys.path.insert(0, APP)

import export                                        # noqa: E402
import run_study as rs                               # noqa: E402
from fpsci_metrics import anyconfig                  # noqa: E402

REAL_CONFIG = os.path.join(ROOT, "FPSci-bin", "experimentconfig.Any")

_failures = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  " + detail if detail else ""))
    if not ok:
        _failures.append(label)


def any_loads(text):
    """Parse a generated .Any well enough to prove it is structurally sound."""
    s = anyconfig.strip_comments(text)
    s = re.sub(r'\b(?:Vector|Point|Color)[234]\s*\(', '[', s)
    s = s.replace(')', ']')
    s = re.sub(r',(\s*[\]}])', r'\1', s)
    return json.loads(s)


# --------------------------------------------------------------------------

def test_preflight_fires():
    """Every check must actually trigger on a config that breaks it."""
    print("\npreflight catches real problems")
    good = open(REAL_CONFIG, encoding="utf-8").read()
    tmp = tempfile.mkdtemp(prefix="fpsci_pre_")
    try:
        exe = os.path.join(tmp, "FirstPersonScience.exe")
        open(exe, "w").close()
        log = os.path.join(tmp, "userstatus.sessions.csv")
        open(log, "w").close()

        def run(text):
            path = os.path.join(tmp, "experimentconfig.Any")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return " | ".join(rs.preflight(exe=exe, config=path, sessions_log=log))

        check(run(good) == "", "a good config produces no problems", run(good))

        broken = [
            ("apostrophe",
             good.replace("// =====", "// it's fine =====", 1), "apostrophe"),
            ("logToSingleDb true",
             good.replace('"logToSingleDb": false', '"logToSingleDb": true'),
             "logToSingleDb"),
            ("session feedback gate reopened",
             good.replace('"sessionFeedbackDuration": 2.5',
                          '"sessionFeedbackDuration": 86400.0'),
             "sessionFeedbackDuration"),
            ("submit prompt removed",
             good.replace('"prompt": "Submit this run?"',
                          '"prompt": "Something else?"', 1),
             "submit prompt"),
            ("randomOrder dropped",
             good.replace('"randomOrder": false', '"randomOrder": true', 1),
             "randomOrder"),
            ("parser hook restored",
             good.replace('"logToSingleDb": false',
                          '"commandsOnSessionStart": [ { "command": "x" } ],'
                          + chr(10) + '    "logToSingleDb": false'),
             "commandsOnSessionStart"),
        ]
        for label, text, needle in broken:
            out = run(text)
            check(needle in out, "catches: " + label, out[:90] or "(nothing)")

        # A read-only completed-session log is the play.bat hack coming back.
        # Restore the good config first: otherwise this check passes on the
        # previous case's breakage rather than on the one it is testing.
        run(good)
        os.chmod(log, 0o444)
        out = " | ".join(rs.preflight(exe=exe, config=os.path.join(
            tmp, "experimentconfig.Any"), sessions_log=log))
        os.chmod(log, 0o666)
        check(out.count("|") == 0 and "read-only" in out,
              "catches: read-only sessions log, and only that",
              out[:90] or "(nothing)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_generated_configs():
    print("\ngenerated configs are valid")
    tmp = tempfile.mkdtemp(prefix="fpsci_cfg_")
    try:
        status = os.path.join(tmp, "userstatus.Any")
        config = os.path.join(tmp, "userconfig.Any")

        rs.write_user_status("Mokshagna", ["Mokshagna", "ise"], path=status)
        rs.write_user_config(
            {"Mokshagna": {"mouse_dpi": "800", "mouse_deg_per_mm": "1.2"},
             "ise": {}}, path=config)

        text = open(status, encoding="utf-8").read()
        parsed = any_loads(text)
        check(parsed["allowRepeat"] is True, "allowRepeat is on")
        check(parsed["currentUser"] == "Mokshagna", "currentUser is set")
        check("'" not in text, "no apostrophes in userstatus.Any")

        per_user = {u["id"]: u["sessions"] for u in parsed["users"]}
        check(set(per_user) == {"Mokshagna", "ise"}, "every participant listed",
              ", ".join(sorted(per_user)))
        counts = {s: per_user["Mokshagna"].count(s) for s in rs.SCENARIOS}
        check(set(counts.values()) == {rs.REPEAT_QUOTA},
              "each scenario listed %d times" % rs.REPEAT_QUOTA, str(counts))

        cfg = any_loads(open(config, encoding="utf-8").read())
        users = {u["id"]: u for u in cfg["users"]}
        check(users["Mokshagna"]["mouseDPI"] == 800, "known DPI preserved")
        check(users["ise"]["mouseDPI"] == rs.DEFAULT_DPI,
              "unknown participant gets the default DPI")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sitting_log():
    print("\nsitting log tracks condition changes")
    tmp = tempfile.mkdtemp(prefix="fpsci_sit_")
    try:
        log = rs.SittingLog(tmp)
        log.start("Mokshagna", "Control")
        time.sleep(0.01)
        log.start("Mokshagna", "Breathing")     # changing the dropdown
        time.sleep(0.01)
        log.close()

        rows = export.read_csv(export.sittings_path(tmp))
        check(len(rows) == 2, "one row per condition", "%d rows" % len(rows))
        check([r["warmup"] for r in rows] == ["Control", "Breathing"],
              "rows are in chronological order")
        check(all(r["end_utc"] for r in rows), "every stretch was closed")
        check(rows[0]["end_utc"] <= rows[1]["start_utc"],
              "stretches do not overlap")

        loaded = export.load_sittings(tmp)
        mid = (loaded[1]["start"] + loaded[1]["end"]) / 2
        check(export.warmup_for(loaded, "Mokshagna", mid) == "Breathing",
              "a run mid-stretch gets that stretch's condition")
        check(export.warmup_for(loaded, "Mokshagna", loaded[0]["start"]) == "Control",
              "a run in the earlier stretch keeps the earlier condition")
        check(export.warmup_for(loaded, "someone-else", mid) is None,
              "another participant is not labelled from this sitting")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_watcher_holds_back_live_db():
    print("\nwatcher waits for FPSci to finish a database")
    tmp = tempfile.mkdtemp(prefix="fpsci_watch_")
    try:
        results = os.path.join(tmp, "results")
        os.makedirs(results)
        old = os.path.join(results, "old.db")
        new = os.path.join(results, "new.db")
        for p in (old, new):
            with open(p, "wb") as f:
                f.write(b"x" * 16)

        # Make "old" clearly stale and "new" the one being written.
        past = time.time() - 3600
        os.utime(old, (past, past))

        w = rs.Watcher(queue.Queue(), results=results,
                       analysis=os.path.join(tmp, "analysis"))
        w.game_running.set()
        w.settled()                     # first pass records sizes
        settled = {os.path.basename(p) for p in w.settled()}
        check("new.db" not in settled,
              "the database FPSci has open is left alone", str(sorted(settled)))
        check("old.db" in settled, "a finished database is picked up")

        w.game_running.clear()          # FPSci exited
        settled = {os.path.basename(p) for p in w.settled()}
        check("new.db" in settled,
              "after the game exits, the last database is released too",
              str(sorted(settled)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gui_builds():
    print("\ncontrol panel builds")
    try:
        root = rs.build_gui()
        root.withdraw()
        root.update_idletasks()
        combos = []

        def walk(w):
            for child in w.winfo_children():
                combos.append(child)
                walk(child)
        walk(root)
        values = [c.cget("values") for c in combos
                  if c.winfo_class() == "TCombobox"]
        flat = [tuple(v) for v in values]
        check(any(set(rs.WARMUP_CONDITIONS) <= set(v) for v in flat),
              "warm-up dropdown offers every condition",
              "%d conditions" % len(rs.WARMUP_CONDITIONS))
        root.destroy()
    except Exception as e:
        check(False, "control panel builds", str(e))


def main():
    print("launcher test")
    test_preflight_fires()
    test_generated_configs()
    test_sitting_log()
    test_watcher_holds_back_live_db()
    test_gui_builds()

    print()
    if _failures:
        print("%d check(s) failed:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
