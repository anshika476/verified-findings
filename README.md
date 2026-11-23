# Verified Findings

`python3 plearun_cli.py REPORT RECORDS_DIR > findings.json` checks each report claim against the records. Add `--explain` to print clock offsets and reasoning to stderr. `make test` runs 44 offline tests, and `make findings` regenerates the sample output. It uses only the Python standard library: no network, no LLM.

## How it works
1. **Load** every `.csv`, `.json` and `.vtt` file. It handles UTF-8 with a BOM, falls back to Windows-1252, and skips `#` marker lines. Each row keeps its exact raw text. A row is the CSV data row after the header, the JSON event's position in its list, or the VTT cue number.
2. **Times:** reads epoch ms, ISO with offset, datetimes with no zone (which take their file's offset), and bare `21:15`. A bare time takes its date from the dated rows above and below it. If those dates differ, the time is not used. Each time carries its precision: ±1 s, or ±30 s for minute-only values.
3. **Clocks:** each device (each camera, dispatch, the transcript) gets its own offset relative to the officer notes. Offsets come from events that appear in two sources, like the 911 call and the stop. A device is left unaligned if it has no shared event, if its events disagree, or if its offset is over 30 min. The report is never used to set offsets. On the sample: dispatch +41 s, CAM-03 +42 s, CAM-07 −48 s.
4. **Claims:** one claim per sentence. It extracts times, gaps between events, speeds, limits and counts. A sentence with none of these is `not_checkable`.
5. **Decide:** a **match** must hold even with the evidence's uncertainty added. A **conflict** must hold even with that uncertainty subtracted, from every aligned source, with no unaligned source holding the same event.
6. **Verify:** the cited row is re-read from disk. If the quote is not an exact substring of it, the finding drops to `not_settled`.

## Three hardest decisions
1. **Which camera reading is the sedan.** Cameras don't identify vehicles. A reading counts only if it is the only moving reading in the window, or clearly the closest: at least 60 s nearer than the next reading, even in the worst case of clock error. In the sample, 62 and 63 mph are too close to tell apart, so the 58 mph claim is `not_settled`. Calling a conflict there could be the confident wrong conflict the brief warns against.
2. **When a clock is trusted.** One clear shared event is enough for dispatch or a camera. The transcript needs two, because it has no real clock. Conflicts are blocked when an unaligned source has the same event.
3. **Conflict threshold.** There is no extra margin beyond tolerance plus uncertainty. The estimated clock errors are under a minute, so being 3 min off an "around" time counts as a real disagreement.

## What it gets wrong
- Claims about the events used to set the offsets (stop, call) partly check the notes against themselves.
- Minute-only notes give every offset ±30 s, so borderline claims stay `not_settled`.
- It gives one verdict per sentence, and finds claims by keywords, so it misses wording like "got there".
- The row numbering is my interpretation. If the graders count file lines instead, the citations would be off.

## What breaks first on real records
Claim extraction from free prose. Then: 0 mph readings as the stop anchor on busy roads, clocks that drift mid-incident (offsets are assumed constant), timezone and DST changes, and unfamiliar CAD event types.

## Tools used
- Claude Code (Anthropic): generated the code, tests and this README from my instructions. I reviewed the rules step by step and changed three of them: how bare times get their date, the conflict threshold for times, and the clearly-closest camera reading rule. No other tools.
- The optional `--llm` path only picks which of several already-valid rows to cite. Its reply is checked against `schema.json`, and it can't change or add findings.
