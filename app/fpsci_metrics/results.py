"""Reading one FPSci results database.

``Targets.size`` is the target's *diameter in metres*, despite the "(in
degrees)" comment on ``TargetConfig::size``. FPSciApp.cpp builds the target
model array with ``default_scale = 1.0f / extent[0]`` and the comment "Setup
scale so that default model is 1m across", so the config ``visualSize`` is a
world diameter. The on-target threshold is therefore computed per frame from
the true camera-to-target distance:

    angular_radius = degrees(atan((size / 2) / distance))

Note that ``eccH``/``eccV`` and ``speed`` *are* genuinely angular (degrees and
degrees/second) -- only ``visualSize`` is metric.
"""

from __future__ import annotations

import bisect
import math
import os
import sqlite3
from urllib.request import pathname2url

from .config import TRAJECTORY_MATCH_TOL_S
from .geometry import parse_time


class TargetTrack:
    """Per-frame trajectory for one target, with nearest-time lookup."""

    __slots__ = ("target_id", "times", "pos")

    def __init__(self, target_id):
        self.target_id = target_id
        self.times = []
        self.pos = []

    def finalize(self):
        order = sorted(range(len(self.times)), key=lambda i: self.times[i])
        self.times = [self.times[i] for i in order]
        self.pos = [self.pos[i] for i in order]

    def pos_at(self, t, tol=TRAJECTORY_MATCH_TOL_S):
        """Position at the sample nearest ``t``, or None if none is close enough.

        Returning None is how a target that had not spawned yet -- or was
        already dead -- drops out of the "which targets were live" set.
        """
        if not self.times:
            return None
        i = bisect.bisect_left(self.times, t)
        best, bestd = None, None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(self.times):
                d = abs(self.times[j] - t)
                if bestd is None or d < bestd:
                    best, bestd = j, d
        if best is None or bestd > tol:
            return None
        return self.pos[best]


class Results:
    """Everything we need out of one FPSci results database."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        # Read-only, always. Results databases are the raw record of a run and
        # the whole project rests on them being immutable; opening them
        # read-write would also let SQLite drop a journal file beside them.
        # The original parser opened these read-write -- that was safe only
        # because it worked on copies.
        uri = "file:" + pathname2url(os.path.abspath(path)) + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        self.con = con
        self.tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }

    def has(self, table):
        return table in self.tables

    def cols(self, table):
        if not self.has(table):
            return []
        return [r[1] for r in self.con.execute(f"PRAGMA table_info({table})")]

    def rows(self, table, order_by=None):
        if not self.has(table):
            return []
        q = f"SELECT * FROM {table}"
        if order_by:
            q += f" ORDER BY {order_by}"
        return [dict(r) for r in self.con.execute(q)]

    # -- loading ----------------------------------------------------------

    def load(self):
        self.trials = self.rows("Trials")
        self.sessions = self.rows("Sessions")
        self.users = self.rows("Users")
        # Answers to the in-app "questions" dialogs. Empty on databases recorded
        # before any question was configured, which rows() handles by returning [].
        self.questions = self.rows("Questions")

        self.actions = []
        for r in self.rows("Player_Action", order_by="time"):
            t = parse_time(r.get("time"))
            if t is None:
                continue
            r["_t"] = t
            self.actions.append(r)
        self.action_times = [r["_t"] for r in self.actions]

        self.tracks = {}
        for r in self.rows("Target_Trajectory"):
            t = parse_time(r.get("time"))
            if t is None:
                continue
            tid = r.get("target_id")
            # The reference target is the click-to-start sphere, not a task
            # target. It sits ~1 m dead ahead, so leaving it in would let it
            # win the nearest-target search and corrupt every error metric.
            if tid == "reference" or r.get("state") == "referenceTarget":
                continue
            tr = self.tracks.get(tid)
            if tr is None:
                tr = self.tracks[tid] = TargetTrack(tid)
            tr.times.append(t)
            tr.pos.append((r.get("position_x"), r.get("position_y"), r.get("position_z")))
        for tr in self.tracks.values():
            tr.finalize()

        self.targets = {}
        for r in self.rows("Targets"):
            r["_spawn_t"] = parse_time(r.get("spawn_time"))
            self.targets[r.get("target_id")] = r

        self.frames = []
        for r in self.rows("Frame_Info", order_by="time"):
            t = parse_time(r.get("time"))
            if t is None:
                continue
            self.frames.append((t, r.get("sdt")))

    def close(self):
        self.con.close()

    def actions_between(self, t0, t1):
        lo = bisect.bisect_left(self.action_times, t0)
        hi = bisect.bisect_right(self.action_times, t1)
        return self.actions[lo:hi]

    def target_size_m(self, target_id):
        """Logged target diameter in metres, or None if unknown."""
        row = self.targets.get(target_id)
        if not row:
            return None
        try:
            return float(row["size"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def angular_radius(size_m, distance_m):
        """Angular radius in degrees of a sphere of diameter ``size_m``.

        Computed from the real camera-to-target distance, so it stays correct
        for any target distance and needs no assumption about the config.
        """
        if not size_m or not distance_m or distance_m <= 0:
            return None
        return math.degrees(math.atan((size_m / 2.0) / distance_m))


def collect_dbs(target):
    if os.path.isdir(target):
        return sorted(
            os.path.join(target, f) for f in os.listdir(target)
            if f.lower().endswith(".db")
        )
    return [target]
