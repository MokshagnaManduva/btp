"""Numbering runs per subject, and tying answers back to the run they came from."""

from __future__ import annotations

from .geometry import parse_time


class SessionInstance:
    """One run of one session by one subject.

    ``number`` is that subject's Nth session overall, counted chronologically
    across every database parsed -- so if a player runs gridshot, then flicks,
    then gridshot again, those are their sessions 1, 2 and 3.
    """

    __slots__ = ("db", "session_id", "subject_id", "t_start", "t_end", "row",
                 "number", "code", "warmup")

    def __init__(self, db, row):
        self.db = db
        self.row = row
        self.session_id = row.get("session_id")
        self.subject_id = row.get("subject_id") or "unknown"
        self.t_start = parse_time(row.get("start_time"))
        self.t_end = parse_time(row.get("end_time"))
        self.number = None
        self.code = ""          # stable per-person id, e.g. S01
        self.warmup = ""        # answer to the warm-up question, if asked


def index_sessions(results_list):
    """Number every session per subject, chronologically. Returns the list."""
    insts = []
    for res in results_list:
        for s in res.sessions:
            insts.append(SessionInstance(res.name, s))

    by_subject = {}
    for i in insts:
        by_subject.setdefault(i.subject_id, []).append(i)
    for lst in by_subject.values():
        lst.sort(key=lambda x: (x.t_start if x.t_start is not None else 0.0))
        for n, inst in enumerate(lst, start=1):
            inst.number = n

    # Stable short id per person, ordered by when they first played. Kept
    # separate from the typed FPSci user id so the CSVs can carry both an
    # anonymous key and the readable name.
    first_seen = sorted(
        by_subject.items(),
        key=lambda kv: min((i.t_start if i.t_start is not None else 0.0) for i in kv[1]))
    for n, (_name, lst) in enumerate(first_seen, start=1):
        for inst in lst:
            inst.code = f"S{n:02d}"
    return insts


# Any question whose prompt mentions "warm" is treated as the warm-up
# condition, so rewording the prompt in the .Any config does not break this.
WARMUP_PROMPT_KEY = "warm"


def attach_warmup(insts, results_list):
    """Fill in each session run's warm-up label from the Questions table.

    As of the Phase 1 config there is no warm-up question in the game any more
    -- the condition is chosen in the launcher and joined from the sitting
    manifest instead. This is kept because it still correctly reads databases
    recorded before that change, and because it costs nothing when the table
    holds no matching prompt.
    """
    for res in results_list:
        for q in res.questions:
            prompt = (q.get("question") or "").lower()
            if WARMUP_PROMPT_KEY not in prompt:
                continue
            resp = q.get("response")
            if resp is None or str(resp).strip() == "":
                continue
            inst = resolve_session(insts, res.name, q.get("session_id"),
                                   parse_time(q.get("time")))
            if inst is not None:
                inst.warmup = str(resp).strip()


def resolve_session(insts, db, session_id, t_trial):
    """Find which run of ``session_id`` a trial at time ``t_trial`` belongs to.

    Matching on session id alone is not enough once a scenario is replayed, so
    the trial's own timestamp picks out the right run.
    """
    cands = [i for i in insts if i.db == db and i.session_id == session_id]
    if not cands:
        return None
    if t_trial is not None:
        for i in cands:
            if i.t_start is not None and i.t_end is not None and i.t_start <= t_trial <= i.t_end:
                return i
        prior = [i for i in cands if i.t_start is not None and i.t_start <= t_trial]
        if prior:
            return max(prior, key=lambda x: x.t_start)
    return cands[0]
