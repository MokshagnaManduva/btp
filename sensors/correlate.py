#!/usr/bin/env python
"""Join a Polar H10 recording to the FPSci runs it overlaps.

    python correlate.py                          # newest recording, every run in it
    python correlate.py --session data/raw/20260820-154720Z_184CB835
    python correlate.py --fpsci-db ../FPSci-bin/results/gridshot_M_2026_08_21-17_03_10.db
    python correlate.py --list                   # what is available to join

FPSci writes one database per run, so a sitting of four scenarios is four
files. By default every database holding gameplay inside the recording window
is joined; --fpsci-db pins it to specific ones and can be repeated.

Both sides timestamp in UTC with the same format, so the join is a plain time
comparison -- no clock syncing, no marker matching, no timezone maths.  Outputs
land in data/processed/<recording id>/:

    trials_hr.csv      one row per FPSci trial: performance + HR/HRV over it
    sessions_hr.csv    the same rolled up per FPSci session (scenario)
    timeline.csv       fixed-rate merged timeline of HR and gameplay events
    summary.json       overlap diagnostics, baseline, what was written

Stdlib only -- no pandas needed.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sqlite3
import sys
from urllib.request import pathname2url

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# The aim metrics come from the same engine the exporter uses. Nothing about
# aim performance is computed twice in this project: accuracy for a trial has
# exactly one implementation, in app/fpsci_metrics.
APP = os.path.normpath(os.path.join(HERE, "..", "app"))
if APP not in sys.path:
    sys.path.insert(0, APP)

from polar_h10 import fileio, hrv                    # noqa: E402
from polar_h10.clock import ClockFit                 # noqa: E402
import export as fpsci_export                        # noqa: E402
from fpsci_metrics import analyse                    # noqa: E402

DEFAULT_RAW_ROOT = os.path.join(HERE, "data", "raw")
DEFAULT_PROCESSED_ROOT = os.path.join(HERE, "data", "processed")
DEFAULT_FPSCI_RESULTS = os.path.normpath(os.path.join(HERE, "..", "FPSci-bin", "results"))

SHOT_EVENTS = ("hit", "miss", "destroy")
HIT_EVENTS = ("hit", "destroy")

#: Physiology, appended to whatever aim metrics the row already carries.
HR_COLUMNS = [
    "n_beats", "n_rejected", "hr_mean_bpm", "hr_min_bpm", "hr_max_bpm",
    "mean_rr_ms", "sdnn_ms", "rmssd_ms", "pnn50_pct",
    "hr_delta_vs_baseline_bpm", "rmssd_delta_vs_baseline_ms", "hr_coverage_pct",
]

#: Aim metrics carried into the joined tables. The full set -- all 66 per trial
#: -- lives in analysis/<scenario>/<person>/; these are the ones worth having
#: next to a heart rate.
AIM_COLUMNS = [
    "shots", "hits", "misses", "kills", "accuracy_pct",
    "reaction_time_s", "movement_time_s", "time_to_first_hit_s",
    "mean_angular_error_deg", "median_angular_error_deg",
    "time_on_target_pct", "mean_normalized_error", "path_efficiency",
    "submovement_count", "overshoot_count",
    "peak_angular_velocity_dps", "mean_angular_velocity_dps", "aim_jitter_dps2",
]

TRIAL_COLUMNS = ([
    "subject_id", "warmup", "session_id", "block_id", "task_id", "task_index",
    "trial_id", "trial_index", "start_utc", "end_utc", "start_unix", "end_unix",
    "duration_s", "pretrial_duration", "task_execution_time",
    "destroyed_targets", "total_targets",
] + AIM_COLUMNS + HR_COLUMNS)

SESSION_COLUMNS = ([
    "subject_id", "warmup", "session_id", "scenario", "start_utc", "end_utc",
    "duration_s", "n_trials", "task_time_s",
] + AIM_COLUMNS + HR_COLUMNS)

TIMELINE_COLUMNS = [
    "t_unix", "utc", "hr_bpm", "hr_source", "rr_ms", "n_beats",
    "session_id", "task_id", "trial_id", "trial_index",
    "shots", "hits", "misses",
]


# --------------------------------------------------------------------------
# Loading the Polar side
# --------------------------------------------------------------------------

def load_polar(session_dir: str, refit: bool = True) -> dict:
    manifest = fileio.read_manifest(session_dir)
    fit = ClockFit.from_dict(manifest.get("clock_fit") or {"offset_s": 0.0})

    beats = []
    if fileio.has_stream(session_dir, "rr"):
        for row in fileio.read_stream(fileio.stream_path(session_dir, "rr")):
            if row.get("t_unix") is None or not row.get("rr_ms"):
                continue
            beats.append((row["t_unix"], row["rr_ms"]))
        beats.sort()

    hr_rows = []
    if fileio.has_stream(session_dir, "hr"):
        hr_rows = [r for r in fileio.read_stream(fileio.stream_path(session_dir, "hr"))
                   if r.get("t_unix") is not None]

    # ECG/ACC carry device timestamps and can be re-derived from the final clock
    # fit; RR and HR come from the standard GATT service, which has no device
    # timestamp, so they keep their arrival-based times either way.
    refit_note = None
    if refit and fit.kind == "linear":
        refit_note = ("ECG/ACC timestamps can be re-derived with "
                      "fileio.apply_clock_fit(); drift %.1f ppm" % fit.drift_ppm)

    markers = []
    if fileio.has_stream(session_dir, "markers"):
        markers = fileio.read_stream(fileio.stream_path(session_dir, "markers"))

    return {
        "dir": session_dir,
        "manifest": manifest,
        "beats": beats,
        "hr_rows": hr_rows,
        "markers": markers,
        "clock_fit": fit,
        "refit_note": refit_note,
        "t0": manifest.get("start_unix"),
        "t1": manifest.get("end_unix"),
    }


def beats_between(beats, t0: float, t1: float):
    """RR intervals whose beat falls in [t0, t1), plus the beat times."""
    inside = [(t, rr) for t, rr in beats if t0 <= t < t1]
    return [rr for _, rr in inside], [t for t, _ in inside]


# --------------------------------------------------------------------------
# Loading the FPSci side
# --------------------------------------------------------------------------

def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open an FPSci results file without any chance of writing to it.

    The results databases are the raw record of a session, so this never opens
    them read-write -- not even to let SQLite create a journal beside them.
    """
    uri = "file:" + pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_one(db_path: str) -> dict:
    """Sessions, trials and shot events out of a single results database."""
    if not os.path.exists(db_path):
        raise SystemExit("no such FPSci database: " + db_path)
    conn = open_readonly(db_path)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute(
        "select name from sqlite_master where type='table'")}

    sessions = [dict(r) for r in conn.execute("select * from Sessions")] \
        if "Sessions" in tables else []
    trials = [dict(r) for r in conn.execute("select * from Trials")] \
        if "Trials" in tables else []

    # Only the sparse shot events are needed; the per-frame 'aim' rows are the
    # bulk of the table and would be pointless to pull into memory.
    events = []
    if "Player_Action" in tables:
        placeholders = ",".join("?" * len(SHOT_EVENTS))
        events = [dict(r) for r in conn.execute(
            "select time, event, target_id from Player_Action "
            "where event in (%s) order by time" % placeholders, SHOT_EVENTS)]

    experiment = None
    if "Experiments" in tables:
        row = conn.execute("select description, time from Experiments limit 1").fetchone()
        if row:
            experiment = dict(row)
    conn.close()

    for row in sessions + trials:
        row["db"] = os.path.basename(db_path)
    return {"sessions": sessions, "trials": trials, "events": events,
            "experiment": experiment}


def load_fpsci(db_paths) -> dict:
    """Merge one or more results databases into a single view.

    FPSci writes one database per run, so a Polar recording that spans a whole
    sitting has to be joined against several of them. Everything downstream
    works on timestamps, so merging is just concatenation plus a sort -- there
    is no need to know which run a trial came from beyond the ``db`` label.
    """
    if isinstance(db_paths, str):
        db_paths = [db_paths]
    db_paths = list(db_paths)
    if not db_paths:
        raise SystemExit("no FPSci databases to read")

    sessions, trials, events, experiment = [], [], [], None
    for path in db_paths:
        part = _load_one(path)
        sessions.extend(part["sessions"])
        trials.extend(part["trials"])
        events.extend(part["events"])
        experiment = experiment or part["experiment"]

    for row in sessions + trials:
        row["start_unix"] = _safe_time(row.get("start_time"))
        row["end_unix"] = _safe_time(row.get("end_time"))
    for row in events:
        row["t_unix"] = _safe_time(row.get("time"))

    # An FPSci session row whose end equals its start never ran a trial: the
    # menu was opened and left.  Keep them out of the aggregates.
    for row in sessions:
        row["ran"] = bool(row["start_unix"] and row["end_unix"]
                          and row["end_unix"] > row["start_unix"])

    trials = [t for t in trials if t["start_unix"]]
    trials.sort(key=lambda t: t["start_unix"])
    events = [e for e in events if e["t_unix"]]
    events.sort(key=lambda e: e["t_unix"])
    # Estimation runs after merging, so a trial with no end_time can be bounded
    # by the next trial even when that trial lives in a different database.
    _fill_missing_trial_ends(trials, sessions)

    # The canonical aim metrics. Derived from the same databases by the same
    # code the exporter runs, so these numbers cannot drift from analysis/.
    metrics = analyse(db_paths)
    for res in metrics.databases:
        res.close()

    return {"paths": db_paths, "sessions": sessions, "trials": trials,
            "events": events, "experiment": experiment, "metrics": metrics}


def _safe_time(text):
    if not text:
        return None
    try:
        return fileio.parse_utc(str(text))
    except ValueError:
        return None


def _fill_missing_trial_ends(trials, sessions) -> None:
    """A trial aborted mid-run has a NULL end_time; bound it sensibly."""
    for i, trial in enumerate(trials):
        if trial["end_unix"]:
            continue
        candidates = [t["start_unix"] for t in trials[i + 1:] if t["start_unix"]]
        ends = [s["end_unix"] for s in sessions
                if s["end_unix"] and s["end_unix"] > trial["start_unix"]]
        options = candidates[:1] + sorted(ends)[:1]
        trial["end_unix"] = min(options) if options else None
        trial["end_estimated"] = True


AIM_FROM_ENGINE = {
    "shots": "shots_fired",
    "hits": "shots_hit",
    "misses": "shots_missed",
    "kills": "kills",
}


def aim_fields(metric: dict) -> dict:
    """Pull the carried aim metrics out of an engine row, renamed for this table.

    The engine calls them shots_fired / shots_hit / shots_missed; these tables
    have always said shots / hits / misses. Only the labels differ -- the
    numbers are the engine's, computed once.
    """
    out = {}
    for name in AIM_COLUMNS:
        source = AIM_FROM_ENGINE.get(name, name)
        out[name] = metric.get(source)
    return out


# --------------------------------------------------------------------------
# Correlation
# --------------------------------------------------------------------------

def compute_baseline(polar: dict, trials, window_s: float) -> dict:
    """HR/HRV over the quiet window just before the first trial starts."""
    beats = polar["beats"]
    if not beats:
        return {"available": False}
    first_trial = min((t["start_unix"] for t in trials if t["start_unix"]),
                      default=None)
    end = first_trial if first_trial else beats[-1][0]
    start = max(end - window_s, beats[0][0])
    if end - start < 10.0:
        return {"available": False, "reason":
                "less than 10 s of recording before the first trial"}
    rr, times = beats_between(beats, start, end)
    metrics = hrv.hrv_metrics(rr)
    metrics.update({"available": metrics["n_beats"] >= 5,
                    "start_utc": fileio.utc_string(start),
                    "end_utc": fileio.utc_string(end),
                    "window_s": round(end - start, 2)})
    return metrics


def hr_fields(polar: dict, baseline: dict, t0: float, t1: float) -> dict:
    """HR and HRV over one window, plus how much of it the beats actually cover."""
    rr, beat_times = beats_between(polar["beats"], t0, t1)
    metrics = hrv.hrv_metrics(rr)
    out = {k: metrics.get(k) for k in
           ("n_beats", "n_rejected", "hr_mean_bpm", "hr_min_bpm", "hr_max_bpm",
            "mean_rr_ms", "sdnn_ms", "rmssd_ms", "pnn50_pct")}
    duration = t1 - t0
    out["hr_coverage_pct"] = (
        round(100.0 * (beat_times[-1] - beat_times[0]) / duration, 1)
        if len(beat_times) >= 2 and duration > 0 else None)
    out["hr_delta_vs_baseline_bpm"] = _delta(metrics.get("hr_mean_bpm"),
                                             baseline.get("hr_mean_bpm"))
    out["rmssd_delta_vs_baseline_ms"] = _delta(metrics.get("rmssd_ms"),
                                               baseline.get("rmssd_ms"))
    return out


def _window(metric: dict):
    t0 = _safe_time(metric.get("start_time"))
    t1 = _safe_time(metric.get("end_time"))
    return (t0, t1) if t0 and t1 and t1 > t0 else (None, None)


def trial_rows(fpsci: dict, polar: dict, baseline: dict, warmups=None):
    """One row per trial: the engine's aim metrics, plus the strap."""
    rows = []
    for metric in fpsci["metrics"].trials:
        t0, t1 = _window(metric)
        if t0 is None:
            continue
        row = {
            "subject_id": metric.get("name"),
            "warmup": _warmup_for(warmups, metric, t0),
            "session_id": metric.get("session_id"),
            "block_id": metric.get("block_id"),
            "task_id": metric.get("task_id"),
            "task_index": metric.get("task_index"),
            "trial_id": metric.get("trial_id"),
            "trial_index": metric.get("trial_index"),
            "start_utc": metric.get("start_time"),
            "end_utc": metric.get("end_time"),
            "start_unix": "%.6f" % t0,
            "end_unix": "%.6f" % t1,
            "duration_s": round(metric.get("wall_duration_s") or (t1 - t0), 3),
            "pretrial_duration": metric.get("pretrial_duration_s"),
            "task_execution_time": metric.get("task_execution_time_s"),
            "destroyed_targets": metric.get("destroyed_targets"),
            "total_targets": metric.get("total_targets"),
        }
        row.update(aim_fields(metric))
        row.update(hr_fields(polar, baseline, t0, t1))
        rows.append(row)
    return rows


def _warmup_for(warmups, metric, t0):
    """The condition in force, from the launcher manifest or the database."""
    if warmups:
        label = fpsci_export.warmup_for(warmups, metric.get("name"), t0)
        if label:
            return label
    return metric.get("warmup") or ""


def session_rows(fpsci: dict, polar: dict, baseline: dict, warmups=None):
    """One row per run -- the analysis unit, aim metrics and physiology together.

    With one database per run a session and a run are the same thing, so this
    is the engine's per-run rollup with the strap attached over the same
    window. Rates here are recomputed by the engine from run totals rather than
    averaged across trials, so a short trial cannot skew them.
    """
    rows = []
    for metric in fpsci["metrics"].runs:
        t0, t1 = _window(metric)
        if t0 is None:
            continue
        row = {
            "subject_id": metric.get("name"),
            "warmup": _warmup_for(warmups, metric, t0),
            "session_id": metric.get("session_id"),
            "scenario": metric.get("scenario"),
            "start_utc": metric.get("start_time"),
            "end_utc": metric.get("end_time"),
            "duration_s": round(t1 - t0, 3),
            "n_trials": metric.get("n_trials"),
            "task_time_s": metric.get("task_time_s"),
        }
        row.update(aim_fields(metric))
        row.update(hr_fields(polar, baseline, t0, t1))
        rows.append(row)
    return rows


def timeline_rows(fpsci: dict, polar: dict, bin_s: float, max_gap_s: float = 10.0):
    """Fixed-rate merged timeline over the span the Polar recording covers.

    Everything is bucketed in a single pass per source rather than rescanning
    the beat list for each bin -- an hour of ECG-rate recording is a few
    thousand beats and a few thousand bins, and the naive form is quadratic.
    """
    beats = polar["beats"]
    if not beats:
        return []
    start = polar["t0"] or beats[0][0]
    end = polar["t1"] or beats[-1][0]
    n_bins = max(int((end - start) / bin_s) + 1, 1)

    hr_sum = [0.0] * n_bins
    hr_n = [0] * n_bins
    rr_sum = [0.0] * n_bins
    for t, rr in beats:
        i = int((t - start) / bin_s)
        if 0 <= i < n_bins and rr:
            hr_sum[i] += 60000.0 / rr
            rr_sum[i] += rr
            hr_n[i] += 1

    shots = [0] * n_bins
    hits = [0] * n_bins
    misses = [0] * n_bins
    for event in fpsci["events"]:
        i = int((event["t_unix"] - start) / bin_s)
        if 0 <= i < n_bins:
            shots[i] += 1
            if event["event"] in HIT_EVENTS:
                hits[i] += 1
            elif event["event"] == "miss":
                misses[i] += 1

    trial_of = _label_bins([t for t in fpsci["trials"]
                            if t["start_unix"] and t["end_unix"]],
                           start, bin_s, n_bins)
    session_of = _label_bins([s for s in fpsci["sessions"] if s.get("ran")],
                             start, bin_s, n_bins)

    ihr = [(t, 60000.0 / rr) for t, rr in beats if rr]
    rows = []
    cursor = 0
    for i in range(n_bins):
        t0 = start + i * bin_s
        while cursor < len(ihr) and ihr[cursor][0] <= t0:
            cursor += 1

        if hr_n[i]:
            hr_value = round(hr_sum[i] / hr_n[i], 2)
            source = "beats"
        else:
            hr_value, source = _interpolate_hr(ihr, cursor, t0, max_gap_s)

        trial = trial_of[i]
        session = session_of[i]
        rows.append({
            "t_unix": "%.3f" % t0,
            "utc": fileio.utc_string(t0),
            "hr_bpm": hr_value,
            "hr_source": source,
            "rr_ms": round(rr_sum[i] / hr_n[i], 2) if hr_n[i] else None,
            "n_beats": hr_n[i],
            "session_id": (trial or session or {}).get("session_id") or "",
            "task_id": (trial or {}).get("task_id") or "",
            "trial_id": (trial or {}).get("trial_id") or "",
            "trial_index": (trial or {}).get("trial_index"),
            "shots": shots[i],
            "hits": hits[i],
            "misses": misses[i],
        })
    return rows


def _label_bins(rows, start: float, bin_s: float, n_bins: int):
    """Map each bin to the row (trial or session) whose window contains it."""
    labels = [None] * n_bins
    for row in rows:
        first = math.ceil((row["start_unix"] - start) / bin_s)
        last = math.ceil((row["end_unix"] - start) / bin_s) - 1
        for i in range(max(first, 0), min(last, n_bins - 1) + 1):
            labels[i] = row
    return labels


def _interpolate_hr(ihr, cursor: int, t: float, max_gap_s: float):
    """Linear interpolation between the beats bracketing ``t``, if they are close.

    ``cursor`` is the index of the first beat after ``t``, maintained by the
    caller as it sweeps forward.
    """
    if cursor == 0 or cursor >= len(ihr):
        return None, ""
    before, after = ihr[cursor - 1], ihr[cursor]
    span = after[0] - before[0]
    if span > max_gap_s:
        return None, ""
    weight = 0.0 if span <= 0 else (t - before[0]) / span
    return round(before[1] + weight * (after[1] - before[1]), 2), "interp"


def _delta(value, baseline_value):
    if value is None or baseline_value is None:
        return None
    return round(value - baseline_value, 2)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_rows(path: str, columns, rows) -> str:
    with fileio.CsvStream(path, columns, flush_interval=0.0) as stream:
        for row in rows:
            stream.write(*[_blank(row.get(c)) for c in columns])
    return path


def _blank(value):
    return "" if value is None else value


def check_overlap(polar: dict, fpsci: dict) -> dict:
    p0, p1 = polar["t0"], polar["t1"]
    game_times = [t["start_unix"] for t in fpsci["trials"] if t["start_unix"]]
    game_times += [t["end_unix"] for t in fpsci["trials"] if t["end_unix"]]
    game_times += [s["start_unix"] for s in fpsci["sessions"] if s.get("ran")]
    if not game_times or p0 is None:
        return {"overlap_s": 0.0, "ok": False,
                "reason": "one side has no usable timestamps"}
    g0, g1 = min(game_times), max(game_times)
    overlap = min(p1, g1) - max(p0, g0)
    return {
        "polar_start_utc": fileio.utc_string(p0),
        "polar_end_utc": fileio.utc_string(p1),
        "fpsci_start_utc": fileio.utc_string(g0),
        "fpsci_end_utc": fileio.utc_string(g1),
        "overlap_s": round(max(overlap, 0.0), 2),
        "ok": overlap > 0,
    }


def run(session_dir: str, db_path, out_dir: str, baseline_s: float,
        bin_s: float, refit: bool, results_dir: str = None) -> int:
    polar = load_polar(session_dir, refit=refit)

    print("Polar recording : %s" % session_dir)
    print("                  %s -> %s  (%d beats)"
          % (polar["manifest"].get("start_utc"), polar["manifest"].get("end_utc"),
             len(polar["beats"])))

    # With one database per run, the databases to join are whichever ones hold
    # gameplay inside this recording -- not simply the most recent file.
    if db_path:
        db_paths = [db_path] if isinstance(db_path, str) else list(db_path)
    else:
        t0, t1 = polar["t0"], polar["t1"]
        if t0 is None or t1 is None:
            # No manifest window (an interrupted recording): fall back to the
            # beats, which are (t_unix, rr_ms) tuples sorted by time.
            beats = polar["beats"]
            t0 = t0 if t0 is not None else (beats[0][0] if beats else 0.0)
            t1 = t1 if t1 is not None else (beats[-1][0] if beats else float("inf"))
        db_paths = dbs_for_window(results_dir, t0, t1)
        if not db_paths:
            raise SystemExit(
                "no FPSci database under %s holds a completed run inside this "
                "recording. Play a run, or pass --fpsci-db explicitly."
                % results_dir)

    fpsci = load_fpsci(db_paths)

    if len(db_paths) == 1:
        print("FPSci results   : %s" % db_paths[0])
    else:
        print("FPSci results   : %d databases" % len(db_paths))
        for path in db_paths:
            print("                  %s" % os.path.basename(path))
    print("                  %d trials, %d shot events"
          % (len(fpsci["trials"]), len(fpsci["events"])))

    overlap = check_overlap(polar, fpsci)
    if not overlap["ok"]:
        print("")
        print("!! These two recordings do not overlap in time.")
        for key in ("polar_start_utc", "polar_end_utc",
                    "fpsci_start_utc", "fpsci_end_utc"):
            if key in overlap:
                print("     %-18s %s" % (key, overlap[key]))
        print("   Both are UTC.  If they look a whole number of hours apart, one")
        print("   of them was written by something that used local time.")
        return 2
    print("Overlap         : %.1f s" % overlap["overlap_s"])

    warmups = fpsci_export.load_sittings(fpsci_export.ANALYSIS)

    baseline = compute_baseline(polar, fpsci["trials"], baseline_s)
    if baseline.get("available"):
        print("Baseline        : %.1f bpm over %.0f s before the first trial"
              % (baseline["hr_mean_bpm"], baseline["window_s"]))
    else:
        print("Baseline        : not available (%s)"
              % baseline.get("reason", "too few beats before the first trial"))

    os.makedirs(out_dir, exist_ok=True)
    trials = trial_rows(fpsci, polar, baseline, warmups)
    sessions = session_rows(fpsci, polar, baseline, warmups)
    timeline = timeline_rows(fpsci, polar, bin_s)

    written = {
        "trials_hr.csv": write_rows(os.path.join(out_dir, "trials_hr.csv"),
                                    TRIAL_COLUMNS, trials),
        "sessions_hr.csv": write_rows(os.path.join(out_dir, "sessions_hr.csv"),
                                      SESSION_COLUMNS, sessions),
        "timeline.csv": write_rows(os.path.join(out_dir, "timeline.csv"),
                                   TIMELINE_COLUMNS, timeline),
    }

    summary = {
        "polar_session": os.path.basename(session_dir),
        "polar_session_dir": os.path.abspath(session_dir),
        "fpsci_dbs": [os.path.abspath(p) for p in db_paths],
        "generated_utc": fileio.utc_string(fileio.now_unix()),
        "overlap": overlap,
        "baseline": baseline,
        "timeline_bin_s": bin_s,
        "counts": {"trials": len(trials), "sessions": len(sessions),
                   "timeline_bins": len(timeline), "beats": len(polar["beats"])},
        "clock_fit": polar["clock_fit"].to_dict(),
        "outputs": {k: os.path.abspath(v) for k, v in written.items()},
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")

    _print_table(trials)
    print("")
    print("Written to %s" % out_dir)
    for name in written:
        print("  " + name)
    print("  summary.json")
    return 0


def _print_table(trials) -> None:
    if not trials:
        print("\nNo completed trials fell inside the recording.")
        return
    print("")
    header = ("%-14s %-16s %5s %6s %7s %7s %8s %8s"
              % ("session", "trial", "idx", "dur s", "acc %", "HR bpm", "dHR", "RMSSD"))
    print(header)
    print("-" * len(header))
    for row in trials:
        print("%-14s %-16s %5s %6s %7s %7s %8s %8s" % (
            _short(row["session_id"], 14), _short(row["trial_id"], 16),
            _fmt(row["trial_index"], "%d"), _fmt(row["duration_s"], "%.1f"),
            _fmt(row["accuracy_pct"], "%.1f"), _fmt(row["hr_mean_bpm"], "%.1f"),
            _fmt(row["hr_delta_vs_baseline_bpm"], "%+.1f"),
            _fmt(row["rmssd_ms"], "%.1f")))


def _short(text, width):
    text = "" if text is None else str(text)
    return text if len(text) <= width else text[:width - 1] + "~"


def _fmt(value, spec):
    return "-" if value is None else spec % value


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def db_span(db_path: str):
    """(first trial start, last trial end) in unix seconds, or None."""
    try:
        conn = open_readonly(db_path)
        row = conn.execute(
            "select min(start_time), max(end_time) from Trials").fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    start, end = _safe_time(row[0]), _safe_time(row[1])
    return (start, end or start) if start else None


def dbs_for_window(results_dir: str, t0: float, t1: float):
    """Every results database holding gameplay inside a recording window.

    This replaces picking the single newest file. Since FPSci moved to one
    database per run, a sitting of four scenarios is four databases, and taking
    only the newest would silently join against the last run alone.

    Databases with no completed trial -- the stubs left behind by browsing the
    menu -- have no span and drop out here without needing a special case.
    """
    picked = []
    for path in sorted(glob.glob(os.path.join(results_dir, "*.db"))):
        span = db_span(path)
        if not span:
            continue
        start, end = span
        if end >= t0 and start <= t1:
            picked.append(path)
    return picked


def list_available(raw_root: str, results_dir: str) -> int:
    print("Polar recordings under %s:" % raw_root)
    found = False
    if os.path.isdir(raw_root):
        for name in sorted(os.listdir(raw_root)):
            manifest_path = os.path.join(raw_root, name, fileio.MANIFEST_NAME)
            if not os.path.exists(manifest_path):
                continue
            found = True
            manifest = fileio.read_manifest(os.path.join(raw_root, name))
            print("  %-32s %6.1f s  %5d beats  %s"
                  % (name, manifest.get("duration_s") or 0.0,
                     (manifest.get("counts") or {}).get("rr", 0),
                     manifest.get("subject") or ""))
    if not found:
        print("  (none yet -- run record.py first)")

    print("")
    print("FPSci result databases under %s:" % results_dir)
    dbs = sorted(glob.glob(os.path.join(results_dir, "*.db")), key=os.path.getmtime)
    if not dbs:
        print("  (none)")
    for path in dbs:
        try:
            data = load_fpsci(path)
            span = ""
            starts = [t["start_unix"] for t in data["trials"] if t["start_unix"]]
            if starts:
                span = fileio.utc_string(min(starts))
            print("  %-44s %3d trials  %s"
                  % (os.path.basename(path), len(data["trials"]), span))
        except sqlite3.Error as exc:
            print("  %-44s unreadable (%s)" % (os.path.basename(path), exc))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=None,
                    help="Polar recording directory (default: the newest one)")
    ap.add_argument("--raw-root", default=DEFAULT_RAW_ROOT,
                    help="where recordings live (default: %(default)s)")
    ap.add_argument("--fpsci-db", default=None, action="append",
                    help="a specific FPSci results .db; repeatable. "
                         "Default: every database overlapping the recording")
    ap.add_argument("--fpsci-results", default=DEFAULT_FPSCI_RESULTS,
                    help="FPSci results directory (default: %(default)s)")
    ap.add_argument("--out", default=None,
                    help="output directory (default: data/processed/<recording id>)")
    ap.add_argument("--baseline", type=float, default=120.0, metavar="SECONDS",
                    help="resting window before the first trial (default: %(default)s)")
    ap.add_argument("--bin", type=float, default=1.0, metavar="SECONDS",
                    dest="bin_s", help="timeline resolution (default: %(default)s)")
    ap.add_argument("--no-refit", action="store_true",
                    help="do not re-derive ECG/ACC times from the fitted clock model")
    ap.add_argument("--list", action="store_true",
                    help="show the recordings and databases available, then exit")
    args = ap.parse_args(argv)

    if args.list:
        return list_available(args.raw_root, args.fpsci_results)

    session_dir = args.session or fileio.find_latest_session(args.raw_root)
    out_dir = args.out or os.path.join(DEFAULT_PROCESSED_ROOT,
                                       os.path.basename(os.path.normpath(session_dir)))
    return run(session_dir, args.fpsci_db, out_dir, args.baseline, args.bin_s,
               refit=not args.no_refit, results_dir=args.fpsci_results)


if __name__ == "__main__":
    raise SystemExit(main())
