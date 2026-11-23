"""Verified Findings: check incident-report claims against machine records.

Usage: python3 plearun_cli.py REPORT RECORDS_DIR [--explain] [--llm] > findings.json
  --explain  print clock offsets and per-claim reasoning to stderr
  --llm      let mock_llm (or any provider with the same interface) pick which of
             several equally valid rows to cite; it can never change a finding
"""
import json
import sys

from plearun import claims as claims_mod
from plearun import clocks, records, timeutil
from plearun.checks import Checker


def run(report_path, records_dir, explain=False, use_llm=False):
    warn = lambda msg: print(f"warning: {msg}", file=sys.stderr)
    log = (lambda msg: print(msg, file=sys.stderr)) if explain else (lambda msg: None)

    day, claim_list = claims_mod.parse_report(records.read_text(report_path))
    recs = records.load_dir(records_dir, warn)
    tz, day = clocks.resolve_times(recs, day, warn)
    timeline = clocks.Timeline(recs, log, tz)
    timeline.align()

    def claim_time(seconds_of_day):
        """Report times are placed on the incident date; unknown date -> None (not settled)."""
        if day is None:
            return None
        h, rem = divmod(int(seconds_of_day), 3600)
        return timeutil.resolve(("tod", (h, rem // 60, rem % 60), 0), tz, day)

    provider = None
    if use_llm:
        import mock_llm as provider
        from plearun import llm

    checker = Checker(recs, timeline, claim_time)
    findings = []
    for c in claim_list:
        status, result, atoms = checker.claim(c)
        log(f"\nCLAIM: {c.text}")
        for a, r in atoms:
            log(f"  {a.type:<8} {a.kind or a.what or '':<10} -> {r.status:<9} {r.note}")
        entry = {"claim": c.text, "finding": status}
        if status in ("match", "conflict"):
            rec = result.rec
            if provider and result.alts and len(result.alts) > 1:
                rec = llm.choose(provider, c.text, result.alts, warn) or rec
            raw = records.raw_row(records_dir, rec.file, rec.row)     # fresh read from disk
            if rec.quote and raw is not None and rec.quote in raw:
                entry["cite"] = {"file": rec.file, "row": rec.row}
                entry["quote"] = rec.quote
            else:
                warn(f"quote check failed for {rec.file} row {rec.row}; downgraded to not_settled")
                entry["finding"] = "not_settled"
        log(f"  => {entry['finding']}" + (f" ({entry['cite']['file']} row {entry['cite']['row']})"
                                           if "cite" in entry else ""))
        findings.append(entry)
    return {"findings": findings}


def main(argv):
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2 or flags - {"--explain", "--llm"}:
        print(__doc__, file=sys.stderr)
        return 2
    out = run(args[0], args[1], "--explain" in flags, "--llm" in flags)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
