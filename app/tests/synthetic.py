"""Build small but realistic FPSci result databases, with no game needed.

The test suite used to read whatever happened to be in FPSci-bin/results, so
clearing the data turned two suites red for lack of input rather than for any
fault. A suite that fails when idle trains you to ignore it, so the tests now
fall back to these when no real databases are present.

The schema and column names match what FPSci v23.04.01 writes; the geometry is
simplified but consistent, so the metric engine produces real numbers rather
than a column of blanks. Real databases are still preferred when they exist --
these are the floor, not the target.
"""

from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S.%f"
BASE = datetime(2026, 8, 26, 12, 0, 0)

#: Camera sits at the origin at eye height, targets about a metre ahead --
#: the same arrangement FPSci uses for player-space targets.
CAMERA = (0.0, 1.5, 0.0)
TARGET_DISTANCE = 1.0
TARGET_SIZE_M = 0.033          # ~1.9 deg across, the gridshot target
FRAME_HZ = 120                 # low enough to keep the files small


def stamp(offset_s):
    return (BASE + timedelta(seconds=offset_s)).strftime(FMT)


def _schema(conn):
    conn.executescript("""
        create table Experiments (description text, time text, hash text, config text);
        create table Sessions (session_id text, start_time text, end_time text,
            subject_id text, description text, complete boolean,
            tasks_complete int, trials_complete int, frameRate text,
            maxTrialDuration text, pretrialDuration text, scoreModel text);
        create table Tasks (session_id text, block_id int, task_id text,
            task_index int, start_time text, end_time text, trial_order text,
            trials_complete int, complete int);
        create table Trials (session_id text, block_id int, task_id text,
            task_index int, trial_id text, trial_index int, start_time text,
            end_time text, pretrial_duration real, task_execution_time real,
            destroyed_targets int, total_targets int);
        create table Targets (target_id text, target_type text, spawn_time text,
            size real, spawn_ecc_h real, spawn_ecc_v real);
        create table Target_Types (target_type text, motion_type text,
            dest_space text, min_size real, max_size real);
        create table Target_Trajectory (time text, target_id text, state text,
            position_x real, position_y real, position_z real);
        create table Player_Action (time text, position_az real, position_el real,
            position_x real, position_y real, position_z real, state text,
            event text, target_id text);
        create table Frame_Info (time text, sdt real);
        create table Questions (time text, session_id text, task_id text,
            task_index int, trial_id text, trial_index int, question text,
            response_array text, key_array text, presented_responses text,
            response text);
        create table Users (subject_id text, session_id text, time text,
            cmp360 real, mouse_deg_per_mm real, mouse_dpi real,
            reticle_index int, sensitivity_x real, sensitivity_y real);
    """)


def _target_position(az_deg, el_deg):
    """World position of a target at a given angular offset, a metre out."""
    az, el = math.radians(az_deg), math.radians(el_deg)
    ce = math.cos(el)
    return (CAMERA[0] + TARGET_DISTANCE * ce * math.sin(az),
            CAMERA[1] + TARGET_DISTANCE * math.sin(el),
            CAMERA[2] - TARGET_DISTANCE * ce * math.cos(az))


def make_run(path, scenario="gridshot", subject="Mokshagna", selected_s=0,
             played_s=None, duration_s=10, submit="Yes", n_targets=3,
             trials=1, with_questions=True):
    """One results database holding one run.

    ``selected_s`` is when the scenario was picked in the menu (which is when
    FPSci opens the file) and ``played_s`` when the run actually started. The
    gap between them is where the warm-up condition can change, so it is worth
    being able to set them apart.
    """
    if played_s is None:
        played_s = selected_s + 2
    conn = sqlite3.connect(path)
    _schema(conn)

    end_s = played_s + duration_s * trials
    conn.execute("insert into Experiments values (?,?,?,?)",
                 ("aimtrain", stamp(selected_s), "0xtest", "// synthetic"))
    conn.execute("insert into Sessions values (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (scenario, stamp(selected_s), stamp(end_s), subject,
                  "aimtrain/" + scenario, 1, 1, trials,
                  "1000 ", "60 ", "0.4 ", '"targets destroyed"'))
    conn.execute("insert into Users values (?,?,?,?,?,?,?,?,?)",
                 (subject, scenario, "start", 30.0, 1.2, 800.0, 39, 1.0, 1.0))
    conn.execute("insert into Target_Types values (?,?,?,?,?)",
                 (scenario + "_target", "static", "player",
                  TARGET_SIZE_M, TARGET_SIZE_M))

    frame_dt = 1.0 / FRAME_HZ
    for t_index in range(trials):
        t0 = played_s + t_index * duration_s
        t1 = t0 + duration_s
        conn.execute("insert into Trials values (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (scenario, 0, scenario + "_task", 0,
                      scenario + "_trial", t_index, stamp(t0), stamp(t1),
                      0.4, float(duration_s), n_targets, n_targets))

        # Targets spread across a plausible eccentricity box.
        specs = []
        for i in range(n_targets):
            az = 6.0 + 4.0 * i
            el = 2.0 * ((-1) ** i)
            tid = "%s_%d_%d" % (scenario, t_index, i)
            specs.append((tid, az, el))
            conn.execute("insert into Targets values (?,?,?,?,?,?)",
                         (tid, scenario + "_target", stamp(t0),
                          TARGET_SIZE_M, az, el))

        # Per-frame aim sweeping onto each target in turn, with the kill
        # logged when the crosshair arrives.
        n_frames = int(duration_s * FRAME_HZ)
        per_target = max(n_frames // n_targets, 1)
        for f in range(n_frames):
            t = t0 + f * frame_dt
            idx = min(f // per_target, n_targets - 1)
            tid, taz, tel = specs[idx]
            # Ease from the previous aim onto this target over its slice.
            phase = min((f % per_target) / float(per_target) * 1.5, 1.0)
            az, el = taz * phase, tel * phase

            conn.execute("insert into Player_Action values (?,?,?,?,?,?,?,?,?)",
                         (stamp(t), az, el, CAMERA[0], CAMERA[1], CAMERA[2],
                          "trialTask", "aim", None))
            conn.execute("insert into Frame_Info values (?,?)",
                         (stamp(t), frame_dt))
            for stid, staz, stel in specs:
                px, py, pz = _target_position(staz, stel)
                conn.execute(
                    "insert into Target_Trajectory values (?,?,?,?,?,?)",
                    (stamp(t), stid, "trialTask", px, py, pz))

            # One miss early in each slice, one destroy at the end of it.
            if f % per_target == max(per_target // 4, 1):
                conn.execute(
                    "insert into Player_Action values (?,?,?,?,?,?,?,?,?)",
                    (stamp(t), az, el, CAMERA[0], CAMERA[1], CAMERA[2],
                     "trialTask", "miss", None))
            if f % per_target == per_target - 1:
                conn.execute(
                    "insert into Player_Action values (?,?,?,?,?,?,?,?,?)",
                    (stamp(t), taz, tel, CAMERA[0], CAMERA[1], CAMERA[2],
                     "trialTask", "destroy", tid))

    if with_questions and submit:
        conn.execute("insert into Questions values (?,?,?,?,?,?,?,?,?,?,?)",
                     (stamp(end_s + 2), scenario, None, None, None, None,
                      "Submit this run?", '( "Yes", "No") ', '( "Y", "N") ',
                      '( "Yes (Y)", "No (N)") ', "%s (%s)" % (submit, submit[0])))
    conn.commit()
    conn.close()
    return path


def make_stub(path, scenario="gridshot", subject="Mokshagna", selected_s=0):
    """A menu stub: the database exists, but no run was ever played in it."""
    conn = sqlite3.connect(path)
    _schema(conn)
    conn.execute("insert into Sessions values (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (scenario, stamp(selected_s), stamp(selected_s), subject,
                  "aimtrain/" + scenario, 0, 0, 0,
                  "1000 ", "60 ", "0.4 ", '"targets destroyed"'))
    for f in range(20):
        conn.execute("insert into Player_Action values (?,?,?,?,?,?,?,?,?)",
                     (stamp(selected_s + f * 0.05), 0.0, 0.0,
                      CAMERA[0], CAMERA[1], CAMERA[2], "initial", "aim", None))
    conn.commit()
    conn.close()
    return path


def populate(directory, runs=2):
    """A small sitting: ``runs`` completed runs plus a menu stub."""
    os.makedirs(directory, exist_ok=True)
    made = []
    scenarios = ("gridshot", "flicks", "microadjust", "tracking")
    for i in range(runs):
        scenario = scenarios[i % len(scenarios)]
        name = "%s_Synthetic_2026_08_26-12_%02d_00.db" % (scenario, i)
        made.append(make_run(os.path.join(directory, name), scenario=scenario,
                             subject="Synthetic", selected_s=i * 60,
                             played_s=i * 60 + 3, duration_s=8,
                             submit="Yes" if i % 2 == 0 else "No"))
    make_stub(os.path.join(directory, "gridshot_Synthetic_2026_08_26-12_59_00.db"),
              selected_s=runs * 60)
    return made
