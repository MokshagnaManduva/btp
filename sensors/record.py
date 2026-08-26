#!/usr/bin/env python
"""Record a Polar H10 session to CSV.

    python record.py                       # record until you press q / Ctrl+C
    python record.py --duration 600        # ten minutes, then stop
    python record.py --streams hr,rr       # heart rate only, no raw ECG
    python record.py --note "session 3, post-caffeine"

Start this BEFORE you start FPSci and stop it after; everything is stamped in
UTC so the two logs line up with no further work.  See docs/data-format.md.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from polar_h10.recorder import DEFAULT_STREAMS, RecorderConfig, record  # noqa: E402

# The id printed on the back of the strap; also the suffix of its BLE name.
DEFAULT_DEVICE_ID = "184CB835"

STREAM_CHOICES = ("hr", "rr", "ecg", "acc")


def parse_streams(text: str) -> tuple:
    names = tuple(s.strip().lower() for s in text.split(",") if s.strip())
    bad = [n for n in names if n not in STREAM_CHOICES]
    if bad:
        raise argparse.ArgumentTypeError(
            "unknown stream(s) %s; choose from %s" % (bad, ", ".join(STREAM_CHOICES)))
    if not names:
        raise argparse.ArgumentTypeError("no streams selected")
    return names


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device-id", default=DEFAULT_DEVICE_ID,
                    help="id printed on the strap (default: %(default)s)")
    ap.add_argument("--address", default=None,
                    help="connect straight to this BLE address, skipping the scan")
    ap.add_argument("--streams", type=parse_streams, default=DEFAULT_STREAMS,
                    help="comma separated: hr,rr,ecg,acc (default: all)")
    ap.add_argument("--acc-rate", type=int, default=50, choices=(25, 50, 100, 200),
                    help="accelerometer sample rate in Hz (default: %(default)s)")
    ap.add_argument("--acc-range", type=int, default=8, choices=(2, 4, 8),
                    help="accelerometer range in g (default: %(default)s)")
    ap.add_argument("--duration", type=float, default=None, metavar="SECONDS",
                    help="stop automatically after this long")
    ap.add_argument("--subject", default="",
                    help="subject id; use the same string as the FPSci player id")
    ap.add_argument("--note", default="",
                    help="free-text note stored in the manifest and as a marker")
    ap.add_argument("--out", default=os.path.join(HERE, "data", "raw"),
                    help="directory to create the session folder in")
    ap.add_argument("--scan-timeout", type=float, default=15.0,
                    help="how long to look for the strap (default: %(default)s s)")
    ap.add_argument("--hr-latency-ms", type=float, default=0.0,
                    help="assumed BLE delay for heart-rate notifications, "
                         "subtracted from beat timestamps (default: %(default)s)")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = RecorderConfig(
        device_id=args.device_id,
        address=args.address,
        out_root=args.out,
        streams=tuple(args.streams),
        acc_rate_hz=args.acc_rate,
        acc_range_g=args.acc_range,
        duration_s=args.duration,
        subject=args.subject,
        note=args.note,
        scan_timeout=args.scan_timeout,
        hr_latency_ms=args.hr_latency_ms,
    )
    session_dir = record(cfg)
    print("")
    print("Next:  python correlate.py --session %s" % session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
