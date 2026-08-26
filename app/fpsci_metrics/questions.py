"""Reading answers out of the FPSci ``Questions`` table.

The trap this module exists to avoid: ``response`` holds the option label *as
it was displayed*, not the option text from the config. With ``optionKeys`` set
the submit prompt stores ``"Yes (Y)"``, never ``"Yes"``. The row also carries
``presented_responses`` and ``response_array``, so the clean answer is
recovered by index -- which stays correct even if options are ever shown in a
random order.

The submit answer decides whether a run reaches the analysis tables, so
anything that cannot be mapped is reported as unanswered rather than guessed.
"""

from __future__ import annotations

import re

# G3D serialises an Any array as: ( "Yes", "No")
ANY_STRINGS = re.compile(r'"([^"]*)"')

SUBMIT_PROMPT_KEY = "submit"


def clean_answer(row):
    """The chosen option as written in the config, or None if unmappable."""
    resp = (row.get("response") or "").strip()
    if not resp:
        return None
    shown = ANY_STRINGS.findall(row.get("presented_responses") or "")
    clean = ANY_STRINGS.findall(row.get("response_array") or "")
    if resp in shown and len(shown) == len(clean):
        return clean[shown.index(resp)]
    # No key hints configured: the displayed label is already the clean one.
    if resp in clean:
        return resp
    return None


def submit_state(questions):
    """Classify a run from its question rows.

    Returns "yes", "no", or "unanswered". Anything unrecognised is
    "unanswered", so a run is never silently treated as submitted.
    """
    rows = [q for q in questions
            if SUBMIT_PROMPT_KEY in (q.get("question") or "").lower()]
    if not rows:
        return "unanswered"
    answer = clean_answer(rows[-1])
    if answer == "Yes":
        return "yes"
    if answer == "No":
        return "no"
    return "unanswered"
