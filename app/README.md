# app

The harness around FPSci. See `docs/rewrite-plan.html` for why it looks like this.

## Running a session

```
python app/run_study.py
```

Pick the participant, pick the warm-up, hit Launch. Play, answer "Submit this
run?" after each run, and quit FPSci through the menu when you are done. Runs
export themselves a few seconds after each one finishes, and the last one is
picked up when the game closes.

The warm-up dropdown stays live the whole time. Change it whenever you like --
each run is stamped with whatever is selected when it starts, so you never
answer it twice and never have to decide up front.

```
python app/export.py                export runs that have not been exported yet
python app/tests/run_all.py         every test suite
python app/check_phase1.py          verify the config flip, from the databases
```

Launching `FirstPersonScience.exe` directly still works. You lose the warm-up
label and the automatic export; `python app/export.py` afterwards recovers the
export, but not the label.

## Where data goes

```
FPSci-bin/results/                  raw, one database per run, never modified
  <scenario>_<user>_<UTC>.db
  _stubs/                           gameplay-free files, after --sweep-stubs

analysis/                           derived, delete and rebuild at any time
  <scenario>/<person>/
    runs.csv        1 row per run
    trials.csv
    shots.csv
    targets.csv
  _discarded/<scenario>/<person>/   runs the player answered "No" to
  _pending/<scenario>/<person>/     runs whose submit answer could not be read
  _index/
    runs.csv        the ledger: every run, where it went, headline metrics
    participants.csv  id, name, folder slug, mouse DPI and deg/mm

study/                              NOT derived, never deleted
  sittings.csv      warm-up condition per stretch, written by the launcher
```

A run is never deleted for being discarded. It keeps its raw database and its
derived rows; it just sits out of the way of the analysis tables.

## export.py

```
python app/export.py                 # incremental: only databases not yet done
python app/export.py --rebuild       # wipe analysis/ and redo everything
python app/export.py --sweep-stubs   # also move menu stubs into results/_stubs/
python app/export.py --dry-run       # report, change nothing
```

Three properties are covered by `tests/test_export.py` and worth relying on:
re-running changes nothing, `--rebuild` twice is byte-identical, and an
incremental export gives the same tree as a full rebuild.

That last one is why run numbers come from `_index/runs.csv` rather than from
the metric engine. The engine numbers sessions across whatever databases it was
handed, which is only correct when it is handed all of them, so exporting one
new run would otherwise renumber it as the first.

## Layout

```
app/
  run_study.py       the control panel: participant, warm-up, launch, export
  export.py          runs -> per-scenario, per-person CSVs
  check_phase1.py    Phase 1 verification
  fpsci_metrics/     the metric engine, importable
    config.py        tunables -- change one and every number downstream moves
    geometry.py      angles, timestamps, percentiles, velocity, submovements
    results.py       reading one results database
    sessions.py      numbering runs per subject across databases
    trial.py         analyse_trial: the full metric set for one trial
    aggregate.py     trials -> one row per run, and one row per session
    questions.py     decoding answers out of the Questions table
    anyconfig.py     reading .Any config safely (comments, keys, scalars)
    csvio.py         CSV writing, column order preserved
  tests/
    run_all.py       every suite in one go
    test_golden.py   old parser vs new module, diffed byte-for-byte
    test_export.py   every routing path, in a throwaway tree
    test_launcher.py preflight, generated configs, sittings, the watcher
```

## What the launcher owns

`run_study.py` regenerates `userstatus.Any` and `userconfig.Any` on every
launch. It does **not** touch `experimentconfig.Any` -- that stays
hand-maintained, and preflight checks it instead. Each check corresponds to
something that actually went wrong during the rewrite:

| Check | What it prevents |
|-------|------------------|
| apostrophes | an empty `Experiments` table, losing config provenance |
| `logToSingleDb` false | runs sharing one database again |
| `sessionFeedbackDuration` small | the whole end-of-session path going dead |
| submit prompt on all 4 scenarios | a run that can never be submitted |
| `randomOrder: false` | Yes and No swapping places between runs |
| no `commandsOnSessionStart` | the game shelling out to a parser mid-run |
| writable `userstatus.sessions.csv` | the repeat quota silently not working |

A preflight problem blocks the launch and says why.

```python
import sys; sys.path.insert(0, "app")
from fpsci_metrics import analyse

result = analyse("FPSci-bin/results")
for run in result.runs:
    print(run["scenario"], run["name"], run["accuracy_pct"])
```

`analyse` returns a `MetricSet` with `runs`, `trials`, `shots`, `targets` and
`sessions` as lists of dicts. It writes nothing -- where rows go is the
exporter's decision, not the engine's.

## The warm-up label

A run is stamped with the condition in force **when the run was played**, not
when the scenario was selected. FPSci opens the database the moment you pick a
scenario in the menu and the run can start a minute later, which is long enough
to change the dropdown in between. Anchoring on the session start recorded the
condition that was showing while the player was still on the click-to-start
screen. Observed live: a run played entirely under Breathing filed as Control.

`study/sittings.csv` holds one row per stretch of time under one condition.
Changing the dropdown closes the open stretch and opens a new one. It lives
outside `analysis/` because it cannot be re-derived and `--rebuild` deletes
that tree.

`--rebuild` now removes only the directories the exporter created, so anything
else in `analysis/` survives.

## Three things that will bite you

**`Questions.response` is the displayed label, not the option.** With
`optionKeys` set it reads `"Yes (Y)"`, never `"Yes"`. Use
`questions.clean_answer` / `questions.submit_state`, which map it back through
`presented_responses`. Anything unmappable becomes `unanswered` rather than a
guess, because this field decides whether a run reaches the analysis tables.

**Not every `.db` is a run.** FPSci opens a database when a scenario is
*selected*, so browsing the menu leaves stubs behind. Classify on gameplay, not
on the `Trials` count: a stub has zero `trialTask` frames, while a run
abandoned mid-trial has thousands and still no `Trials` row. The first is
disposable, the second is real data and `export.py` flags it for you to look at.

**FPSci rewrites `userstatus.Any` itself.** Now that sessions complete,
`saveUserConfig` runs at the end of every one and strips the file down to bare
values. Documentation cannot live there. It is why the launcher will generate
that file from a template rather than editing it in place.

## The golden test

`fpsci_metrics` is a refactor of `FPSci/scripts/analysis/fpsci_parse.py`, which
stays untouched as the reference implementation. `analyse_trial` and the
aggregation functions were extracted verbatim -- only imports moved.

The test runs both over the same databases and diffs the CSVs byte-for-byte, so
it catches a column that moved or a float that formats differently, not only a
value that changed.

It prefers real databases from `FPSci-bin/results` and falls back to synthetic
ones from `tests/synthetic.py` when there are none, so the whole suite stays
green on a clean checkout. A suite that fails when idle just teaches you to
ignore red. Real data is the better test; the synthetic set is the floor.
