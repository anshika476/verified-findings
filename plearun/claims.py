"""Split a report into sentence claims and pull out checkable atoms.

An atom is one checkable fact: an event time, a gap between two events,
a speed, a posted limit, or a count. A sentence with no atoms is
not_checkable. Words we cannot check (colour, mile marker, "only car in
the lane") are simply not turned into atoms.
"""
import re
from dataclasses import dataclass, field

from . import kinds

WORDNUM = {"one": 1, "a single": 1, "only one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
NUM = r"\d+(?:\.\d+)?|" + "|".join(sorted(WORDNUM, key=len, reverse=True))
APPROX = r"about|around|approximately|approx\.?|roughly|circa|nearly|almost|some|~"
ABBREV = {"approx", "a.m", "p.m", "am", "pm", "no", "mr", "mrs", "ms", "dr", "ofc", "sgt",
          "lt", "st", "rt", "e.g", "i.e", "vs", "u.s", "approx.", "det", "cpl"}

RE_REL = re.compile(
    rf"(?P<within>\bwithin\s+)?(?P<q>\b(?:{APPROX})\s*)?(?P<n>\b(?:{NUM}))\s*(?P<unit>minutes?|mins?|seconds?|secs?)\s+"
    rf"(?P<rel>after|before|following|later than|prior to|of)\s+(?:the\s+)?(?P<ref>[\w ]{{2,30}}?)(?=[.,;]|$|\s+(?:and|but|while)\b)",
    re.I)
RE_TIME = re.compile(
    rf"(?P<q>\b(?:{APPROX}|by|before|after|no later than)\s+|~\s*)?(?<![\d:])(?P<h>\d{{1,2}}):(?P<m>\d{{2}})(?::(?P<s>\d{{2}}))?"
    r"(?![\d:])\s*(?P<ap>[ap]\.?m\.?)?", re.I)
RE_ZONE = re.compile(r"(?:posted|speed limit(?:\s+(?:of|is|was))?|limit of)\s*(?:of\s*)?(?P<v>\d{2,3})(?:\s*mph)?(?:\s*zone)?"
                     r"|(?P<v2>\d{2,3})\s*(?:mph\s*)?zone", re.I)
RE_SPEED = re.compile(rf"(?P<q>\b(?:{APPROX}|over|more than|at least|above|under|less than|below)\s+)?"
                      r"(?P<v>\d{1,3}(?:\.\d)?)\s*(?:mph|miles per hour|mi/h)\b", re.I)
RE_COUNT = re.compile(rf"\b(?!911\s+calls?\b)(?P<n>{NUM})\s+(?:separate\s+|different\s+|distinct\s+)?(?P<what>911 calls?|calls?|units?)\b", re.I)


@dataclass
class Atom:
    type: str                      # time | relative | speed | zone | count
    kind: str | None = None        # event kind (time/relative)
    ref_kind: str | None = None    # other event (relative)
    value: float = 0.0             # seconds since midnight, gap seconds, mph, count
    approx: bool = False
    bound: str = "="               # "=", "<=", ">="
    has_seconds: bool = False
    what: str = ""                 # count target


@dataclass
class Claim:
    text: str
    atoms: list = field(default_factory=list)


def _num(s):
    s = s.lower().strip()
    return float(WORDNUM[s]) if s in WORDNUM else float(s)


def sentences(text):
    flat = re.sub(r"\s+", " ", text).strip()
    out, start = [], 0
    for m in re.finditer(r"[.!?](?=\s+[A-Z0-9\"'(\[]|\s*$)", flat):
        prev = flat[start:m.start()].split(" ")[-1].lower().rstrip(".")
        if prev in ABBREV or (len(prev) == 1 and prev.isalpha()):
            continue
        out.append(flat[start:m.end()].strip())
        start = m.end()
    if flat[start:].strip():
        out.append(flat[start:].strip())
    return [s for s in out if s]


def is_metadata(sentence):
    return bool(re.search(r"\b(SYNTHETIC|FICTIONAL)\b", sentence) or
                re.match(r"^Incident\s+\d{4}-\d{2}-\d{2}\b", sentence))


def _bound(q):
    q = (q or "").strip().lower()
    if q in ("by", "before", "no later than", "under", "less than", "below"):
        return "<="
    if q in ("after", "over", "more than", "at least", "above"):
        return ">="
    return "="


def _approx(q):
    return bool(q) and bool(re.fullmatch(APPROX, q.strip(), re.I))


def parse_claim(sentence):
    claim = Claim(sentence)
    work = sentence

    for m in RE_REL.finditer(sentence):
        ref = kinds.pick_one(kinds.kinds_in(m.group("ref")))
        if ref is None:
            continue
        mult = 60.0 if m.group("unit").lower().startswith("min") else 1.0
        rel = m.group("rel").lower()
        bound = "<=" if m.group("within") else "="
        sign = -1.0 if rel in ("before", "prior to") else 1.0
        claim.atoms.append(Atom("relative", ref_kind=ref, value=sign * _num(m.group("n")) * mult,
                                approx=_approx(m.group("q")), bound=bound))
        work = work.replace(m.group(0), " " * len(m.group(0)))   # keep positions stable

    hits = kinds.positions(work)
    main_kinds = [k for _, k in hits]
    for a in claim.atoms:                                            # subject of the gap
        a.kind = kinds.pick_one(set(main_kinds) - {a.ref_kind})

    for m in RE_COUNT.finditer(work):
        what = "units" if m.group("what").lower().startswith("unit") else "calls"
        claim.atoms.append(Atom("count", value=_num(m.group("n")), what=what))

    zone_spans = []
    for m in RE_ZONE.finditer(work):
        zone_spans.append(m.span())
        claim.atoms.append(Atom("zone", value=float(m.group("v") or m.group("v2"))))
    for m in RE_SPEED.finditer(work):
        if any(a <= m.start("v") < b for a, b in zone_spans):
            continue
        q = m.group("q")
        claim.atoms.append(Atom("speed", value=float(m.group("v")), approx=_approx(q), bound=_bound(q)))

    for m in RE_TIME.finditer(work):
        h, mi = int(m.group("h")), int(m.group("m"))
        ap = (m.group("ap") or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            continue
        sec = int(m.group("s") or 0)
        before = [(m.start() - p, k) for p, k in hits if p < m.start()]
        after = [(p - m.end(), k) for p, k in hits if p >= m.end()]
        near = min(before or after or [(0, None)])[1]                # nearest event word, prefer preceding
        q = m.group("q")
        claim.atoms.append(Atom("time", kind=near, value=h * 3600 + mi * 60 + sec,
                                approx=_approx(q), bound=_bound(q), has_seconds=bool(m.group("s"))))
    return claim


def parse_report(text):
    from .timeutil import parse_date
    sents = sentences(text)
    day = next((parse_date(s) for s in sents if is_metadata(s) and parse_date(s)), None) or parse_date(text)
    return day, [parse_claim(s) for s in sents if not is_metadata(s)]
