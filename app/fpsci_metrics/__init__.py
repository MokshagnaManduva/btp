"""Aim-performance metrics from FPSci result databases.

The metric code here is a refactor of FPSci/scripts/analysis/fpsci_parse.py --
same geometry, same numbers, no CLI and no output layer. Where that script was
a program, this is a library the exporter calls:

    from fpsci_metrics import analyse

    result = analyse("FPSci-bin/results")
    for run in result.runs:
        print(run["scenario"], run["accuracy_pct"])

``analyse`` deliberately does not decide where anything is written. Routing
rows into per-scenario, per-person files is the exporter's job.
"""

from __future__ import annotations

import os
import sqlite3
import sys

from .aggregate import aggregate_sessions, build_runs
from .csvio import write_csv
from .questions import clean_answer, submit_state
from .results import Results, collect_dbs
from .sessions import attach_warmup, index_sessions
from .trial import analyse_trial

__all__ = [
    "analyse", "MetricSet", "Results", "collect_dbs", "write_csv",
    "clean_answer", "submit_state",
]


class MetricSet:
    """Everything derived from one call to :func:`analyse`.

    ``runs`` is the headline table: exactly one row per run, comparable across
    scenarios. ``sessions`` also carries runs with no completed trials, so an
    abandoned run stays visible instead of silently vanishing.
    """

    __slots__ = ("runs", "trials", "shots", "targets", "sessions", "databases")

    def __init__(self, runs, trials, shots, targets, sessions, databases):
        self.runs = runs
        self.trials = trials
        self.shots = shots
        self.targets = targets
        self.sessions = sessions
        self.databases = databases

    def __repr__(self):
        return ("MetricSet(runs=%d, trials=%d, shots=%d, targets=%d, "
                "sessions=%d, databases=%d)"
                % (len(self.runs), len(self.trials), len(self.shots),
                   len(self.targets), len(self.sessions), len(self.databases)))


def load(paths, on_error=None):
    """Open and read every database, skipping ones that cannot be used.

    Loading is a separate pass from analysis because session numbering spans
    all databases: a subject's Nth run is only knowable once every file is in
    hand.
    """
    report = on_error or (lambda msg: print(msg, file=sys.stderr))
    loaded = []
    for path in paths:
        try:
            res = Results(path)
            res.load()
        except sqlite3.Error as e:
            report(f"  !! {os.path.basename(path)}: {e}")
            continue
        if not res.has("Trials"):
            report(f"  -- {os.path.basename(path)}: no Trials table, skipping")
            continue
        loaded.append(res)
    return loaded


def analyse(target, on_error=None):
    """Derive every metric table from a database, or a directory of them.

    ``target`` may be a path to one .db, a directory containing them, or an
    already-collected list of paths.
    """
    if isinstance(target, (list, tuple)):
        paths = list(target)
    else:
        paths = collect_dbs(target)

    loaded = load(paths, on_error=on_error)
    if not loaded:
        return MetricSet([], [], [], [], [], [])

    insts = index_sessions(loaded)
    attach_warmup(insts, loaded)

    all_trials, all_shots, all_targets, all_sessions = [], [], [], []
    for res in loaded:
        tm = []
        for tr in res.trials:
            m = analyse_trial(res, tr, all_shots, all_targets, insts)
            if m:
                tm.append(m)
        all_trials.extend(tm)
        all_sessions.extend(aggregate_sessions(res, tm, insts))

    return MetricSet(build_runs(all_trials), all_trials, all_shots,
                     all_targets, all_sessions, loaded)
