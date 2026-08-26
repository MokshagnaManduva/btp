"""Heart-rate variability metrics from RR intervals.

Only time-domain metrics are implemented; they are the ones that stay valid on
the short windows an aim-training trial gives you (45-60 s).  Frequency-domain
measures such as LF/HF need 2-5 minutes of stationary data and are deliberately
left out rather than computed on windows too short to support them.

All functions take RR intervals in milliseconds and tolerate empty input.
"""

from __future__ import annotations

import math

# Physiologically plausible bounds for a healthy adult, and the maximum
# beat-to-beat change accepted before a beat is treated as an artifact
# (ectopic beat, or a dropped BLE packet joining two intervals into one).
RR_MIN_MS = 250.0     # 240 bpm
RR_MAX_MS = 2000.0    # 30 bpm
MAX_REL_CHANGE = 0.20
REFERENCE_WINDOW = 5  # beats of context the comparison is made against
RESYNC_AFTER = 3      # consistent off-reference beats that count as a real shift


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])


def filter_rr(rr_ms, rr_min=RR_MIN_MS, rr_max=RR_MAX_MS,
              max_rel_change=MAX_REL_CHANGE, window=REFERENCE_WINDOW,
              resync_after=RESYNC_AFTER):
    """Drop implausible RR intervals.

    Returns (kept, n_rejected).  A beat is rejected if it falls outside the
    absolute bounds, or if it differs by more than ``max_rel_change`` from the
    median of the last ``window`` accepted beats.

    Two details matter and are easy to get wrong:

    * The reference is a *median of recent beats*, not the single previous beat.
      One ectopic beat next to a good one would otherwise reject the good one
      just as readily as the reverse.

    * Heart rate genuinely jumps -- that is the whole point of recording it
      during an aim-training run.  A naive filter that only ever compares
      against the last accepted beat gets stuck at the old rate and throws away
      every subsequent beat, so the trials where HR actually moved come out
      looking like the resting baseline.  Here, ``resync_after`` consecutive
      beats that disagree with the reference but agree with *each other* are
      taken as a real change of level and accepted, and the reference moves.
    """
    kept = []
    rejected = 0
    recent = []
    pending = []

    for rr in rr_ms:
        if rr is None or not (rr_min <= rr <= rr_max):
            rejected += 1
            continue

        reference = _median(recent) if recent else None
        if reference is None or abs(rr - reference) / reference <= max_rel_change:
            rejected += len(pending)      # isolated artifacts, now confirmed
            pending = []
            kept.append(rr)
            recent.append(rr)
            del recent[:-window]
            continue

        pending.append(rr)
        if len(pending) >= resync_after:
            shifted = _median(pending)
            if all(abs(v - shifted) / shifted <= max_rel_change for v in pending):
                kept.extend(pending)      # a sustained, real change in rate
                recent = list(pending[-window:])
                pending = []
            else:
                rejected += 1
                pending.pop(0)

    return kept, rejected + len(pending)


def hrv_metrics(rr_ms, filtered: bool = True) -> dict:
    """Time-domain HRV summary for one window of RR intervals.

    mean_rr_ms   mean inter-beat interval
    hr_mean_bpm  60000 / mean_rr  (differs slightly from the mean of the
                 sensor's own bpm field, which is itself a smoothed estimate)
    sdnn_ms      standard deviation of RR -- overall variability
    rmssd_ms     root mean square of successive differences -- short-term,
                 parasympathetic ("relaxation") component
    pnn50_pct    percentage of successive differences greater than 50 ms
    """
    rejected = 0
    if filtered:
        rr_ms, rejected = filter_rr(rr_ms)
    n = len(rr_ms)
    out = {
        "n_beats": n,
        "n_rejected": rejected,
        "mean_rr_ms": None,
        "hr_mean_bpm": None,
        "hr_min_bpm": None,
        "hr_max_bpm": None,
        "sdnn_ms": None,
        "rmssd_ms": None,
        "pnn50_pct": None,
    }
    if n == 0:
        return out

    mean_rr = sum(rr_ms) / n
    out["mean_rr_ms"] = round(mean_rr, 2)
    out["hr_mean_bpm"] = round(60000.0 / mean_rr, 2)
    out["hr_min_bpm"] = round(60000.0 / max(rr_ms), 2)
    out["hr_max_bpm"] = round(60000.0 / min(rr_ms), 2)

    if n >= 2:
        var = sum((rr - mean_rr) ** 2 for rr in rr_ms) / (n - 1)
        out["sdnn_ms"] = round(math.sqrt(var), 2)
        diffs = [rr_ms[i + 1] - rr_ms[i] for i in range(n - 1)]
        out["rmssd_ms"] = round(math.sqrt(sum(d * d for d in diffs) / len(diffs)), 2)
        out["pnn50_pct"] = round(
            100.0 * sum(1 for d in diffs if abs(d) > 50.0) / len(diffs), 2)
    return out


def instantaneous_hr(rr_ms):
    """Per-beat heart rate in bpm from a list of RR intervals."""
    return [60000.0 / rr for rr in rr_ms if rr]
