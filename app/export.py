#!/usr/bin/env python3
"""Route finished runs into analysis/<scenario>/<person>/*.csv.

    python app/export.py                 # export databases not yet exported
    python app/export.py --rebuild       # wipe analysis/ and redo everything
    python app/export.py --sweep-stubs   # also tidy menu stubs out of results/
    python app/export.py --dry-run       # say what would happen, change nothing

Where a run lands is decided by the player: the "Submit this run?" answer sends
it to the person's tables, to _discarded/, or to _pending/ when it could not be
read. Nothing is ever deleted -- a discarded run keeps its raw database and its
derived rows, just out of the way of the analysis tables.

Raw databases in FPSci-bin/results are never modified. --sweep-stubs moves
gameplay-free files, and moves only those.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import sys
from urllib.request import pathname2url

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fpsci_metrics import analyse                          # noqa: E402
from fpsci_metrics.geometry import parse_time              # noqa: E402
from fpsci_metrics.questions import submit_state           # noqa: E402

RESULTS = os.path.join(ROOT, "FPSci-bin", "results")
ANALYSIS = os.path.join(ROOT, "analysis")

#: Facts the experimenter recorded, which no amount of re-parsing can recreate.
#: This lives OUTSIDE analysis/ on purpose: analysis/ is derived and --rebuild
#: deletes it, and the warm-up condition is the one thing in this project that
#: cannot be recovered from the databases. It was kept in analysis/_index/ once
#: and a --rebuild destroyed it.
SITTINGS_DIR = os.path.join(ROOT, "study")

INDEX_DIR = "_index"
DISCARDED_DIR = "_discarded"
PENDING_DIR = "_pending"
STUBS_DIRNAME = "_stubs"

GRAINS = ("runs", "trials", "shots", "targets")

#: The scenarios, defined once here and imported by the launcher. Used to name
#: the directories a rebuild is allowed to delete, and to check the config.
SCENARIOS = ("gridshot", "flicks", "microadjust", "tracking")

#: The experimental conditions, as confirmed for this study. The launcher
#: offers exactly these in its dropdown and writes the chosen one into
#: study/sittings.csv; this is the one place the list is defined.
WARMUP_CONDITIONS = ("Control", "Breathing", "Aim warmup",
                     "Physical warmup", "Music")

#: Identity columns, pinned to the front of every file in this order so the
#: tables stay readable no matter which metrics happen to be present.
LEAD = ("row_id", "run_id", "participant", "name", "warmup", "scenario",
        "run_number", "scenario_run_number", "submitted")

#: Sort key per grain, so a rebuild is byte-for-byte reproducible.
SORT_KEYS = {
    "runs":    lambda r: (r.get("start_time") or "", r.get("run_id") or ""),
    "trials":  lambda r: (r.get("start_time") or "", r.get("run_id") or "",
                          _num(r.get("trial_index"))),
    "shots":   lambda r: (r.get("run_id") or "", _num(r.get("trial_index")),
                          _num(r.get("shot_index"))),
    "targets": lambda r: (r.get("run_id") or "", _num(r.get("trial_index")),
                          r.get("target_id") or ""),
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return -1.0


def slugify(name):
    """A filesystem-safe directory name for a person.

    Deliberately strict: lowercase, and anything that is not a letter, digit or
    dash becomes a dash. Two different typed names can therefore collide, which
    the participant registry detects rather than silently merging them.
    """
    out = []
    for ch in (name or "").strip().lower():
        out.append(ch if ch.isalnum() else "-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "unknown"


# --------------------------------------------------------------------------
# Reading databases
# --------------------------------------------------------------------------

def open_ro(path):
    uri = "file:" + pathname2url(os.path.abspath(path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def inspect(path):
    """Classify a database without running the metric engine over it.

    Cheap on purpose -- this runs over every file in results/ on each export,
    and the expensive per-frame work should only happen for real runs.

    A menu stub and an abandoned run both have zero Trials rows, so the split
    is on gameplay: a stub has no trialTask frames at all, an abandoned run has
    thousands. The first is disposable, the second is real data.
    """
    # "stem" is the filename and becomes run_id; "name" is the person. They are
    # kept apart deliberately -- reusing one key for both silently labelled
    # every run with the player's name instead of the run it came from.
    info = {"path": path,
            "stem": os.path.splitext(os.path.basename(path))[0],
            "name": "unknown"}
    try:
        con = open_ro(path)
    except sqlite3.Error as e:
        info.update(kind="unreadable", error=str(e))
        return info

    try:
        tabs = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
        if "Trials" not in tabs:
            info.update(kind="unreadable", error="no Trials table")
            return info

        trials = con.execute("select count(*) from Trials").fetchone()[0]
        if trials:
            info["kind"] = "run"
        else:
            frames = con.execute(
                "select count(*) from Player_Action where state='trialTask'"
            ).fetchone()[0] if "Player_Action" in tabs else 0
            info["kind"] = "abandoned" if frames else "stub"

        info["trials"] = trials
        questions = [dict(r) for r in con.execute("select * from Questions")] \
            if "Questions" in tabs else []
        info["submitted"] = submit_state(questions)

        sess = con.execute(
            "select session_id, subject_id, start_time from Sessions "
            "order by start_time limit 1").fetchone()
        if sess:
            info["scenario"] = sess["session_id"]
            info["name"] = sess["subject_id"] or "unknown"
            info["start_time"] = sess["start_time"]
    except sqlite3.Error as e:
        info.update(kind="unreadable", error=str(e))
    finally:
        con.close()
    return info


# --------------------------------------------------------------------------
# CSV helpers
# --------------------------------------------------------------------------

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def order_columns(rows):
    keys = [k for k in LEAD if any(k in r for r in rows)]
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    return keys


def upsert_csv(path, new_rows, grain, dry_run=False):
    """Merge rows into a table, replacing any run already present.

    Read-modify-write rather than append, for three reasons: re-exporting a run
    replaces its rows instead of duplicating them, a run that introduces a new
    column does not corrupt the file, and the output is sorted so a rebuild is
    reproducible. These files hold at most a few tens of thousands of rows, so
    rewriting is cheaper than the bookkeeping needed to avoid it.
    """
    if not new_rows and not os.path.exists(path):
        return 0

    incoming = {r.get("run_id") for r in new_rows}
    kept = [r for r in read_csv(path) if r.get("run_id") not in incoming]
    merged = kept + [dict(r) for r in new_rows]

    for r in merged:
        r.pop("row_id", None)
    merged.sort(key=SORT_KEYS[grain])
    for n, r in enumerate(merged, start=1):
        r["row_id"] = n

    if dry_run:
        return len(merged)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    keys = order_columns(merged)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in merged:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})
    return len(merged)


# --------------------------------------------------------------------------
# Participants and sittings
# --------------------------------------------------------------------------

def load_participants(analysis_dir):
    """The registry mapping a typed FPSci id to a stable code and folder name."""
    path = os.path.join(analysis_dir, INDEX_DIR, "participants.csv")
    reg = {}
    for row in read_csv(path):
        reg[row["name"]] = row
    return reg


def ensure_participant(reg, name, users_row=None):
    """Return the registry entry for a person, creating it on first sight."""
    if name in reg:
        entry = reg[name]
    else:
        code = "P%02d" % (len(reg) + 1)
        entry = {"participant": code, "name": name, "slug": slugify(name),
                 "mouse_dpi": "", "mouse_deg_per_mm": "", "cmp360": ""}
        reg[name] = entry
    if users_row:
        for k in ("mouse_dpi", "mouse_deg_per_mm", "cmp360"):
            v = users_row.get(k)
            if v not in (None, "") and not entry.get(k):
                entry[k] = v
    return entry


PARTICIPANT_COLUMNS = ("participant", "name", "slug", "mouse_dpi",
                       "mouse_deg_per_mm", "cmp360")


def write_participants(path, reg):
    """The registry, rewritten whole. It is one row per person, not per run."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted(reg.values(), key=lambda e: e["participant"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PARTICIPANT_COLUMNS,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k))
                        for k in PARTICIPANT_COLUMNS})


def check_slug_collisions(reg):
    by_slug = {}
    for entry in reg.values():
        by_slug.setdefault(entry["slug"], []).append(entry["name"])
    return {s: names for s, names in by_slug.items() if len(names) > 1}


def sittings_path(sittings_dir=None):
    return os.path.join(sittings_dir or SITTINGS_DIR, "sittings.csv")


def load_sittings(sittings_dir=None):
    """Warm-up conditions recorded by the launcher, one row per stretch.

    A run with no matching stretch keeps whatever warm-up label the database
    itself carried, which for anything recorded before the launcher existed is
    nothing.
    """
    path = sittings_path(sittings_dir)
    out = []
    for row in read_csv(path):
        out.append({
            "name": row.get("name") or row.get("participant") or "",
            "warmup": row.get("warmup") or "",
            "start": parse_time(row.get("start_utc")),
            "end": parse_time(row.get("end_utc")),
        })
    return out


def warmup_for(sittings, name, t_start):
    """The warm-up condition in force for a run, or None."""
    if t_start is None:
        return None
    for s in sittings:
        if s["name"] and s["name"] != name:
            continue
        if s["start"] is None:
            continue
        if t_start >= s["start"] and (s["end"] is None or t_start <= s["end"]):
            return s["warmup"] or None
    return None


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def destination(analysis_dir, submitted, scenario, slug):
    if submitted == "yes":
        base = analysis_dir
    elif submitted == "no":
        base = os.path.join(analysis_dir, DISCARDED_DIR)
    else:
        base = os.path.join(analysis_dir, PENDING_DIR)
    return os.path.join(base, scenario, slug)


def stamp(rows, ident):
    """Overwrite the engine's within-batch identity with the durable one.

    The metric engine numbers sessions across whatever databases it was handed,
    so its numbering is only correct for a full rebuild. Run numbers here come
    from the index instead, which makes exporting one new run give the same
    answer as re-deriving everything.
    """
    for r in rows:
        r.pop("subject_id", None)
        r.pop("session_number", None)
        r.pop("db", None)
        r.update(ident)
    return rows


def export_run(info, analysis_dir, reg, sittings, counters, dry_run=False):
    """Derive one database and write its rows to the right place."""
    result = analyse([info["path"]])
    if not result.runs:
        for res in result.databases:
            res.close()
        return None

    users = result.databases[0].users if result.databases else []
    entry = ensure_participant(reg, info.get("name", "unknown"),
                               users[0] if users else None)

    scenario = info.get("scenario") or (result.runs[0].get("scenario") or "unknown")

    # Anchor on when the run was PLAYED, not when the scenario was selected.
    # FPSci opens the database the moment you pick a scenario in the menu, and
    # a run can start a minute later -- long enough for the experimenter to
    # change the warm-up in between. Stamping from the session start recorded
    # the condition that was showing while the player was still on the
    # click-to-start screen, which is the wrong one.
    t_start = (parse_time(result.runs[0].get("start_time"))
               or parse_time(info.get("start_time")))

    counters["run"] = counters.get("run", 0) + 1
    key = (entry["participant"], scenario)
    counters[key] = counters.get(key, 0) + 1

    warm = warmup_for(sittings, info.get("name"), t_start)
    if warm is None:
        warm = result.runs[0].get("warmup") or ""

    ident = {
        "run_id": info["stem"],
        "participant": entry["participant"],
        "name": info.get("name", "unknown"),
        "warmup": warm,
        "scenario": scenario,
        "run_number": counters["run"],
        "scenario_run_number": counters[key],
        "submitted": info["submitted"],
    }

    out_dir = destination(analysis_dir, info["submitted"], scenario, entry["slug"])
    grains = {
        "runs": stamp(result.runs, ident),
        "trials": stamp(result.trials, ident),
        "shots": stamp(result.shots, ident),
        "targets": stamp(result.targets, ident),
    }
    for grain in GRAINS:
        upsert_csv(os.path.join(out_dir, grain + ".csv"), grains[grain],
                   grain, dry_run=dry_run)

    run_row = dict(ident)
    head = result.runs[0]
    run_row.update({
        "start_time": head.get("start_time"),
        "end_time": head.get("end_time"),
        "n_trials": head.get("n_trials"),
        "shots_fired": head.get("shots_fired"),
        "shots_hit": head.get("shots_hit"),
        "kills": head.get("kills"),
        "accuracy_pct": head.get("accuracy_pct"),
        "kills_per_second": head.get("kills_per_second"),
        "task_time_s": head.get("task_time_s"),
        "db": os.path.relpath(info["path"], ROOT).replace(os.sep, "/"),
        "exported_to": os.path.relpath(out_dir, ROOT).replace(os.sep, "/"),
    })

    for res in result.databases:
        res.close()
    return run_row


def sweep_stubs(results_dir, stubs, dry_run=False):
    """Move gameplay-free databases out of the way. Never deletes."""
    if not stubs:
        return 0
    dest = os.path.join(results_dir, STUBS_DIRNAME)
    if not dry_run:
        os.makedirs(dest, exist_ok=True)
    moved = 0
    for info in stubs:
        target = os.path.join(dest, os.path.basename(info["path"]))
        if os.path.exists(target):
            continue
        if not dry_run:
            shutil.move(info["path"], target)
        moved += 1
    return moved


# --------------------------------------------------------------------------
# Driving an export
# --------------------------------------------------------------------------

def clear_derived(analysis, scenarios=SCENARIOS):
    """Remove what the exporter wrote, and only that.

    Deliberately not ``rmtree(analysis)``. A blanket delete once destroyed the
    sitting log, which was the only record of the experimental condition. Even
    with that file moved out, removing named subtrees means anything a person
    put in analysis/ by hand survives a rebuild.
    """
    targets = [os.path.join(analysis, name)
               for name in (INDEX_DIR, DISCARDED_DIR, PENDING_DIR) + tuple(scenarios)]
    removed = 0
    for path in targets:
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed += 1
    return removed


def run_export(results=RESULTS, analysis=ANALYSIS, rebuild=False,
               sweep=False, dry_run=False, only=None, log=print,
               sittings_dir=None):
    """Export runs. Returns a summary dict.

    ``only`` restricts the work to a set of database paths. The launcher uses
    it to hand over just the databases FPSci has finished with, because the
    one it currently has open may have a completed trial but not yet the submit
    answer that decides where the run belongs.

    A run already in the ledger is skipped -- unless it was recorded as
    ``unanswered``, which is re-derived every time. That is the safety net for
    a run exported a moment too early: once the answer lands on disk, the next
    export moves it out of _pending/ and into the right place.
    """
    summary = {"exported": [], "skipped": 0, "stubs": 0, "abandoned": [],
               "unreadable": [], "moved": 0}

    if not os.path.isdir(results):
        log("No results directory at %s" % results)
        return summary

    paths = sorted(
        (os.path.join(results, f) for f in os.listdir(results)
         if f.lower().endswith(".db")),
        key=lambda p: os.path.basename(p))
    if only is not None:
        wanted = {os.path.abspath(p) for p in only}
        paths = [p for p in paths if os.path.abspath(p) in wanted]
    if not paths:
        return summary

    if rebuild and os.path.isdir(analysis) and not dry_run:
        clear_derived(analysis)

    inspected = [inspect(p) for p in paths]
    runs = [i for i in inspected if i["kind"] == "run"]
    stubs = [i for i in inspected if i["kind"] == "stub"]
    summary["stubs"] = len(stubs)
    summary["abandoned"] = [i["stem"] for i in inspected if i["kind"] == "abandoned"]
    summary["unreadable"] = [(i["stem"], i.get("error"))
                             for i in inspected if i["kind"] == "unreadable"]

    # Chronological, so run numbering matches the order they were played.
    runs.sort(key=lambda i: (i.get("start_time") or "", i["stem"]))

    reg = load_participants(analysis)
    sittings = load_sittings(sittings_dir)

    index_path = os.path.join(analysis, INDEX_DIR, "runs.csv")
    ledger = [] if rebuild else read_csv(index_path)
    settled = {r["run_id"] for r in ledger if r.get("submitted") in ("yes", "no")}

    counters = {}
    for row in ledger:
        counters["run"] = max(counters.get("run", 0), int(row.get("run_number") or 0))
        key = (row.get("participant"), row.get("scenario"))
        counters[key] = max(counters.get(key, 0),
                            int(row.get("scenario_run_number") or 0))

    for info in runs:
        if info["stem"] in settled:
            summary["skipped"] += 1
            continue
        row = export_run(info, analysis, reg, sittings, counters, dry_run=dry_run)
        if row is None:
            log("  [warn] %s: no runs derived, skipped" % info["stem"])
            continue
        summary["exported"].append(row)
        log("  %-10s %-46s -> %s" % (info["submitted"], info["stem"],
                                     row["exported_to"]))

    if summary["exported"] and not dry_run:
        upsert_csv(index_path, summary["exported"], "runs")
        write_participants(os.path.join(analysis, INDEX_DIR,
                                        "participants.csv"), reg)

    for stem in summary["abandoned"]:
        log("  [check] %s has gameplay but no completed trial -- quit mid-run?"
            % stem)
    for stem, err in summary["unreadable"]:
        log("  [warn]  %s unreadable: %s" % (stem, err))

    if stubs and sweep:
        summary["moved"] = sweep_stubs(results, stubs, dry_run=dry_run)

    for slug, names in check_slug_collisions(reg).items():
        log("  [warn]  %r all map to folder %r -- give them distinct ids"
            % (names, slug))

    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Export FPSci runs into per-scenario, per-person CSVs.")
    ap.add_argument("--results", default=RESULTS, help="directory of .db files")
    ap.add_argument("--analysis", default=ANALYSIS, help="output directory")
    ap.add_argument("--sittings", default=None,
                    help="directory holding sittings.csv (default: %s)"
                         % SITTINGS_DIR)
    ap.add_argument("--rebuild", action="store_true",
                    help="delete the analysis tree and re-export every database")
    ap.add_argument("--sweep-stubs", action="store_true",
                    help="move gameplay-free databases into results/_stubs/")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen without writing anything")
    args = ap.parse_args(argv)

    print("export%s" % ("  (dry run, nothing will be written)" if args.dry_run else ""))
    print("  results:  %s" % args.results)
    print("  analysis: %s\n" % args.analysis)

    s = run_export(args.results, args.analysis, rebuild=args.rebuild,
                   sweep=args.sweep_stubs, dry_run=args.dry_run,
                   sittings_dir=args.sittings)

    print()
    if s["skipped"]:
        print("  %d run(s) already exported, left alone" % s["skipped"])
    if s["stubs"]:
        if args.sweep_stubs:
            print("  moved %d menu stub(s) to results/%s/" % (s["moved"], STUBS_DIRNAME))
        else:
            print("  %d menu stub(s) ignored (--sweep-stubs to tidy them away)"
                  % s["stubs"])

    ex = s["exported"]
    print("\n%d run(s) exported, %d discarded, %d pending"
          % (sum(1 for r in ex if r["submitted"] == "yes"),
             sum(1 for r in ex if r["submitted"] == "no"),
             sum(1 for r in ex if r["submitted"] == "unanswered")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
