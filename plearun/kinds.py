"""Event kinds shared by claims and records, detected by keyword patterns."""
import re

PATTERNS = {
    "ON_SCENE": r"\bon[- ]scene\b|\barriv(?:ed|es|al|ing)\b",
    "DISPATCHED": r"\bdispatched\b|\ben[- ]?route\b",
    "STOP": r"\bstop(?:ped|s)?\b|\bpull(?:ed|ing|s)?\s+(?:in|into|over)\b|\bcame to a (?:stop|halt)\b",
    "OBSERVE": r"\bobserv(?:ed|es|ing|ation)\b|\bspotted\b|\bclocked\b|\bfirst saw\b",
    "CALL": r"\b911\b|\bcaller\b|\bcalls?\b",
}
# When one row mentions several kinds, the more specific action wins
# ("unit dispatched to the 911 call" is a dispatch, not a call).
PRIORITY = ["ON_SCENE", "DISPATCHED", "STOP", "OBSERVE", "CALL"]
COMPILED = {k: re.compile(p, re.I) for k, p in PATTERNS.items()}


def kinds_in(text):
    return {k for k, rx in COMPILED.items() if rx.search(text or "")}


def pick_one(kinds):
    for k in PRIORITY:
        if k in kinds:
            return k
    return None


def positions(text):
    """[(start, kind)] for every keyword hit, in text order."""
    hits = [(m.start(), k) for k, rx in COMPILED.items() for m in rx.finditer(text)]
    return sorted(hits)
