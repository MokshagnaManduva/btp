# Correlating heart rate with FPSci

How `correlate.py` joins a Polar recording to an FPSci results database, what
each output column means, and the ways this can quietly go wrong.

## The join

There is no synchronisation step. Both sides write UTC in the same format, so a
trial and a heartbeat are matched by comparing timestamps:

```
a beat belongs to a trial  <=>  trial.start_time <= beat.t_unix < trial.end_time
```

That is the whole mechanism. `record.py` does not need to know FPSci exists, and
FPSci does not need to know about the strap. You only have to have the recorder
running while you play.

For that to hold, one thing must be true: **the PC clock must not jump during a
session.** Windows time sync normally makes small, gradual corrections, but a
large step correction mid-session would shift one side relative to the other.
If a session looks impossible, check the Windows time service.

## Running it

```
python correlate.py                       # newest recording, every run inside it
python correlate.py --list                # what is available
python correlate.py --session data/raw/20260820-154720Z_184CB835
python correlate.py --fpsci-db ../FPSci-bin/results/gridshot_M_2026_08_21-17_03_10.db
python correlate.py --baseline 180        # longer resting window
python correlate.py --bin 0.5             # finer timeline
```

### One sitting is several databases

FPSci writes **one database per run** (`logToSingleDb: false`), named
`<scenario>_<user>_<UTC>.db`. A sitting of four scenarios is therefore four
files, and correlate.py joins every database whose gameplay falls inside the
recording window rather than picking the newest one.

This used to be "newest file wins", which under per-run databases would have
silently joined a whole sitting against its last run alone.
`tests/test_multi_db.py` covers the selection; pass `--fpsci-db` (repeatable)
to override it.

FPSci databases are always opened **read-only** — the results files are raw data
and this never writes to them, not even a SQLite journal.

## What FPSci gives us

Tables `correlate.py` reads, from `FPSci-bin/results/*.db`:

| table | used for |
|-------|----------|
| `Sessions` | scenario windows, `subject_id`, completion state |
| `Trials` | trial windows, `destroyed_targets`, `task_execution_time` |
| `Player_Action` | shot events |
| `Experiments` | the experiment description, and the full config that produced the run |

`Player_Action` has one row **per frame** — at an unlocked frame rate that is
tens of thousands of rows per minute, and the bulk of the database. Almost all of
them are `event = 'aim'`. Only `hit`, `miss`, and `destroy` are shots, so the
query filters to those in SQL rather than pulling the table into memory.

Note that with a one-shot-kill weapon config a successful shot logs as `destroy`,
not `hit` — both count as hits here.

### Quirks worth knowing

- **Abandoned session rows.** Opening the FPSci menu and backing out writes a
  `Sessions` row whose `end_time` equals its `start_time`. These are skipped.
- **Repeated `session_id`s.** Several databases in one sitting can each hold a
  `gridshot` session. Trials are attributed to the session row whose window
  contains them, so merging across files needs no disambiguation.
- **`total_targets = -1`.** Means "respawn forever" (`respawnCount: -1`), not a
  count. Accuracy is computed from shot events, never from this field.
- **NULL `end_time`.** A trial quit mid-run has no end. It is bounded by the next
  trial's start or the session end, whichever comes first.
- **Databases with zero trials** are menu stubs: selecting a scenario opens a
  database before any run starts, so browsing the menu leaves small files
  behind. They have no trial span and drop out of the selection automatically.
  `--list` still shows the trial count.
- **`Experiments` used to be empty.** FPSci splices the whole config file into
  SQL wrapped in single quotes with no escaping, so a single apostrophe
  anywhere in it — including inside a comment — silently voided the insert.
  Fixed 2026-08-21; databases recorded before that have an empty table.

## Where the aim numbers come from

Every performance column here is computed by `app/fpsci_metrics`, the same
engine `app/export.py` runs to build `analysis/`. `correlate.py` does not count
a shot or compute an accuracy itself; it derives the aim metrics through that
engine and attaches physiology over the same time window.

That matters because the alternative is two implementations of one quantity,
and a thesis with two different accuracies for the same trial.
`tests/test_shared_metrics.py` runs both consumers over the same databases and
diffs every shared column.

The columns carried here are a useful subset. The full set -- 66 metrics per
trial, including submovements, path efficiency and per-frame angular error --
lives in `analysis/<scenario>/<person>/trials.csv`.

## trials_hr.csv

One row per completed FPSci trial. Performance columns come from FPSci,
physiology columns from the strap.

**Identity:** `subject_id`, `session_id`, `block_id`, `task_id`, `task_index`,
`trial_id`, `trial_index`

**Window:** `start_utc`, `end_utc`, `start_unix`, `end_unix`, `duration_s`,
`pretrial_duration`, `task_execution_time`

**Performance:** `destroyed_targets`, `total_targets`, `shots`, `hits`,
`misses`, `accuracy_pct`

**Physiology:**

| column | meaning |
|--------|---------|
| `n_beats` | beats inside the trial that survived artifact filtering |
| `n_rejected` | beats discarded as artifacts — if this is a large fraction, distrust the row |
| `hr_mean_bpm` | `60000 / mean(RR)`, not the strap's smoothed bpm |
| `hr_min_bpm`, `hr_max_bpm` | from the longest and shortest interval |
| `mean_rr_ms` | mean inter-beat interval |
| `sdnn_ms` | SD of RR — overall variability |
| `rmssd_ms` | RMS of successive differences — short-term, parasympathetic |
| `pnn50_pct` | % of successive differences over 50 ms |
| `hr_delta_vs_baseline_bpm` | `hr_mean_bpm` minus the resting baseline |
| `rmssd_delta_vs_baseline_ms` | same for RMSSD |
| `hr_coverage_pct` | how much of the trial the beats actually span; well under 100 means dropouts |

## sessions_hr.csv

One row per **run** — the analysis unit. With one database per run a session and
a run are the same thing, so this is the engine's per-run rollup with the strap
attached over the same window, carrying `warmup`, `scenario`, `n_trials` and
`task_time_s` alongside the aim and physiology columns.

Rates here are recomputed by the engine from run totals rather than averaged
across trials, so a short trial cannot skew them — which matters for `flicks`,
where one run is 25 short pair-trials rather than a single 60 s one.

This is the table to reach for when comparing conditions: one row per run, with
the warm-up that was in force, what the player did, and what their heart did.

## timeline.csv

One row per time bin (1 s by default, `--bin` to change) spanning the whole
recording.

| column | meaning |
|--------|---------|
| `t_unix`, `utc` | start of the bin |
| `hr_bpm` | mean instantaneous HR of beats in the bin |
| `hr_source` | `beats` if measured here, `interp` if interpolated, empty if unknown |
| `rr_ms` | mean RR in the bin |
| `n_beats` | beats in the bin |
| `session_id`, `task_id`, `trial_id`, `trial_index` | what was happening, blank between trials |
| `shots`, `hits`, `misses` | events in the bin |

At a resting 60 bpm, a 1 s bin often contains no beat at all, so gaps are filled
by interpolating between the surrounding beats and flagged as `interp`. Gaps
longer than 10 s are left empty rather than invented. **Check `hr_source` before
treating this file as a measured signal** — it is built for plotting and for
lag analysis, not as a substitute for `rr.csv`.

This is the file to plot: heart rate against time with trial boundaries marked
shows the arousal ramp into a run and the recovery after it.

## HRV, honestly

Only time-domain metrics are computed. That is deliberate: LF/HF and other
frequency-domain measures need 2–5 minutes of stationary data, and a 45–60 s
aim-training trial supplies neither the length nor the stationarity. Computing
them anyway would produce numbers that look precise and mean nothing.

Even for RMSSD, treat a single trial as noisy. What holds up:

- **`hr_delta_vs_baseline_bpm`** is robust — arousal raises heart rate, and the
  effect is large relative to the noise.
- **RMSSD compared across many trials** of the same scenario is meaningful.
  RMSSD from one 60 s trial in isolation is not.
- **Anything below ~30 beats** in a window is too few. Watch `n_beats`.

### Artifact filtering

`hrv.filter_rr` drops intervals outside 250–2000 ms, and intervals differing by
more than 20% from the median of the last five accepted beats.

The subtlety: heart rate genuinely jumps when a run starts, which is the entire
point of the measurement. A filter that compares each beat only to the previous
accepted one gets stuck at the old rate and rejects every beat after the change —
so the trials where HR actually moved come out reading like the resting baseline.
That failure mode is silent and it is the reason for the resync rule: three
consecutive beats that disagree with the reference but agree with *each other*
are taken as a real change of level, and the reference moves to follow them.

`tests/test_protocol.py` pins both behaviours — an isolated ectopic beat is
rejected, a sustained 64→88 bpm ramp is not.

Rejected beats leave a gap, so the successive difference spanning the gap is not
a true beat-to-beat difference. With few rejections this barely moves RMSSD;
with many, `n_rejected` is your warning to distrust the row.

## The baseline

By default, the 120 s ending at the first trial start. **Record two quiet
minutes before you launch FPSci** — sitting still, strap on, not yet playing.
Without it there is nothing to compare against and the delta columns are blank.

Use `--baseline` to change the window. `summary.json` records exactly which
window was used, so a baseline can always be reconstructed.

Note this is a *pre-session* baseline, not a true resting baseline: sitting down
having just put a chest strap on is not the same as resting. It works well for
within-session comparisons, less well across days. For cross-day work, take the
same deliberate five-minute seated measurement each time and record it as its own
session with `--note baseline`.

## Recipes

**Does accuracy fall as heart rate rises?**

```python
import csv, statistics
rows = list(csv.DictReader(open("data/processed/<id>/trials_hr.csv")))
rows = [r for r in rows if r["hr_mean_bpm"] and r["accuracy_pct"]]
hr  = [float(r["hr_mean_bpm"]) for r in rows]
acc = [float(r["accuracy_pct"]) for r in rows]
print(statistics.correlation(hr, acc), "over", len(rows), "trials")
```

Aim for 20+ trials before reading anything into that number.

**Compare scenarios:**

```python
from collections import defaultdict
by_scenario = defaultdict(list)
for r in rows:
    by_scenario[r["session_id"]].append(float(r["hr_delta_vs_baseline_bpm"]))
for name, deltas in by_scenario.items():
    print("%-12s %+5.1f bpm over baseline (n=%d)"
          % (name, sum(deltas) / len(deltas), len(deltas)))
```

**Query FPSci directly.** Because the timestamp format is fixed-width, string
comparison is chronological comparison:

```sql
SELECT event, COUNT(*) FROM Player_Action
WHERE time >= '2026-08-20 15:47:55.000000'
  AND time <  '2026-08-20 15:48:55.000000'
GROUP BY event;
```

**Does heart rate lag performance?** `timeline.csv` is built for this — shift
`hr_bpm` against `hits` by 0–30 s and look for the lag with the strongest
relationship. Cardiac responses to a stressor take several seconds to develop,
so expect any real effect to sit at a positive lag rather than at zero.

## Extending

Things this does not do yet, roughly in order of effort:

- **Aim kinematics.** `Player_Action` logs `position_az` / `position_el` every
  frame. Differentiating those gives aim velocity, overshoot, and micro-correction
  counts, which are the movement measures worth correlating with HRV.
- **Own R-peak detection** on `ecg.csv`, for beat timing better than the strap's
  own detector reports.
- **Respiration** from ECG amplitude modulation (EDR), or from the accelerometer
  — breathing rate drives much of the RMSSD signal.
- **Per-shot phase.** With real R-peak times, whether a shot landed in systole or
  diastole becomes answerable.
