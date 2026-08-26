"""Mapping the Polar H10's internal clock onto host UTC.

Why this exists
---------------
Every PMD frame carries a device timestamp in nanoseconds since
2000-01-01T00:00:00Z.  Those timestamps are *precise* (they come from the
sensor's own sampling clock, so inter-sample spacing is exact) but their
*absolute* value is meaningless as wall clock: the H10's real-time clock is only
set when it syncs with the Polar Flow app, and it drifts freely afterwards.

Meanwhile the host records an arrival time for every notification.  That value
is anchored to real UTC -- the same clock FPSci logs with -- but it is noisy,
because BLE delivers notifications in bursts on connection-interval boundaries.

The fix is the standard one-way-delay estimator used by NTP: for each frame,
    offset = arrival_unix - device_seconds
is the true offset *plus* that frame's transport delay.  Delay is always
positive, so the **minimum** observed offset is the best estimate of the true
offset.  Taking a running minimum converges within a few seconds.

Over a long session the two clocks also drift apart (the H10's crystal is
typically tens of ppm off).  ``ClockModel.fit()`` therefore fits a straight line
to the *lower envelope* of the offsets -- the per-bucket minima -- giving both a
constant offset and a drift term.

Residual accuracy: the estimator cannot see the minimum transport delay itself,
so all timestamps carry a constant bias of roughly +10 to +40 ms (samples are
reported very slightly later than they occurred).  That is far below the
resolution of anything you would correlate a 45-60 s aim-training trial against,
but it is a bias, not noise -- record it and subtract it if you ever need
beat-accurate alignment.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class ClockFit:
    """A fitted device-clock -> host-UTC mapping.

    unix = device_s + offset_s + drift * (device_s - ref_device_s)
    """

    offset_s: float
    drift: float = 0.0            # seconds of offset change per second elapsed
    ref_device_s: float = 0.0
    n_observations: int = 0
    n_buckets: int = 0
    residual_ms: float = 0.0
    span_s: float = 0.0
    kind: str = "constant"        # "constant" or "linear"

    def apply(self, device_s: float) -> float:
        return device_s + self.offset_s + self.drift * (device_s - self.ref_device_s)

    @property
    def drift_ppm(self) -> float:
        return self.drift * 1e6

    def to_dict(self) -> dict:
        d = asdict(self)
        d["drift_ppm"] = self.drift_ppm
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ClockFit":
        fields = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**fields)


class ClockModel:
    """Running estimator of the device -> host clock offset.

    Feed it ``observe(device_s, arrival_unix)`` for every notification.  Use
    ``to_unix()`` for live timestamps and ``fit()`` at the end of the session
    for the best-quality mapping to re-derive timestamps offline.
    """

    def __init__(self, bucket_seconds: float = 10.0):
        self.bucket_seconds = bucket_seconds
        self._min_offset = None
        self._buckets = {}        # bucket index -> [min offset, device_s at that min]
        self._n = 0
        self._first_device_s = None
        self._last_device_s = None

    def observe(self, device_s: float, arrival_unix: float) -> float:
        """Record one (device time, host arrival time) pair; returns the offset."""
        offset = arrival_unix - device_s
        if self._min_offset is None or offset < self._min_offset:
            self._min_offset = offset
        if self._first_device_s is None:
            self._first_device_s = device_s
        self._last_device_s = device_s

        key = int(device_s // self.bucket_seconds)
        slot = self._buckets.get(key)
        if slot is None or offset < slot[0]:
            self._buckets[key] = [offset, device_s]
        self._n += 1
        return offset

    @property
    def ready(self) -> bool:
        return self._min_offset is not None

    @property
    def offset(self) -> float:
        """Best running estimate of the constant offset (minimum seen so far)."""
        if self._min_offset is None:
            raise RuntimeError("clock model has no observations yet")
        return self._min_offset

    def to_unix(self, device_s: float) -> float:
        """Live conversion using the running-minimum offset."""
        return device_s + self.offset

    def fit(self) -> ClockFit:
        """Least-squares line through the lower envelope of the offsets."""
        if not self.ready:
            return ClockFit(offset_s=0.0, kind="undefined")

        span = (self._last_device_s or 0.0) - (self._first_device_s or 0.0)
        points = sorted((dev, off) for off, dev in self._buckets.values())
        ref = points[0][0]

        if len(points) < 3 or span < 30.0:
            # Too short to separate drift from noise -- a constant is honest.
            resid = [(off - self._min_offset) for _, off in points]
            return ClockFit(offset_s=self._min_offset, drift=0.0, ref_device_s=ref,
                            n_observations=self._n, n_buckets=len(points),
                            residual_ms=_rms(resid) * 1000.0, span_s=span,
                            kind="constant")

        xs = [dev - ref for dev, _ in points]
        ys = [off for _, off in points]
        n = float(len(points))
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx <= 0:
            return ClockFit(offset_s=mean_y, drift=0.0, ref_device_s=ref,
                            n_observations=self._n, n_buckets=len(points),
                            span_s=span, kind="constant")
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        slope = sxy / sxx
        intercept = mean_y - slope * mean_x

        resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        return ClockFit(offset_s=intercept, drift=slope, ref_device_s=ref,
                        n_observations=self._n, n_buckets=len(points),
                        residual_ms=_rms(resid) * 1000.0, span_s=span,
                        kind="linear")


def _rms(values) -> float:
    if not values:
        return 0.0
    return (sum(v * v for v in values) / len(values)) ** 0.5
