# Data format

Every file `record.py` produces, column by column, plus what the timestamps
actually mean and how accurate they are.

## Session directory

One recording is one directory under `data/raw/`:

```
data/raw/20260820-154720Z_184CB835/
├── session.json
├── hr.csv
├── rr.csv
├── ecg.csv
├── acc.csv
└── markers.csv
```

The directory name is the UTC start time plus the strap's device id, so it sorts
chronologically and never collides.

## Timestamps

**Every row of every file carries two time columns:**

| column | meaning |
|--------|---------|
| `t_unix` | Unix seconds UTC, microsecond resolution, e.g. `1787240840.708293` |
| `utc` | the same instant as `2026-08-20 15:47:20.708293` |

The string format is `%Y-%m-%d %H:%M:%S.%f`, chosen because it is byte-for-byte
what FPSci writes.

### Why UTC, and why that matters

FPSci builds its timestamps in `FPSci/source/Logger.cpp`:

```cpp
GetSystemTimePreciseAsFileTime(&ft);   // UTC FILETIME
FileTimeToSystemTime(&ft, &datetime);  // no timezone conversion
sprintf(tmCharArray, "%04d-%02d-%02d %02d:%02d:%02d.%06d", ...);
```

Neither call applies a timezone, so **FPSci result timestamps are UTC**, even
though they carry no marker saying so. Confirm it on your own data at any time:

```
sqlite3 FPSci-bin/results/<newest>.db "select start_time from Sessions limit 1"
```

Compare that against the file's modification time. On this machine the gap is
5:30 — exactly IST's offset from UTC — which means the stored string is UTC and
the filesystem time is local.

Assuming those strings are local time is the single easiest way to ruin this
analysis, and it fails quietly: you get a plausible-looking join that is 5.5
hours out, so every trial picks up resting heart rate.

Because the format is fixed-width, **string comparison equals chronological
comparison**. `WHERE time >= '2026-08-20 15:47:00.000000'` is a valid time filter
in SQL against an FPSci database, no parsing required.

### Which timestamps are precise, and which are merely close

The two data paths have genuinely different timing properties.

**ECG and ACC** arrive over Polar Measurement Data, where every frame carries the
sensor's own nanosecond timestamp. Sample spacing inside a stream is therefore
exact: 130 Hz ECG really is 1/130 s apart. Those device timestamps are mapped
onto host UTC by the clock model described below. Both files keep the raw
`device_ts_ns` column so timestamps can be re-derived later.

**HR and RR** come from the standard Bluetooth Heart Rate Service, which carries
*no* device timestamp. Those rows are stamped when the notification arrived at
the PC. The RR *intervals* are still exact — they are measured on the strap — so
beat-to-beat timing is trustworthy; only the anchoring of the beat train to wall
clock carries the BLE delay.

### Accuracy budget

| stream | absolute accuracy vs UTC | relative accuracy within the stream |
|--------|--------------------------|-------------------------------------|
| ECG / ACC | ~10–40 ms, a constant bias | exact (sensor's own clock) |
| RR | ~20–60 ms | exact intervals |
| HR bpm | up to 1 s | the sensor's own smoothed estimate |

Against 45–60 s trials, all of this is negligible. It only matters if you go
looking for beat-level effects — for example, whether a shot lands during
systole. For that, use `ecg.csv` and detect R-peaks yourself; that gives you
sub-millisecond *relative* timing, still subject to the constant transport bias.

## The clock model

`session.json` carries a `clock_fit` block:

```json
"clock_fit": {
  "offset_s": 1787240399.812,
  "drift": 4.7e-05,
  "drift_ppm": 47.0,
  "ref_device_s": 812345.6,
  "residual_ms": 1.8,
  "kind": "linear"
}
```

The H10's real-time clock is only set when it syncs with the Polar Flow app and
drifts freely afterwards, so its absolute value is meaningless. What the
recorder does instead, for every PMD frame:

```
offset = host_arrival_time - device_timestamp
```

That offset is the true offset *plus* that frame's transport delay. Delay is
always positive, so the **minimum** observed offset is the best estimate — the
same one-way-delay trick NTP uses. It converges within a few seconds.

Over a longer session the two crystals also drift apart, so at the end
`ClockModel.fit()` fits a straight line to the *lower envelope* of the offsets
(the per-10-second minima) and stores both the constant and the drift term.
`residual_ms` is how well that line fits; if it is more than a few milliseconds,
the BLE link was unstable.

To re-derive timestamps offline:

```python
from polar_h10 import fileio
from polar_h10.clock import ClockFit

session = "data/raw/20260820-154720Z_184CB835"
manifest = fileio.read_manifest(session)
fit = ClockFit.from_dict(manifest["clock_fit"])

rows = fileio.read_stream(fileio.stream_path(session, "ecg"))
fileio.apply_clock_fit(rows, fit)      # rewrites t_unix / utc in place
```

The live `t_unix` values are written with the running-minimum offset, which is
still converging in the first seconds of a session. Re-deriving from the final
fit makes the whole file internally consistent.

## hr.csv

One row per heart-rate notification, roughly 1 Hz.

| column | unit | notes |
|--------|------|-------|
| `t_unix`, `utc` | — | notification arrival, not beat time |
| `hr_bpm` | bpm | the strap's own smoothed estimate |
| `contact` | `ok` / `poor` / `unknown` | electrode contact; `poor` means suspect data |
| `energy_j` | kJ | usually blank; the H10 rarely reports it |
| `n_rr` | count | RR intervals delivered in this notification |

`hr_bpm` is smoothed on the sensor. For anything quantitative, derive heart rate
from `rr.csv` instead (`60000 / rr_ms`). Use `hr_bpm` for live display and sanity
checks.

Watch `contact`: a run of `poor` means the strap dried out or shifted, and the
RR intervals over that stretch should be treated as suspect.

## rr.csv

**The most useful file here.** One row per heartbeat.

| column | unit | notes |
|--------|------|-------|
| `t_unix`, `utc` | — | when this beat occurred, back-dated from arrival |
| `beat_index` | count | monotonic from 0 for the session |
| `rr_ms` | ms | interval from the previous beat to this one |
| `hr_bpm` | bpm | `60000 / rr_ms`, instantaneous |
| `t_cumulative_unix` | — | alternative timeline: anchor + running sum of RR |
| `arrival_unix` | — | when the notification carrying this beat arrived |

The RR values come from the strap at 1/1024 s resolution and are converted to
milliseconds.

A notification can carry several beats at once. The newest interval is taken to
end at the arrival time, and earlier ones are stepped back by their own
durations, so beats are placed correctly within the second rather than piling up
at the notification instant.

**Two time columns, deliberately.** `t_unix` re-anchors on every notification, so
it tracks wall clock and never drifts, but it inherits per-notification jitter.
`t_cumulative_unix` is one anchor plus the running sum of every interval since —
perfectly smooth, but it accumulates any error in the anchor and quietly drops
out of step if a notification is ever lost. Use `t_unix` for correlating with
FPSci; use `t_cumulative_unix` if you need an evenly-spaced beat series and care
more about interval fidelity than absolute placement. If they diverge by more
than a second or two, beats were dropped.

## ecg.csv

Raw single-lead ECG at 130 Hz — about 470,000 rows and 25 MB per hour.

| column | unit | notes |
|--------|------|-------|
| `t_unix`, `utc` | — | derived from `device_ts_ns` |
| `device_ts_ns` | ns | the sensor's own clock, since 2000-01-01T00:00:00Z |
| `ecg_uv` | µV | signed |

The H10 fixes ECG at 130 Hz / 14-bit; the samples arrive as signed 24-bit
values. This is the underlying signal the strap derives everything else from —
detect your own R-peaks here if you want beat timing better than `rr.csv`.

Amplitude is not calibrated to any clinical standard and depends on strap
position, so treat µV as relative.

## acc.csv

Torso accelerometer, 50 Hz by default (`--acc-rate` accepts 25/50/100/200).

| column | unit | notes |
|--------|------|-------|
| `t_unix`, `utc` | — | derived from `device_ts_ns` |
| `device_ts_ns` | ns | sensor clock |
| `acc_x_mg`, `acc_y_mg`, `acc_z_mg` | mg | 1000 mg = 1 g; includes gravity |

Mostly useful as an artifact channel: a burst of motion that coincides with an
ugly stretch of ECG explains it. Since FPSci pins the player in place, torso
movement during a trial is mostly leaning and breathing.

## markers.csv

| column | notes |
|--------|-------|
| `t_unix`, `utc` | when the marker was written |
| `label` | `session_start`, `session_stop`, `manual_marker`, or `note: ...` |

Correlation does not need markers — the clocks already agree — but they are
useful for annotating anything the logs cannot see ("phone rang", "restarted
FPSci").

## session.json

```json
{
  "schema_version": 1,
  "session_id": "20260820-154720Z_184CB835",
  "device": { "device_id": "184CB835", "model": "Polar H10",
              "firmware": "5.0.6", "battery_pct": 87 },
  "subject": "Mokshagna",
  "start_utc": "2026-08-20 15:47:20.708293",
  "end_utc":   "2026-08-20 16:19:02.114552",
  "duration_s": 1901.406,
  "streams": { "ecg_sample_rate_hz": 130, "acc_sample_rate_hz": 50, "acc_range_g": 8 },
  "counts": { "hr": 1899, "rr": 2104, "ecg": 247183, "acc": 95070, "markers": 3 },
  "effective_rates_hz": { "hr": 0.999, "rr": 1.107, "ecg": 130.0, "acc": 50.0 },
  "clock_fit": { "...": "see above" },
  "errors": []
}
```

Two fields are worth checking after every recording:

- **`effective_rates_hz`** — `ecg` should be ~130 and `acc` ~50. Materially
  lower means frames were dropped, usually from BLE interference or distance.
- **`errors`** — non-empty means something degraded: a stream that refused to
  start, a mid-session disconnect, or delta-compressed frames appearing.

## Reading the files

```python
from polar_h10 import fileio, hrv

session = "data/raw/20260820-154720Z_184CB835"
beats = fileio.read_stream(fileio.stream_path(session, "rr"))

window = [b["rr_ms"] for b in beats
          if 1787240900 <= b["t_unix"] < 1787240960]
print(hrv.hrv_metrics(window))
# {'n_beats': 88, 'hr_mean_bpm': 87.5, 'rmssd_ms': 27.2, ...}
```

The CSVs are ordinary comma-separated files with a header row, so
`pandas.read_csv` works too if you would rather use it — nothing here requires
it.
