"""Live BLE recorder for the Polar H10.

Connects to the strap, subscribes to heart rate + raw ECG + accelerometer, and
streams everything to CSV with UTC timestamps that line up with FPSci's logs.

Requires bleak:  python -m pip install bleak
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

from . import fileio
from . import protocol as p
from .clock import ClockModel

DEFAULT_STREAMS = ("hr", "rr", "ecg", "acc")

# Rough BLE transport delay for a heart-rate notification.  The RR intervals
# themselves are exact; this only shifts where the beat train is anchored in
# wall-clock time.  Left at zero by default -- see docs/data-format.md.
DEFAULT_HR_LATENCY_MS = 0.0


def _require_bleak():
    try:
        import bleak  # noqa: F401
    except ImportError:
        raise SystemExit(
            "bleak is not installed.\n"
            "    python -m pip install -r sensors/requirements.txt"
        )
    return bleak


@dataclass
class RecorderConfig:
    device_id: str = "184CB835"
    out_root: str = "data/raw"
    streams: tuple = DEFAULT_STREAMS
    acc_rate_hz: int = 50
    acc_range_g: int = 8
    duration_s: object = None          # float seconds, or None for "until stopped"
    subject: str = ""
    note: str = ""
    scan_timeout: float = 15.0
    address: object = None             # skip scanning if given
    hr_latency_ms: float = DEFAULT_HR_LATENCY_MS

    @property
    def wants(self):
        return set(self.streams)


async def find_device(device_id: str, timeout: float = 15.0):
    """Locate the strap by the id printed on it (also part of its BLE name)."""
    _require_bleak()
    from bleak import BleakScanner

    needle = device_id.strip().upper()
    print("Scanning for Polar %s (up to %.0f s)..." % (needle, timeout))
    devices = await BleakScanner.discover(timeout=timeout)
    for dev in devices:
        name = (dev.name or "").upper()
        if needle in name or needle == (dev.address or "").replace(":", "").upper():
            print("Found %s  [%s]" % (dev.name, dev.address))
            return dev

    seen = ", ".join(sorted(d.name for d in devices if d.name)) or "nothing"
    raise SystemExit(
        "Could not find Polar %s.  Saw: %s\n"
        "  - Is the strap moistened and worn?  It only advertises when it can\n"
        "    read a signal.\n"
        "  - Is another app (Polar Flow / Beat) already connected?  The H10\n"
        "    accepts very few simultaneous connections.\n"
        "  - Is Bluetooth on?" % (needle, seen)
    )


class Recorder:
    """One recording session."""

    def __init__(self, cfg: RecorderConfig):
        self.cfg = cfg
        self.clock = ClockModel()
        self.streams = {}
        self.session_dir = ""
        self.started_unix = 0.0
        self.stopped_unix = 0.0
        self.device_info = {}
        self.counts = {"hr": 0, "rr": 0, "ecg": 0, "acc": 0, "markers": 0}
        self.errors = []
        self.pmd_started = []
        self.last_hr = None
        self.last_contact = None
        self._beat_index = 0
        self._beat_clock = None          # cumulative RR timeline anchor
        self._stop = None                # asyncio.Event, created in run()
        self._control_q = None
        self._client = None
        self._delta_warned = False

    # -- output ----------------------------------------------------------

    def _open_outputs(self):
        self.started_unix = fileio.now_unix()
        sid = fileio.session_id(self.started_unix, self.cfg.device_id)
        self.session_dir = os.path.join(self.cfg.out_root, sid)
        os.makedirs(self.session_dir, exist_ok=True)

        wanted = list(self.cfg.wants) + ["markers"]
        for name in ("hr", "rr", "ecg", "acc", "markers"):
            if name in wanted:
                self.streams[name] = fileio.CsvStream(
                    fileio.stream_path(self.session_dir, name), fileio.SCHEMAS[name])
        return sid

    def mark(self, label: str, t_unix: float = None) -> None:
        """Write a labelled instant into markers.csv."""
        stream = self.streams.get("markers")
        if stream is None:
            return
        t = fileio.now_unix() if t_unix is None else t_unix
        stream.write(_f(t), fileio.utc_string(t), label)
        self.counts["markers"] += 1

    # -- notification handlers -------------------------------------------

    def _on_hr(self, _sender, data: bytearray) -> None:
        arrival = fileio.now_unix()
        try:
            sample = p.decode_hr_measurement(bytes(data))
        except Exception as exc:                      # keep the stream alive
            self.errors.append("hr decode: %r" % (exc,))
            return

        self.last_hr = sample.hr_bpm
        self.last_contact = sample.contact_detected

        hr_stream = self.streams.get("hr")
        if hr_stream is not None:
            hr_stream.write(_f(arrival), fileio.utc_string(arrival), sample.hr_bpm,
                            _contact(sample), _blank(sample.energy_j),
                            len(sample.rr_ms))
            self.counts["hr"] += 1

        rr_stream = self.streams.get("rr")
        if rr_stream is None or not sample.rr_ms:
            return

        # Back-date the beats in this notification: the newest RR interval ends
        # at (arrival - transport latency), each earlier one ends one interval
        # before the beat that follows it.
        latency = self.cfg.hr_latency_ms / 1000.0
        t_last = arrival - latency
        offsets = []
        acc = 0.0
        for rr in reversed(sample.rr_ms):
            offsets.append(acc)
            acc += rr / 1000.0
        offsets.reverse()                              # oldest first
        beat_times = [t_last - off for off in offsets]

        if self._beat_clock is None:
            self._beat_clock = beat_times[0]

        for rr, t_beat in zip(sample.rr_ms, beat_times):
            self._beat_clock += rr / 1000.0
            rr_stream.write(_f(t_beat), fileio.utc_string(t_beat),
                            self._beat_index, round(rr, 3),
                            round(60000.0 / rr, 3) if rr else "",
                            _f(self._beat_clock), _f(arrival))
            self._beat_index += 1
            self.counts["rr"] += 1

    def _on_pmd_data(self, _sender, data: bytearray) -> None:
        arrival = fileio.now_unix()
        try:
            frame = p.decode_pmd_frame(bytes(data))
        except Exception as exc:
            self.errors.append("pmd decode: %r" % (exc,))
            return

        if frame.delta_coded and not self._delta_warned:
            self._delta_warned = True
            self.errors.append("sensor is sending delta-compressed frames")

        rate = p.ECG_SAMPLE_RATE if frame.meas_type == p.ECG else self.cfg.acc_rate_hz
        self.clock.observe(frame.device_ts_s, arrival)
        times = p.frame_sample_times(frame, rate)

        if frame.meas_type == p.ECG:
            stream = self.streams.get("ecg")
            if stream is None:
                return
            for dev_s, value in zip(times, frame.samples):
                t = self.clock.to_unix(dev_s)
                stream.write(_f(t), fileio.utc_string(t), int(dev_s * 1e9), value)
            self.counts["ecg"] += len(frame.samples)

        elif frame.meas_type == p.ACC:
            stream = self.streams.get("acc")
            if stream is None:
                return
            for dev_s, (x, y, z) in zip(times, frame.samples):
                t = self.clock.to_unix(dev_s)
                stream.write(_f(t), fileio.utc_string(t), int(dev_s * 1e9), x, y, z)
            self.counts["acc"] += len(frame.samples)

    def _on_control(self, _sender, data: bytearray) -> None:
        resp = p.decode_control_response(bytes(data))
        if resp is not None and self._control_q is not None:
            self._control_q.put_nowait(resp)

    # -- PMD control ------------------------------------------------------

    async def _command(self, payload: bytes, timeout: float = 8.0):
        while not self._control_q.empty():             # drop stale responses
            self._control_q.get_nowait()
        await self._client.write_gatt_char(p.PMD_CONTROL, payload, response=True)
        return await asyncio.wait_for(self._control_q.get(), timeout)

    async def _start_measurement(self, name: str, meas_type: int, payload: bytes):
        try:
            resp = await self._command(payload)
            resp.raise_for_status()
            self.pmd_started.append(meas_type)
            print("  %s stream started" % name)
        except Exception as exc:
            # A failed raw stream must not cost you the heart-rate recording.
            msg = "%s stream could not start: %s" % (name, exc)
            print("  ! " + msg)
            self.errors.append(msg)

    async def _read_device_info(self):
        info = {}
        for key, uuid in (("model", p.MODEL_NUMBER),
                          ("manufacturer", p.MANUFACTURER_NAME),
                          ("serial", p.SERIAL_NUMBER),
                          ("firmware", p.FIRMWARE_REVISION),
                          ("hardware", p.HARDWARE_REVISION)):
            try:
                raw = await self._client.read_gatt_char(uuid)
                info[key] = bytes(raw).decode("utf-8", "replace").strip("\x00").strip()
            except Exception:
                pass
        try:
            info["battery_pct"] = int((await self._client.read_gatt_char(p.BATTERY_LEVEL))[0])
        except Exception:
            pass
        return info

    # -- main loop --------------------------------------------------------

    async def run(self) -> str:
        _require_bleak()
        from bleak import BleakClient

        device = self.cfg.address or await find_device(self.cfg.device_id,
                                                       self.cfg.scan_timeout)
        self._stop = asyncio.Event()
        self._control_q = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _on_disconnect(_client):
            self.errors.append("sensor disconnected")
            loop.call_soon_threadsafe(self._stop.set)

        # Turn Ctrl+C into a normal stop request rather than an exception, so
        # the measurements are stopped cleanly and the manifest still gets
        # written.  add_signal_handler is unavailable on the Windows proactor
        # loop, hence the plain signal handler.
        import signal
        previous_sigint = None
        try:
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT,
                          lambda *_: loop.call_soon_threadsafe(self._stop.set))
        except (ValueError, OSError):
            previous_sigint = None

        async with BleakClient(device, disconnected_callback=_on_disconnect) as client:
            self._client = client
            self.device_info = await self._read_device_info()
            print("Connected: %s fw %s, battery %s%%" % (
                self.device_info.get("model", "Polar H10"),
                self.device_info.get("firmware", "?"),
                self.device_info.get("battery_pct", "?")))

            sid = self._open_outputs()
            self.mark("session_start", self.started_unix)
            if self.cfg.note:
                self.mark("note: " + self.cfg.note, self.started_unix)

            if {"hr", "rr"} & self.cfg.wants:
                await client.start_notify(p.HR_MEASUREMENT, self._on_hr)
                print("  heart rate + RR stream started")

            if {"ecg", "acc"} & self.cfg.wants:
                await client.start_notify(p.PMD_CONTROL, self._on_control)
                await client.start_notify(p.PMD_DATA, self._on_pmd_data)
                if "ecg" in self.cfg.wants:
                    await self._start_measurement("ECG 130 Hz", p.ECG,
                                                  p.ecg_start_command())
                if "acc" in self.cfg.wants:
                    await self._start_measurement(
                        "ACC %d Hz" % self.cfg.acc_rate_hz, p.ACC,
                        p.acc_start_command(self.cfg.acc_rate_hz, self.cfg.acc_range_g))

            self._banner(sid)
            await self._wait_for_stop()

            for meas_type in self.pmd_started:
                try:
                    await self._command(p.stop_command(meas_type), timeout=4.0)
                except Exception:
                    pass
            for uuid in (p.HR_MEASUREMENT, p.PMD_DATA, p.PMD_CONTROL):
                try:
                    await client.stop_notify(uuid)
                except Exception:
                    pass

            self.stopped_unix = fileio.now_unix()
            self.mark("session_stop", self.stopped_unix)

        if previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint)
            except (ValueError, OSError):
                pass
        return self._finalize()

    def _banner(self, sid: str) -> None:
        print("")
        print("=" * 68)
        print("  RECORDING -> %s" % self.session_dir)
        print("  Start your FPSci session now; clocks are already aligned.")
        print("  [m] drop a marker    [q] or Ctrl+C to stop")
        if self.cfg.duration_s:
            print("  Stopping automatically after %.0f s" % self.cfg.duration_s)
        print("=" * 68)
        print("")

    async def _wait_for_stop(self) -> None:
        tasks = [asyncio.create_task(self._status_task()),
                 asyncio.create_task(self._key_task())]
        try:
            if self.cfg.duration_s:
                try:
                    await asyncio.wait_for(self._stop.wait(), self.cfg.duration_s)
                except asyncio.TimeoutError:
                    pass
            else:
                await self._stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            sys.stdout.write("\n")

    async def _status_task(self) -> None:
        while True:
            elapsed = fileio.now_unix() - self.started_unix
            contact = {True: "ok", False: "POOR", None: "?"}[self.last_contact]
            sys.stdout.write(
                "\r  %5.0fs | HR %3s bpm | beats %4d | ecg %6d | acc %6d | contact %-4s "
                % (elapsed, self.last_hr if self.last_hr else "--",
                   self.counts["rr"], self.counts["ecg"], self.counts["acc"], contact))
            sys.stdout.flush()
            await asyncio.sleep(1.0)

    async def _key_task(self) -> None:
        """Non-blocking keyboard polling (Windows console)."""
        try:
            import msvcrt
        except ImportError:
            return
        while True:
            while msvcrt.kbhit():
                key = msvcrt.getch().decode("utf-8", "ignore").lower()
                if key == "q":
                    self._stop.set()
                    return
                if key == "m":
                    self.mark("manual_marker")
                    sys.stdout.write("\n  marker %d written\n" % self.counts["markers"])
            await asyncio.sleep(0.05)

    def _finalize(self) -> str:
        for stream in self.streams.values():
            stream.close()

        fit = self.clock.fit()
        duration = self.stopped_unix - self.started_unix
        manifest = {
            "schema_version": 1,
            "session_id": os.path.basename(self.session_dir),
            "device": dict({"device_id": self.cfg.device_id}, **self.device_info),
            "subject": self.cfg.subject,
            "note": self.cfg.note,
            "start_utc": fileio.utc_string(self.started_unix),
            "end_utc": fileio.utc_string(self.stopped_unix),
            "start_unix": self.started_unix,
            "end_unix": self.stopped_unix,
            "duration_s": round(duration, 3),
            "time_format": fileio.TIME_FORMAT,
            "time_zone": "UTC (matches FPSci result timestamps)",
            "streams": {
                "requested": list(self.cfg.streams),
                "ecg_sample_rate_hz": p.ECG_SAMPLE_RATE if "ecg" in self.cfg.wants else None,
                "acc_sample_rate_hz": self.cfg.acc_rate_hz if "acc" in self.cfg.wants else None,
                "acc_range_g": self.cfg.acc_range_g if "acc" in self.cfg.wants else None,
                "hr_latency_ms": self.cfg.hr_latency_ms,
            },
            "counts": dict(self.counts),
            "clock_fit": fit.to_dict(),
            "errors": self.errors,
        }
        if duration > 0:
            manifest["effective_rates_hz"] = {
                key: round(value / duration, 3)
                for key, value in self.counts.items() if key != "markers"
            }
        fileio.write_manifest(self.session_dir, manifest)
        self._report(manifest, fit)
        return self.session_dir

    def _report(self, manifest: dict, fit) -> None:
        print("Recording stopped after %.1f s" % manifest["duration_s"])
        print("  beats %d | ecg %d | acc %d | markers %d"
              % (self.counts["rr"], self.counts["ecg"],
                 self.counts["acc"], self.counts["markers"]))
        if fit.n_observations:
            print("  clock fit: %s, drift %.1f ppm, residual %.1f ms"
                  % (fit.kind, fit.drift_ppm, fit.residual_ms))
        if self.errors:
            print("  warnings:")
            for err in dict.fromkeys(self.errors):
                print("    - " + err)
        print("  written to %s" % self.session_dir)


def _f(t: float) -> str:
    """Unix seconds with microsecond resolution and no scientific notation."""
    return "%.6f" % t


def _blank(value):
    return "" if value is None else value


def _contact(sample) -> str:
    if not sample.contact_supported or sample.contact_detected is None:
        return "unknown"
    return "ok" if sample.contact_detected else "poor"


def record(cfg: RecorderConfig) -> str:
    """Blocking entry point: run one recording session, return its directory."""
    recorder = Recorder(cfg)
    try:
        return asyncio.run(recorder.run())
    except KeyboardInterrupt:
        # Ctrl+C during connect/scan, before the event loop owns the signal.
        if recorder.session_dir:
            recorder.stopped_unix = fileio.now_unix()
            return recorder._finalize()
        raise SystemExit("cancelled before recording started")
