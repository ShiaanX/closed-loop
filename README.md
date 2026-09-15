# closed-loop

ShiaanX's shop-floor closed loop for Krishna / STM: joins the CAM plan, the
machine's own telemetry, and the QC inspection report into one labelled
record per (part × operation), appends it to a running knowledge base, and
renders a self-contained HTML report.

```
   CAM program            machine telemetry           QC report
  (the plan)      →      (what actually ran)     →   (did it pass)
   S, F, strategy,        actual RPM, feed,           nominal, tol±,
   workplane/datum,       feed-override, alarms,      measured, PASS/FAIL
   move geometry          cutting/air/stopped/manual
        └──────────────────────┴───────────────────────────┘
                               ▼
              one labelled operation record per (part × operation)
                               ▼
                  shop_knowledge.jsonl  (accumulates across parts)
```

This reads *from* InfluxDB (telemetry already written by `sx-mqtt-service`)
and produces a report + a knowledge base for engineers/programmers to consult.
It does not talk to CAM/CAPP or the live web dashboard — see
[Related repos](#related-repos).

## Setup

```bash
pip install requests openpyxl Pillow
cp .env.example .env      # then fill in the real InfluxDB values
```

`.env` is gitignored — never commit real credentials. Every script also works
with `--no-telemetry` (CAM + QC only, no InfluxDB call).

## Data location

Part data (CAM, QC, job.json, photos) lives on **Google Drive**, not in this
repo — `G:\My Drive\Closed Loop\Clean Data\` — so the whole team can add/edit
a part without touching git. This repo holds the code + the accumulated
`shop_knowledge.jsonl` only.

`Clean Data` currently holds parts from more than one machine (folder names
carry the machine, e.g. `-Krishna` / `_TS`). `--machine-id`/`--factory-id`
still set the *default* for a run, but each part's own `job.json` can
override them (`machine`/`factory` fields) — a part folder that's already
been run once and has a `job.json` will always pull its own machine's
telemetry correctly, even in a mixed `--parts-root` run. A **new** part with
no `job.json` yet still needs its first run pointed at with the right
`--machine-id`/`--factory-id`, since that's what seeds the template.

## Run it

```bash
# one part
python closed_loop.py "G:\My Drive\Closed Loop\Clean Data\Bottom Plate-Krishna"

# specific Krishna parts, combined into one multi-part report
python closed_loop.py "G:\My Drive\Closed Loop\Clean Data\Bottom Plate-Krishna" "G:\My Drive\Closed Loop\Clean Data\NAS PC CPU Top Plate (2026-09-09)-Krishna"
```

`--parts-root` (auto-discovering every part folder) works the same way, but
only point it at a folder whose immediate children are all Krishna parts —
see the caveat above.

Output: `krishna_report.html` (open it in a browser — self-contained, no
server needed), `krishna_metrics.json`, and per-part `closed_loop_record.json`
+ `job.json` / `operation_qc_map.json` (auto-scaffolded on first run, then
hand-filled — see below).

## Adding a new part

Create `G:\My Drive\Closed Loop\Clean Data\<part name>-Krishna\` with:

```
input/<PART>.stp  <PART>.pdf              # CAD model + drawing
CAM files_*/*.tap                          # posted G-code toolpaths
CAM Project_*/<PART>/<PART>.pmlprj         # the PowerMILL project (setup/workplane/designed feeds)
Inspection Report_*/<PART>.xlsx            # QC / inspection report
Workholding/*.jpg                          # optional — photos of the actual setup, get embedded in the report
```

Then run `closed_loop.py` on it. It writes:

- `job.json` — the on-ground half of the loop: real run window(s), per-setup
  workholding/orientation, tool substitutions, part outcome. Fill it via the
  report's **Inputs** tab (downloads the file for you), or by hand.
- `operation_qc_map.json` — maps each operation to the QC report's SL numbers
  it's responsible for. Comes pre-annotated with what each operation makes
  and each dimension's nominal/measured value, so it doesn't need
  cross-referencing the drawing from scratch.
- `step_analysis.json` — optional, hand-authored plain-English notes and
  emerging shop rules for that part (see the **Step-by-step** / **Rules**
  tabs in the report).

`job.json` also supports `excluded_runs`: if the machine ran a program
matching this part's CAM files but was actually a different job (e.g. the
operator reused a program name later that day), list it there — it stays in
the machine-level Summary but drops out of this part's own analysis.

## Files

| File | What it does |
|---|---|
| `krishna_metrics.py` | Pulls telemetry from InfluxDB, segments it into operations/cycles, computes machine + per-operation metrics (cutting/air/stopped/manual time, feed override, alarms, log coverage). |
| `closed_loop.py` | The orchestrator — CAM `.tap` parsing, QC xlsx parsing, `job.json`/`operation_qc_map.json` handling, the knowledge base, and calling `krishna_metrics` + `pml_project`. Entry point. |
| `pml_project.py` | Reads a PowerMILL project's `.pmlprj`/`.pmlent` files — designed feeds/speeds, strategy, and (self-validated, version-independent) the workplane's real origin + rotation, so setup orientation doesn't need guessing. |
| `krishna_report_template.html` | The report shell — `closed_loop.py` injects the data and writes `krishna_report.html`. Tabs: Summary, Part by part, Step-by-step, Rules, Inputs, Data gaps. |
| `shop_knowledge.jsonl` | The flywheel — one JSON record per (part × operation), rewritten (not appended blindly) each run so re-running a part supersedes its own rows without disturbing others. This is what the Rules tab's parameter table aggregates across. |
| `data/<part>/` | Per-part CAM, CAM project, QC, CAD, and the generated/hand-filled files above. |

## Related repos

- **`sx-mqtt-service`** — upstream: machine → MQTT → this InfluxDB. Ingestion only, doesn't read this repo's output.
- **`shiaanx-backend`** / **`shiaanx-frontend-admin`** — the live web telemetry dashboard (different audience: a always-on API + UI, not a per-part analysis report).
- **`shiaanx-CAPP`** — CAD-to-process-plan pipeline (the *plan*, before a part is ever cut). Not yet wired to this repo's knowledge base — see the closed-loop discussion in that repo's session notes for what's needed to feed recommendations forward into CAPP's parameter calculation.
