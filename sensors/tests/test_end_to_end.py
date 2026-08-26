"""End-to-end test of the recording format and the FPSci correlation.

    python tests/test_end_to_end.py

Synthesises a Polar recording (a beat train whose rate rises during gameplay)
and an FPSci results database with the real schema, runs correlate.py over the
pair, and checks that the heart rate lands inside the right trials.  This covers
everything except the BLE transport itself.
"""

import csv
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import correlate                                  # noqa: E402
from polar_h10 import fileio                      # noqa: E402

# A fixed instant so the test is deterministic: 2026-08-20 15:40:00 UTC.
T0 = fileio.parse_utc("2026-08-20 15:40:00.000000")
DURATION = 400.0

# (start offset, end offset, heart rate during the window)
TRIALS = [
    ("gridshot", "gridshot_60s", 0, 150.0, 210.0, 88.0),
    ("flicks", "flick_single", 0, 250.0, 280.0, 102.0),
]
BASELINE_BPM = 64.0


def hr_at(offset):
    for _, _, _, start, end, bpm in TRIALS:
        if start <= offset < end:
            return bpm
    return BASELINE_BPM


def make_polar_session(root):
    """Write a recording that looks exactly like one record.py would produce."""
    session_dir = os.path.join(root, fileio.session_id(T0, "184CB835"))
    os.makedirs(session_dir)

    beats = []
    offset = 0.0
    index = 0
    while offset < DURATION:
        # A little respiratory variation so RMSSD is non-zero and realistic.
        rr = 60000.0 / hr_at(offset) * (1.0 + 0.02 * math.sin(offset / 4.0))
        offset += rr / 1000.0
        if offset >= DURATION:
            break
        beats.append((T0 + offset, rr, index))
        index += 1

    with fileio.CsvStream(fileio.stream_path(session_dir, "rr"),
                          fileio.SCHEMAS["rr"], flush_interval=0.0) as stream:
        cumulative = T0
        for t, rr, i in beats:
            cumulative += rr / 1000.0
            stream.write("%.6f" % t, fileio.utc_string(t), i, round(rr, 3),
                         round(60000.0 / rr, 3), "%.6f" % cumulative, "%.6f" % t)

    with fileio.CsvStream(fileio.stream_path(session_dir, "hr"),
                          fileio.SCHEMAS["hr"], flush_interval=0.0) as stream:
        for second in range(int(DURATION)):
            t = T0 + second
            stream.write("%.6f" % t, fileio.utc_string(t),
                         int(round(hr_at(second))), "ok", "", 1)

    with fileio.CsvStream(fileio.stream_path(session_dir, "markers"),
                          fileio.SCHEMAS["markers"], flush_interval=0.0) as stream:
        stream.write("%.6f" % T0, fileio.utc_string(T0), "session_start")
        stream.write("%.6f" % (T0 + DURATION), fileio.utc_string(T0 + DURATION),
                     "session_stop")

    fileio.write_manifest(session_dir, {
        "schema_version": 1,
        "session_id": os.path.basename(session_dir),
        "device": {"device_id": "184CB835", "model": "Polar H10"},
        "subject": "test",
        "start_utc": fileio.utc_string(T0),
        "end_utc": fileio.utc_string(T0 + DURATION),
        "start_unix": T0,
        "end_unix": T0 + DURATION,
        "duration_s": DURATION,
        "time_format": fileio.TIME_FORMAT,
        "counts": {"hr": int(DURATION), "rr": len(beats), "ecg": 0, "acc": 0,
                   "markers": 2},
        "clock_fit": {"offset_s": 1.0, "drift": 0.0, "kind": "constant"},
        "errors": [],
    })
    return session_dir, beats


def make_fpsci_db(path):
    """A minimal database with FPSci's real table and column names."""
    conn = sqlite3.connect(path)
    conn.execute("create table Experiments (description text, time text, "
                 "hash text, config text)")
    conn.execute("create table Sessions (session_id text, start_time text, "
                 "end_time text, subject_id text, description text, "
                 "complete int, tasks_complete int, trials_complete int)")
    conn.execute("create table Trials (session_id text, block_id int, "
                 "task_id text, task_index int, trial_id text, trial_index int, "
                 "start_time text, end_time text, pretrial_duration real, "
                 "task_execution_time real, destroyed_targets int, "
                 "total_targets int)")
    conn.execute("create table Player_Action (time text, position_az real, "
                 "position_el real, state text, event text, target_id text)")
    conn.execute("insert into Experiments values (?,?,?,?)",
                 ("aimtrain", fileio.utc_string(T0 + 100), "0xtest", "{}"))

    shots = {}
    for session_id, task_id, index, start, end, _ in TRIALS:
        t_start, t_end = T0 + start, T0 + end
        conn.execute("insert into Sessions values (?,?,?,?,?,?,?,?)",
                     (session_id, fileio.utc_string(t_start - 5),
                      fileio.utc_string(t_end + 2), "test",
                      "aimtrain/" + session_id, 1, 1, 1))
        conn.execute("insert into Trials values (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (session_id, 0, task_id, 0, task_id, index,
                      fileio.utc_string(t_start), fileio.utc_string(t_end),
                      0.4, end - start, 10, -1))
        # One shot per second: two thirds destroy, one third miss.
        hits = misses = 0
        for i in range(int(end - start)):
            event = "miss" if i % 3 == 2 else "destroy"
            hits += event == "destroy"
            misses += event == "miss"
            conn.execute("insert into Player_Action values (?,?,?,?,?,?)",
                         (fileio.utc_string(t_start + i + 0.25), 0.0, 0.0,
                          "trialTask", event, "t"))
        # Aim rows are the bulk of the real table; include some to prove they
        # are excluded from the shot counts.
        for i in range(50):
            conn.execute("insert into Player_Action values (?,?,?,?,?,?)",
                         (fileio.utc_string(t_start + i * 0.1), 0.0, 0.0,
                          "trialTask", "aim", ""))
        shots[task_id] = (hits + misses, hits, misses)

    # A session row that was opened and abandoned -- must be ignored.
    conn.execute("insert into Sessions values (?,?,?,?,?,?,?,?)",
                 ("tracking", fileio.utc_string(T0 + 390),
                  fileio.utc_string(T0 + 390), "test", "aimtrain/tracking", 0, 0, 0))
    conn.commit()
    conn.close()
    return shots


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main():
    tmp = tempfile.mkdtemp(prefix="polar_e2e_")
    try:
        raw_root = os.path.join(tmp, "raw")
        os.makedirs(raw_root)
        session_dir, beats = make_polar_session(raw_root)
        db_path = os.path.join(tmp, "aimtrain_test_0xdeadbeef.db")
        expected_shots = make_fpsci_db(db_path)
        out_dir = os.path.join(tmp, "processed")

        rc = correlate.run(session_dir, db_path, out_dir,
                           baseline_s=120.0, bin_s=1.0, refit=True)
        assert rc == 0, "correlate.run returned %r" % rc

        trials = read_csv_rows(os.path.join(out_dir, "trials_hr.csv"))
        assert len(trials) == len(TRIALS), "expected %d trials, got %d" % (
            len(TRIALS), len(trials))

        by_trial = {row["trial_id"]: row for row in trials}
        for _, task_id, _, start, end, bpm in TRIALS:
            row = by_trial[task_id]
            measured = float(row["hr_mean_bpm"])
            assert abs(measured - bpm) < 2.0, \
                "%s: HR %.1f should be near %.1f" % (task_id, measured, bpm)
            delta = float(row["hr_delta_vs_baseline_bpm"])
            assert delta > 15.0, "%s: delta vs baseline was only %.1f" % (task_id, delta)
            shots, hits, misses = expected_shots[task_id]
            assert int(row["shots"]) == shots, \
                "%s: %s shots, expected %d" % (task_id, row["shots"], shots)
            assert int(row["hits"]) == hits and int(row["misses"]) == misses
            assert abs(float(row["accuracy_pct"]) - 100.0 * hits / shots) < 0.01
            assert float(row["duration_s"]) == end - start
            assert int(row["n_beats"]) > 20
            assert float(row["rmssd_ms"]) > 0.0

        sessions = read_csv_rows(os.path.join(out_dir, "sessions_hr.csv"))
        assert len(sessions) == len(TRIALS), \
            "abandoned session rows should be dropped, got %d" % len(sessions)

        timeline = read_csv_rows(os.path.join(out_dir, "timeline.csv"))
        assert abs(len(timeline) - DURATION) <= 2, len(timeline)
        labelled = [r for r in timeline if r["trial_id"]]
        expected_bins = sum(int(end - start) for _, _, _, start, end, _ in TRIALS)
        assert abs(len(labelled) - expected_bins) <= 2, \
            "%d bins inside trials, expected about %d" % (len(labelled), expected_bins)
        covered = [r for r in timeline if r["hr_bpm"]]
        assert len(covered) > 0.95 * len(timeline), \
            "only %d/%d bins carry a heart rate" % (len(covered), len(timeline))
        # The first bin of a trial straddles the beat that spans the boundary,
        # so it legitimately still reads close to the resting rate; the body of
        # the trial must be clearly elevated.
        in_trial_hr = sorted(float(r["hr_bpm"]) for r in labelled if r["hr_bpm"])
        elevated = [hr for hr in in_trial_hr if hr > BASELINE_BPM + 10.0]
        assert len(elevated) > 0.9 * len(in_trial_hr), \
            "only %d/%d in-trial bins were elevated" % (len(elevated), len(in_trial_hr))
        assert in_trial_hr[len(in_trial_hr) // 2] > BASELINE_BPM + 15.0

        summary = json.load(open(os.path.join(out_dir, "summary.json"),
                                 encoding="utf-8"))
        assert summary["overlap"]["ok"] is True
        assert summary["baseline"]["available"] is True
        assert abs(summary["baseline"]["hr_mean_bpm"] - BASELINE_BPM) < 1.5
        assert summary["counts"]["beats"] == len(beats)

        # A recording taken an hour after the game must be rejected, not joined.
        far_dir = os.path.join(tmp, "raw_far", os.path.basename(session_dir))
        shutil.copytree(session_dir, far_dir)
        manifest = fileio.read_manifest(far_dir)
        manifest["start_unix"] += 7200
        manifest["end_unix"] += 7200
        manifest["start_utc"] = fileio.utc_string(manifest["start_unix"])
        manifest["end_utc"] = fileio.utc_string(manifest["end_unix"])
        fileio.write_manifest(far_dir, manifest)
        shifted = os.path.join(tmp, "processed_far")
        rc = correlate.run(far_dir, db_path, shifted, 120.0, 1.0, True)
        assert rc == 2, "a non-overlapping pair should fail loudly, got %r" % rc

        print("")
        print("end-to-end OK: %d beats, %d trials, %d timeline bins"
              % (len(beats), len(trials), len(timeline)))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
