# btp

Does a warm-up change how well you aim, and can a heart-rate strap see it?

Two halves that never talk to each other while recording. FPSci records aim
performance, a Polar H10 records heart rate and ECG, and both stamp every row
with UTC in the same format — so joining them is a time comparison and nothing
more. No sync step, no markers to line up, no shared clock to maintain.

Afterwards they share one thing: the aim-metric engine in `app/fpsci_metrics`.
Both the exporter and the heart-rate join derive performance through it, so no
number is computed twice.

## Run a session

```
python app/run_study.py
```

Pick the participant, pick the warm-up, hit Launch. Play, answer **Submit this
run?** after each run, and quit FPSci through its menu when you are done. Each
run exports itself a few seconds after it finishes.

With the heart-rate strap, start the recorder first and give it two quiet
minutes for a resting baseline:

```
cd sensors
python record.py --subject <the id you pick in the launcher>
# ... play the session ...
python correlate.py
```

## Layout

```
app/           the harness: launcher, exporter, metric engine, tests
FPSci-bin/     the game (stock FirstPersonScience.exe) and its config
  results/     raw, one database per run, never modified
study/         sittings.csv -- which warm-up was in force, and when
analysis/      derived CSVs, per scenario and per person
sensors/       Polar H10 capture and the heart-rate join
docs/          the rewrite plan, and a snapshot of the pre-rewrite config
FPSci/         FPSci source, for reference and the original parser
```

Two rules hold everywhere: **raw data is never modified**, and **anything
derived can be deleted and rebuilt** from a script. `analysis/` and
`sensors/data/processed/` are both disposable; `FPSci-bin/results/`,
`sensors/data/raw/` and `study/` are not.

`study/sittings.csv` is the odd one out and the most easily lost: it is the
only record of which warm-up condition was running, it is written by the
launcher rather than measured, and nothing can reconstruct it from the
databases. It sits outside `analysis/` because `--rebuild` deletes that tree --
which destroyed this file once.

## Where the numbers end up

```
analysis/
  gridshot/mokshagna/
    runs.csv        one row per run -- the headline table
    trials.csv
    shots.csv       per shot: aim, nearest target, angular error
    targets.csv
  _discarded/       runs the player answered "No" to
  _pending/         runs whose submit answer could not be read
  _index/
    runs.csv        the ledger: every run, where it went, headline metrics
    participants.csv
```

A run answered "No" is not deleted. Its raw database and its derived rows both
survive, just out of the way of the analysis tables.

## The four scenarios

Modelled on Aim Labs tasks, defined in `FPSci-bin/experimentconfig.Any`:

| id | task | shape |
|----|------|-------|
| `gridshot` | Gridshot Ultimate | 3 static targets, respawning, 60 s |
| `flicks` | Spidershot | pairs of targets across the whole screen, 3 s shot clock |
| `microadjust` | Microshot | 3 small static targets, precision, 60 s |
| `tracking` | Spheretrack | one moving target, hold-to-beam, 60 s |

## Docs

- **[docs/rewrite-plan.html](docs/rewrite-plan.html)** — why the harness looks
  like this. Read this before changing how data is collected.
- **[app/README.md](app/README.md)** — the harness, and three traps that will
  otherwise bite you.
- **[sensors/README.md](sensors/README.md)** — capture, and what each stream is.
- **[sensors/docs/](sensors/docs/)** — data format, the BLE protocol, and the join.

## Tests

```
python app/tests/run_all.py                    the harness
python sensors/tests/test_protocol.py          decoding, clock, HRV
python sensors/tests/test_end_to_end.py        a synthetic session, joined
python sensors/tests/test_multi_db.py          one sitting, several databases
python sensors/tests/test_shared_metrics.py    both consumers agree exactly
```

None of them touch Bluetooth, and none need recorded data -- they generate
what they need when `FPSci-bin/results` is empty. They run on a clean checkout.
