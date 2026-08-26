#!/usr/bin/env python
"""Find the strap and check what it will give you, without recording anything.

    python scan.py               # list nearby BLE devices, highlight Polar ones
    python scan.py --settings    # connect to the H10 and query its PMD capabilities

Run this first if record.py cannot find the sensor, or after a firmware update
to confirm which ECG/ACC sample rates the strap actually offers.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from polar_h10 import protocol as p          # noqa: E402
from polar_h10.recorder import find_device   # noqa: E402

DEFAULT_DEVICE_ID = "184CB835"


async def list_devices(timeout: float) -> int:
    from bleak import BleakScanner

    print("Scanning for %.0f s..." % timeout)
    devices = await BleakScanner.discover(timeout=timeout)
    if not devices:
        print("No BLE devices found at all -- is Bluetooth switched on?")
        return 1
    polar = [d for d in devices if (d.name or "").lower().startswith("polar")]
    for dev in sorted(devices, key=lambda d: (d.name or "~").lower()):
        marker = "  <-- Polar" if dev in polar else ""
        print("  %-34s %s%s" % (dev.name or "(no name)", dev.address, marker))
    if not polar:
        print("\nNo Polar device advertising.  Moisten the electrodes and wear the\n"
              "strap -- the H10 stays silent until it can read a signal.")
        return 1
    return 0


async def show_settings(device_id: str, timeout: float) -> int:
    from bleak import BleakClient

    device = await find_device(device_id, timeout)
    async with BleakClient(device) as client:
        for label, uuid in (("model", p.MODEL_NUMBER),
                            ("firmware", p.FIRMWARE_REVISION),
                            ("hardware", p.HARDWARE_REVISION),
                            ("serial", p.SERIAL_NUMBER)):
            try:
                raw = await client.read_gatt_char(uuid)
                print("  %-10s %s" % (label, bytes(raw).decode("utf-8", "replace").strip()))
            except Exception as exc:
                print("  %-10s unavailable (%s)" % (label, exc))
        try:
            battery = (await client.read_gatt_char(p.BATTERY_LEVEL))[0]
            print("  %-10s %d%%" % ("battery", battery))
        except Exception:
            pass

        responses = asyncio.Queue()

        def on_control(_sender, data):
            resp = p.decode_control_response(bytes(data))
            if resp is not None:
                responses.put_nowait(resp)

        await client.start_notify(p.PMD_CONTROL, on_control)
        try:
            features = bytes(await client.read_gatt_char(p.PMD_CONTROL))
            print("  %-10s %s" % ("pmd feat", features.hex()))
        except Exception:
            pass

        for meas_type in (p.ECG, p.ACC):
            name = p.MEAS_NAMES[meas_type]
            try:
                await client.write_gatt_char(
                    p.PMD_CONTROL, p.get_settings_command(meas_type), response=True)
                resp = await asyncio.wait_for(responses.get(), 8.0)
                if not resp.ok:
                    print("\n%s: %s" % (name, resp.error))
                    continue
                print("\n%s supported settings:" % name)
                for key, values in p.decode_settings_payload(resp.payload).items():
                    print("    %-16s %s" % (key, values))
            except asyncio.TimeoutError:
                print("\n%s: no response from the control point" % name)
        await client.stop_notify(p.PMD_CONTROL)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", action="store_true",
                    help="connect and query the PMD measurement capabilities")
    ap.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args(argv)

    try:
        import bleak  # noqa: F401
    except ImportError:
        raise SystemExit("bleak is not installed:  python -m pip install bleak")

    if args.settings:
        return asyncio.run(show_settings(args.device_id, args.timeout))
    return asyncio.run(list_devices(args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
