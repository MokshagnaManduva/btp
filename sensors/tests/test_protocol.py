"""Decoder / clock / HRV unit tests.

    python tests/test_protocol.py

Plain asserts, no pytest needed.  Frames are synthesised byte-for-byte from the
documented layouts, so these tests pin the wire format rather than whatever the
decoder happens to do.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polar_h10 import fileio, hrv                       # noqa: E402
from polar_h10 import protocol as p                     # noqa: E402
from polar_h10.clock import ClockModel                  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


# --------------------------------------------------------------------------
# Control point
# --------------------------------------------------------------------------

@check
def test_start_commands():
    # ECG: start, type 0, sample rate 130 (0x0082), resolution 14 (0x000E)
    assert p.ecg_start_command().hex() == "0200000182000101 0e00".replace(" ", "")
    # ACC: start, type 2, 50 Hz (0x0032), 16 bit (0x0010), 8 g (0x0008)
    assert p.acc_start_command(50, 8).hex() == \
        "0202000132000101100002010800"
    assert p.acc_start_command(200, 2).hex() == \
        "020200 01c800 0101 1000 0201 0200".replace(" ", "")
    assert p.stop_command(p.ECG).hex() == "0300"
    assert p.get_settings_command(p.ACC).hex() == "0102"

    for bad in ((37, 8), (50, 16)):
        try:
            p.acc_start_command(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError("accepted an unsupported ACC setting %r" % (bad,))


@check
def test_control_response():
    ok = p.decode_control_response(bytes([0xF0, 0x02, 0x00, 0x00, 0x00]))
    assert ok is not None and ok.ok and ok.opcode == p.OP_START
    ok.raise_for_status()

    bad = p.decode_control_response(bytes([0xF0, 0x02, 0x00, 0x05, 0x00]))
    assert bad.error == "ERROR_INVALID_PARAMETER"
    try:
        bad.raise_for_status()
    except p.PmdError as exc:
        assert "ERROR_INVALID_PARAMETER" in str(exc)
    else:
        raise AssertionError("a rejected command did not raise")

    # A data frame must never be mistaken for a control response.
    assert p.decode_control_response(bytes([0x00, 0x01, 0x02, 0x03])) is None


@check
def test_settings_payload():
    payload = bytes([0x00, 0x01]) + struct.pack("<H", 130) + \
              bytes([0x01, 0x01]) + struct.pack("<H", 14)
    assert p.decode_settings_payload(payload) == {
        "sample_rate_hz": [130], "resolution_bits": [14]}


# --------------------------------------------------------------------------
# Heart rate characteristic
# --------------------------------------------------------------------------

@check
def test_hr_uint8_no_rr():
    sample = p.decode_hr_measurement(bytes([0x00, 62]))
    assert sample.hr_bpm == 62
    assert sample.rr_ms == []
    assert sample.contact_supported is False
    assert sample.contact_detected is None


@check
def test_hr_with_contact_and_rr():
    # flags: RR present (0x10) | contact supported (0x04) | contact detected (0x02)
    data = bytes([0x16, 58]) + struct.pack("<HH", 1024, 512)
    sample = p.decode_hr_measurement(data)
    assert sample.hr_bpm == 58
    assert sample.contact_supported and sample.contact_detected
    # RR is in 1/1024 s units
    assert [round(v, 3) for v in sample.rr_ms] == [1000.0, 500.0]


@check
def test_hr_uint16_with_energy():
    # flags: 16-bit HR (0x01) | energy expended (0x08) | RR (0x10)
    data = bytes([0x19]) + struct.pack("<HH", 300, 1234) + struct.pack("<H", 819)
    sample = p.decode_hr_measurement(data)
    assert sample.hr_bpm == 300
    assert sample.energy_j == 1234
    assert round(sample.rr_ms[0], 1) == 799.8


# --------------------------------------------------------------------------
# PMD data frames
# --------------------------------------------------------------------------

def _ecg_frame(ts_ns, values):
    body = b"".join(v.to_bytes(3, "little", signed=True) for v in values)
    return bytes([p.ECG]) + ts_ns.to_bytes(8, "little") + bytes([0x00]) + body


def _acc_frame(ts_ns, triples):
    body = b"".join(struct.pack("<hhh", *t) for t in triples)
    return bytes([p.ACC]) + ts_ns.to_bytes(8, "little") + bytes([0x01]) + body


@check
def test_ecg_frame():
    values = [100, -100, 0, 8388607, -8388608]
    frame = p.decode_pmd_frame(_ecg_frame(1_000_000_000, values))
    assert frame.meas_type == p.ECG
    assert frame.device_ts_ns == 1_000_000_000
    assert frame.samples == values
    assert not frame.delta_coded


@check
def test_acc_frame():
    triples = [(1, -1, 1000), (-32768, 32767, 0)]
    frame = p.decode_pmd_frame(_acc_frame(2_000_000_000, triples))
    assert frame.samples == triples


@check
def test_delta_frame():
    # reference sample, then one group: 4-bit deltas, 2 samples, +1 then -1.
    ref = (500).to_bytes(3, "little", signed=True)
    packed = bytes([0x04, 0x02, 0x01 | (0x0F << 4)])
    data = bytes([p.ECG]) + (3_000_000_000).to_bytes(8, "little") + \
        bytes([0x80]) + ref + packed
    frame = p.decode_pmd_frame(data)
    assert frame.delta_coded
    assert frame.samples == [500, 501, 500]


@check
def test_frame_sample_times():
    frame = p.decode_pmd_frame(_ecg_frame(1_000_000_000, [0, 0, 0]))
    times = p.frame_sample_times(frame, 130.0)
    # The header stamps the LAST sample, so earlier ones are back-dated.
    assert abs(times[-1] - 1.0) < 1e-9
    assert abs(times[0] - (1.0 - 2 / 130.0)) < 1e-9


@check
def test_bad_frames_raise():
    for data in (b"", bytes(9), bytes([0x05]) + bytes(9)):
        try:
            p.decode_pmd_frame(data)
        except ValueError:
            pass
        else:
            raise AssertionError("decoded a frame that should have been rejected")


# --------------------------------------------------------------------------
# Clock model
# --------------------------------------------------------------------------

def _fake_latency(i):
    """Deterministic, always-positive jitter standing in for BLE delay."""
    return 0.004 + 0.03 * (((i * 7919) % 97) / 97.0)


@check
def test_clock_offset_recovery():
    model = ClockModel()
    true_offset = 1_700_000_000.0
    for i in range(300):
        device_s = 12345.0 + i * 0.1
        model.observe(device_s, device_s + true_offset + _fake_latency(i))
    # The minimum observed offset should sit just above the true offset by the
    # smallest latency in the sample.
    assert 0 <= model.offset - true_offset < 0.01
    fit = model.fit()
    assert fit.kind in ("constant", "linear")
    assert abs(fit.apply(12345.0) - (12345.0 + true_offset)) < 0.02


@check
def test_clock_drift_recovery():
    model = ClockModel()
    offset0, drift = 1_700_000_000.0, 50e-6      # 50 ppm
    ref = 1000.0
    for i in range(1200):                         # 20 minutes at 1 Hz
        device_s = ref + i
        arrival = device_s + offset0 + drift * (device_s - ref) + _fake_latency(i)
        model.observe(device_s, arrival)
    fit = model.fit()
    assert fit.kind == "linear"
    assert abs(fit.drift_ppm - 50.0) < 5.0, fit.drift_ppm
    assert fit.residual_ms < 5.0
    worst = max(abs(fit.apply(ref + i) - (ref + i + offset0 + drift * i))
                for i in range(0, 1200, 60))
    assert worst < 0.02, worst


# --------------------------------------------------------------------------
# HRV + timestamps
# --------------------------------------------------------------------------

@check
def test_hrv_constant_rr():
    metrics = hrv.hrv_metrics([1000.0] * 10)
    assert metrics["n_beats"] == 10
    assert metrics["hr_mean_bpm"] == 60.0
    assert metrics["sdnn_ms"] == 0.0
    assert metrics["rmssd_ms"] == 0.0
    assert metrics["pnn50_pct"] == 0.0


@check
def test_hrv_filters_artifacts():
    clean = [800.0, 810.0, 805.0, 815.0]
    kept, rejected = hrv.filter_rr(clean + [3000.0, 60.0])
    assert kept == clean and rejected == 2
    assert hrv.hrv_metrics([])["n_beats"] == 0


@check
def test_hrv_rejects_isolated_ectopic():
    beats = [800.0, 810.0, 805.0, 815.0, 400.0, 800.0, 806.0]
    kept, rejected = hrv.filter_rr(beats)
    assert 400.0 not in kept
    assert rejected == 1 and len(kept) == 6


@check
def test_hrv_follows_a_real_rate_change():
    """HR rising from 64 to 88 bpm mid-window must survive the filter.

    A filter anchored to the last accepted beat rejects everything after the
    step, which would make every trial read as the resting baseline.
    """
    resting = [937.0, 940.0, 935.0, 938.0, 936.0]
    working = [682.0, 684.0, 686.0, 683.0, 685.0, 681.0]
    kept, rejected = hrv.filter_rr(resting + working)
    assert len(kept) == len(resting) + len(working), \
        "%d of %d beats survived" % (len(kept), len(resting) + len(working))
    assert rejected == 0
    metrics = hrv.hrv_metrics(resting + working)
    assert 74.0 < metrics["hr_mean_bpm"] < 78.0, metrics["hr_mean_bpm"]


@check
def test_hrv_survives_a_boundary_beat():
    """One leftover slow beat at the start of a window must not poison it."""
    window = [937.0] + [682.0 + 2.0 * (i % 3) for i in range(60)]
    kept, rejected = hrv.filter_rr(window)
    assert len(kept) == len(window) and rejected == 0
    assert abs(hrv.hrv_metrics(window)["hr_mean_bpm"] - 87.3) < 1.0


@check
def test_timestamp_roundtrip():
    text = "2026-08-20 15:47:20.708293"
    t = fileio.parse_utc(text)
    assert fileio.utc_string(t) == text
    # This is the exact literal FPSci writes, and it is UTC.
    assert abs(t - 1787240840.708293) < 1e-6


@check
def test_timestamps_sort_lexicographically():
    stamps = ["2026-08-20 15:47:20.708293", "2026-08-20 15:47:20.708294",
              "2026-08-20 15:47:21.000000", "2026-08-20 16:00:00.000000"]
    assert sorted(stamps) == stamps
    assert [fileio.parse_utc(s) for s in stamps] == \
        sorted(fileio.parse_utc(s) for s in stamps)


def main():
    failures = 0
    for fn in CHECKS:
        try:
            fn()
        except Exception as exc:
            failures += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
        else:
            print("ok   %s" % fn.__name__)
    print("")
    print("%d/%d passed" % (len(CHECKS) - failures, len(CHECKS)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
