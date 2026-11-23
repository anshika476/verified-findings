"""Decide each atom, then each claim.

Every comparison has a claim tolerance (tol) and an evidence uncertainty (u):
  match    if the evidence is inside tol even in the worst case: |d| + u <= tol
  conflict if it is outside tol + a safety margin in the best case: |d| - u > tol + margin
  otherwise not settled.
A conflict also needs the evidence to be attributable: e.g. a camera speed
only counts when exactly one reading could be the vehicle in question.
"""
from dataclasses import dataclass

from .clocks import rss

TIME_TOL = {"exact": 60.0, "approx": 120.0, "seconds": 30.0}
TIME_MARGIN = 60.0
SPEED_TOL = {"exact": (1.0, 4.0), "approx": (3.0, 8.0)}     # (match within, conflict beyond)
CITE_ORDER = {
    "CALL": ["dispatch", "notes"], "DISPATCHED": ["dispatch", "notes"],
    "ON_SCENE": ["dispatch", "notes"], "STOP": ["notes", "dispatch", "camera", "transcript"],
    "OBSERVE": ["notes", "transcript", "camera"],
}
ATOM_CITE_PRIORITY = ["speed", "relative", "count", "time", "zone"]


@dataclass
class Result:
    status: str            # match | conflict | unsettled | none (no evidence of this type)
    rec: object = None
    note: str = ""
    alts: list = None      # every row that supports the same verdict (for the optional LLM)


class Checker:
    def __init__(self, recs, timeline, claim_time):
        self.recs, self.tl, self.claim_time = recs, timeline, claim_time

    # -------------------------------------------------------- helpers
    def events(self, kind):
        """Aligned representative rows of `kind`, grouped by clock."""
        out = {}
        for clock, kinds in self.tl.events.items():
            if kind in kinds and clock in self.tl.offset:
                out[clock] = kinds[kind]
        return out

    def exists(self, kind):
        return any(r.kind == kind for r in self.recs)

    def unplaced(self, kind):
        """Rows of this kind on clocks we could not align: evidence we cannot rule out."""
        return any(r.kind == kind and r.clock not in self.tl.offset for r in self.recs)

    def _guard(self, res, *kinds):
        if res.status == "conflict" and any(self.unplaced(k) for k in kinds):
            return Result("unsettled", note=res.note + "; but an unaligned source also has this event")
        return res

    def _cite(self, kind, recs):
        order = CITE_ORDER.get(kind, [])
        return min(recs, key=lambda r: (order.index(r.source) if r.source in order else 9, r.file, r.row))

    @staticmethod
    def _judge(d, u, tol, margin, bound="="):
        if bound == "<=":
            return "in" if d + u <= tol else ("out" if d - u > tol + margin else "?")
        if bound == ">=":
            return "in" if d - u >= -tol else ("out" if d + u < -tol - margin else "?")
        if abs(d) + u <= tol:
            return "in"
        return "out" if abs(d) - u > tol + margin else "?"

    def _combine(self, per_clock, kind, label):
        """per_clock: {clock: [(verdict, rec)]}. Same-clock events are alternatives
        (any one may be the event meant); different clocks must not disagree."""
        verdicts = {}
        for clock, items in per_clock.items():
            vs = {v for v, _ in items}
            verdicts[clock] = "in" if "in" in vs else ("out" if vs == {"out"} else "?")
        if not verdicts:
            return Result("unsettled", note=f"{label}: no aligned evidence")
        if "in" in verdicts.values() and "out" not in verdicts.values():
            recs = [r for c, items in per_clock.items() for v, r in items if v == "in"]
            return Result("match", self._cite(kind, recs), f"{label}: consistent", recs)
        if set(verdicts.values()) == {"out"}:
            recs = [r for items in per_clock.values() for _, r in items]
            return Result("conflict", self._cite(kind, recs), f"{label}: every aligned source disagrees", recs)
        return Result("unsettled", note=f"{label}: sources {verdicts}")

    # -------------------------------------------------------- atoms
    def time(self, a):
        if a.kind is None:
            return Result("none", note="time not tied to a known event")
        groups = self.events(a.kind)
        if not groups:
            return Result("unsettled" if self.exists(a.kind) else "none",
                          note=f"no aligned {a.kind} record")
        t = self.claim_time(a.value)
        tol = TIME_TOL["seconds" if a.has_seconds else "approx" if a.approx else "exact"]
        per = {c: [(self._judge(self.tl.ref_time(r)[0] - t, self.tl.ref_time(r)[1], tol, TIME_MARGIN, a.bound), r)
                   for r in reps] for c, reps in groups.items()}
        return self._guard(self._combine(per, a.kind, f"{a.kind} time"), a.kind)

    def relative(self, a):
        if a.kind is None:
            return Result("none", note="gap subject unknown")
        subj, ref = self.events(a.kind), self.events(a.ref_kind)
        if not subj or not ref:
            missing = a.kind if not subj else a.ref_kind
            return Result("unsettled" if self.exists(missing) else "none", note=f"no aligned {missing}")
        ref_pts = [(c, r) for c, reps in ref.items() if len(reps) == 1 for r in reps]
        if not ref_pts:
            return Result("unsettled", note=f"{a.ref_kind} ambiguous")
        times = [self.tl.ref_time(r) for _, r in ref_pts]
        if any(abs(x[0] - y[0]) > 2 * rss(x[1], y[1]) + 60 for x in times for y in times):
            return Result("unsettled", note=f"sources disagree on {a.ref_kind} time")
        gap = abs(a.value)
        tol = max(60.0, 0.25 * gap) if a.approx else 30.0
        per = {}
        for clock, reps in subj.items():
            same = [r for c, r in ref_pts if c == clock]          # same clock: offset cancels
            base = same[0] if same else self._cite(a.ref_kind, [r for _, r in ref_pts])
            items = []
            for r in reps:
                if same:
                    d, u = r.t - base.t, rss(r.unc, base.unc)
                else:
                    (ta, ua), (tb, ub) = self.tl.ref_time(r), self.tl.ref_time(base)
                    d, u = ta - tb, rss(ua, ub)
                d = d if a.value >= 0 else -d
                if a.bound == "<=":
                    v = "in" if (d + u <= gap and d - u >= -tol) else ("out" if d - u > gap + TIME_MARGIN else "?")
                else:
                    v = self._judge(d - gap, u, tol, TIME_MARGIN)
                items.append((v, r))
            per[clock] = items
        return self._guard(self._combine(per, a.kind, f"{a.kind} vs {a.ref_kind}"), a.kind, a.ref_kind)

    def _unplaced_camera(self):
        return any(r.source == "camera" and r.clock not in self.tl.offset for r in self.recs)

    def _window(self, claim):
        """Readings that could fall in the claim's time window: [(rec, robustly_inside, |d|)]."""
        times = [x for x in claim.atoms if x.type == "time"]
        cams = [r for r in self.recs if r.source == "camera" and r.speed is not None
                and self.tl.ref_time(r) is not None]
        seen, uniq = set(), []
        for r in cams:                                            # duplicate rows count once
            key = (r.clock, round(r.t), r.speed)
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        if not times:
            return [(r, True, 0.0) for r in uniq]
        a = next((x for x in times if x.kind == "OBSERVE"), times[0])
        t = self.claim_time(a.value)
        tol = TIME_TOL["approx" if a.approx else "exact"]
        out = []
        for r in uniq:
            rt, u = self.tl.ref_time(r)
            d = abs(rt - t)
            if d - u <= tol:
                out.append((r, d + u <= tol, d))
        return out

    def speed(self, a, claim):
        if not any(r.source == "camera" for r in self.recs):
            return Result("none", note="no camera records")
        win = [w for w in self._window(claim) if w[0].speed > 0]
        if not win:
            return Result("unsettled", note="no aligned moving reading in the time window")
        m_tol, c_tol = SPEED_TOL["approx" if a.approx else "exact"]

        def verdict(v):
            diff = v - a.value
            if a.bound == ">=":
                return "in" if diff >= -m_tol else ("out" if diff < -c_tol else "?")
            if a.bound == "<=":
                return "in" if diff <= m_tol else ("out" if diff > c_tol else "?")
            return "in" if abs(diff) <= m_tol else ("out" if abs(diff) >= c_tol else "?")

        verdicts = [(verdict(r.speed), r, robust, d) for r, robust, d in win]
        if len(win) == 1 and win[0][1]:
            v, r, _, _ = verdicts[0]
            status = {"in": "match", "out": "conflict"}.get(v, "unsettled")
            if status == "conflict" and self._unplaced_camera():
                return Result("unsettled", note="single aligned reading disagrees, but another camera is unaligned")
            return Result(status, r, f"single reading in window: {r.speed:g} mph")
        if all(v == "in" for v, *_ in verdicts):
            return Result("match", min(verdicts, key=lambda x: x[3])[1], "every reading in window agrees")
        return Result("unsettled", note=f"{len(win)} readings in window "
                      f"({', '.join(f'{r.speed:g}' for r, *_ in win)} mph); cannot tell which is the vehicle")

    def zone(self, a, claim):
        if not any(r.source == "camera" for r in self.recs):
            return Result("none", note="no camera records")
        win = [w for w in self._window(claim) if w[0].zone is not None]
        if not win:
            return Result("unsettled", note="no aligned reading in window")
        zones = {r.zone for r, *_ in win}
        nearest = min(win, key=lambda w: w[2])[0]
        if zones == {a.value}:
            return Result("match", nearest, f"all {len(win)} readings in window show {a.value:g}")
        if len(win) == 1 and win[0][1] and abs(nearest.zone - a.value) >= 5 and not self._unplaced_camera():
            return Result("conflict", nearest, f"reading shows zone {nearest.zone:g}")
        return Result("unsettled", note=f"zones in window: {sorted(zones)}")

    def count(self, a):
        disp = [r for r in self.recs if r.source == "dispatch"]
        if not disp:
            return Result("none", note="no dispatch records")
        if a.what == "calls":
            first = {}                                            # same id or same instant = one call
            for r in disp:
                key = r.ident or (round(r.t) if r.t is not None else r.row)
                if r.kind == "CALL" and key not in first:
                    first[key] = r
            n, pool = len(first), list(first.values())
        else:
            first = {}
            for r in disp:
                if r.unit and r.unit not in first:
                    first[r.unit] = r
            n, pool = len(first), list(first.values())
        if n == a.value and pool:
            return Result("match", pool[0], f"{n} {a.what}")
        if n > a.value and len(pool) > a.value:
            return Result("conflict", pool[int(a.value)], f"records show {n} {a.what}")
        return Result("unsettled", note=f"records show {n} {a.what}; export may be incomplete")

    # -------------------------------------------------------- claim
    def claim(self, c):
        results = []
        for a in c.atoms:
            if a.type == "time":
                results.append((a, self.time(a)))
            elif a.type == "relative":
                results.append((a, self.relative(a)))
            elif a.type == "speed":
                results.append((a, self.speed(a, c)))
            elif a.type == "zone":
                results.append((a, self.zone(a, c)))
            elif a.type == "count":
                results.append((a, self.count(a)))
        live = [(a, r) for a, r in results if r.status != "none"]
        rank = lambda ar: ATOM_CITE_PRIORITY.index(ar[0].type)
        conflicts = sorted([x for x in live if x[1].status == "conflict"], key=rank)
        if conflicts:
            return "conflict", conflicts[0][1], results
        if live and all(r.status == "match" for _, r in live):
            return "match", sorted(live, key=rank)[0][1], results
        if not live:
            return "not_checkable", None, results
        return "not_settled", None, results
