Part 2 — machined on Krishna / STM, 9 Sep 2026.

Drop the same kinds of files here as the Bottom Plate folder, then run:

  conda run -n occ python closed_loop.py --parts-root "Claude output for program sheet/Dataset/Krishna"

which assembles EVERY part folder under Dataset/Krishna into one multi-part
krishna_report.html (part selector at the top).

Files to add:
  input/<PART>.stp  and  <PART>.pdf          (CAD model + drawing)
  CAM files_Krishna/*.tap                     (the posted G-code toolpaths)
  CAM Project_Krishna/<PART>/<PART>.pmlprj    (the PowerMILL project folder — for setup/workplane/designed feeds)
  Inspection Report_Krishna/<PART>.xlsx       (the QC / inspection report)

closed_loop.py will then auto-write, in this folder:
  job.json                 - fill via the report's Inputs tab (run window, workholding, outcome)
  operation_qc_map.json    - map each operation to its QC SL numbers (Inputs tab)
  closed_loop_record.json  - generated
  step_analysis.json       - optional hand notes / rules (add later)

Telemetry for 9 Sep was captured almost the whole working day
(~09:36-18:00 IST, ~20,400 samples, 90% running) so this part has near-complete
machine data, unlike the 4-Sep Bottom Plate (~21% of the job).
