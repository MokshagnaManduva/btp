# Sensors

Heart-rate and raw-ECG capture from a **Polar H10** (device id `184CB835`),
timestamped so it lines up with FPSci aim-training sessions without any manual
syncing.

## The idea in one line

FPSci stamps its result rows with UTC in the format `%Y-%m-%d %H:%M:%S.%f`, so
this toolkit stamps every heart-rate sample the same way — correlating the two
is then just comparing timestamps.

## Install

```
python -m pip install -r requirements.txt
```

Only `bleak` (Bluetooth) is needed, and only for recording. The analysis side is
stdlib-only.

`correlate.py` imports the aim-metric engine from `../app/fpsci_metrics`, so the
two halves of the project share one implementation of every performance number.
Nothing about aim is computed twice.

## Quickstart

```
python scan.py                    # confirm the strap is visible
python record.py                  # start recording, then play
python correlate.py               # join the newest recording to the runs in it
```

A normal session looks like this:

1. Wet the electrodes, put the strap on. It does not advertise over Bluetooth
   until it can read a signal, so a dry strap is invisible.
2. `python record.py` and leave it running. Sit still for **two minutes** — that
   quiet stretch becomes the resting baseline every trial is compared against.
3. Start the study launcher and play: `python ../app/run_study.py`. Nothing
   needs to be told about the sensor, and the sensor needs to be told nothing
   about FPSci.
4. Quit FPSci through its menu, then press `q` in the recorder window.
5. `python correlate.py`.

A sitting of four scenarios produces four FPSci databases, one per run.
`correlate.py` joins every database whose gameplay falls inside the recording,
so there is nothing to select by hand.

Press `m` at any time during recording to drop a labelled marker.

## What you get

`python record.py` writes `data/raw/<timestamp>Z_184CB835/`:

| file | contents |
|------|----------|
| `hr.csv` | heart rate in bpm, ~1 Hz, with electrode-contact quality |
| `rr.csv` | RR intervals — one row per heartbeat, the basis for all HRV |
| `ecg.csv` | raw ECG, 130 Hz, microvolts |
| `acc.csv` | accelerometer, 50 Hz, milli-g (torso movement) |
| `markers.csv` | session start/stop and anything you marked with `m` |
| `session.json` | device info, counts, clock fit, warnings |

`python correlate.py` writes `data/processed/<same id>/`:

| file | contents |
|------|----------|
| `trials_hr.csv` | one row per FPSci trial: aim metrics **and** HR, RMSSD, SDNN, delta vs baseline |
| `sessions_hr.csv` | the same per run -- the analysis unit: warm-up, aim, physiology |
| `timeline.csv` | 1 Hz merged timeline of heart rate and shots/hits |
| `summary.json` | overlap diagnostics, baseline window, what was written |

and prints a table like:

```
session        trial              idx  dur s   acc %  HR bpm      dHR    RMSSD
------------------------------------------------------------------------------
gridshot       gridshot_60s         0   60.0    66.7    87.5    +23.4     27.2
flicks         flick_single         0   30.0    66.7   100.8    +36.8     48.6
```

## Layout

```
sensors/
├── record.py        CLI: record a session
├── scan.py          CLI: find the strap, query its capabilities
├── correlate.py     CLI: join a recording to an FPSci results database
├── polar_h10/
│   ├── protocol.py  BLE UUIDs, PMD commands, frame decoders
│   ├── clock.py     sensor clock -> host UTC mapping
│   ├── hrv.py       RR artifact filtering and HRV metrics
│   ├── fileio.py    timestamps, CSV streams, session manifest
│   └── recorder.py  the live BLE recorder
├── docs/
│   ├── data-format.md            every column, every unit, timing accuracy
│   ├── polar-h10-protocol.md     the BLE wire format, for extending this
│   └── correlating-with-fpsci.md the join, the metrics, the pitfalls
├── tests/           run directly with python, no pytest needed
├── data/raw/        recordings, never edited in place
├── data/processed/  everything derived, reproducible from raw + a script
└── notes/           datasheets, calibration, per-session log
```

## Conventions

- Raw data is never modified after it is written. Anything in `data/processed/`
  must be reproducible by re-running a script over `data/raw/`.
- Log every capture session in `notes/sessions.md`.
- All timestamps everywhere are UTC. See `docs/data-format.md` for why.

## Tests

```
python tests/test_protocol.py       # frame decoding, clock fitting, HRV
python tests/test_end_to_end.py     # synthetic recording + FPSci db, joined
python tests/test_multi_db.py       # one sitting spread over several databases
python tests/test_shared_metrics.py # the join and the exporter agree exactly
```

Neither touches Bluetooth, so they run anywhere.

## Troubleshooting

**`record.py` cannot find the strap.** Run `python scan.py`. If no Polar device
appears: the strap is dry (moisten the electrodes), not worn, or already
connected to something else — the H10 accepts very few simultaneous connections,
so close Polar Flow / Polar Beat first.

**ECG fails to start but heart rate works.** The recorder keeps going and notes
it in `session.json` under `errors`. Usual cause: the strap is in a charger, or
another app holds the PMD stream. `python scan.py --settings` shows what the
sensor will actually offer.

**`correlate.py` says the recordings do not overlap.** It prints both time
ranges. If they differ by a whole number of hours, something wrote local time
instead of UTC.
