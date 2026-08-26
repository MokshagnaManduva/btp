"""Angles, time, and the small statistics helpers the metrics are built from.

Geometry
--------
All angles are degrees. FPSci logs the aim direction as (azimuth, elevation)
using, from ``Session.h::getViewDirection()``:

    az = atan2(view.x, -view.z) * 180/pi        # 0 = straight ahead, + = right
    el = atan2(view.y, hypot(view.x, view.z)) * 180/pi   # + = up

so the unit vector for an (az, el) pair is

    x = cos(el) sin(az),  y = sin(el),  z = -cos(el) cos(az)

``Player_Action.position_{x,y,z}`` is the *camera* position (it comes from
``m_camera->frame().translation``), and ``Target_Trajectory.position_{x,y,z}``
is the target's world position, so the angle between where the player was
aiming and where a target actually was is exact -- no eye-height fudge.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime

from .config import EPOCH, SMOOTH_HALFWIDTH, SUBMOVEMENT_MIN_DPS
from .config import SUBMOVEMENT_PROMINENCE, TIME_FORMATS, VELOCITY_WINDOW_S


def parse_time(s):
    """FPSci timestamp -> epoch seconds (float).

    Times are written as UTC with microsecond precision. We convert against a
    fixed epoch rather than ``datetime.timestamp()`` so no local-timezone or
    DST adjustment can creep into a difference.
    """
    if s is None:
        return None
    s = str(s).strip()
    for fmt in TIME_FORMATS:
        try:
            return (datetime.strptime(s, fmt) - EPOCH).total_seconds()
        except ValueError:
            continue
    return None


def azel_to_vec(az_deg, el_deg):
    """(azimuth, elevation) in degrees -> unit vector, matching getViewDirection."""
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    ce = math.cos(el)
    return (ce * math.sin(az), math.sin(el), -ce * math.cos(az))


def vec_to_azel(x, y, z):
    """World-space direction vector -> (azimuth, elevation) in degrees."""
    az = math.degrees(math.atan2(x, -z))
    el = math.degrees(math.atan2(y, math.hypot(x, z)))
    return az, el


def angsep_deg(az1, el1, az2, el2):
    """Great-circle angle between two aim directions, in degrees.

    Done via the dot product rather than differencing azimuths, so it stays
    correct across the +/-180 wrap and near the poles.
    """
    ax, ay, az_ = azel_to_vec(az1, el1)
    bx, by, bz = azel_to_vec(az2, el2)
    d = ax * bx + ay * by + az_ * bz
    return math.degrees(math.acos(max(-1.0, min(1.0, d))))


def safe(fn, *args, **kwargs):
    """Run a statistic, returning None instead of raising on empty input."""
    try:
        v = fn(*args, **kwargs)
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else v
    except (statistics.StatisticsError, ValueError, ZeroDivisionError, IndexError):
        return None


def pct(values, q):
    """Linear-interpolated percentile of an unsorted list (q in 0..100)."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * (q / 100.0)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def ratio(num, den):
    return (num / den) if den else None


def clean_param(v):
    """Tidy a value logged via ``sessionParametersToLog``.

    FPSci writes those through the Any serializer into a text column, so they
    arrive as things like ``'240 '`` or ``'"targets destroyed"'``. Strip the
    padding and quotes, and put numbers back into numeric form.
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


def smooth(xs, half=SMOOTH_HALFWIDTH):
    """Centred moving average; returns a list the same length as the input."""
    if half <= 0 or len(xs) <= 2 * half:
        return list(xs)
    out = []
    for i in range(len(xs)):
        a = max(0, i - half)
        b = min(len(xs), i + half + 1)
        out.append(sum(xs[a:b]) / (b - a))
    return out


def velocity_series(times, azs, els, window=VELOCITY_WINDOW_S):
    """Angular speed (deg/s) sampled over a fixed time window.

    For each sample, compares against the newest earlier sample that is at
    least ``window`` old, so the measurement is a real displacement-over-time
    rather than a per-frame difference. See VELOCITY_WINDOW_S for why.
    """
    out_t, out_v = [], []
    j = 0
    for i in range(1, len(times)):
        while j + 1 < i and times[i] - times[j + 1] >= window:
            j += 1
        dt = times[i] - times[j]
        if dt <= 0:
            continue
        out_v.append(angsep_deg(azs[j], els[j], azs[i], els[i]) / dt)
        out_t.append(times[i])
    return out_t, out_v


def count_submovements(speeds):
    """Count corrective submovements as prominent peaks in the speed profile.

    A ballistic flick is one big velocity peak; every extra peak above the
    prominence floor is a correction the player had to make to land the shot.
    """
    if len(speeds) < 3:
        return 0
    s = smooth(speeds)
    peak = max(s)
    if peak < SUBMOVEMENT_MIN_DPS:
        return 0
    floor = peak * SUBMOVEMENT_PROMINENCE
    count = 0
    i = 1
    while i < len(s) - 1:
        if s[i] >= s[i - 1] and s[i] > s[i + 1] and s[i] >= SUBMOVEMENT_MIN_DPS:
            # Confirm the peak by checking it drops far enough on both sides.
            left = min(s[:i]) if i else s[0]
            j = i
            while j < len(s) - 1 and s[j] >= s[j + 1]:
                j += 1
            right = min(s[i:j + 1]) if j > i else s[i]
            if (s[i] - max(left, right)) >= floor:
                count += 1
            i = max(j, i + 1)
        else:
            i += 1
    return count
