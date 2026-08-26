"""Rolling per-trial metrics up to one row per run, and one row per session.

The bodies below are copied verbatim from FPSci/scripts/analysis/fpsci_parse.py
(lines 858-1054). Only the imports moved. The golden test diffs both copies.
"""

from __future__ import annotations

import statistics

from .config import STUTTER_FACTOR
from .geometry import clean_param, pct, ratio, safe


AGG_FIELDS = [
    "accuracy_pct", "reaction_time_s", "movement_time_s", "time_to_first_hit_s",
    "time_to_first_kill_s", "time_to_enter_target_s", "kills_per_second",
    "shots_per_second", "shots_per_kill", "mean_angular_error_deg",
    "median_angular_error_deg", "p95_angular_error_deg", "angular_error_sd_deg",
    "mean_normalized_error", "time_on_target_pct", "initial_angular_error_deg",
    "path_efficiency", "submovement_count", "overshoot_count",
    "peak_angular_velocity_dps", "mean_angular_velocity_dps", "aim_jitter_dps2",
    "path_length_deg", "task_execution_time_s", "mean_inter_kill_s",
]


# --------------------------------------------------------------------------
# Per-run rollup: exactly one row per run, for every scenario
# --------------------------------------------------------------------------

# (output column, source trial metric, statistic)
#
# The statistic is chosen per metric rather than emitting mean/median/sd for
# everything. Latencies are right-skewed and a single slow trial drags the mean,
# so those use the median. Per-frame quantities (error, velocity, time on
# target) are already averaged over thousands of samples inside each trial and
# are well behaved, so those use the mean. Counts are summed, peaks take the max.
RUN_SPEC = [
    ("shots_fired",              "shots_fired",               "sum"),
    ("shots_hit",                "shots_hit",                 "sum"),
    ("kills",                    "kills",                     "sum"),
    ("task_time_s",              "task_execution_time_s",     "sum"),

    ("reaction_time_s",          "reaction_time_s",           "median"),
    ("reaction_time_sd_s",       "reaction_time_s",           "sd"),
    ("movement_time_s",          "movement_time_s",           "median"),
    ("time_to_first_hit_s",      "time_to_first_hit_s",       "median"),
    ("time_to_enter_target_s",   "time_to_enter_target_s",    "median"),
    ("inter_kill_s",             "mean_inter_kill_s",         "median"),

    ("angular_error_deg",        "mean_angular_error_deg",    "mean"),
    ("angular_error_p95_deg",    "p95_angular_error_deg",     "mean"),
    ("angular_error_sd_deg",     "angular_error_sd_deg",      "mean"),
    ("normalized_error",         "mean_normalized_error",     "mean"),
    ("initial_angular_error_deg","initial_angular_error_deg", "median"),
    ("time_on_target_pct",       "time_on_target_pct",        "mean"),

    ("path_efficiency",          "path_efficiency",           "mean"),
    ("path_length_deg",          "path_length_deg",           "mean"),
    ("submovement_count",        "submovement_count",         "mean"),
    ("overshoot_count",          "overshoot_count",           "mean"),

    ("peak_angular_velocity_dps","peak_angular_velocity_dps", "max"),
    ("angular_velocity_dps",     "mean_angular_velocity_dps", "mean"),
    ("aim_jitter_dps2",          "aim_jitter_dps2",           "mean"),
]

_RUN_STATS = {
    "sum":    lambda v: sum(v),
    "mean":   lambda v: safe(statistics.fmean, v),
    "median": lambda v: safe(statistics.median, v),
    "sd":     lambda v: safe(statistics.pstdev, v) if len(v) > 1 else None,
    "max":    lambda v: max(v),
}


def build_runs(all_trials):
    """Collapse every run to a single row, whatever its trial structure.

    gridshot, microadjust and tracking are one 60 s trial per run, so this is a
    straight copy. flicks is 25 pair-trials per run, so those 25 rows become one
    summarised row here -- which is the point: every scenario ends up directly
    comparable, one row per run.

    Runs with no completed trials are absent by construction, so this file has
    none of the empty rows that sessions.csv keeps for auditing.
    """
    groups = {}
    order = []
    for m in all_trials:
        key = (m.get("db"), m.get("subject_id"), m.get("session_number"), m.get("session_id"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    out = []
    for key in order:
        ms = groups[key]
        first = ms[0]
        row = {
            "subject_id": first.get("subject_id"),
            "name": first.get("name"),
            "warmup": first.get("warmup"),
            "scenario": first.get("scenario"),
            "session_number": first.get("session_number"),
            "session_id": first.get("session_id"),
            "db": first.get("db"),
            "n_trials": len(ms),
            "start_time": min((m.get("start_time") for m in ms if m.get("start_time")), default=None),
            "end_time": max((m.get("end_time") for m in ms if m.get("end_time")), default=None),
        }
        for out_name, src, stat in RUN_SPEC:
            vals = [m[src] for m in ms if isinstance(m.get(src), (int, float))]
            row[out_name] = _RUN_STATS[stat](vals) if vals else None

        # Rates recomputed from the run totals rather than averaged per trial,
        # so they are not distorted by short trials counting as much as long ones.
        row["accuracy_pct"] = (ratio(row["shots_hit"], row["shots_fired"]) or 0) * 100             if row["shots_fired"] else None
        row["kills_per_second"] = ratio(row["kills"], row["task_time_s"])
        row["shots_per_second"] = ratio(row["shots_fired"], row["task_time_s"])
        row["shots_per_kill"] = ratio(row["shots_fired"], row["kills"])
        out.append(row)
    return out


def aggregate_sessions(res, trial_metrics, insts):
    """One row per session run, carrying every Sessions and Users column.

    Sessions with no completed trials are still emitted (with ``n_trials`` 0)
    so an aborted run is visible in the output rather than silently missing.
    """
    by_key = {}
    for m in trial_metrics:
        by_key.setdefault((m.get("subject_id"), m.get("session_number"),
                           m.get("session_id")), []).append(m)

    # Latest Users row per session wins, but prefer the "start" snapshot.
    user_meta = {}
    for u in res.users:
        k = u.get("session_id")
        if u.get("time") == "start" or k not in user_meta:
            user_meta[k] = u

    out = []
    for inst in [i for i in insts if i.db == res.name]:
        key = (inst.code, inst.number, inst.session_id)
        ms = by_key.get(key, [])
        row = {
            "subject_id": inst.code,
            "name": inst.subject_id,
            "warmup": inst.warmup,
            "scenario": inst.session_id,
            "session_number": inst.number,
            "session_id": inst.session_id,
            "db": res.name,
            "n_trials": len(ms),
            "total_shots": sum(m.get("shots_fired") or 0 for m in ms),
            "total_hits": sum(m.get("shots_hit") or 0 for m in ms),
            "total_kills": sum(m.get("kills") or 0 for m in ms),
            "total_task_time_s": sum(m.get("task_execution_time_s") or 0 for m in ms),
        }
        row["overall_accuracy_pct"] = (
            ratio(row["total_hits"], row["total_shots"]) or 0) * 100 if row["total_shots"] else None
        row["overall_kills_per_second"] = ratio(row["total_kills"], row["total_task_time_s"])
        row["continuous_weapon"] = max((m.get("continuous_weapon") or 0) for m in ms) if ms else 0

        # every Sessions column, including whatever sessionParametersToLog added
        for k, v in inst.row.items():
            if k not in row:
                row[k] = clean_param(v)

        # every Users column, prefixed so it cannot collide with a session field
        um = user_meta.get(inst.session_id, {})
        for k, v in um.items():
            if k in ("subject_id", "session_id", "time"):
                continue
            row[f"user_{k}" if k in row else k] = v

        # mean / sd / median for every per-trial metric worth summarising
        for f in AGG_FIELDS:
            vals = [m[f] for m in ms if isinstance(m.get(f), (int, float))]
            if not vals:
                continue
            row[f"{f}__mean"] = safe(statistics.fmean, vals)
            row[f"{f}__median"] = safe(statistics.median, vals)
            if len(vals) > 1:
                row[f"{f}__sd"] = safe(statistics.pstdev, vals)
                mu = row[f"{f}__mean"]
                # Coefficient of variation = consistency, the thing aim
                # training actually cares about run to run.
                if mu:
                    row[f"{f}__cv"] = row[f"{f}__sd"] / abs(mu)

        # frame timing over the session window
        st, et = inst.t_start, inst.t_end
        sdts = [
            sdt for t, sdt in res.frames
            if sdt and (st is None or t >= st) and (et is None or t <= et)
        ]
        if sdts:
            med = statistics.median(sdts)
            row["frame_time_mean_ms"] = statistics.fmean(sdts) * 1000
            row["frame_time_median_ms"] = med * 1000
            row["frame_time_p99_ms"] = (pct(sdts, 99) or 0) * 1000
            row["mean_fps"] = 1.0 / statistics.fmean(sdts) if statistics.fmean(sdts) else None
            row["p1_low_fps"] = 1.0 / pct(sdts, 99) if pct(sdts, 99) else None
            row["stutter_count"] = sum(1 for s in sdts if s > STUTTER_FACTOR * med)
            row["n_frames"] = len(sdts)

        out.append(row)
    return out
