"""Load every record file into a flat list of `Rec` rows.

Row numbering (used in citations):
  CSV  : 1-based data row, after any '#' marker lines and the header.
  JSON : 1-based position in the event list.
  VTT  : 1-based cue number in file order.
`Rec.raw` is the exact decoded text of that row; quotes are cut from it.
Record text is only ever pattern-matched as data, never acted on.
"""
import csv
import json
import os
import re
from dataclasses import dataclass, field

from . import timeutil
from .kinds import kinds_in, pick_one

MAX_SPEED = 250.0


@dataclass
class Rec:
    file: str
    row: int
    raw: str
    clock: str                    # rows sharing a clock share one offset
    source: str                   # camera | dispatch | notes | transcript
    kind: str | None = None       # CALL, STOP, OBSERVE, ON_SCENE, DISPATCHED
    ts: tuple | None = None       # timeutil.classify() result
    t: float | None = None        # clock reading in seconds (set by resolve step)
    relative: bool = False        # True for transcript cue times
    speed: float | None = None
    zone: float | None = None
    unit: str | None = None
    ident: str | None = None
    text: str = ""
    quote: str = ""               # exact substring of raw to cite
    extra: dict = field(default_factory=dict)

    @property
    def unc(self):
        return self.ts[2] if self.ts else None


# ---------------------------------------------------------------- decoding
def decode(data: bytes) -> str:
    """UTF-8 (BOM stripped) if valid, otherwise Windows-1252, otherwise Latin-1."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def read_text(path):
    with open(path, "rb") as fh:
        return decode(fh.read())


# ---------------------------------------------------------------- CSV
def csv_rows(text):
    """Yield (header, fields, raw_text) for each data row.

    Leading '#' marker lines and blank lines are skipped. raw_text is the
    exact source text of the row (a quoted field may span several lines).
    """
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    consumed = []

    def feed():
        for ln in lines[i:]:
            consumed.append(ln)
            yield ln

    header = None
    for fields in csv.reader(feed()):
        raw = "".join(consumed).rstrip("\r\n")
        consumed.clear()
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if header is None:
            header = [h.strip().lower() for h in fields]
            continue
        yield header, fields, raw


def _col(header, pattern):
    for idx, name in enumerate(header):
        if re.search(pattern, name):
            return idx
    return None


def _num(s):
    try:
        v = float(str(s).strip())
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= MAX_SPEED else None


def load_csv(path, name):
    out = []
    rows = csv_rows(read_text(path))
    for n, (header, fields, raw) in enumerate(rows, start=1):
        get = lambda idx: fields[idx].strip() if idx is not None and idx < len(fields) else ""
        c_time = _col(header, r"time|logged|date|^ts$")
        c_speed = _col(header, r"speed")
        if c_speed is not None:                                   # camera export
            device = get(_col(header, r"device|camera|cam")) or "?"
            r = Rec(name, n, raw, f"{name}:{device}", "camera", ts=timeutil.classify(get(c_time)),
                    speed=_num(get(c_speed)), zone=_num(get(_col(header, r"zone|limit|posted"))),
                    ident=device, quote=raw.strip())
            if r.speed == 0:
                r.kind = "STOP"                                   # stationary vehicle
        else:                                                     # free-text notes
            text = get(_col(header, r"note|text|desc|comment|message|remark"))
            r = Rec(name, n, raw, name, "notes", ts=timeutil.classify(get(c_time)),
                    ident=get(_col(header, r"^id$")), text=text,
                    kind=pick_one(kinds_in(text)), quote=raw.strip())
            r.extra["author"] = get(_col(header, r"author|officer|^by$|source"))
        out.append(r)
    return out


# ---------------------------------------------------------------- JSON
def _hook(pairs):
    d, dups = {}, set()
    for k, v in pairs:
        if k in d:
            dups.add(k)
        d[k] = v
    if dups:
        d["__duplicate_keys__"] = sorted(dups)
    return d


DECODER = json.JSONDecoder(object_pairs_hook=_hook)
TIME_KEY = r"logged|time|timestamp|^ts$|^at$|date"


def _time_key(ev):
    for k in ev:
        if re.search(TIME_KEY, k.lower()) and timeutil.classify(ev[k]):
            return k
    return None


def _find_event_list(node, key=None):
    """Return (key, list) for the list of dicts with the most timestamped items."""
    best = (0, None, None)
    stack = [(key, node)]
    while stack:
        k, v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.items())
        elif isinstance(v, list):
            score = sum(1 for e in v if isinstance(e, dict) and _time_key(e))
            if score > best[0]:
                best = (score, k, v)
            stack.extend((k, e) for e in v)
    return best[1], best[2]


def _element_spans(text, key, events):
    """Character spans of each element of `events` in the raw JSON text."""
    starts = ([m.end() - 1 for m in re.finditer(r'"%s"\s*:\s*\[' % re.escape(key), text)]
              if key else [text.index("[")] if "[" in text else [])
    for start in starts:
        try:
            value, _ = DECODER.raw_decode(text, start)
        except ValueError:
            continue
        if value != events:
            continue
        spans, pos = [], start + 1
        ws = re.compile(r"[\s,]*")
        while True:
            pos = ws.match(text, pos).end()
            if pos >= len(text) or text[pos] == "]":
                return spans
            _, end = DECODER.raw_decode(text, pos)
            spans.append((pos, end))
            pos = end
    return None


def _quote_for_key(raw, key, value):
    """The exact `"key": value` text in raw whose value equals the parsed value."""
    pat = re.compile(r'"%s"\s*:\s*' % re.escape(key))
    found = None
    for m in pat.finditer(raw):
        try:
            v, end = DECODER.raw_decode(raw, m.end())
        except ValueError:
            continue
        if v == value:
            found = raw[m.start():end]
    return found


def load_json(path, name):
    text = read_text(path)
    data = DECODER.decode(text)
    key, events = _find_event_list(data)
    if not events:
        return []
    spans = _element_spans(text, key, events)
    out = []
    for n, ev in enumerate(events, start=1):
        if not isinstance(ev, dict):
            continue
        raw = text[spans[n - 1][0]:spans[n - 1][1]] if spans else json.dumps(ev)
        tk = _time_key(ev)
        dup = set(ev.get("__duplicate_keys__", []))
        etype = str(ev.get("type") or ev.get("event") or ev.get("event_type") or "")
        r = Rec(name, n, raw, name, "dispatch",
                kind=pick_one(kinds_in(etype.replace("_", " "))),
                ts=None if (tk is None or tk in dup) else timeutil.classify(ev[tk]),
                unit=str(ev["unit"]) if ev.get("unit") is not None else None,
                ident=str(ev.get("id", "")), text=etype)
        if r.ts:
            r.quote = _quote_for_key(raw, tk, ev[tk]) or ""
        if "type" in dup:
            r.kind = None                                       # contradictory event type
        out.append(r)
    return out


# ---------------------------------------------------------------- WebVTT
CUE_TIME = re.compile(r"((?:\d+:)?\d{1,2}:\d{2}(?:\.\d{1,3})?)\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}(?:\.\d{1,3})?)")
SPEED_CALL = re.compile(r"\b(\d{2,3})\s+in\s+an?\s+(\d{2})\b")


def _cue_seconds(s):
    parts = [float(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def load_vtt(path, name):
    text = read_text(path)
    blocks = re.split(r"\r?\n[ \t]*\r?\n", text.replace("\r\n", "\n"))
    out, n = [], 0
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines or re.match(r"^(WEBVTT|NOTE|STYLE|REGION)\b", lines[0]):
            continue
        ti = next((i for i, ln in enumerate(lines[:2]) if CUE_TIME.search(ln)), None)
        if ti is None:
            continue
        n += 1
        m = CUE_TIME.search(lines[ti])
        payload = lines[ti + 1:]
        spoken = re.sub(r"<[^>]+>", "", " ".join(payload))
        start, end = _cue_seconds(m.group(1)), _cue_seconds(m.group(2))
        kinds = kinds_in(spoken) & {"STOP", "OBSERVE"}     # speech anchors only what is happening now
        if SPEED_CALL.search(spoken):
            kinds.add("OBSERVE")
        r = Rec(name, n, "\n".join(lines), name, "transcript", kind=pick_one(kinds),
                ts=("cue", start, max(1.0, (end - start) / 2)), t=start, relative=True,
                text=spoken, quote=payload[0].strip() if payload else "")
        out.append(r)
    return out


# ---------------------------------------------------------------- entry point
LOADERS = {".csv": load_csv, ".json": load_json, ".vtt": load_vtt}


def load_dir(records_dir, warn):
    recs = []
    for name in sorted(os.listdir(records_dir)):
        path = os.path.join(records_dir, name)
        loader = LOADERS.get(os.path.splitext(name)[1].lower())
        if not loader or not os.path.isfile(path):
            continue
        try:
            recs.extend(loader(path, name))
        except Exception as exc:          # one broken file must not sink the run
            warn(f"skipped {name}: {exc.__class__.__name__}: {exc}")
    return recs


def raw_row(records_dir, file, row):
    """Re-read a file from disk and return the raw text of one cited row."""
    path = os.path.join(records_dir, file)
    loader = LOADERS.get(os.path.splitext(file)[1].lower())
    for r in loader(path, file) if loader else []:
        if r.row == row:
            return r.raw
    return None
