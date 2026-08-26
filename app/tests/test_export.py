#!/usr/bin/env python3
"""Exercise every routing path the exporter has, in a throwaway tree.

    python app/tests/test_export.py

Uses the real databases in FPSci-bin/results as source material but copies
them into a temp directory, so nothing here touches real data. Two cases
cannot be produced by playing normally and are synthesised: a run whose submit
answer is missing (crash, or killed mid-prompt), and one whose answer is
unreadable.

No pytest needed -- run it directly.
"""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ROOT = os.path.dirname(APP)
RESULTS = os.path.join(ROOT, "FPSci-bin", "results")
EXPORT = os.path.join(APP, "export.py")

if APP not in sys.path:
    sys.path.insert(0, APP)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import synthetic                                   # noqa: E402
from export import slugify                         # noqa: E402

_failures = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  " + detail if detail else ""))
    if not ok:
        _failures.append(label)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_dbs(directory=RESULTS):
    """Databases in a directory that hold a completed run."""
    out = []
    if not os.path.isdir(directory):
        return out
    for f in sorted(os.listdir(directory)):
        if not f.lower().endswith(".db"):
            continue
        p = os.path.join(directory, f)
        con = sqlite3.connect(p)
        try:
            n = con.execute("select count(*) from Trials").fetchone()[0]
        except sqlite3.Error:
            n = 0
        finally:
            con.close()
        if n:
            out.append(p)
    return out


def stub_dbs():
    """Source databases with no gameplay at all -- menu navigation leftovers."""
    out = []
    for f in sorted(os.listdir(RESULTS)):
        if not f.lower().endswith(".db"):
            continue
        p = os.path.join(RESULTS, f)
        con = sqlite3.connect(p)
        try:
            trials = con.execute("select count(*) from Trials").fetchone()[0]
            frames = con.execute(
                "select count(*) from Player_Action where state='trialTask'"
            ).fetchone()[0]
        except sqlite3.Error:
            trials, frames = 1, 1
        finally:
            con.close()
        if not trials and not frames:
            out.append(p)
    return out


def strip_answer(path, garble=False):
    """Make a copy look like a run whose submit answer never made it to disk."""
    con = sqlite3.connect(path)
    if garble:
        con.execute("update Questions set response = 'Maybe' "
                    "where question like '%Submit%'")
    else:
        con.execute("delete from Questions where question like '%Submit%'")
    con.commit()
    con.close()


def session_window(path):
    con = sqlite3.connect(path)
    row = con.execute("select min(start_time), max(end_time) from Sessions").fetchone()
    con.close()
    return row


def export(results, analysis, *extra, sittings=None):
    cmd = [sys.executable, EXPORT, "--results", results, "--analysis", analysis]
    if sittings:
        cmd += ["--sittings", sittings]
    proc = subprocess.run(cmd + list(extra), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("export failed (exit %d)" % proc.returncode)
    return proc.stdout


def main():
    print("export routing test")
    tmp = tempfile.mkdtemp(prefix="fpsci_export_")
    try:
        results = os.path.join(tmp, "results")
        analysis = os.path.join(tmp, "analysis")
        os.makedirs(results)

        real = run_dbs()
        if len(real) >= 2:
            print("  source: %d real databases" % len(real))
            for p in real:
                shutil.copy2(p, results)
        else:
            # Nothing recorded yet. Generate a sitting so the routing paths
            # are still exercised on a clean checkout.
            synthetic.populate(results, runs=2)
            print("  source: no recorded runs, using synthetic")
        sources = run_dbs(results)

        # Synthesise the two cases normal play cannot produce.
        missing = os.path.join(results, "pending_" + os.path.basename(sources[0]))
        shutil.copy2(sources[0], missing)
        strip_answer(missing)

        garbled = os.path.join(results, "garbled_" + os.path.basename(sources[0]))
        shutil.copy2(sources[0], garbled)
        strip_answer(garbled, garble=True)

        # A sitting covering everything, to prove the warm-up join. It lives
        # outside analysis/ because it is recorded, not derived -- keeping it
        # inside meant --rebuild once deleted it. The name is left blank so it
        # applies to whoever played, rather than assuming a participant.
        sittings = os.path.join(tmp, "study")
        os.makedirs(sittings)
        with open(os.path.join(sittings, "sittings.csv"), "w",
                  newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "warmup", "start_utc", "end_utc"])
            w.writerow(["", "Breathing", "2000-01-01 00:00:00.000000",
                        "2099-01-01 00:00:00.000000"])

        out = export(results, analysis, sittings=sittings)
        print()

        index = read_csv(os.path.join(analysis, "_index", "runs.csv"))
        by_state = {}
        for r in index:
            by_state.setdefault(r["submitted"], []).append(r)

        # Derived, not hardcoded: the source folder grows every time a real
        # sitting is played, and a test that assumes a count goes stale.
        expected = len(sources) + 2          # + the two synthesised cases
        check(len(index) == expected, "every run reached the ledger",
              "%d rows, expected %d" % (len(index), expected))
        check(len(by_state.get("yes", [])) >= 1, "a submitted run was routed")
        check(len(by_state.get("no", [])) >= 1, "a discarded run was routed")
        check(len(by_state.get("unanswered", [])) == 2,
              "missing and garbled answers both became unanswered",
              "%d rows" % len(by_state.get("unanswered", [])))

        # Folder name comes from the ledger, not from a guess at who played.
        for r in by_state.get("yes", []):
            slug = slugify(r["name"])
            p = os.path.join(analysis, r["scenario"], slug, "runs.csv")
            check(os.path.exists(p), "submitted run lands in the person folder",
                  os.path.relpath(p, analysis))
        for r in by_state.get("no", []):
            p = os.path.join(analysis, "_discarded", r["scenario"],
                             slugify(r["name"]), "runs.csv")
            check(os.path.exists(p), "discarded run lands in _discarded")
        for r in by_state.get("unanswered", []):
            p = os.path.join(analysis, "_pending", r["scenario"],
                             slugify(r["name"]), "runs.csv")
            check(os.path.exists(p), "unanswered run lands in _pending")

        check(all(r["warmup"] == "Breathing" for r in index),
              "warm-up joined from the sitting manifest",
              "{%s}" % ", ".join(sorted({r["warmup"] for r in index})))

        check(all(r["participant"] == "P01" for r in index),
              "one person got one stable code")

        nums = sorted(int(r["run_number"]) for r in index)
        check(nums == list(range(1, expected + 1)),
              "run numbers are dense and start at 1", str(nums))

        # Every grain must be present wherever a run landed.
        for root, _, files in os.walk(analysis):
            if "_index" in root or not files:
                continue
            names = {f for f in files if f.endswith(".csv")}
            if "runs.csv" in names:
                check(names >= {"runs.csv", "trials.csv", "shots.csv"},
                      "all grains written in %s" % os.path.relpath(root, analysis),
                      ", ".join(sorted(names)))

        # Re-exporting must not duplicate anything.
        export(results, analysis, sittings=sittings)
        again = read_csv(os.path.join(analysis, "_index", "runs.csv"))
        check(len(again) == len(index), "re-export does not duplicate rows",
              "%d -> %d" % (len(index), len(again)))

        # Stub sweeping moves gameplay-free files and only those. Bring in a
        # couple from the real folder if it has any -- and if it does not,
        # synthesise one, because a successful sweep leaves that folder with no
        # stubs in it and this check would then silently test nothing.
        real_stubs = stub_dbs()[:2]
        for p in real_stubs:
            shutil.copy2(p, results)
        if not real_stubs:
            synthetic.make_stub(os.path.join(results, "menu_stub.db"))
        before = [f for f in os.listdir(results) if f.endswith(".db")]
        n_stubs = len(before) - len(run_dbs(results))

        export(results, analysis, "--sweep-stubs", sittings=sittings)

        left = [f for f in os.listdir(results) if f.endswith(".db")]
        swept = os.path.join(results, "_stubs")
        moved = os.listdir(swept) if os.path.isdir(swept) else []

        check(n_stubs > 0, "there was at least one stub to sweep",
              "%d stub(s)" % n_stubs)
        check(len(left) == len(before) - n_stubs,
              "sweep leaves every real run in place", "%d left" % len(left))
        check(len(moved) == n_stubs, "sweep moved the stubs",
              "%d moved to _stubs/" % len(moved))
        check(len(left) + len(moved) == len(before), "sweep deleted nothing")

        print()
        if _failures:
            print("%d check(s) failed." % len(_failures))
            return 1
        print("All checks passed.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
