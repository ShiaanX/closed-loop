# closed-loop — Session State

Paste this file into a new conversation to resume from where we left off.
This repo was split out of `shiaanx-CAPP` on 2026-09-12 (full build history —
every design decision, bug, and fix that got this pipeline to where it is —
lives in that repo's own `SESSION_STATE.md`; this file is forward-looking
from the split onward).

---

## What this is

CAM plan + machine telemetry + QC report → one labelled record per (part ×
operation) → a running knowledge base + an HTML report. See `README.md` for
setup/usage. See `RELIABILITY_LOG.md` for the running, plain-language record
of concrete things this system has caught or fixed — that log is the
evidence trail for Phase 1's objective (below), keep adding to it.

## Phase 1 objective (set 2026-09-12)

**Reliability, not optimisation.** There's no baseline yet, so the goal right
now is maximising trust in the record — catching the specific ways a
shop-floor record normally goes wrong (wrong file, wrong attribution,
invisible data, misleading metric) before it causes a bad decision — not
speed, cost, or parameter tuning. Every reliability win gets logged in
`RELIABILITY_LOG.md`, in language a machinist/programmer would recognise as
real, not a statistic they have to trust blindly.

## Data moved to Google Drive (2026-09-14)

Part data no longer lives in this repo's `data/` folder — it's now at
`G:\My Drive\Closed Loop\Clean Data\` (shared/synced, so anyone can drop in a
new part's CAM/QC without touching git). Verified: Bottom Plate-Krishna and
NAS PC CPU Top Plate (2026-09-09)-Krishna on Drive are byte-identical to what
was committed here; ran `closed_loop.py` directly against the Drive paths
and it reads/writes there fine (`closed_loop_record.json` lands back on
Drive). `data/` untracked from git (`.gitignore`'d), old committed copies
still in history if ever needed, not deleted from disk yet.

**New gap found while testing this:** `Clean Data` now also has
`Motor Mount_TS` — a part on a **different machine** (TS, not Krishna/STM).
This pipeline currently hardcodes one `--machine-id`/`--factory-id` per run
(CLI args, same for every part folder passed in) — there's no per-part
machine/factory override, so `--parts-root` can't safely be pointed at the
whole `Clean Data` folder once it's multi-machine; it would try to pull
Motor Mount_TS's telemetry as if it were STM/krishna. Added to next steps
below. Until fixed, call `closed_loop.py` with explicit Krishna part paths,
not `--parts-root` on the shared Drive root.

## Current status (as of the 2026-09-12 split)

- 2 parts processed: Bottom Plate (SE-004-052, 4-Sep, ~21% telemetry
  coverage, QC 16/19 FAIL) and Top Plate (RKSE-004-053, 9-Sep, near-full-day
  coverage, QC 7/9 FAIL). Both companion parts of the same job (2-setup
  flip, decoded from the CAM project — not guessed).
- `feed_override` came alive as a real signal on 9-Sep — first genuine read
  of operator feed-override behaviour (was dead/absent before).
- `machine_mode` decoded → Manual/MDI time now split out of Stopped.
- Multi-part report (`--parts-root`) with a part selector; knowledge base
  and Rules tab aggregate across all parts.
- In-page Inputs tab: fills `job.json` (workholding, orientation, run
  window, outcome) and `operation_qc_map.json` (QC↔operation mapping) without
  hand-editing JSON; downloads the files, no server needed.
- Credentials moved out of source into `.env` (gitignored) as part of this
  repo split — see README.

## Done (2026-09-15)

- **#1 Per-part machine/factory support** — `job.json`'s `machine`/`factory`
  now override the CLI default per part in `assemble_part` (before telemetry
  is pulled), so one `--parts-root` run can safely span parts on different
  machines. New template job.json defaults these two fields from whatever
  `--machine-id`/`--factory-id` the first run used, instead of always
  hardcoding STM/krishna. Tested with a throwaway part + `--no-telemetry`:
  console prints the override, `part_meta.machine`/`.factory` reflect it
  correctly, real `shop_knowledge.jsonl` untouched.
- **#4 Domain-rule root-cause flags** — three rules added to `build_records`:
  (a) a large feed deviation on an operation with a FAILED linked dimension
  gets called out as a possible contributing factor; (b) a failed dimension
  whose QC remark/description mentions a tapped/threaded feature gets
  flagged as a likely nominal-reference mismatch, not a real defect — this
  is the exact SL8 case from `RELIABILITY_LOG.md`, now automatic; (c) the
  alarm-count flag now says "likely benign" when there's no linked QC
  failure vs. "worth a closer look" when there is one. Verified against real
  data (Bottom Plate + Top Plate) — caught one regex bug in testing
  (`\btap\b` missed "Tapped", fixed to `\btap`) before it shipped silently
  broken.

## Next steps — what to BUILD now (not data collection)

The user is already tracking "collect more data" as its own, separate,
time-based track (see the earlier VC-timeline discussion in the CAPP repo's
session notes — narrow trustworthy signal in ~4-6 weeks with a deliberate
test block, broad coverage in 1-2 quarters with steady order flow). The list
below is everything else — engineering that should happen regardless of how
much data has accumulated, because it either (a) doesn't need volume to be
useful, or (b) needs to exist *before* volume arrives so the volume isn't
wasted.

1. **Automate the pipeline run.** Today, every regeneration is a person
   running `closed_loop.py` by hand after manually dropping files in.
   Needed: detect new telemetry/QC/CAM automatically (watch the Google Drive
   `Clean Data` folder, or poll InfluxDB for a new part window) and re-run without a
   human remembering to. This is itself a reliability item — a forgotten
   manual step is exactly the kind of gap this log exists to catch.

2. **Fix job/part identity at the source, not after the fact.** This
   session's two biggest bugs (telemetry attributed to the wrong part,
   3 real machine runs vanishing because their program name didn't match
   any CAM file) both trace back to one root cause: **the controller has no
   concept of a work order** — only a reused program name. The permanent fix
   is a shop-floor process change (enforce a unique program name per job —
   even a date/job-ID prefix) or a tag added at the `sx-mqtt-service`
   ingestion layer, not a script noticing the mismatch after the fact every
   time. Worth raising with whoever owns program-naming discipline on the
   floor.

3. **Wire a (thin) read path into CAPP now, even before there's enough data
   to make it useful.** `shiaanx-CAPP`'s `parameter_calculation.py` still
   only reads the static tool catalogue. Build the function that looks up
   `shop_knowledge.jsonl` for a given (material, tool, feature) and returns
   either "not enough data, use catalogue default" or a real recommendation
   with its pass-rate — build the plumbing while data is thin so the day
   there's enough, it starts working automatically instead of needing new
   engineering at that point.

4. **Start tool-instance / wear tracking now, even with zero history.**
   Nothing currently tracks a *specific physical tool's* cumulative cutting
   time — only tool type/diameter from the program. The infrastructure
   (a tool-instance ID + running cutting-time totals per instance) should
   exist now, even though it won't say anything useful until a tool has
   real history behind it. Starting late just delays when it becomes useful.

5. **Scope whether in-process ground truth is even possible on this
   machine** — not a build yet, a research task. Today the only quality
   signal is the final QC sheet, after the part is fully done — too late to
   intervene, and it only checks final dimensions. Before assuming this needs
   new sensors, check what the Mitsubishi controller on Krishna actually
   supports (an in-cycle probing cycle? any force/load signal beyond the
   already-dead `spindle_load`?) — this determines whether "close the loop
   mid-part" is a real near-term option or needs new hardware.

## Explicitly NOT in this list

Volume/statistical significance (need ~20-30 repeats per tool/feature/
material combo before a recommendation is trustworthy) is tracked separately
as the data-collection track, not an engineering task — see the CAPP repo's
session notes for the reasoning and the day-count estimate.
