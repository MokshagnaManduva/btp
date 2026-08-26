#!/usr/bin/env python3
"""Run a session: pick the participant, set the warm-up, play, export.

    python app/run_study.py

Opens a small control panel, launches FPSci, and exports each run a few
seconds after you finish it. The warm-up dropdown stays live for the whole
sitting -- change it whenever you like and the next run is stamped with the new
condition. Nothing has to be answered up front and nothing is asked twice.

What it writes:
  FPSci-bin/userstatus.Any     regenerated every launch -- FPSci rewrites this
                               file itself, so it cannot be hand-maintained
  FPSci-bin/userconfig.Any     mouse settings per participant
  study/sittings.csv           one row per stretch of time under one condition,
                               kept outside analysis/ because it is recorded
                               rather than derived and cannot be rebuilt

It does not touch experimentconfig.Any. That stays hand-maintained; the
preflight below checks it rather than overwriting it, so a regression is caught
before you play rather than after.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if HERE not in sys.path:
    sys.path.insert(0, HERE)

import export                                             # noqa: E402
from export import SCENARIOS, WARMUP_CONDITIONS, slugify  # noqa: E402
from fpsci_metrics import anyconfig                       # noqa: E402

BIN = os.path.join(ROOT, "FPSci-bin")
EXE = os.path.join(BIN, "FirstPersonScience.exe")
RESULTS = os.path.join(BIN, "results")
ANALYSIS = os.path.join(ROOT, "analysis")
EXPERIMENT_CONFIG = os.path.join(BIN, "experimentconfig.Any")
USER_STATUS = os.path.join(BIN, "userstatus.Any")
USER_CONFIG = os.path.join(BIN, "userconfig.Any")
SESSIONS_LOG = os.path.join(BIN, "userstatus.sessions.csv")

#: How many times each scenario may be replayed before it leaves the menu.
#: Generous on purpose: the quota exists to keep scenarios available, not to
#: cap anyone. Answering "No" to the submit prompt still uses one up.
REPEAT_QUOTA = 50

#: Defaults for a participant we have never seen before.
DEFAULT_DPI = 800
DEFAULT_DEG_PER_MM = 1.2

#: A database is considered finished once FPSci has moved on to another one and
#: its size has stopped changing for this long.
SETTLE_S = 4.0
POLL_S = 2.0

TIME_FMT = "%Y-%m-%d %H:%M:%S.%f"


def utc_now():
    return datetime.now(timezone.utc).strftime(TIME_FMT)


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def preflight(exe=EXE, config=EXPERIMENT_CONFIG, sessions_log=SESSIONS_LOG):
    """Check the things that quietly ruin a session. Returns a list of problems.

    Every one of these has actually happened during the rewrite, which is why
    they are checked before launching rather than discovered afterwards in the
    data. Paths are arguments so the checks can be tested against deliberately
    broken configs.
    """
    problems = []

    if not os.path.exists(exe):
        problems.append("FirstPersonScience.exe is missing from FPSci-bin")

    if not os.path.exists(config):
        problems.append("experimentconfig.Any is missing")
        return problems

    raw = open(config, encoding="utf-8").read()

    # Deliberately on the raw text: FPSci embeds the file verbatim, so an
    # apostrophe inside a comment breaks the insert just as a real one would.
    if "'" in raw:
        problems.append(
            "experimentconfig.Any contains %d apostrophe(s). FPSci splices this "
            "file into SQL without escaping, so the Experiments table will be "
            "left empty and you lose the record of which config produced a run."
            % raw.count("'"))

    if anyconfig.scalar(raw, "logToSingleDb") != "false":
        problems.append("logToSingleDb is not false -- runs will share one "
                        "database instead of getting one each")

    if anyconfig.count_key(raw, "commandsOnSessionStart"):
        problems.append("commandsOnSessionStart is set -- the launcher exports, "
                        "so the game should not shell out to a parser")

    prompts = anyconfig.strip_comments(raw).count('"Submit this run?"')
    if prompts != len(SCENARIOS):
        problems.append(
            "the submit prompt is on %d of %d scenarios -- a run without it can "
            "never be submitted" % (prompts, len(SCENARIOS)))

    if anyconfig.strip_comments(raw).count('"randomOrder": false') < len(SCENARIOS):
        problems.append('a submit prompt is missing "randomOrder": false, so '
                        'Yes and No will swap places between runs')

    feedback = anyconfig.scalar(raw, "sessionFeedbackDuration")
    if feedback is not None:
        try:
            value = float(feedback)
        except ValueError:
            value = None
        if value is not None and value > 60:
            problems.append(
                "sessionFeedbackDuration is %g s. Anything large disables the "
                "whole end-of-session path: no flush, no submit prompt, no "
                "completion." % value)

    if os.path.exists(sessions_log) and not os.access(sessions_log, os.W_OK):
        problems.append("userstatus.sessions.csv is read-only -- completions "
                        "cannot be logged and the repeat quota will not work")

    return problems


# --------------------------------------------------------------------------
# Config generation
# --------------------------------------------------------------------------

def any_string_array(values, indent):
    pad = " " * indent
    return ("[" + chr(10) + pad + ("," + chr(10) + pad).join(
        '"%s"' % v for v in values) + chr(10) + " " * (indent - 4) + "]")


def write_user_status(current, participants, path=USER_STATUS):
    """Regenerate userstatus.Any.

    FPSci rewrites this file itself at the end of every session, stripping any
    comment, so it is generated rather than edited. allowRepeat plus a long
    session list is what keeps every scenario on the menu.
    """
    order = []
    for scenario in SCENARIOS:
        order.extend([scenario] * REPEAT_QUOTA)

    names = list(dict.fromkeys([current] + list(participants)))
    users = []
    for name in names:
        users.append('        {' + chr(10)
                     + '            "id": "%s",' % name + chr(10)
                     + '            "sessions": %s' % any_string_array(order, 16)
                     + chr(10) + '        }')

    body = ('{' + chr(10)
            + '    "settingsVersion": 1,' + chr(10)
            + '    "allowRepeat": true,' + chr(10)
            + '    "currentUser": "%s",' % current + chr(10)
            + '    "sessions": %s,' % any_string_array(order, 8) + chr(10)
            + '    "users": [' + chr(10)
            + ("," + chr(10)).join(users) + chr(10)
            + '    ]' + chr(10)
            + '}' + chr(10))
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def write_user_config(participants, path=USER_CONFIG):
    """Regenerate userconfig.Any from the registry, keeping mouse settings."""
    users = []
    for name, entry in participants.items():
        dpi = entry.get("mouse_dpi") or DEFAULT_DPI
        dmm = entry.get("mouse_deg_per_mm") or DEFAULT_DEG_PER_MM
        users.append('        {' + chr(10)
                     + '            "id": "%s",' % name + chr(10)
                     + '            "mouseDPI": %s,' % dpi + chr(10)
                     + '            "mouseDegPerMillimeter": %s' % dmm + chr(10)
                     + '        }')
    body = ('{' + chr(10)
            + '    "settingsVersion": 1,' + chr(10)
            + '    "users": [' + chr(10)
            + ("," + chr(10)).join(users) + chr(10)
            + '    ]' + chr(10)
            + '}' + chr(10))
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


# --------------------------------------------------------------------------
# Sittings
# --------------------------------------------------------------------------

SITTING_COLUMNS = ("run_id", "name", "warmup", "start_utc", "end_utc")


class SittingLog:
    """One row per stretch of time a participant spent under one condition.

    Changing the dropdown closes the open stretch and starts a new one, so a
    run is attributed by when it was played rather than by anything the player
    has to remember afterwards.
    """

    def __init__(self, sittings_dir=None):
        self.path = export.sittings_path(sittings_dir)
        self.open_row = None

    def start(self, name, warmup):
        self.close()
        stamp = utc_now()
        self.open_row = {
            "run_id": "%s_%s" % (slugify(name), stamp.replace(":", "").replace(" ", "_")),
            "name": name,
            "warmup": warmup,
            "start_utc": stamp,
            "end_utc": "",
        }
        self._flush()

    def close(self):
        if not self.open_row:
            return
        self.open_row["end_utc"] = utc_now()
        self._flush()
        self.open_row = None

    def _flush(self):
        if not self.open_row:
            return
        rows = [r for r in export.read_csv(self.path)
                if r.get("run_id") != self.open_row["run_id"]]
        rows.append(dict(self.open_row))
        rows.sort(key=lambda r: r.get("start_utc") or "")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        import csv as _csv
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=SITTING_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in SITTING_COLUMNS})


# --------------------------------------------------------------------------
# Watching for finished runs
# --------------------------------------------------------------------------

class Watcher(threading.Thread):
    """Export databases once FPSci is finished writing them.

    The database FPSci currently has open is always the newest, and it may hold
    a completed trial well before it holds the submit answer. So a file only
    counts as finished when the game has moved on to a newer one, or has quit.
    """

    def __init__(self, out, results=RESULTS, analysis=ANALYSIS):
        super().__init__(daemon=True)
        self.out = out
        self.results = results
        self.analysis = analysis
        self.stop_event = threading.Event()
        self.game_running = threading.Event()
        self.sizes = {}
        self.done = set()

    def log(self, msg):
        self.out.put(msg)

    def databases(self):
        if not os.path.isdir(self.results):
            return []
        return [os.path.join(self.results, f)
                for f in os.listdir(self.results) if f.lower().endswith(".db")]

    def settled(self):
        """Databases FPSci is no longer writing to.

        Once the game has exited every file is closed, so the size-stability
        wait is skipped entirely -- otherwise the last run of a sitting would
        sit unexported for two more polls while the process it was waiting on
        no longer exists.
        """
        dbs = self.databases()
        if not dbs:
            return []

        if not self.game_running.is_set():
            return [p for p in dbs if p not in self.done]

        newest = max(dbs, key=os.path.getmtime)
        out = []
        now = time.time()
        for path in dbs:
            if path == newest or path in self.done:
                continue
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            seen = self.sizes.get(path)
            if seen and seen[0] == size and now - mtime >= SETTLE_S:
                out.append(path)
            self.sizes[path] = (size, mtime)
        return out

    def sweep(self, final=False):
        ready = self.settled()
        if not ready:
            return
        summary = export.run_export(self.results, self.analysis, only=ready,
                                    sweep=final, log=self.log)
        for row in summary["exported"]:
            self.done.add(os.path.join(self.results, row["run_id"] + ".db"))
            verdict = {"yes": "submitted", "no": "discarded",
                       "unanswered": "no answer -- left pending"}[row["submitted"]]
            self.log("  %s  %s  %s hits/%s shots  ->  %s"
                     % (row["scenario"], verdict, row["shots_hit"],
                        row["shots_fired"], verdict))
        if summary["moved"]:
            self.log("  tidied %d menu stub(s) into results/_stubs/"
                     % summary["moved"])

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.sweep()
            except Exception as e:                      # keep the sitting alive
                self.log("  [warn] export failed: %s" % e)
            self.stop_event.wait(POLL_S)


# --------------------------------------------------------------------------
# Control panel
# --------------------------------------------------------------------------

def build_gui():
    import tkinter as tk
    from tkinter import ttk

    messages = queue.Queue()
    state = {"proc": None, "watcher": None, "sitting": None}

    root = tk.Tk()
    root.title("FPSci study")
    root.geometry("620x460")
    root.minsize(520, 380)

    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    registry = export.load_participants(ANALYSIS)
    names = sorted(registry.keys())

    ttk.Label(frame, text="Participant").grid(row=0, column=0, sticky="w", pady=4)
    participant = ttk.Combobox(frame, values=names)
    participant.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=4)
    if names:
        participant.set(names[0])

    ttk.Label(frame, text="Warm-up").grid(row=1, column=0, sticky="w", pady=4)
    warmup = ttk.Combobox(frame, values=list(WARMUP_CONDITIONS), state="readonly")
    warmup.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)
    warmup.set(WARMUP_CONDITIONS[0])

    hint = ttk.Label(
        frame, foreground="#555",
        text="Change the warm-up whenever you like. Each run is stamped with "
             "whatever is\nselected when it starts, so you never answer it twice.")
    hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))

    buttons = ttk.Frame(frame)
    buttons.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))
    launch_btn = ttk.Button(buttons, text="Launch FPSci")
    launch_btn.pack(side="left")
    quit_btn = ttk.Button(buttons, text="Close", command=root.destroy)
    quit_btn.pack(side="left", padx=(8, 0))

    log = tk.Text(frame, height=14, wrap="word", state="disabled",
                  font=("Consolas", 9))
    log.grid(row=4, column=0, columnspan=2, sticky="nsew")
    frame.rowconfigure(4, weight=1)

    def say(msg):
        log.configure(state="normal")
        log.insert("end", msg + chr(10))
        log.see("end")
        log.configure(state="disabled")

    def on_warmup_change(_event=None):
        name = participant.get().strip()
        if state["sitting"] and name:
            state["sitting"].start(name, warmup.get())
            say("warm-up is now %s" % warmup.get())

    warmup.bind("<<ComboboxSelected>>", on_warmup_change)

    def launch():
        name = participant.get().strip()
        if not name:
            say("Enter a participant id first.")
            return
        if state["proc"] and state["proc"].poll() is None:
            say("FPSci is already running.")
            return

        problems = preflight()
        if problems:
            say("Cannot start -- fix these first:")
            for p in problems:
                say("  * " + p)
            return

        reg = export.load_participants(ANALYSIS)
        export.ensure_participant(reg, name)
        export.write_participants(
            os.path.join(ANALYSIS, export.INDEX_DIR, "participants.csv"), reg)

        write_user_status(name, reg.keys())
        write_user_config(reg)
        say("configs written for %s (quota %d per scenario)" % (name, REPEAT_QUOTA))

        state["sitting"] = SittingLog()
        state["sitting"].start(name, warmup.get())
        say("sitting open: %s, warm-up %s" % (name, warmup.get()))

        watcher = state["watcher"]
        if watcher is None:
            watcher = state["watcher"] = Watcher(messages)
            watcher.start()
        watcher.game_running.set()

        state["proc"] = subprocess.Popen([EXE], cwd=BIN)
        launch_btn.configure(text="FPSci running", state="disabled")
        participant.configure(state="disabled")
        say("FPSci launched. Play, answer the submit prompt, and quit through "
            "the menu when you are done.")

    launch_btn.configure(command=launch)

    def pump():
        while True:
            try:
                say(messages.get_nowait())
            except queue.Empty:
                break

        proc = state["proc"]
        if proc and proc.poll() is not None:
            state["proc"] = None
            watcher = state["watcher"]
            if watcher:
                watcher.game_running.clear()
                say("FPSci closed. Exporting the last run...")
                try:
                    watcher.sweep(final=True)
                except Exception as e:
                    say("  [warn] final export failed: %s" % e)
            if state["sitting"]:
                state["sitting"].close()
                state["sitting"] = None
            launch_btn.configure(text="Launch FPSci", state="normal")
            participant.configure(state="normal")
            say("Done. CSVs are in analysis/<scenario>/<person>/.")

        root.after(500, pump)

    def on_close():
        if state["watcher"]:
            state["watcher"].stop_event.set()
        if state["sitting"]:
            state["sitting"].close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    problems = preflight()
    if problems:
        say("Preflight found problems:")
        for p in problems:
            say("  * " + p)
    else:
        say("Preflight OK. Pick a participant and a warm-up, then launch.")

    root.after(500, pump)
    return root


def main():
    root = build_gui()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
