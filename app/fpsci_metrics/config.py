"""Tunables for the kinematic metrics, and the constants that pin them down.

Lifted unchanged from FPSci/scripts/analysis/fpsci_parse.py. Changing anything
here changes every number downstream, so the golden test in app/tests will fail
loudly if a value moves -- which is the point.
"""

from datetime import datetime

#: Angular velocity is measured over this fixed time window rather than
#: frame-to-frame. In unlocked frame-rate mode the render rate (400-500+ fps)
#: far exceeds the mouse polling rate, so ~70% of frames report an aim
#: identical to the previous one. Frame-to-frame differencing therefore reads
#: zero on most frames -- which hides sustained motion entirely and inflates
#: the peak (a whole poll's movement divided by one 2 ms frame). A fixed time
#: base is immune to that and yields physically meaningful deg/s at any fps.
VELOCITY_WINDOW_S = 0.010

#: Angular speed (deg/s) that counts as "the player has started moving".
#: Used to separate reaction time from movement time.
ONSET_SPEED_DPS = 15.0

#: The onset speed must hold for at least this long to count, so a single
#: noisy frame does not register as a movement onset.
ONSET_SUSTAIN_S = 0.02

#: Minimum peak speed for a velocity peak to count as a corrective submovement.
SUBMOVEMENT_MIN_DPS = 20.0

#: A peak must rise this fraction above the surrounding troughs to count,
#: which keeps ripple on a single smooth flick from inflating the count.
SUBMOVEMENT_PROMINENCE = 0.25

#: Half-width (in samples) of the moving-average applied to the speed profile
#: before peak picking.
SMOOTH_HALFWIDTH = 2

#: How far a trajectory sample may be from a query time and still be used, in
#: seconds. Player actions and target trajectories are both logged per frame,
#: so at 60 Hz or better the true gap is well under this.
TRAJECTORY_MATCH_TOL_S = 0.05

#: Frame time above ``STUTTER_FACTOR * median`` is reported as a stutter.
STUTTER_FACTOR = 2.0

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")
EPOCH = datetime(1970, 1, 1)

HIT_EVENTS = ("hit", "destroy")
SHOT_EVENTS = ("hit", "destroy", "miss")
