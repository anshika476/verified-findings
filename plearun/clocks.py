"""Put every record on one timeline.

1. `resolve_times` turns each row's timestamp into seconds on its own clock.
2. `align` estimates each clock's offset to a reference clock from events
   that appear in more than one source (anchors), e.g. the stop appears as a
   0 mph camera reading, an officer note and a transcript cue.

offset[clock] = reference_time - clock_time, with an uncertainty in seconds.
A clock is left unaligned (and its rows unused for timing) when it has no
anchor, when its anchors disagree, or when the offset is implausible.
The report is never used to estimate offsets: that would be circular.
"""
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import timeutil

CLUSTER_GAP = 120.0      # rows of one kind this close together are one event
MAX_ABS_OFFSET = 1800.0  # an absolute clock off by >30 min is not trusted
SOURCE_PREFERENCE = ["notes", "dispatch", "camera", "transcript"]


def rss(*xs):
    return math.sqrt(sum(x * x for x in xs))


# ---------------------------------------------------------------- step 1
def resolve_times(recs, report_date, warn):
    classified = [r.ts for r in recs if r.ts and r.ts[0] != "cue"]
    global_off = timeutil.dominant_offset(classified)
    if global_off is None:
        warn("no timestamp carries a UTC offset; assuming UTC for naive times")
    per_file = defaultdict(list)
    for r in recs:
        if r.ts and r.ts[0] != "cue":
            per_file[r.file].append(r.ts)
    tz = timezone(global_off) if global_off is not None else timezone.utc
    file_tz = {}                                            # naive values follow their own file first
    for f, cs in per_file.items():
        own = timeutil.dominant_offset(cs)
        file_tz[f] = timezone(own) if own is not None else tz

    # Pass 1: rows that carry their own date.
    for r in recs:
        if r.ts and r.ts[0] in ("epoch", "aware", "naive"):
            r.t = timeutil.resolve(r.ts, file_tz[r.file], None)
    day_of = lambda r: datetime.fromtimestamp(r.t, file_tz[r.file]).date()
    dated_days = {day_of(r) for r in recs if r.t is not None and not r.relative}
    # Incident date: the report's, else the one date all dated records share, else unknown.
    day = report_date or (next(iter(dated_days)) if len(dated_days) == 1 else None)

    # Pass 2: bare times ("21:15") take their date from the dated rows just above
    # and below them in the same file. If those two disagree (midnight in between)
    # the time is left unusable rather than guessed. No dated neighbours at all:
    # use the incident date, if known.
    by_file = defaultdict(list)
    for r in recs:
        by_file[r.file].append(r)
    unusable = 0
    for rows in by_file.values():
        prev, before = None, []
        for r in rows:                                      # nearest dated row above
            before.append(prev)
            if r.ts and r.ts[0] != "tod" and r.t is not None:
                prev = r
        nxt, after = None, []
        for r in reversed(rows):                            # nearest dated row below
            after.append(nxt)
            if r.ts and r.ts[0] != "tod" and r.t is not None:
                nxt = r
        after.reverse()
        for r, up, down in zip(rows, before, after):
            if not r.ts or r.ts[0] != "tod":
                continue
            days = {day_of(x) for x in (up, down) if x is not None}
            use = days.pop() if len(days) == 1 else (day if not days else None)
            if use is None:
                unusable += 1
                continue
            r.t = timeutil.resolve(r.ts, file_tz[r.file], use)
    if unusable:
        warn(f"{unusable} time-only value(s) left unused: their date is unclear")
    return tz, day


# ---------------------------------------------------------------- step 2
def clusters(recs):
    """Group one clock's rows of one kind into events; return representatives."""
    reps, last = [], None
    for r in sorted(recs, key=lambda r: (r.t, r.row)):
        if last is None or r.t - last > CLUSTER_GAP:
            reps.append(r)
        last = r.t
    return reps


class Timeline:
    def __init__(self, recs, log, tz=timezone.utc):
        self.log, self.tz = log, tz
        self.by_clock = defaultdict(list)
        for r in recs:
            if r.t is not None:
                self.by_clock[r.clock].append(r)
        self.events = {}                         # clock -> kind -> [representative rows]
        for clock, rows in self.by_clock.items():
            kinds = defaultdict(list)
            for r in rows:
                if r.kind:
                    kinds[r.kind].append(r)
            self.events[clock] = {k: clusters(v) for k, v in kinds.items()}
        self.offset = {}                         # clock -> (offset, uncertainty)
        self.reason = {}
        self.ref = None

    def source(self, clock):
        return self.by_clock[clock][0].source

    def relative(self, clock):
        return self.by_clock[clock][0].relative

    def ref_time(self, r):
        """(time on reference clock, uncertainty) or None if not alignable."""
        if r.t is None or r.clock not in self.offset:
            return None
        off, unc = self.offset[r.clock]
        return r.t + off, rss(r.unc, unc)

    # ------------------------------------------------------------
    def choose_reference(self):
        linked = [c for c in self.events if self.events[c] and not self.relative(c)]
        if not linked:
            return None

        def score(c):
            src = self.source(c)
            pref = SOURCE_PREFERENCE.index(src) if src in SOURCE_PREFERENCE else 9
            return (pref, -len(self.events[c]), c)
        return min(linked, key=score)

    def consensus(self, kind, exclude):
        """Reference-clock time of `kind` from clocks already aligned."""
        pts = []
        for c, (off, unc) in self.offset.items():
            if c == exclude:
                continue
            reps = self.events[c].get(kind, [])
            if len(reps) == 1:
                # rank: reference clock first, then absolute clocks, then by uncertainty
                rank = (c != self.ref, self.relative(c), rss(reps[0].unc, unc))
                pts.append((reps[0].t + off, rss(reps[0].unc, unc), rank))
        if not pts:
            return None
        best = min(pts, key=lambda p: p[2])
        if any(abs(p[0] - best[0]) > 2 * rss(p[1], best[1]) + 60 for p in pts):
            return None                          # aligned sources disagree on this event
        return best[:2]

    def estimate(self, clock):
        anchors = []
        for kind, reps in self.events[clock].items():
            ref = self.consensus(kind, clock)
            if ref is None:
                continue
            rep = reps[0] if len(reps) == 1 else self._nearest(reps, ref[0], clock)
            if rep is None:
                continue
            anchors.append((ref[0] - rep.t, rss(ref[1], rep.unc), kind, rep.row))
        need = 2 if self.relative(clock) else 1
        if len(anchors) < need:
            return None, f"{len(anchors)} usable anchor(s), need {need}"
        best = []
        for a in anchors:                        # largest mutually consistent group
            group = [b for b in anchors if abs(a[0] - b[0]) <= 2 * rss(a[1], b[1])]
            if len(group) > len(best):
                best = group
        if len(best) * 2 <= len(anchors) and len(anchors) > 1:
            return None, f"anchors disagree: {[(k, round(o)) for o, _, k, _ in anchors]}"
        w = [1 / (u * u) for _, u, _, _ in best]
        off = sum(o * wi for (o, _, _, _), wi in zip(best, w)) / sum(w)
        spread = (max(o for o, *_ in best) - min(o for o, *_ in best)) / 2
        unc = max(1 / math.sqrt(sum(w)), spread)
        if not self.relative(clock) and abs(off) > MAX_ABS_OFFSET:
            return None, f"offset {off:+.0f}s is implausibly large"
        return (off, unc), "anchors " + ", ".join(f"{k}@row{row}" for _, _, k, row in best)

    def _nearest(self, reps, ref_t, clock):
        """Several candidate events: accept the nearest only if it clearly wins."""
        if self.relative(clock):
            return None
        d = sorted((abs(r.t - ref_t), r.row, r) for r in reps)
        if d[0][0] <= MAX_ABS_OFFSET and d[1][0] >= 3 * d[0][0] + 300:
            return d[0][2]
        return None

    def align(self):
        ref = self.choose_reference()
        if ref is None:
            self.log("no anchor events: no clock can be aligned")
            return
        self.ref = ref
        self.offset[ref] = (0.0, 0.0)
        self.reason[ref] = "reference clock"
        changed = True
        while changed:
            changed = False
            for clock in sorted(self.events):
                if clock in self.offset:
                    continue
                est, why = self.estimate(clock)
                self.reason[clock] = why
                if est:
                    self.offset[clock] = est
                    changed = True
        for clock in sorted(self.by_clock):
            if clock in self.offset:
                off, unc = self.offset[clock]
                shown = f"{off:+.0f}s ±{unc:.0f}s" if not self.relative(clock) else \
                    f"cue 0 = {timeutil.fmt(off, self.tz)} ±{unc:.0f}s"
                self.log(f"clock {clock}: {shown} ({self.reason[clock]})")
            else:
                self.log(f"clock {clock}: NOT aligned ({self.reason.get(clock, 'no anchor events')})")
