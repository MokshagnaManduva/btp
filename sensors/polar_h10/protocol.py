"""Polar H10 BLE wire protocol.

Pure decoding logic -- no bleak import, no I/O -- so it can be unit-tested
against captured bytes.  Two independent data paths are covered:

1. The standard Bluetooth SIG Heart Rate Service (0x180D).  Gives heart rate in
   bpm plus RR intervals (inter-beat intervals) about once per second.

2. Polar Measurement Data (PMD), a vendor service exposing the raw signals:
   ECG at 130 Hz in microvolts, and accelerometer at 25/50/100/200 Hz in milli-g.

Frame layouts follow the Polar BLE SDK technical documentation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# UUIDs
# --------------------------------------------------------------------------

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BODY_SENSOR_LOCATION = "00002a38-0000-1000-8000-00805f9b34fb"

BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER = "00002a24-0000-1000-8000-00805f9b34fb"
SERIAL_NUMBER = "00002a25-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REVISION = "00002a27-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME = "00002a29-0000-1000-8000-00805f9b34fb"

PMD_SERVICE = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"   # write + indicate
PMD_DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"      # notify

# --------------------------------------------------------------------------
# PMD constants
# --------------------------------------------------------------------------

# Measurement types
ECG = 0x00
PPG = 0x01
ACC = 0x02
PPI = 0x03
GYRO = 0x05
MAG = 0x06

MEAS_NAMES = {ECG: "ECG", PPG: "PPG", ACC: "ACC", PPI: "PPI", GYRO: "GYRO", MAG: "MAG"}

# Control-point opcodes
OP_GET_SETTINGS = 0x01
OP_START = 0x02
OP_STOP = 0x03

CONTROL_RESPONSE = 0xF0  # first byte of every control-point indication

# Setting ids used in the TLV block of a start command
SET_SAMPLE_RATE = 0x00
SET_RESOLUTION = 0x01
SET_RANGE = 0x02
SET_CHANNELS = 0x04

SETTING_IDS = {
    "sample_rate_hz": SET_SAMPLE_RATE,
    "resolution_bits": SET_RESOLUTION,
    "range_g": SET_RANGE,
    "channels": SET_CHANNELS,
}
SETTING_NAMES = {v: k for k, v in SETTING_IDS.items()}

PMD_ERRORS = {
    0: "SUCCESS",
    1: "ERROR_INVALID_OP_CODE",
    2: "ERROR_INVALID_MEASUREMENT_TYPE",
    3: "ERROR_NOT_SUPPORTED",
    4: "ERROR_INVALID_LENGTH",
    5: "ERROR_INVALID_PARAMETER",
    6: "ERROR_ALREADY_IN_STATE",
    7: "ERROR_INVALID_RESOLUTION",
    8: "ERROR_INVALID_SAMPLE_RATE",
    9: "ERROR_INVALID_RANGE",
    10: "ERROR_INVALID_MTU",
    11: "ERROR_INVALID_NUMBER_OF_CHANNELS",
    12: "ERROR_INVALID_STATE",
    13: "ERROR_DEVICE_IN_CHARGER",
}

# The H10's own clock counts nanoseconds from 2000-01-01T00:00:00Z.
# 946684800 is that instant expressed as a Unix timestamp.
POLAR_EPOCH_UNIX = 946684800.0

# Defaults the H10 accepts.  ECG is fixed at 130 Hz / 14 bit.
ECG_SAMPLE_RATE = 130
ECG_RESOLUTION = 14
ACC_SAMPLE_RATES = (25, 50, 100, 200)
ACC_RANGES = (2, 4, 8)
ACC_RESOLUTION = 16


class PmdError(RuntimeError):
    """A PMD control-point command was rejected by the sensor."""


# --------------------------------------------------------------------------
# Control-point commands
# --------------------------------------------------------------------------

def get_settings_command(meas_type: int) -> bytes:
    """Ask the sensor which sample rates / resolutions / ranges it supports."""
    return bytes([OP_GET_SETTINGS, meas_type])


def stop_command(meas_type: int) -> bytes:
    return bytes([OP_STOP, meas_type])


def start_command(meas_type: int, **settings: int) -> bytes:
    """Build a REQUEST_MEASUREMENT_START frame.

    Settings are passed by name (sample_rate_hz, resolution_bits, range_g,
    channels) and encoded as a TLV list of
    <setting id> <array length = 1> <uint16 little-endian value>.
    """
    out = bytearray([OP_START, meas_type])
    for name, value in settings.items():
        if name not in SETTING_IDS:
            raise ValueError("unknown PMD setting " + repr(name))
        out += bytes([SETTING_IDS[name], 0x01]) + struct.pack("<H", value)
    return bytes(out)


def ecg_start_command(sample_rate_hz: int = ECG_SAMPLE_RATE,
                      resolution_bits: int = ECG_RESOLUTION) -> bytes:
    return start_command(ECG, sample_rate_hz=sample_rate_hz,
                         resolution_bits=resolution_bits)


def acc_start_command(sample_rate_hz: int = 50, range_g: int = 8,
                      resolution_bits: int = ACC_RESOLUTION) -> bytes:
    if sample_rate_hz not in ACC_SAMPLE_RATES:
        raise ValueError("H10 accelerometer supports " + str(ACC_SAMPLE_RATES) + " Hz")
    if range_g not in ACC_RANGES:
        raise ValueError("H10 accelerometer supports +/- " + str(ACC_RANGES) + " g")
    return start_command(ACC, sample_rate_hz=sample_rate_hz,
                         resolution_bits=resolution_bits, range_g=range_g)


@dataclass
class ControlResponse:
    opcode: int
    meas_type: int
    error_code: int
    payload: bytes

    @property
    def ok(self) -> bool:
        return self.error_code == 0

    @property
    def error(self) -> str:
        return PMD_ERRORS.get(self.error_code, "UNKNOWN_ERROR_" + str(self.error_code))

    def raise_for_status(self) -> "ControlResponse":
        if not self.ok:
            name = MEAS_NAMES.get(self.meas_type, str(self.meas_type))
            raise PmdError(
                "%s op=0x%02x rejected by sensor: %s" % (name, self.opcode, self.error)
            )
        return self


def decode_control_response(data: bytes) -> "ControlResponse | None":
    """Decode a PMD control-point indication.  Returns None if not a response.

    Layout: F0 <opcode> <measurement type> <error code> <more flag> <payload...>
    """
    if len(data) < 4 or data[0] != CONTROL_RESPONSE:
        return None
    return ControlResponse(opcode=data[1], meas_type=data[2],
                           error_code=data[3], payload=bytes(data[5:]))


def decode_settings_payload(payload: bytes) -> dict:
    """Decode the TLV capability list returned by GET_SETTINGS."""
    out: dict = {}
    i = 0
    while i + 2 <= len(payload):
        setting_id = payload[i]
        count = payload[i + 1]
        i += 2
        values = []
        for _ in range(count):
            if i + 2 > len(payload):
                break
            values.append(struct.unpack_from("<H", payload, i)[0])
            i += 2
        out[SETTING_NAMES.get(setting_id, "setting_" + str(setting_id))] = values
    return out


# --------------------------------------------------------------------------
# Heart Rate Measurement characteristic (0x2A37)
# --------------------------------------------------------------------------

@dataclass
class HrSample:
    hr_bpm: int
    rr_ms: list = field(default_factory=list)
    contact_supported: bool = False
    contact_detected: object = None   # True / False / None when unsupported
    energy_j: object = None


def decode_hr_measurement(data: bytes) -> HrSample:
    """Decode the SIG Heart Rate Measurement characteristic.

    Bit 0 of the flags byte selects uint8/uint16 heart rate; bits 1-2 carry
    electrode-contact status; bit 3 flags an energy-expended field; bit 4 flags
    a trailing list of RR intervals in units of 1/1024 s.
    """
    if not data:
        raise ValueError("empty heart rate measurement")
    flags = data[0]
    wide = bool(flags & 0x01)
    contact_supported = bool(flags & 0x04)
    contact_detected = bool(flags & 0x02) if contact_supported else None
    has_energy = bool(flags & 0x08)
    has_rr = bool(flags & 0x10)

    i = 1
    if wide:
        hr = struct.unpack_from("<H", data, i)[0]
        i += 2
    else:
        hr = data[i]
        i += 1

    energy = None
    if has_energy:
        energy = struct.unpack_from("<H", data, i)[0]
        i += 2

    rr_ms = []
    if has_rr:
        while i + 2 <= len(data):
            ticks = struct.unpack_from("<H", data, i)[0]
            i += 2
            rr_ms.append(ticks * 1000.0 / 1024.0)

    return HrSample(hr_bpm=hr, rr_ms=rr_ms, contact_supported=contact_supported,
                    contact_detected=contact_detected, energy_j=energy)


# --------------------------------------------------------------------------
# PMD data frames
# --------------------------------------------------------------------------

@dataclass
class PmdFrame:
    meas_type: int
    device_ts_ns: int          # timestamp of the LAST sample in the frame
    frame_type: int
    samples: list              # ECG: list[int] uV.  ACC: list[(x, y, z)] mg.
    delta_coded: bool = False

    @property
    def device_ts_s(self) -> float:
        return self.device_ts_ns / 1e9


def _signed(raw: bytes) -> int:
    return int.from_bytes(raw, "little", signed=True)


class _BitReader:
    """LSB-first bit reader used by the delta-compressed frame format."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, start_byte: int = 0):
        self.data = data
        self.pos = start_byte * 8

    def read(self, n_bits: int, signed: bool = True) -> int:
        value = 0
        for k in range(n_bits):
            byte_i, bit_i = divmod(self.pos, 8)
            if byte_i >= len(self.data):
                raise ValueError("delta frame truncated")
            value |= ((self.data[byte_i] >> bit_i) & 1) << k
            self.pos += 1
        if signed and n_bits and (value >> (n_bits - 1)) & 1:
            value -= 1 << n_bits
        return value

    @property
    def exhausted(self) -> bool:
        return self.pos >= len(self.data) * 8


def _decode_delta(body: bytes, n_channels: int, ref_bytes: int) -> list:
    """Decode Polar's delta-compressed sample block.

    Layout: one absolute reference sample (n_channels little-endian signed
    integers of ref_bytes bytes), then repeating groups of
    <delta bit width : 1 byte> <sample count : 1 byte> <packed deltas>.
    Each delta is a signed value of the given bit width, added to the running
    sample.  A group header of zeroes marks the end (trailing padding).
    """
    if len(body) < n_channels * ref_bytes:
        raise ValueError("delta frame too short for reference sample")
    current = [_signed(body[i * ref_bytes:(i + 1) * ref_bytes]) for i in range(n_channels)]
    out = [tuple(current)]

    reader = _BitReader(body, start_byte=n_channels * ref_bytes)
    while not reader.exhausted:
        try:
            delta_bits = reader.read(8, signed=False)
            sample_count = reader.read(8, signed=False)
        except ValueError:
            break
        if delta_bits == 0 or sample_count == 0:
            break
        try:
            for _ in range(sample_count):
                for ch in range(n_channels):
                    current[ch] += reader.read(delta_bits, signed=True)
                out.append(tuple(current))
        except ValueError:
            break
    return out


def decode_pmd_frame(data: bytes) -> PmdFrame:
    """Decode one notification from the PMD data characteristic.

    Header: <measurement type : 1> <device timestamp : uint64 LE nanoseconds>
            <frame type : 1>.  Bit 7 of the frame type marks a delta-compressed
            body; the low 7 bits give the sample width.
    """
    if len(data) < 10:
        raise ValueError("PMD frame too short (%d bytes)" % len(data))
    meas_type = data[0]
    device_ts_ns = int.from_bytes(data[1:9], "little", signed=False)
    frame_type = data[9]
    body = bytes(data[10:])
    delta = bool(frame_type & 0x80)
    base_type = frame_type & 0x7F

    if meas_type == ECG:
        # H10 ECG samples are 3-byte signed microvolts regardless of the
        # advertised 14-bit resolution.
        if delta:
            samples = [s[0] for s in _decode_delta(body, 1, 3)]
        else:
            samples = [_signed(body[i:i + 3]) for i in range(0, len(body) - 2, 3)]
        return PmdFrame(meas_type, device_ts_ns, frame_type, samples, delta)

    if meas_type == ACC:
        width = {0: 1, 1: 2, 2: 3}.get(base_type)
        if width is None:
            raise ValueError("unsupported ACC frame type 0x%02x" % frame_type)
        if delta:
            samples = _decode_delta(body, 3, width)
        else:
            step = width * 3
            samples = [
                (_signed(body[i:i + width]),
                 _signed(body[i + width:i + 2 * width]),
                 _signed(body[i + 2 * width:i + 3 * width]))
                for i in range(0, len(body) - step + 1, step)
            ]
        return PmdFrame(meas_type, device_ts_ns, frame_type, samples, delta)

    raise ValueError("unhandled PMD measurement type 0x%02x" % meas_type)


def frame_sample_times(frame: PmdFrame, sample_rate_hz: float) -> list:
    """Per-sample device timestamps, in seconds on the sensor's own clock.

    The header timestamp belongs to the *last* sample of the frame, so earlier
    samples are back-dated by one sampling period each.
    """
    n = len(frame.samples)
    last = frame.device_ts_s
    period = 1.0 / sample_rate_hz
    return [last - (n - 1 - i) * period for i in range(n)]
