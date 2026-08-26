# Polar H10 BLE protocol

What the strap speaks over Bluetooth, at the level of detail needed to extend
`polar_h10/protocol.py`. Everything here is implemented in that module and
pinned by `tests/test_protocol.py`.

## Finding the strap

The H10 advertises as `Polar H10 <device id>` — for this one, `Polar H10
184CB835`. The id is printed on the back of the plastic pod, and `record.py`
matches on it, so nothing has to be hard-coded to a Bluetooth address (which
Windows randomises anyway).

Three things stop the strap from showing up:

- **It is not being worn, or the electrodes are dry.** The H10 does not advertise
  at all until it detects a signal. This is the usual cause.
- **Something else is connected.** It accepts very few simultaneous
  connections; Polar Flow or Polar Beat on a phone will take the slot.
- **It is on the charger.** PMD commands are refused with
  `ERROR_DEVICE_IN_CHARGER`.

Pairing in Windows Settings is *not* required — bleak connects directly.

## Services

| service | UUID | what for |
|---------|------|----------|
| Heart Rate | `0000180d-…` | bpm + RR intervals |
| Battery | `0000180f-…` | battery percentage |
| Device Information | `0000180a-…` | model, firmware, serial |
| Polar Measurement Data | `fb005c80-02e7-f387-1cad-8acd2d8df0c8` | raw ECG and accelerometer |

PMD has two characteristics:

| characteristic | UUID | mode |
|----------------|------|------|
| control point | `fb005c81-…` | write + indicate |
| data | `fb005c82-…` | notify |

## Heart Rate Measurement (`00002a37`)

The standard Bluetooth SIG layout, not a Polar invention.

```
byte 0   flags
         bit 0  0 = heart rate is uint8, 1 = uint16
         bit 1  electrode contact detected
         bit 2  contact detection supported
         bit 3  energy expended field present
         bit 4  RR intervals present
byte 1.. heart rate            (1 or 2 bytes per bit 0)
         energy expended       (uint16, only if bit 3)
         RR intervals          (uint16 each, only if bit 4, to end of packet)
```

RR intervals are in units of **1/1024 s**, so `ms = ticks * 1000 / 1024`. Bit 1
is only meaningful when bit 2 is set.

One notification may carry zero, one, or several RR intervals depending on how
many beats fell in the last interval — do not assume one beat per notification.

## PMD control point

Every command is a write; the reply comes back as an indication on the same
characteristic. Enable notifications on the control point *before* writing.

### Commands

| opcode | meaning | payload |
|--------|---------|---------|
| `0x01` | get measurement settings | measurement type |
| `0x02` | start measurement | measurement type + settings TLV |
| `0x03` | stop measurement | measurement type |

Measurement types: `0x00` ECG, `0x01` PPG, `0x02` ACC, `0x03` PPI, `0x05` GYRO,
`0x06` MAG. The H10 supports ECG and ACC (PPG and gyro belong to other Polar
devices).

Settings are a TLV list of `<setting id> <array length = 1> <uint16 LE value>`:

| id | setting |
|----|---------|
| `0x00` | sample rate, Hz |
| `0x01` | resolution, bits |
| `0x02` | range, g |
| `0x04` | channels |

**Start ECG at 130 Hz, 14-bit:**

```
02 00  00 01 82 00  01 01 0E 00
│  │   │  │  └──┴── 130 Hz          │  │  └──┴── 14 bits
│  │   └──┴── setting 0, 1 value    └──┴── setting 1, 1 value
│  └── ECG
└── start
```

**Start ACC at 50 Hz, 16-bit, ±8 g:**

```
02 02  00 01 32 00  01 01 10 00  02 01 08 00
```

The H10 accepts ACC at 25/50/100/200 Hz and ±2/4/8 g. ECG is fixed at 130 Hz and
14-bit — asking for anything else is rejected. `python scan.py --settings` queries
the strap directly rather than trusting this table.

### Responses

```
F0 <opcode> <measurement type> <error code> <more flag> <payload…>
```

Error code `0` is success. The rest:

| code | name | usual cause |
|------|------|-------------|
| 1 | `ERROR_INVALID_OP_CODE` | malformed command |
| 2 | `ERROR_INVALID_MEASUREMENT_TYPE` | asked for PPG/gyro on an H10 |
| 3 | `ERROR_NOT_SUPPORTED` | — |
| 4 | `ERROR_INVALID_LENGTH` | malformed TLV |
| 5 | `ERROR_INVALID_PARAMETER` | — |
| 6 | `ERROR_ALREADY_IN_STATE` | the stream is already running |
| 7–9 | `ERROR_INVALID_RESOLUTION` / `_SAMPLE_RATE` / `_RANGE` | unsupported setting |
| 10 | `ERROR_INVALID_MTU` | — |
| 11 | `ERROR_INVALID_NUMBER_OF_CHANNELS` | — |
| 12 | `ERROR_INVALID_STATE` | — |
| 13 | `ERROR_DEVICE_IN_CHARGER` | take it off the charger |

For a `get settings` reply the payload is the same TLV encoding, listing every
value the sensor supports.

## PMD data frames

```
byte 0    measurement type
bytes 1-8 device timestamp, uint64 little-endian, NANOSECONDS
byte 9    frame type  (bit 7 set = delta-compressed body)
byte 10.. samples
```

Two properties of that timestamp matter:

- Its epoch is **2000-01-01T00:00:00Z**, not the Unix epoch.
- It belongs to the **last** sample in the frame, not the first. Earlier samples
  are back-dated one sampling period each — that is what
  `protocol.frame_sample_times()` does.

### ECG samples

Frame type `0x00`: signed 24-bit little-endian values, one per sample, in
microvolts. Note that the samples are 3 bytes wide even though the advertised
resolution is 14 bits.

### Accelerometer samples

Frame type gives the sample width: `0x00` 8-bit, `0x01` 16-bit, `0x02` 24-bit.
Each sample is three consecutive signed values — x, y, z — in milli-g. With the
default 16-bit resolution the H10 sends frame type `0x01`.

### Delta-compressed frames

When bit 7 of the frame type is set the body is packed:

```
reference sample   channels × ceil(resolution/8) bytes, signed LE
then repeating:
  delta bit width  1 byte
  sample count     1 byte
  packed deltas    count × channels × width bits, LSB-first, signed
```

Each delta is added to the running sample. A group header of zeroes marks the
end (the rest is padding).

An H10 on current firmware sends uncompressed frames for both ECG and ACC, so
this path is a safety net rather than the normal case. The recorder decodes it
anyway and notes `sensor is sending delta-compressed frames` in `session.json`
so the change does not go unnoticed.

## Extending this

To add gyroscope or magnetometer support (Verity Sense, not H10) the pieces are:

1. Add the measurement type constant in `protocol.py`.
2. Add a branch to `decode_pmd_frame` with the right channel count and width.
3. Add a schema to `fileio.SCHEMAS` and a writer branch in
   `recorder.Recorder._on_pmd_data`.
4. Synthesise a frame in `tests/test_protocol.py` and assert the decode.

Step 4 is the one worth doing first — every decoder here was written against a
byte-for-byte synthetic frame before it ever saw a real one.
