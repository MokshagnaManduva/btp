"""Per-trial analysis: the full metric set for one trial.

The body of ``analyse_trial`` below is copied verbatim from
FPSci/scripts/analysis/fpsci_parse.py (lines 514-855). Only the imports moved.
The golden test in app/tests/test_golden.py re-derives every number with both
copies and diffs them, so any accidental edit here shows up immediately.
"""

from __future__ import annotations

import math
import statistics

from .config import HIT_EVENTS, ONSET_SPEED_DPS, ONSET_SUSTAIN_S, SHOT_EVENTS
from .geometry import (angsep_deg, count_submovements, pct, ratio, safe,
                       vec_to_azel, velocity_series)
from .geometry import parse_time
from .sessions import resolve_session


def analyse_trial(res, trial, shot_rows, target_rows, insts):
    """Compute the full metric set for one trial.

    Returns a dict of trial-level metrics; appends per-shot rows to
    ``shot_rows`` and per-target-instance rows to ``target_rows``.
    """
    sid = trial.get("session_id")
    t_start = parse_time(trial.get("start_time"))
    t_end = parse_time(trial.get("end_time"))
    if t_start is None or t_end is None:
        return None

    inst = resolve_session(insts, res.name, sid, t_start)
    subject = inst.subject_id if inst else "unknown"
    sess_no = inst.number if inst else None

    m = {
        # Leading identity block, in the order the CSVs are meant to read:
        # row_id (added by write_csv) - subject_id - name - warmup - scenario.
        "subject_id": inst.code if inst else "",
        "name": subject,
        "warmup": inst.warmup if inst else "",
        "scenario": sid,
        "session_number": sess_no,
        "session_id": sid,
        "db": res.name,
        "trial_id": trial.get("trial_id"),
        "trial_index": trial.get("trial_index"),
        "task_id": trial.get("task_id"),
        "task_index": trial.get("task_index"),
        "block_id": trial.get("block_id"),
        "start_time": trial.get("start_time"),
        "end_time": trial.get("end_time"),
        "wall_duration_s": t_end - t_start,
        "pretrial_duration_s": trial.get("pretrial_duration"),
        "task_execution_time_s": trial.get("task_execution_time"),
        "destroyed_targets": trial.get("destroyed_targets"),
        "total_targets": trial.get("total_targets"),
    }
    tot = trial.get("total_targets")
    dest = trial.get("destroyed_targets")
    m["trial_success"] = (
        int(dest >= tot) if isinstance(tot, int) and isinstance(dest, int) and tot > 0 else None
    )

    acts = res.actions_between(t_start, t_end)
    task = [a for a in acts if a.get("state") == "trialTask"]
    if not task:
        m["n_task_frames"] = 0
        return m

    t0 = task[0]["_t"]
    m["n_task_frames"] = len(task)

    # ---------------- aim series + per-frame target geometry ----------------
    times, azs, els, errs, radii, nearest = [], [], [], [], [], []
    radius_by_target = {}
    dists = []
    for a in task:
        az, el = a.get("position_az"), a.get("position_el")
        if az is None or el is None:
            continue
        cam = (a.get("position_x"), a.get("position_y"), a.get("position_z"))
        t = a["_t"]

        best_err, best_id, best_r, best_d = None, None, None, None
        if None not in cam:
            for tid, tr in res.tracks.items():
                p = tr.pos_at(t)
                if p is None or None in p:
                    continue
                dx, dy, dz = p[0] - cam[0], p[1] - cam[1], p[2] - cam[2]
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                taz, tel = vec_to_azel(dx, dy, dz)
                e = angsep_deg(az, el, taz, tel)
                r = res.angular_radius(res.target_size_m(tid), dist)
                if r is not None:
                    radius_by_target.setdefault(tid, []).append(r)
                if best_err is None or e < best_err:
                    best_err, best_id, best_r, best_d = e, tid, r, dist

        times.append(t - t0)
        azs.append(az)
        els.append(el)
        errs.append(best_err)
        radii.append(best_r)
        nearest.append(best_id)
        if best_d is not None:
            dists.append(best_d)

    if dists:
        m["mean_target_distance_m"] = safe(statistics.fmean, dists)
    if radii:
        rr = [r for r in radii if r is not None]
        if rr:
            m["mean_target_angular_radius_deg"] = safe(statistics.fmean, rr)

    # ---------------- angular velocity ----------------
    speed_times, speeds = velocity_series(times, azs, els)

    m["path_length_deg"] = sum(
        angsep_deg(azs[i - 1], els[i - 1], azs[i], els[i]) for i in range(1, len(azs))
    ) if len(azs) > 1 else 0.0
    m["peak_angular_velocity_dps"] = max(speeds) if speeds else None
    m["mean_angular_velocity_dps"] = safe(statistics.fmean, speeds) if speeds else None
    m["median_angular_velocity_dps"] = safe(statistics.median, speeds) if speeds else None
    if speeds:
        m["time_to_peak_velocity_s"] = speed_times[speeds.index(max(speeds))]

    # Angular acceleration -> a jitter/smoothness proxy.
    accels = []
    for i in range(1, len(speeds)):
        dt = speed_times[i] - speed_times[i - 1]
        if dt > 0:
            accels.append((speeds[i] - speeds[i - 1]) / dt)
    m["aim_jitter_dps2"] = safe(statistics.pstdev, accels) if len(accels) > 1 else None

    # ---------------- reaction time / movement onset ----------------
    onset_t = None
    run_start = None
    for t, s in zip(speed_times, speeds):
        if s >= ONSET_SPEED_DPS:
            if run_start is None:
                run_start = t
            elif t - run_start >= ONSET_SUSTAIN_S:
                onset_t = run_start
                break
        else:
            run_start = None
    if onset_t is None and run_start is not None:
        onset_t = run_start
    m["reaction_time_s"] = onset_t

    # ---------------- shots / events ----------------
    ev = [a for a in task if a.get("event") in SHOT_EVENTS]
    cooldowns = [a for a in task if a.get("event") == "fireCooldown"]
    hits = [a for a in ev if a.get("event") in HIT_EVENTS]
    kills = [a for a in task if a.get("event") == "destroy"]
    misses = [a for a in ev if a.get("event") == "miss"]

    m["shots_fired"] = len(ev)
    m["shots_hit"] = len(hits)
    m["shots_missed"] = len(misses)
    m["kills"] = len(kills)
    m["fire_cooldown_count"] = len(cooldowns)
    m["accuracy_pct"] = (ratio(len(hits), len(ev)) or 0) * 100 if ev else None
    m["shots_per_kill"] = ratio(len(ev), len(kills))

    # In laser mode (firePeriod 0 + autoFire) a hit is emitted every frame the
    # beam is on a target, so "shots" arrive at the frame rate. Detect that by
    # comparing shot spacing against frame spacing, not by shots-per-frame: a
    # player tracking below 50% of the time would defeat a simple ratio test
    # and have their run misread as click accuracy.
    cont = 0
    if len(ev) >= 3 and not misses:
        isi = [b["_t"] - a["_t"] for a, b in zip(ev, ev[1:])]
        fdt = [d for d in (times[i] - times[i - 1] for i in range(1, len(times))) if d > 0]
        if isi and fdt:
            med_fdt = statistics.median(fdt)
            if med_fdt > 0 and statistics.median(isi) <= 3 * med_fdt:
                cont = 1
    m["continuous_weapon"] = cont

    if ev:
        first = ev[0]
        m["first_shot_time_s"] = first["_t"] - t0
        m["first_shot_hit"] = int(first.get("event") in HIT_EVENTS)
    if hits:
        m["time_to_first_hit_s"] = hits[0]["_t"] - t0
    if kills:
        m["time_to_first_kill_s"] = kills[0]["_t"] - t0
        ktimes = [k["_t"] for k in kills]
        gaps = [b - a for a, b in zip(ktimes, ktimes[1:])]
        m["mean_inter_kill_s"] = safe(statistics.fmean, gaps) if gaps else None
        m["median_inter_kill_s"] = safe(statistics.median, gaps) if gaps else None
        m["inter_kill_sd_s"] = safe(statistics.pstdev, gaps) if len(gaps) > 1 else None
    tet = trial.get("task_execution_time")
    if isinstance(tet, (int, float)) and tet > 0:
        m["kills_per_second"] = len(kills) / tet
        m["shots_per_second"] = len(ev) / tet

    # Movement time = onset of the aim movement -> the shot that landed.
    # Only meaningful when the onset precedes the hit; in a continuous scenario
    # like gridshot the player is already moving when the trial starts, so an
    # onset detected after the first kill is not a movement time at all.
    if onset_t is not None and m.get("time_to_first_hit_s") is not None:
        mt = m["time_to_first_hit_s"] - onset_t
        m["movement_time_s"] = mt if mt >= 0 else None

    # ---------------- error / precision ----------------
    valid = [e for e in errs if e is not None]
    if valid:
        m["mean_angular_error_deg"] = safe(statistics.fmean, valid)
        m["median_angular_error_deg"] = safe(statistics.median, valid)
        m["min_angular_error_deg"] = min(valid)
        m["p95_angular_error_deg"] = pct(valid, 95)
        m["angular_error_sd_deg"] = safe(statistics.pstdev, valid) if len(valid) > 1 else None
        m["initial_angular_error_deg"] = errs[0]

    # Geometric time-on-target: fraction of frames the crosshair was inside the
    # target. This is the real tracking score, and it is independent of the
    # hit-event stream.
    on = [1 for e, r in zip(errs, radii) if e is not None and r is not None and e <= r]
    denom = sum(1 for e, r in zip(errs, radii) if e is not None and r is not None)
    m["time_on_target_pct"] = (len(on) / denom * 100) if denom else None
    m["on_target_frames"] = len(on)
    m["scored_frames"] = denom
    if m.get("task_execution_time_s") and m.get("time_on_target_pct") is not None:
        m["time_on_target_s"] = m["task_execution_time_s"] * m["time_on_target_pct"] / 100.0

    # Normalized error: 1.0 == exactly on the target's edge, so it is
    # comparable across scenarios with different target sizes.
    norm = [e / r for e, r in zip(errs, radii) if e is not None and r]
    if norm:
        m["mean_normalized_error"] = safe(statistics.fmean, norm)
        m["median_normalized_error"] = safe(statistics.median, norm)

    # ---------------- approach phase (spawn -> first hit) ----------------
    if m.get("time_to_first_hit_s") is not None:
        cut = m["time_to_first_hit_s"]
        idx = [i for i, t in enumerate(times) if t <= cut]
        if len(idx) > 1:
            a_az = [azs[i] for i in idx]
            a_el = [els[i] for i in idx]
            approach_path = sum(
                angsep_deg(a_az[i - 1], a_el[i - 1], a_az[i], a_el[i])
                for i in range(1, len(a_az))
            )
            m["approach_path_length_deg"] = approach_path
            # 1.0 = a perfectly straight flick; lower = wandering / correcting.
            if errs[0] is not None and approach_path > 0:
                m["path_efficiency"] = min(1.0, errs[0] / approach_path)

            a_speeds = [s for t, s in zip(speed_times, speeds) if t <= cut]
            m["submovement_count"] = count_submovements(a_speeds)

            # Overshoot: crosshair was inside the target, then left it again
            # before the kill landed.
            over = 0
            inside_before = None
            for i in idx:
                e, r = errs[i], radii[i]
                if e is None or r is None:
                    continue
                inside = e <= r
                if inside_before is True and not inside:
                    over += 1
                inside_before = inside
            m["overshoot_count"] = over

            first_in = next(
                (times[i] for i in idx
                 if errs[i] is not None and radii[i] is not None and errs[i] <= radii[i]),
                None,
            )
            m["time_to_enter_target_s"] = first_in

    # ---------------- per-shot rows ----------------
    for n, a in enumerate(ev):
        t = a["_t"]
        az, el = a.get("position_az"), a.get("position_el")
        cam = (a.get("position_x"), a.get("position_y"), a.get("position_z"))
        err = rad = tid = dist = None
        if az is not None and el is not None and None not in cam:
            for cand, tr in res.tracks.items():
                p = tr.pos_at(t)
                if p is None or None in p:
                    continue
                dx, dy, dz = p[0] - cam[0], p[1] - cam[1], p[2] - cam[2]
                d = math.sqrt(dx * dx + dy * dy + dz * dz)
                taz, tel = vec_to_azel(dx, dy, dz)
                e = angsep_deg(az, el, taz, tel)
                if err is None or e < err:
                    err, tid, dist = e, cand, d
                    rad = res.angular_radius(res.target_size_m(cand), d)
        shot_rows.append({
            "subject_id": inst.code if inst else "",
            "name": subject,
            "warmup": inst.warmup if inst else "",
            "scenario": sid,
            "session_number": sess_no,
            "session_id": sid,
            "db": res.name,
            "trial_id": trial.get("trial_id"),
            "trial_index": trial.get("trial_index"),
            "shot_index": n,
            "t_rel_s": t - t0,
            "event": a.get("event"),
            "is_hit": int(a.get("event") in HIT_EVENTS),
            "is_kill": int(a.get("event") == "destroy"),
            "hit_target_id": a.get("target_id") or "",
            "aim_az_deg": az,
            "aim_el_deg": el,
            "nearest_target_id": tid or "",
            "angular_error_deg": err,
            "target_distance_m": dist,
            "target_radius_deg": rad,
            # <= 1.0 means the crosshair was inside the target when this shot
            # was taken. A good cross-check that the geometry is right.
            "normalized_error": (err / rad) if (err is not None and rad) else None,
            "inter_shot_interval_s": (t - ev[n - 1]["_t"]) if n else None,
        })

    # ---------------- per-target-instance rows ----------------
    killed_ids = {k.get("target_id") for k in kills if k.get("target_id")}
    for tid, trow in res.targets.items():
        st = trow.get("_spawn_t")
        if st is None or not (t_start <= st <= t_end):
            continue
        kill_t = next((k["_t"] for k in kills if k.get("target_id") == tid), None)
        target_rows.append({
            "subject_id": inst.code if inst else "",
            "name": subject,
            "warmup": inst.warmup if inst else "",
            "scenario": sid,
            "session_number": sess_no,
            "session_id": sid,
            "db": res.name,
            "trial_id": trial.get("trial_id"),
            "trial_index": trial.get("trial_index"),
            "target_id": tid,
            "target_type": trow.get("target_type"),
            "spawn_t_rel_s": st - t0,
            "size_m": trow.get("size"),            # world diameter, not degrees
            "angular_radius_deg": safe(statistics.median, radius_by_target.get(tid, []))
                                  if radius_by_target.get(tid) else None,
            "spawn_ecc_h_deg": trow.get("spawn_ecc_h"),
            "spawn_ecc_v_deg": trow.get("spawn_ecc_v"),
            "spawn_ecc_total_deg": (
                math.hypot(trow["spawn_ecc_h"], trow["spawn_ecc_v"])
                if trow.get("spawn_ecc_h") is not None and trow.get("spawn_ecc_v") is not None
                else None
            ),
            "was_killed": int(tid in killed_ids),
            "time_to_kill_s": (kill_t - st) if kill_t is not None else None,
        })

    return m


# --------------------------------------------------------------------------
# Session aggregation
