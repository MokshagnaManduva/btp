"""On-disk format: timestamps, CSV streams, and the session manifest.

The single most important decision in this whole toolkit lives here:

    Every row of every file is stamped with UTC in FPSci's exact string format,
    "%Y-%m-%d %H:%M:%S.%f".

FPSci writes its result timestamps with GetSystemTimePreciseAsFileTime() and
formats them with FileTimeToSystemTime() (see FPSci/source/Logger.cpp), which
means they are UTC, not local time -- a detail that will silently ruin an
analysis if you assume otherwise.  Matching that format byte-for-byte means
correlating heart rate with gameplay is a plain comparison of two strings (or of
the Unix floats beside them), with no timezone conversion anywhere.
"""

from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone

# FPSci's timestamp format, verified against FPSci/source/Logger.cpp:17-27.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def now_unix() -> float:
    return time.time()


def utc_string(t_unix: float) -> str:
    """Format a Unix timestamp the way FPSci formats its own timestamps."""
    return datetime.fromtimestamp(t_unix, tz=timezone.utc).strftime(TIME_FORMAT)


def parse_utc(text: str) -> float:
    """Parse an FPSci / Polar timestamp string back into Unix seconds (UTC)."""
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")
    if "." not in text:                     # tolerate a whole-second timestamp
        text += ".000000"
    dt = datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)
    return dt.timestamp()


def session_id(t_unix: float, device_id: str) -> str:
    """e.g. 20260820-154720Z_184CB835 -- sorts chronologically, names the device."""
    stamp = datetime.fromtimestamp(t_unix, tz=timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return stamp + "_" + device_id


# --------------------------------------------------------------------------
# Stream schemas
# --------------------------------------------------------------------------
# Columns common to every stream:
#   t_unix  best estimate of the sample's true time, Unix seconds UTC
#   utc     the same instant as an FPSci-format string
# Raw streams additionally carry device_ts_ns, the sensor's own clock, so that
# t_unix can be recomputed offline from the fitted clock model in session.json.

SCHEMAS = {
    "hr": ["t_unix", "utc", "hr_bpm", "contact", "energy_j", "n_rr"],
    "rr": ["t_unix", "utc", "beat_index", "rr_ms", "hr_bpm",
           "t_cumulative_unix", "arrival_unix"],
    "ecg": ["t_unix", "utc", "device_ts_ns", "ecg_uv"],
    "acc": ["t_unix", "utc", "device_ts_ns", "acc_x_mg", "acc_y_mg", "acc_z_mg"],
    "markers": ["t_unix", "utc", "label"],
}


class CsvStream:
    """Append-only CSV writer that flushes on a timer.

    Data is written as it arrives rather than buffered to the end of the
    session, so a crash or a dead battery costs you the last second at most.
    """

    def __init__(self, path: str, columns, flush_interval: float = 2.0):
        self.path = path
        self.columns = list(columns)
        self.flush_interval = flush_interval
        self.rows_written = 0
        self._last_flush = time.monotonic()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(self.columns)
        self._fh.flush()

    def write(self, *values) -> None:
        self._writer.writerow(values)
        self.rows_written += 1
        now = time.monotonic()
        if now - self._last_flush >= self.flush_interval:
            self._fh.flush()
            self._last_flush = now

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

_INT_COLUMNS = {"beat_index", "device_ts_ns", "ecg_uv", "n_rr",
                "acc_x_mg", "acc_y_mg", "acc_z_mg", "hr_bpm", "energy_j"}


def _convert(column: str, value: str):
    if value == "":
        return None
    if column in ("utc", "label", "contact"):
        return value
    try:
        return int(value) if column in _INT_COLUMNS else float(value)
    except ValueError:
        return value


def read_stream(path: str):
    """Read one recorded CSV into a list of dicts with numeric columns typed."""
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k: _convert(k, v) for k, v in row.items()}
                for row in csv.DictReader(fh)]


def stream_path(session_dir: str, name: str) -> str:
    return os.path.join(session_dir, name + ".csv")


def has_stream(session_dir: str, name: str) -> bool:
    return os.path.exists(stream_path(session_dir, name))


def apply_clock_fit(rows, fit) -> int:
    """Recompute t_unix / utc from device_ts_ns using a fitted clock model.

    The recorder timestamps live rows with a running-minimum offset, which is
    still converging during the first seconds of a session.  Re-deriving the
    times from the final fit makes the whole file internally consistent and
    corrects for clock drift.  Rows without a device timestamp (heart rate and
    RR, which the standard GATT service does not timestamp) are left alone.
    """
    n = 0
    for row in rows:
        ts = row.get("device_ts_ns")
        if ts is None:
            continue
        t = fit.apply(ts / 1e9)
        row["t_unix"] = t
        row["utc"] = utc_string(t)
        n += 1
    return n


# --------------------------------------------------------------------------
# Session manifest
# --------------------------------------------------------------------------

MANIFEST_NAME = "session.json"


def write_manifest(session_dir: str, manifest: dict) -> str:
    path = os.path.join(session_dir, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def read_manifest(session_dir: str) -> dict:
    path = os.path.join(session_dir, MANIFEST_NAME)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def find_latest_session(root: str) -> str:
    """Newest directory under ``root`` that contains a session manifest."""
    if not os.path.isdir(root):
        raise FileNotFoundError("no recordings directory at " + root)
    candidates = [
        os.path.join(root, name) for name in os.listdir(root)
        if os.path.exists(os.path.join(root, name, MANIFEST_NAME))
    ]
    if not candidates:
        raise FileNotFoundError("no recorded sessions under " + root)
    return max(candidates, key=lambda p: os.path.basename(p))
