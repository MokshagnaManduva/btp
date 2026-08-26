"""Polar H10 acquisition + FPSci correlation toolkit.

Sub-modules:
    protocol  BLE UUIDs, PMD control commands, frame decoders (no I/O, no bleak)
    clock     Polar device-clock -> host UTC mapping
    hrv       RR-interval derived heart-rate variability metrics
    fileio    CSV / session-manifest reading and writing, timestamp helpers
    recorder  the live BLE recorder (requires bleak)
"""

__version__ = "1.0.0"
