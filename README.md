# Verified Findings

`python3 plearun_cli.py REPORT RECORDS_DIR > findings.json` (add `--explain` to see offsets and reasoning on stderr). `make test` runs 24 offline tests. Standard library only, no network.

## How it works
1. **Load** (`plearun/records.py`) every `.csv/.json/.vtt` in the folder. Text is decoded as UTF-8 (BOM stripped), then Windows-1252, then Latin-1. `#` marker lines are skipped. Each row keeps its exact raw text. Row numbers: the CSV data row (after the header), the position in the JSON event list (D-1001 = 1), the VTT cue number.
2. **Times** (`timeutil.py`): epoch ms, ISO with offset, naive datetimes and bare `21:15` are all read. A naive value takes the most common UTC offset in its file, then across all files. A bare time goes on the report's incident date (±1 day, whichever is nearest). Each value carries its precision (±1 s, or ±30 s for minute-only values).
3. **Clocks** (`clocks.py`): each device has its own clock (each camera ID, the dispatch file, the notes file, the transcript). The officer notes are the reference clock, since the report is written in the officer's time. Other clocks are aligned from *anchors*, meaning the same event in two sources: the 911 call, the stop (a 0 mph reading, "Stopped at lot", "Pulling in"), the first observation, arrival and dispatch. A clock is aligned only if it has ≥1 anchor (≥2 for the transcript, which has no absolute time), its anchors agree, and the offset is under 30 min. Otherwise it is left unaligned and not used for timing. The report is never used to estimate offsets.
4. **Claims** (`claims.py`): one claim per report sentence (the header is skipped). Regexes pull out atoms: event times, gaps ("2 minutes after the stop"), speeds, posted limits and counts. A sentence with no atoms is `not_checkable`.
5. **Decide** (`checks.py`): it is a **match** only if the claim is within tolerance even after adding the evidence's uncertainty. It is a **conflict** only if the claim is still outside tolerance + a 60 s margin after subtracting that uncertainty, every aligned source disagrees, and no unaligned source has the same event. A sentence is a conflict if any atom conflicts, and a match only if every checkable atom matches.
6. **Verify**: before a finding is written, the cited file is re-read from disk and the quote must be an exact substring of that row. Otherwise the finding is downgraded to `not_settled`.

**Sample offsets** (relative to the notes): dispatch +41 s, CAM-03 +42 s, CAM-07 −48 s, transcript cue 0 ≈ 21:15:26. With these, the 911 call lands on 21:11:00 and the unit arrives exactly 2:00 after the stop.

## Three hardest decisions
1. **The 58 mph claim is `not_settled`.** CAM-07 has a 58 reading, but at about 21:06 after alignment, which is the wrong vehicle. Near 21:14 there are four moving readings (61–63 mph and 31 mph) on two cameras. The cameras don't identify vehicles, so I don't know which reading is the sedan. Calling this a conflict at 62 mph would be the "confident wrong conflict" the brief warns about. So a speed can only match or conflict when exactly one reading could be the vehicle.
2. **What counts as reliably aligned.** For an absolute clock I accept one unambiguous anchor, since the brief allows estimating from shared events. Every offset carries an uncertainty, and decisions must hold across that uncertainty. I also refuse a conflict when an unaligned source holds the same event, because evidence I couldn't place might support the claim.
3. **Citation conventions.** For JSON the "row" is the event's position in the list, and the quote is the exact `"logged": …` text from that event's source span. Duplicate JSON keys are detected and that value is ignored.

## What it gets wrong
- **Circular checks.** Claims about anchor events (stop, call) partly check the reference source against itself, because the same events set the offsets.
- **Minute precision.** The notes only record minutes, so every offset is ±30 s. Close calls end up `not_settled`.
- **One verdict per sentence.** A sentence with one false part and one unverifiable part is reported as one finding.
- **Keyword parsing.** Claims are found by keywords and regexes. Unusual wording ("the unit got there") is missed, and the sentence becomes `not_checkable`.
- **Posted limits.** A limit is matched if every reading in the time window shows it. Which camera is at mile marker 4 is unknown.

## What breaks first on real records
Claim extraction from free prose. After that: using 0 mph readings as the stop anchor (real roads have many stopped cars, and then the cameras simply stay unaligned), clocks that drift during the incident (I assume a constant offset), DST and timezone changes, and CAD exports with event types my keyword list doesn't know.

## Tools used
- Claude (Anthropic): [describe honestly how you used it, e.g. "generated the first draft of the code, tests and README; I reviewed and changed …"].
- Python 3 standard library only. The optional `--llm` path (`plearun/llm.py`) only picks which of several already-valid rows to cite. Its reply is checked against `schema.json`, and it cannot change or add a finding.
