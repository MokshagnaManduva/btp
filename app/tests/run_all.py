#!/usr/bin/env python3
"""Run every test in one go.

    python app/tests/run_all.py

Each test is a plain script that exits non-zero on failure, so this just runs
them in order and reports. No pytest, no config.
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)

TESTS = [
    ("golden", "test_golden.py", "metrics module still matches the original parser"),
    ("export", "test_export.py", "every routing path, in a throwaway tree"),
    ("launcher", "test_launcher.py", "preflight, generated configs, sittings, watcher"),
    ("warmup", "test_warmup_anchor.py", "condition labelling, and rebuild safety"),
]


def main():
    results = []
    for name, script, blurb in TESTS:
        print("=" * 68)
        print("%s -- %s" % (name, blurb))
        print("=" * 68)
        proc = subprocess.run([sys.executable, os.path.join(HERE, script)])
        results.append((name, proc.returncode == 0))
        print()

    print("=" * 68)
    for name, ok in results:
        print("  %-10s %s" % (name, "PASS" if ok else "FAIL"))
    failed = [n for n, ok in results if not ok]
    if failed:
        print("\n%d suite(s) failed: %s" % (len(failed), ", ".join(failed)))
        return 1
    print("\nAll suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
