"""
closed_loop.py — assemble the CAM → telemetry → QC closed-loop record for one part.

No CAPP. The "plan" is the CAM program; the loop is:
   CAM (.tap)  →  machine telemetry  →  QC report  →  shop_knowledge.jsonl

For a part folder like:
   Claude output for program sheet/Dataset/Krishna/Bottom Plate/
     input/<PART>.stp  <PART>.pdf
     CAM files_Krishna/*.tap
     Inspection Report_Krishna/<PART>.xlsx
     operation_qc_map.json          (hand-filled; a stub is written if missing)

it produces one LABELLED RECORD per (part × operation) — tool, feature, programmed S/F,
actual RPM/feed/time, deltas, QC outcome — writes:
   <part_dir>/closed_loop_record.json
   krishna_metrics.json + krishna_report.html   (telemetry report, with a Closed-loop tab)
   ./shop_knowledge.jsonl                        (append — the flywheel)

Usage:
   python closed_loop.py "Claude output for program sheet/Dataset/Krishna/Bottom Plate"
   python closed_loop.py "<dir>" --start 2026-09-04T03:00:00Z --end 2026-09-04T14:00:00Z
   python closed_loop.py "<dir>" --no-telemetry     # CAM + QC only (skip the InfluxDB pull)

See audit/SPEC_krishna_closed_loop.md for the design.
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).parent
PILOT_START = "2026-09-04T03:00:00Z"
PILOT_STOP = "2026-09-04T14:00:00Z"
PILOT_ACTUAL = "2026-09-04T03:30:00Z,2026-09-04T13:30:00Z"

RPM_FLAG_PCT = 15          # |actual − programmed| beyond this % of programmed → flag
FEED_FLAG_PCT = 15

# domain-rule root-cause hints (see SESSION_STATE.md #4) — plain engineering
# judgement, not statistics: thresholds are deliberately conservative (only
# fire on a large, already-linked-to-a-failure deviation) so these read as a
# suggestion worth checking, not a diagnosis.
ROOT_CAUSE_FEED_PCT = 30   # feed deviation beyond this %, on a FAILED op, gets called out
ROOT_CAUSE_ALARM_N = 10    # alarm count beyond this gets a benign/worth-a-look qualifier
_TAP_WORD = re.compile(r"\btap|thread", re.I)   # matches tap/tapped/tapping/tap-drill, not "bootstrap"


# ───────────────────────────────────────────────────────────────── CAM ──

_HDR = {
    "tool_type":  re.compile(r"TOOL\s*TYPE\s*:\s*([A-Za-z ]+)", re.I),
    "tool_id":    re.compile(r"TOOL\s*ID\s*:\s*(\S+)", re.I),
    "tool_dia":   re.compile(r"TOOL\s*DIA\.?\s*:\s*([\d.]+)", re.I),
    "tool_len":   re.compile(r"LENGTH\s*([\d.]+)", re.I),
    "toolpath":   re.compile(r"TOOLPATH\s*:\s*(\S+)", re.I),
    "tolerance":  re.compile(r"TOLERANCE\s*:\s*([\d.]+)", re.I),
    "stock":      re.compile(r"STOCK\s*:\s*([+\-]?[\d.]+)", re.I),
    "date":       re.compile(r"DATE\s*:\s*([\d.]+)", re.I),
}
_WORD = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")
_CANNED = {"G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89"}
_GCODE_EXT = re.compile(r"\.(tap|nc|mpf|prg)$", re.I)


def toolpath_name(path: Path) -> str:
    """Program name — strip only real G-code extensions. Path.stem is wrong for
    '4_3.2drill' (it thinks the ext is '.2drill')."""
    return _GCODE_EXT.sub("", path.name)


def parse_tap(path: Path) -> dict:
    """Parse one PowerMILL/Fanuc-ISO .tap: header + programmed S/F + cut-path length +
    geometry extents. Approximates G2/G3 arcs as chords (fine for a feed-time estimate)."""
    text = path.read_text(errors="ignore")
    out = {"file": path.name, "toolpath": toolpath_name(path)}
    for k, rx in _HDR.items():
        m = rx.search(text)
        if m:
            out[k] = m.group(1).strip()

    tool_type = (out.get("tool_type") or "").strip().lower()
    out["tool"] = {
        "type": tool_type or None,
        "id": out.get("tool_id"),
        "dia_mm": _f(out.get("tool_dia")),
        "length_mm": _f(out.get("tool_len")),
    }
    out["tolerance_mm"] = _f(out.get("tolerance"))
    out["stock_mm"] = _f(out.get("stock"))

    # modal machine state
    x = y = z = None
    motion = None            # 0 / 1 / 2 / 3
    feed = None
    canned = None            # active canned cycle, e.g. G83
    r_plane = None
    spindle_s = None
    cut_len = 0.0            # mm of feed-rate linear moves (excl. canned cycles)
    cut_time_s = 0.0
    drill_len = 0.0          # sum of (r_plane − z_bottom) over canned-cycle holes
    drill_time_s = 0.0
    holes = 0
    feed_hist = {}           # feed value → mm of cut at that feed
    xs, ys, zs = [], [], []

    for raw in text.splitlines():
        line = raw.split("(", 1)[0].strip().upper()   # drop inline comments
        if not line or line.startswith("%") or line.startswith("O"):
            continue
        words = dict(_WORD.findall(line))
        # spindle
        if spindle_s is None and "S" in words and "M03" in line.replace(" ", ""):
            spindle_s = int(float(words["S"]))
        elif spindle_s is None and re.search(r"\bS\d", line) and "M3" in line.replace(" ", ""):
            spindle_s = int(float(words.get("S", 0))) or None
        # motion / canned-cycle modes
        for g in re.findall(r"G\d+", line):
            if g in ("G00", "G0"):
                motion, canned = 0, None
            elif g in ("G01", "G1"):
                motion, canned = 1, None
            elif g in ("G02", "G2"):
                motion, canned = 2, None
            elif g in ("G03", "G3"):
                motion, canned = 3, None
            elif g in _CANNED:
                canned = g
            elif g == "G80":
                canned = None
        if "F" in words:
            feed = _f(words["F"])
        if "R" in words:
            r_plane = _f(words["R"])

        nx = _f(words["X"]) if "X" in words else x
        ny = _f(words["Y"]) if "Y" in words else y
        nz = _f(words["Z"]) if "Z" in words else z
        for v, acc in ((nx, xs), (ny, ys), (nz, zs)):
            if v is not None:
                acc.append(v)

        has_pos = any(k in words for k in ("X", "Y", "Z"))

        if canned and has_pos and ("X" in words or "Y" in words):
            # each X/Y position under a canned cycle = one hole
            holes += 1
            depth = abs((r_plane if r_plane is not None else 0.0) - (nz if nz is not None else (z or 0.0)))
            drill_len += depth
            if feed:
                drill_time_s += depth / feed * 60.0
        elif motion in (1, 2, 3) and has_pos and x is not None and y is not None:
            seg = math.dist((x, y, z or 0.0), (nx or x, ny or y, nz if nz is not None else (z or 0.0)))
            cut_len += seg
            if feed:
                cut_time_s += seg / feed * 60.0
                feed_hist[feed] = feed_hist.get(feed, 0.0) + seg

        x, y, z = nx, ny, nz

    cut_feed = max(feed_hist, key=feed_hist.get) if feed_hist else None
    lead_feed = min(feed_hist) if feed_hist else None
    out["programmed"] = {
        "S": spindle_s,
        "F_cut": round(cut_feed) if cut_feed else None,
        "F_lead": round(lead_feed) if lead_feed else None,
        "path_len_mm": round(cut_len, 1) if cut_len else None,
        "est_cut_s": round(cut_time_s, 1) if cut_time_s else None,
        "drill_holes": holes or None,
        "drill_depth_total_mm": round(drill_len, 1) if drill_len else None,
        "est_drill_s": round(drill_time_s, 1) if drill_time_s else None,
    }
    out["geometry"] = {
        "x_mm": [round(min(xs), 1), round(max(xs), 1)] if xs else None,
        "y_mm": [round(min(ys), 1), round(max(ys), 1)] if ys else None,
        "z_mm": [round(min(zs), 1), round(max(zs), 1)] if zs else None,
    }
    out["feature"] = infer_feature(out)
    return out


def infer_feature(cam: dict) -> str:
    name = cam["toolpath"].lower()
    tt = (cam.get("tool_type") or "").lower()
    if "drill" in tt or "cd" in name or re.search(r"\bdrill\b", name):
        return "spot/centre drill" if ("cd" in name or "1cd" in name) else "drilled holes"
    if "outer" in name:
        return "outer profile (finish)" if "fin" in name else "outer profile (rough)"
    if "flat" in name or "face" in name:
        return "face"
    if "rest" in name:
        return "rest machining"
    if "boll" in name or "ball" in name:
        return "3D / ball-nose"
    if "ruf" in name or "rough" in name:
        return "roughing"
    if "fin" in name:
        return "finishing pass"
    return "milling"


# ───────────────────────────────────────────────────────────────── QC ──

_NUM = re.compile(r"-?\d+\.?\d*")


def parse_qc_xlsx(path: Path) -> list[dict]:
    z = zipfile.ZipFile(path)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        r = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in r.findall(f"{ns}si"):
            ss.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
    sheet = next(n for n in z.namelist() if re.match(r"xl/worksheets/sheet1\.xml", n))
    root = ET.fromstring(z.read(sheet))

    def _colnum(ref):                       # "B11" -> 1 (0-based)
        m = re.match(r"([A-Z]+)", ref or "")
        n = 0
        for ch in (m.group(1) if m else ""):
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    rows = []
    for row in root.iter(f"{ns}row"):
        by_col = {}
        for c in row:
            ci = _colnum(c.get("r"))
            if c.get("t") == "inlineStr":          # openpyxl / some exporters
                isn = c.find(f"{ns}is")
                by_col[ci] = "".join(t.text or "" for t in isn.iter(f"{ns}t")) if isn is not None else ""
                continue
            v = c.find(f"{ns}v")
            if v is None:
                by_col[ci] = ""
            elif c.get("t") == "s":
                by_col[ci] = ss[int(v.text)]
            else:
                by_col[ci] = v.text
        width = max(by_col) + 1 if by_col else 0
        rows.append([str(by_col.get(i, "")).strip() for i in range(width)])

    meta = {}
    for r in rows:
        joined = " ".join(r)
        m = re.search(r"Part\s*Name\s*:\s*(.+?)(?:\s{2,}|Project|$)", joined, re.I)
        if m and "part_name" not in meta:
            meta["part_name"] = m.group(1).strip()
        m = re.search(r"Material\s*:\s*([A-Za-z ]+?)(?:\s{2,}|QTY|$)", joined, re.I)
        if m and "material" not in meta:
            meta["material"] = m.group(1).strip().lower()

    out = []
    for r in rows:
        if not r or not r[0].isdigit():
            continue
        sl = int(r[0])
        desc = r[1] if len(r) > 1 else ""
        nom_m = _NUM.search(desc.replace("ø", "").replace("Ø", ""))
        nominal = float(nom_m.group()) if nom_m else None
        tol_p = _f(r[2]) if len(r) > 2 else None
        tol_m = _f(r[3]) if len(r) > 3 else None
        obs = r[5] if len(r) > 5 else ""
        meas = [float(x) for x in _NUM.findall(obs)] or None
        remark = (r[-1] if r else "").strip()

        rec = {"sl": sl, "description": desc, "nominal": nominal,
               "tol_plus": tol_p, "tol_minus": tol_m,
               "measured": [min(meas), max(meas)] if meas else None,
               "remark": remark}
        if nominal is not None and meas is not None and tol_p is not None and tol_m is not None:
            lo, hi = nominal - tol_m, nominal + tol_p
            mid = sum(meas) / len(meas)
            rec["pass"] = all(lo - 1e-6 <= m <= hi + 1e-6 for m in meas)
            rec["dev_mm"] = round(mid - nominal, 4)
        else:
            rec["pass"] = None
            rec["dev_mm"] = None
        out.append(rec)
    return {"rows": out, "meta": meta}


# ────────────────────────────────────────────────────────── telemetry ──


def load_telemetry(part_dir: Path, args) -> dict | None:
    if args.no_telemetry:
        return None
    spec = importlib.util.spec_from_file_location("krishna_metrics", HERE / "krishna_metrics.py")
    km = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(km)
    ns = SimpleNamespace(
        start=args.start, end=args.end, days=None,
        factory_id=args.factory_id, machine_id=args.machine_id,
        override_field="feed_override", part_gap_hours=2.0,
        part_actual=args.part_actual, windows=[1, 7, 30], out_dir=".",
    )
    try:
        data = km.build(ns)
    except SystemExit as e:
        print(f"  ! telemetry: {e}")
        return None
    except Exception as e:                       # DNS / connection / timeout / auth
        cache = HERE / "krishna_metrics.json"
        note = f"InfluxDB unreachable ({type(e).__name__})"
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                data.pop("closed_loop", None)    # rebuilt below from fresh CAM/QC/job
                data["_telemetry_source"] = (
                    f"cache — {cache.name}, pulled {data.get('generated_at', '?')} "
                    f"(live pull failed: {type(e).__name__})")
                print(f"  ! {note}. Reusing cached telemetry from {cache.name} "
                      f"(pulled {data.get('generated_at', '?')}). CAM / QC / job / rules are fresh; "
                      f"the machine-side numbers are from the last successful pull.")
            except (json.JSONDecodeError, OSError):
                print(f"  ! {note} and no usable cache — continuing without telemetry.")
                return None
        else:
            print(f"  ! {note} and no {cache.name} cache — continuing without telemetry.")
            return None
    data["_km_module"] = km          # keep for write_report
    return data


# ─────────────────────────────────────────────────────────── assemble ──


def match_ops(name: str, ops: list[dict]) -> list[dict]:
    """Every telemetry operation whose program name matches this CAM toolpath —
    there can be more than one if the operator ran the same program again later
    (not back-to-back, so krishna_metrics segments it as a separate operation,
    not a repeat cycle of one)."""
    n = _GCODE_EXT.sub("", name.strip()).lower()
    return [o for o in ops if _GCODE_EXT.sub("", o["program_name"].strip()).lower() == n]


def build_records(cam_paths, tele, qc_rows, op_qc_map, part_meta,
                  cam_project=None, job=None) -> list[dict]:
    ops = tele["operations"] if tele else []
    # run order by chronological position — keyed by object identity, not name,
    # so two runs of the same program each keep their own real position instead
    # of the later one silently overwriting the earlier one in a name-keyed dict
    run_order_of = {id(o): i + 1 for i, o in enumerate(sorted(ops, key=lambda o: o["start"]))}
    matched_ids = set()
    qc_by_sl = {r["sl"]: r for r in qc_rows}
    cam_tp = {k.lower(): v for k, v in (cam_project or {}).get("toolpaths", {}).items()}
    setup_of = {}
    for s in (job or {}).get("setups", []):
        for opn in s.get("operations", []):
            setup_of[opn.lower()] = s
    op_actuals = {k.lower(): v for k, v in (job or {}).get("operation_actuals", {}).items()}

    records = []
    for capath in sorted(cam_paths):
        cam = parse_tap(capath)
        tp = cam["toolpath"]
        matches = sorted(match_ops(tp, ops), key=lambda o: o["start"])
        op = matches[-1] if matches else None           # most recent run represents it
        earlier_runs = matches[:-1]                      # any earlier, separate run of it
        matched_ids.update(id(o) for o in matches)        # earlier runs are accounted for via the flag below, not "unmatched"
        camp = cam_tp.get(tp.lower())

        actual = None
        deltas = {}
        if op:
            t = op["timing"]
            fov = (op["feed"].get("override") or {})
            actual = {
                "rpm_median": op["spindle"]["rpm_median"],
                "feed_median": op["feed"]["rate_median"],
                "feed_override": {"min": fov["min_active"], "max": fov["max_active"],
                                  "median": fov.get("median_active"),
                                  "events": fov["events"], "n": fov["n"],
                                  "constant": fov["constant"]} if fov.get("available") else None,
                "cutting_s": t["cutting_s"], "air_s": t["air_s"],
                "stopped_s": t["stopped_s"], "setup_idle_s": t["idle_before_s"],
                "cycles": op["cycles"], "alarms": op["alarms"]["events"],
                "run_order": run_order_of.get(id(op)),
            }
            ps, pf = cam["programmed"]["S"], cam["programmed"]["F_cut"]
            if ps and actual["rpm_median"]:
                deltas["rpm_pct"] = round(100 * (actual["rpm_median"] - ps) / ps, 1)
            if pf and actual["feed_median"]:
                deltas["feed_pct"] = round(100 * (actual["feed_median"] - pf) / pf, 1)
            est = cam["programmed"]["est_cut_s"] or cam["programmed"]["est_drill_s"]
            if est and actual["cutting_s"]:
                deltas["cutting_vs_est_pct"] = round(100 * (actual["cutting_s"] - est) / est, 1)
            if actual["cycles"] and actual["cycles"] > 1:
                deltas["cycles"] = f"ran {actual['cycles']}× (CAM is a single execution)"

        # QC linkage
        dims = op_qc_map.get(tp) or op_qc_map.get(capath.name) or []
        results = [qc_by_sl[d] for d in dims if d in qc_by_sl]
        graded = [r for r in results if r["pass"] is not None]
        if not dims:
            outcome = "unmapped"
        elif not graded:
            outcome = "unmeasured"
        elif all(r["pass"] for r in graded):
            outcome = "PASS"
        else:
            outcome = "FAIL"
        worst_dev = max((abs(r["dev_mm"]) for r in graded if r["dev_mm"] is not None), default=None)

        flags = []
        if "rpm_pct" in deltas and abs(deltas["rpm_pct"]) > RPM_FLAG_PCT:
            flags.append(f"actual RPM {deltas['rpm_pct']:+.0f}% vs programmed")
        if "feed_pct" in deltas and abs(deltas["feed_pct"]) > FEED_FLAG_PCT:
            flags.append(f"actual feed {deltas['feed_pct']:+.0f}% vs programmed")

        # domain-rule root-cause hints — engineering judgement now, not waiting on
        # enough volume for statistics to imply causation (closed-loop SESSION_STATE #4)
        if outcome == "FAIL" and abs(deltas.get("feed_pct") or 0) > ROOT_CAUSE_FEED_PCT:
            flags.append(f"feed ran {deltas['feed_pct']:+.0f}% off programmed on an operation "
                         f"with a failed dimension — worth checking whether the feed change is a "
                         f"contributing factor")
        for r in graded:
            if r["pass"] is False and _TAP_WORD.search(
                    f"{r.get('remark') or ''} {r.get('description') or ''}"):
                flags.append(f"SL{r['sl']} failed and its remark/description mentions a "
                             f"tapped/threaded feature — large misses here are often the nominal "
                             f"being the thread's major diameter while a minor/tap-drill diameter "
                             f"got measured, not a real defect; verify against the drawing before "
                             f"treating this as a process failure")
                break  # one mention is enough — don't repeat per dimension

        if actual and actual["alarms"]:
            n = actual["alarms"]
            if n >= ROOT_CAUSE_ALARM_N and outcome == "FAIL":
                flags.append(f"{n} alarm event(s) during the op, AND a QC failure here — unlike "
                             f"most high-alarm operations on this machine (routine retract/cycle "
                             f"noise), this pairing is worth a closer look")
            elif n >= ROOT_CAUSE_ALARM_N:
                flags.append(f"{n} alarm event(s) during the op — high count, but this machine "
                             f"raises the alarm flag on routine things (e.g. every drill-cycle "
                             f"retract); with no linked QC failure here, treat as likely benign")
            else:
                flags.append(f"{n} alarm event(s) during the op")
        if actual and (actual["cycles"] or 0) > 1:
            flags.append("more cycles than the CAM program has")
        if outcome == "FAIL":
            flags.append("QC FAIL on a linked dimension")
        if op is None:
            flags.append("no telemetry — ran outside the analysed window")
        if earlier_runs:
            when = ", ".join(r["start"][11:19] + "Z" for r in earlier_runs)
            flags.append(f"this program also ran earlier ({when}) — only the most recent "
                         f"run's numbers are shown above")
        if camp and camp.get("feed_cut") and cam["programmed"].get("F_cut") \
                and abs(camp["feed_cut"] - cam["programmed"]["F_cut"]) > 1:
            flags.append(f"posted G-code feed {cam['programmed']['F_cut']} ≠ "
                         f"CAM-designed {camp['feed_cut']:g} (.tap hand-edited?)")

        setup = setup_of.get(tp.lower())
        setup_out = None
        if setup:
            setup_out = {
                "id": setup.get("id"),
                "cam_workplane": setup.get("cam_workplane") or (camp or {}).get("workplane"),
                "orientation": setup.get("orientation") or None,
                "workholding": setup.get("workholding") or None,
                "stock_actual_mm": setup.get("stock_actual_mm") or None,
                "notes": setup.get("notes") or None,
            }
        elif camp and camp.get("workplane"):
            setup_out = {"cam_workplane": camp["workplane"],
                         "orientation": None, "workholding": None,
                         "note": "workholding/orientation not recorded — add to job.json"}

        planned_cam = None
        if camp:
            planned_cam = {
                "workplane": camp.get("workplane"),
                "strategy": camp.get("strategy"),
                "stepdown_mm": camp.get("stepdown_mm"),
                "tool_axis": camp.get("tool_axis"),
                "stock_block_mm": camp.get("stock_block_mm"),
                "designed_feed_cut": camp.get("feed_cut"),
                "designed_feed_plunge": camp.get("feed_plunge"),
                "designed_feed_rapid": camp.get("feed_rapid"),
                "designed_vc_m_min": camp.get("vc_m_min"),
                "designed_fz_mm": camp.get("fz_mm"),
                "cam_edited": camp.get("cam_edited"),
            }

        records.append({
            **part_meta,
            "operation": tp, "cam_file": cam["file"],
            "feature": cam["feature"],
            "tool": cam["tool"],
            "programmed": {**cam["programmed"],
                           "tolerance_mm": cam["tolerance_mm"], "stock_mm": cam["stock_mm"]},
            "planned_cam": planned_cam,
            "setup": setup_out,
            "geometry": cam["geometry"],
            "actual": actual,
            "shop_floor": op_actuals.get(tp.lower()),
            "deltas": deltas,
            "qc": {"dims": dims, "results": results,
                   "outcome": outcome, "worst_dev_mm": worst_dev},
            "flags": flags,
        })

    unmatched = [{"program_name": o["program_name"], "start": o["start"], "end": o["end"],
                 "cutting_s": o["timing"]["cutting_s"], "wallclock_s": o["timing"]["wallclock_s"]}
                for o in ops if id(o) not in matched_ids]
    return records, unmatched


# ─────────────────────────────────────────────────────────────── main ──


def find_one(part_dir: Path, *globs) -> Path | None:
    for g in globs:
        hits = sorted(part_dir.glob(g))
        if hits:
            return hits[0]
    return None


def _load_workholding_photos(part_dir: Path) -> list[dict]:
    """Any images in a Workholding/ subfolder (or *workhold* named files) →
    downscaled JPEG data-URIs so they travel inside the self-contained report."""
    exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    hits = []
    for d in sorted(part_dir.iterdir()):
        if d.is_dir() and "workhold" in d.name.lower().replace(" ", ""):
            hits += sorted(f for f in d.iterdir() if f.suffix.lower() in exts)
    hits += sorted(f for f in part_dir.glob("*")
                   if f.suffix.lower() in exts and "workhold" in f.stem.lower())
    if not hits:
        return []
    try:
        import base64
        import io as _io
        from PIL import Image
    except ImportError:
        print("  ! Pillow not installed — workholding photos not embedded")
        return []
    out = []
    for f in hits[:8]:
        try:
            im = Image.open(f)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            if w > 1100:
                im = im.resize((1100, round(h * 1100 / w)))
            buf = _io.BytesIO()
            im.save(buf, "JPEG", quality=70, optimize=True)
            raw = buf.getvalue()
            out.append({"name": f.name,
                        "data_uri": "data:image/jpeg;base64," + base64.b64encode(raw).decode(),
                        "kb": round(len(raw) / 1024)})
        except Exception as e:                                  # noqa: BLE001
            print(f"  ! could not embed {f.name}: {e}")
    if out:
        print(f"workholding: {len(out)} photo(s) embedded ({sum(p['kb'] for p in out)} KB)")
    return out


def _op_hint(cam: dict, cp: dict | None) -> str:
    """One-line description of what an operation makes — to guide QC mapping."""
    g = cam.get("geometry") or {}
    box = " ".join(f"{ax}{v[0]}..{v[1]}" for ax, v in
                   (("X", g.get("x_mm")), ("Y", g.get("y_mm")), ("Z", g.get("z_mm"))) if v)
    tool = cam.get("tool") or {}
    bits = [cam.get("feature") or "?"]
    if tool.get("dia_mm"):
        bits.append(f"{tool.get('type') or 'tool'} Ø{tool['dia_mm']}")
    if cp and cp.get("strategy"):
        bits.append(f"{cp['strategy']}"
                    + (f" WP{cp['workplane']}" if cp.get("workplane") else ""))
    if box:
        bits.append(box)
    return " | ".join(bits)


def _write_qc_map_scaffold(map_path: Path, cam_paths, qc_rows, cam_project) -> None:
    """Refresh the annotated `_*` helper blocks in operation_qc_map.json, keeping
    any lists the user has already filled. Creates the file if missing."""
    existing = {}
    if map_path.exists():
        try:
            existing = json.loads(map_path.read_text())
        except json.JSONDecodeError:
            pass
    tps = {cp.lower(): v for cp, v in (cam_project or {}).get("toolpaths", {}).items()} \
        if cam_project else {}
    hints = {}
    for p in cam_paths:
        name = toolpath_name(p)
        cam = parse_tap(p)
        hints[name] = _op_hint(cam, tps.get(name.lower()))

    out = {
        "_README": ("Map each operation (a .tap toolpath) to the QC SL numbers it produces. "
                    "Open the drawing, and for every ballooned dimension in `_qc_dimensions` "
                    "decide which operation cuts that surface/feature, then add its SL number to "
                    "that operation's list below. Re-run closed_loop.py. An operation left [] gets "
                    "outcome 'unmapped'; mapped but not measured -> 'unmeasured'; otherwise "
                    "PASS/FAIL from the linked dimensions. `_operations` tells you what each "
                    "operation makes; `_qc_dimensions` is nominal + measured for each SL."),
        "_operations": hints,
        "_qc_dimensions": {str(r["sl"]): f'{r["description"]} — measured {r["measured"] or "?"}'
                           for r in qc_rows},
        "_example": {"<operation name>": [1, 4]},
    }
    for p in cam_paths:
        name = toolpath_name(p)
        out[name] = list(existing.get(name, []))
    map_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


_JOB_TEMPLATE = {
    "_README": (
        "The shop-floor 'what actually happened' file — the on-ground half of the closed loop. "
        "CAM gives the plan (feeds, strategy, workplane); telemetry gives what the spindle did; "
        "this file is everything else that only the setter/operator knows: which fixture, how the "
        "part was clamped and oriented, the real touched-off datum, measured stock, tool swaps, "
        "and the final part outcome. Fill what you can each job and re-run closed_loop.py. "
        "Every field is optional; blanks just show as 'not recorded'. Delete this file to disable it. "
        "`excluded_runs`: if the machine ran a program that matches one of this part's CAM files but "
        "was actually a different part (e.g. the operator reused a program name later that day), list "
        "it there by its exact program_name + start time (from the closed-loop table's flags, or the "
        "console output) — it stays in the machine-level Summary tab but drops out of this part's "
        "operation / closed-loop analysis. "
        "`machine`/`factory`: pre-filled from this run's --machine-id/--factory-id; edit them if this "
        "part is actually on a different machine — closed_loop.py reads them back, so one --parts-root "
        "run can safely cover parts on different machines side by side."),
    "part": None, "part_name": None, "quantity": 1,
    "material": None, "machine": "STM", "factory": "krishna",
    "runs": [
        {"start": "2026-09-04T03:30:00Z", "end": "2026-09-04T13:30:00Z",
         "note": "one machining session; edit to the real shop-floor window(s). "
                 "Add more {start,end} objects if the job ran across sessions/days."}
    ],
    "excluded_runs": [],
    "setups": [
        {
            "id": "S1",
            "cam_workplane": None,
            "operations": [],
            "orientation": "",
            "workholding": {
                "type": "",
                "detail": "",
                "datum_touched_off": "",
                "gcode_offset": "G54"
            },
            "stock_actual_mm": {"x": None, "y": None, "z": None},
            "notes": ""
        }
    ],
    "operation_actuals": {
        "<operation name>": {"tool_substituted": "", "notes": "", "outcome": "ok"}
    },
    "outcome": {"parts_good": None, "parts_scrap": 0, "rework": "", "notes": ""}
}


def _write_job_template(job_path: Path, part_id, qc_meta, args, cam_project) -> None:
    if job_path.exists():
        return
    t = json.loads(json.dumps(_JOB_TEMPLATE))          # deep copy
    t["part"] = part_id
    t["part_name"] = qc_meta.get("part_name") or part_id
    t["material"] = qc_meta.get("material") or None
    t["machine"], t["factory"] = args.machine_id, args.factory_id
    t["runs"][0]["start"], t["runs"][0]["end"] = args.start, args.end
    if cam_project:
        wpd = cam_project.get("workplane_detail") or {}
        setups = []
        for wp, ops in (cam_project.get("setups_by_workplane") or {}).items():
            det = wpd.get(str(wp)) or {}
            setups.append({
                "id": f"WP{wp}", "cam_workplane": wp, "operations": sorted(ops),
                "orientation": det.get("orientation") or "",
                "_orientation_hint": "pre-filled from the CAM workplane; correct it if the "
                                     "shop-floor setup differed" if det.get("orientation") else "",
                "workholding": {
                    "type": "", "detail": "", "datum_touched_off": "", "gcode_offset": "G54"},
                "stock_actual_mm": {"x": None, "y": None, "z": None}, "notes": ""
            })
        if setups:
            t["setups"] = setups
    job_path.write_text(json.dumps(t, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  * wrote job.json template — fill in fixture / orientation / datum / outcome "
          f"and re-run to feed the on-ground side of the loop.")


def assemble_part(part_dir: Path, args, km) -> dict | None:
    """Assemble one part's closed-loop payload. Side effects: writes the part's
    closed_loop_record.json, a job.json template, and the qc-map scaffold. Does
    NOT touch the shared knowledge base or the HTML report — the caller does that
    once across all parts.  Returns {part_id, part_name, records, summary,
    flagged, tele}  (tele is None when there is no telemetry)."""
    a = SimpleNamespace(**vars(args))          # per-part copy — job.json may move the window
    part_dir = Path(part_dir)
    if not part_dir.is_dir():
        print(f"  ! not a directory: {part_dir}")
        return None

    step = find_one(part_dir, "input/*.stp", "input/*.step", "*.stp", "*.step")
    part_id = (step.stem if step else part_dir.name).strip()

    # .tap files, plus extensionless G-code (e.g. "4_3.2drill" — note Path.suffix
    # sees ".2drill", so match by name shape instead), excluding CAM-project junk.
    cam_paths = []
    for p in part_dir.glob("**/*"):
        if not p.is_file():
            continue
        low = p.name.lower()
        if low.endswith((".tap", ".nc", ".mpf", ".prg")):
            cam_paths.append(p)
        elif "cam files" in str(p.parent).lower() and re.match(r"\d+[._]", p.name) \
                and not re.search(r"\.(pml|ico|ini|lst|par|lnk|dat|ent|ms)$", low):
            cam_paths.append(p)
    cam_paths = sorted(set(cam_paths))
    qc_xlsx = find_one(part_dir, "**/*.xlsx")

    # PowerMILL project (setup / workplane / strategy / designed feeds) — JSON only
    pmlprj = find_one(part_dir, "**/*.pmlprj")
    cam_project = None
    if pmlprj:
        try:
            spec = importlib.util.spec_from_file_location("pml_project", HERE / "pml_project.py")
            pm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pm)
            cam_project = pm.load_project(pmlprj)
        except Exception as e:                                  # noqa: BLE001
            print(f"  ! CAM project parse failed: {e}")

    # job manifest — the shop-floor "what actually happened at setup" file
    job_path = part_dir / "job.json"
    job = None
    if job_path.exists():
        raw = json.loads(job_path.read_text(encoding="utf-8"))
        job = {k: v for k, v in raw.items() if not k.startswith("_")}

    print(f"\n=== {part_id}   ({part_dir}) ===")
    print(f"CAM       : {len(cam_paths)} toolpath file(s)")
    if cam_project:
        print(f"CAM proj  : {cam_project['project_file']} — "
              f"{len(cam_project['toolpaths'])} toolpaths, workplanes {cam_project['workplanes']}")
    print(f"QC        : {qc_xlsx.name if qc_xlsx else 'NONE'}")
    print(f"CAD       : {step.name if step else 'NONE'}")
    print(f"job.json  : {'loaded' if job else 'not present (see the template it writes)'}")

    # job.json machine/factory override the CLI defaults (on the per-part copy only) —
    # lets one --parts-root run span parts on different machines, e.g. Krishna/STM and
    # TS side by side, instead of assuming every part folder is the same machine.
    if job and (job.get("machine") or job.get("factory")):
        prev_machine, prev_factory = a.machine_id, a.factory_id
        a.machine_id = job.get("machine") or a.machine_id
        a.factory_id = job.get("factory") or a.factory_id
        if (a.machine_id, a.factory_id) != (prev_machine, prev_factory):
            print(f"          machine/factory from job.json: {a.factory_id}/{a.machine_id}"
                  f" (was {prev_factory}/{prev_machine})")

    # job.json run windows override the CLI dates (on the per-part copy only)
    if job and job.get("runs"):
        starts = [r["start"] for r in job["runs"] if r.get("start")]
        ends = [r["end"] for r in job["runs"] if r.get("end")]
        if starts and ends:
            a.start, a.end = min(starts), max(ends)
            a.part_actual = f"{min(starts)},{max(ends)}"
            print(f"          run window from job.json: {a.start} .. {a.end}"
                  + (f"  ({len(job['runs'])} run segments)" if len(job["runs"]) > 1 else ""))

    tele = load_telemetry(part_dir, a)
    ops = tele["operations"] if tele else []
    print(f"telemetry : {len(ops)} operation(s) with data"
          + ("" if tele else "  (none)"))

    # job.json "excluded_runs" — specific telemetry runs the shop has confirmed belong to
    # a different part (e.g. the operator reused a program name later in the day). These
    # stay in the machine-level Summary (computed from the full `tele`, untouched below) but
    # are kept out of this part's closed-loop / per-operation analysis.
    records_ops = ops
    if job and job.get("excluded_runs"):
        excl = {(e.get("program_name", "").lower(), e.get("start")) for e in job["excluded_runs"]}
        records_ops = [o for o in ops if (o["program_name"].lower(), o["start"]) not in excl]
        n_excl = len(ops) - len(records_ops)
        if n_excl:
            print(f"  * excluding {n_excl} telemetry run(s) from the closed-loop analysis per "
                  f"job.json excluded_runs (a different part) — still counted in the "
                  f"machine-level Summary above.")

    qc = parse_qc_xlsx(qc_xlsx) if qc_xlsx else {"rows": [], "meta": {}}
    qc_rows, qc_meta = qc["rows"], qc["meta"]
    if not job:
        _write_job_template(job_path, part_id, qc_meta, a, cam_project)
    n_graded = sum(1 for r in qc_rows if r["pass"] is not None)
    n_pass = sum(1 for r in qc_rows if r["pass"])
    print(f"QC rows   : {len(qc_rows)}  ({n_pass}/{n_graded} in tolerance)")

    # operation → QC-dim map (hand-filled; annotated stub if missing)
    map_path = part_dir / "operation_qc_map.json"
    if map_path.exists():
        raw = json.loads(map_path.read_text())
        op_qc_map = {k: list(v) for k, v in raw.items() if not k.startswith("_")}
        if not any(op_qc_map.values()):
            print(f"  * {map_path.name}: no operation is mapped to a QC dimension yet — "
                  f"QC grading is OFF. Open the drawing, and for each SL number below decide which "
                  f"operation cuts that surface; put its SL numbers in that operation's list. "
                  f"The `_operations` and `_qc_dimensions` blocks in the file give you everything "
                  f"to do this without cross-referencing.")
    else:
        op_qc_map = {}

    # (re)write the annotated helper blocks in the map without touching user mappings
    _write_qc_map_scaffold(map_path, cam_paths, qc_rows, cam_project)

    # optional hand-authored (later: generated) step-by-step deviation notes
    step_path = part_dir / "step_analysis.json"
    step_analysis = None
    if step_path.exists():
        raw = json.loads(step_path.read_text(encoding="utf-8"))
        step_analysis = {k: v for k, v in raw.items() if not k.startswith("_")}
        print(f"step notes: {step_path.name} "
              f"({len(step_analysis.get('operations', []))} operations, "
              f"{len(step_analysis.get('glossary', []))} glossary terms)")

    part_meta = {
        "part": part_id, "part_name": qc_meta.get("part_name") or part_id,
        "material": qc_meta.get("material") or "unknown",
        "machine": a.machine_id, "factory": a.factory_id,
        "run_window": {"start": a.start, "end": a.end} if tele else None,
    }
    tele_for_records = {**tele, "operations": records_ops} if tele else tele
    records, unmatched_ops = build_records(cam_paths, tele_for_records, qc_rows, op_qc_map, part_meta,
                                           cam_project=cam_project, job=job)
    if unmatched_ops:
        print(f"  ! {len(unmatched_ops)} telemetry operation(s) don't match any CAM file — "
              f"real machine time, currently invisible in the closed-loop tables:")
        for u in unmatched_ops:
            print(f"      {u['program_name']:20} {u['start'][:19]}Z .. {u['end'][:19]}Z  "
                  f"(cutting {u['cutting_s']:.0f}s)")

    ran = [r for r in records if r["actual"]]
    fails = [r for r in records if r["qc"]["outcome"] == "FAIL"]
    flagged = [r for r in records if r["flags"]]

    # setups: from job.json if filled, else from the CAM project's workplanes
    wpd = (cam_project or {}).get("workplane_detail") or {}
    setups_out = []
    if job and job.get("setups"):
        for s in job["setups"]:
            det = wpd.get(str(s.get("cam_workplane"))) or {}
            setups_out.append({
                "id": s.get("id"), "cam_workplane": s.get("cam_workplane"),
                "operations": s.get("operations", []),
                "orientation": s.get("orientation") or det.get("orientation") or None,
                "orientation_source": ("job.json" if s.get("orientation")
                                       else "decoded from CAM project" if det.get("orientation")
                                       else None),
                "workplane_transform": {k: det.get(k) for k in
                                        ("rotation_deg", "origin_model_mm", "z_axis_in_model")}
                                       if det.get("decoded") else None,
                "workholding": s.get("workholding") or None,
                "stock_actual_mm": s.get("stock_actual_mm") or None,
                "notes": s.get("notes") or None,
                "source": "job.json",
            })
    elif cam_project:
        wpd = cam_project.get("workplane_detail") or {}
        for wp, wops in (cam_project.get("setups_by_workplane") or {}).items():
            det = wpd.get(str(wp)) or {}
            setups_out.append({
                "id": f"WP{wp}", "cam_workplane": wp, "operations": sorted(wops),
                "orientation": det.get("orientation"),
                "orientation_source": "decoded from CAM project" if det.get("orientation") else None,
                "workplane_transform": {k: det.get(k) for k in
                                        ("rotation_deg", "origin_model_mm", "z_axis_in_model")}
                                       if det.get("decoded") else None,
                "workholding": None, "stock_actual_mm": None,
                "notes": "workholding not recorded — fill job.json",
                "source": "cam_project",
            })

    job_outcome = (job or {}).get("outcome") or {}
    summary = {
        "operations_total": len(records),
        "operations_with_telemetry": len(ran),
        "operations_off_window": len(records) - len(ran),
        "setups": len(setups_out) or None,
        "quantity": (job or {}).get("quantity"),
        "qc_dimensions": len(qc_rows),
        "qc_in_tolerance": f"{n_pass}/{n_graded}",
        "operations_qc_mapped": sum(1 for r in records if r["qc"]["dims"]),
        "operations_failed": len(fails),
        "operations_flagged": len(flagged),
        "part_outcome": "FAIL" if fails else ("PASS" if n_graded and n_pass == n_graded else "incomplete"),
        "shop_floor_outcome": (f"{job_outcome.get('parts_good')} good / "
                               f"{job_outcome.get('parts_scrap')} scrap"
                               if job_outcome.get("parts_good") is not None else None),
        "job_json": "loaded" if job else "not filled — on-ground data missing",
        "cam_project": (f"{cam_project['project_file']} · workplanes "
                        f"{','.join(cam_project['workplanes'])}") if cam_project else None,
        "telemetry_coverage_note": (
            f"telemetry window {a.start} .. {a.end}; operations that ran outside it have actual=null."
            if tele else "no telemetry for this part"),
        "unmatched_telemetry_ops": len(unmatched_ops),
    }

    record_out = {"part_meta": part_meta, "summary": summary,
                  "setups": setups_out, "records": records,
                  "unmatched_telemetry_ops": unmatched_ops,
                  "generated_from": {"cam": [p.name for p in cam_paths],
                                     "cam_project": cam_project["project_file"] if cam_project else None,
                                     "qc": qc_xlsx.name if qc_xlsx else None,
                                     "cad": step.name if step else None,
                                     "job": "job.json" if job else None}}
    (part_dir / "closed_loop_record.json").write_text(
        json.dumps(record_out, indent=2, default=str), encoding="utf-8")
    print(f"wrote {part_dir / 'closed_loop_record.json'}")

    # build the per-part report payload (knowledge_count/_rules are added by the
    # caller once the shared knowledge base has been rebuilt across all parts)
    closed_loop = {"summary": summary, "records": records, "setups": setups_out,
                  "unmatched_telemetry_ops": unmatched_ops}
    photos = _load_workholding_photos(part_dir)
    if photos:
        closed_loop["workholding_photos"] = photos
    if step_analysis:
        closed_loop["step_analysis"] = step_analysis
    if cam_project:
        closed_loop["cam_project"] = {
            "project_file": cam_project["project_file"],
            "workplanes": cam_project["workplanes"],
            "workplane_detail": cam_project.get("workplane_detail"),
            "note": cam_project["workplane_transforms"],
        }

    def _op_key(r):
        ro = (r.get("actual") or {}).get("run_order")
        return (0, ro) if ro is not None else (1, r["operation"].lower())
    ordered = sorted(records, key=_op_key)
    closed_loop["inputs"] = {
        "part": part_id,
        "job": job,
        "qc_map": {r["operation"]: r["qc"]["dims"] for r in ordered},
        "qc_dims": [{"sl": r["sl"], "description": r["description"],
                     "nominal": r["nominal"], "measured": r["measured"],
                     "tol_plus": r["tol_plus"], "tol_minus": r["tol_minus"]}
                    for r in qc_rows],
        "operations": [{"name": r["operation"], "feature": r["feature"],
                        "run_order": (r.get("actual") or {}).get("run_order"),
                        "workplane": (r.get("planned_cam") or {}).get("workplane")}
                       for r in ordered],
        "cam_workplanes": cam_project["workplanes"] if cam_project else [],
    }

    if tele:
        tele.pop("_km_module", None)
        if tele.get("_telemetry_source"):
            closed_loop["telemetry_source"] = tele["_telemetry_source"]
        tele["closed_loop"] = closed_loop

    print(f"summary   : {summary['operations_with_telemetry']}/{summary['operations_total']} ops with "
          f"telemetry · QC {summary['qc_in_tolerance']} · outcome {summary['part_outcome']} · "
          f"{len(flagged)} flagged")
    return {"part_id": part_id, "part_name": part_meta["part_name"],
            "records": records, "summary": summary, "flagged": flagged, "tele": tele}


def _discover_parts(root: Path) -> list[Path]:
    """Immediate subfolders that actually have machining content (a job.json or
    any .tap / extensionless numbered G-code). Empty skeleton folders are skipped."""
    out = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        has_gcode = any(p.glob("**/*.tap")) or any(
            re.match(r"\d+[._]", f.name) for f in p.glob("**/*")
            if f.is_file() and "cam files" in str(f.parent).lower())
        if (p / "job.json").exists() or has_gcode:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("part_dir", nargs="*",
                    help="one or more part folders (omit if using --parts-root)")
    ap.add_argument("--parts-root",
                    help="a folder whose immediate subfolders are parts — assemble them all "
                         "into one multi-part report")
    ap.add_argument("--start", default=PILOT_START)
    ap.add_argument("--end", default=PILOT_STOP)
    ap.add_argument("--part-actual", default=PILOT_ACTUAL)
    ap.add_argument("--factory-id", default="krishna")
    ap.add_argument("--machine-id", default="STM")
    ap.add_argument("--no-telemetry", action="store_true")
    ap.add_argument("--knowledge", default=str(HERE / "shop_knowledge.jsonl"))
    args = ap.parse_args()

    if args.parts_root:
        dirs = _discover_parts(Path(args.parts_root))
        if not dirs:
            sys.exit(f"no part folders under {args.parts_root}")
        print(f"parts-root {args.parts_root} → {len(dirs)} part(s): "
              f"{', '.join(d.name for d in dirs)}")
    elif args.part_dir:
        dirs = [Path(d) for d in args.part_dir]
    else:
        sys.exit("give one or more part folders, or --parts-root <folder>")

    spec = importlib.util.spec_from_file_location("krishna_metrics", HERE / "krishna_metrics.py")
    km = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(km)

    results = [r for r in (assemble_part(d, args, km) for d in dirs) if r]
    if not results:
        sys.exit("no parts assembled")

    # shared knowledge base — one row per (part, operation); regenerated parts
    # replace their own rows, every other part's rows are kept
    kb = Path(args.knowledge)
    regen = {r["part_id"] for r in results}
    kept = []
    if kb.exists():
        for ln in kb.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if o.get("part") not in regen:
                kept.append(ln)
    with kb.open("w", encoding="utf-8") as f:
        for ln in kept:
            f.write(ln + "\n")
        for r in results:
            for rec in r["records"]:
                f.write(json.dumps(rec, default=str) + "\n")
    kr = knowledge_rules(kb)
    n_new = sum(len(r["records"]) for r in results)
    print(f"\nknowledge base: {n_new} record(s) across {len(results)} part(s) written to "
          f"{kb.name}; {len(kept)} row(s) from other parts kept")

    tele_results = [r for r in results if r["tele"]]
    for r in tele_results:
        r["tele"]["closed_loop"]["knowledge_count"] = _count_lines(kb)

    tpl, out = HERE / "krishna_report_template.html", HERE / "krishna_report.html"
    if len(results) == 1:
        tele = results[0]["tele"]
        if not tele:
            print("\nno telemetry for this part — record + knowledge base updated, report not re-rendered")
        else:
            tele["closed_loop"]["knowledge_rules"] = kr
            km.write_report(tele, tpl, out)
            (HERE / "krishna_metrics.json").write_text(
                json.dumps(tele, indent=2, default=str), encoding="utf-8")
    else:
        import datetime as _dt
        mp = [{"label": r["part_name"] or r["part_id"], "part_id": r["part_id"],
               "payload": r["tele"]} for r in tele_results]
        if not mp:
            sys.exit("no part has telemetry — nothing to render")
        combined = {"machined_parts": mp, "knowledge_rules": kr,
                    "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        km.write_report(combined, tpl, out)
        (HERE / "krishna_metrics.json").write_text(
            json.dumps(combined, indent=2, default=str), encoding="utf-8")
        skipped = [r["part_id"] for r in results if not r["tele"]]
        print(f"\nmulti-part report: {len(mp)} part(s) — {', '.join(m['label'] for m in mp)}"
              + (f"  (no telemetry, omitted: {', '.join(skipped)})" if skipped else ""))


def _rng(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    return [round(lo, 1)] if abs(hi - lo) < 1e-9 else [round(lo, 1), round(hi, 1)]


def knowledge_rules(kb_path: Path) -> list[dict]:
    """Group every labelled operation record in shop_knowledge.jsonl by
    (material, tool type, diameter, feature) — the parameter table a programmer
    would consult: what S/F produced good parts for this combination."""
    if not kb_path.exists():
        return []
    G = {}
    for ln in kb_path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = r.get("tool") or {}
        key = (r.get("material"), t.get("type"), t.get("dia_mm"), r.get("feature"))
        g = G.setdefault(key, {"n": 0, "parts": set(), "S_posted": set(), "F_posted": set(),
                               "S_run": [], "F_run": [], "vc_des": set(), "fz_des": set(),
                               "strategy": set(), "qc": collections.Counter(), "ovr": []})
        prog, pc = r.get("programmed") or {}, r.get("planned_cam") or {}
        act, d = r.get("actual") or {}, r.get("deltas") or {}
        g["n"] += 1
        g["parts"].add(r.get("part"))
        if prog.get("S"):
            g["S_posted"].add(prog["S"])
        if prog.get("F_cut"):
            g["F_posted"].add(prog["F_cut"])
        if act.get("rpm_median"):
            g["S_run"].append(act["rpm_median"])
        if act.get("feed_median"):
            g["F_run"].append(act["feed_median"])
        if pc.get("designed_vc_m_min"):
            g["vc_des"].add(round(pc["designed_vc_m_min"], 1))
        if pc.get("designed_fz_mm"):
            g["fz_des"].add(round(pc["designed_fz_mm"], 3))
        if pc.get("strategy"):
            g["strategy"].add(pc["strategy"])
        g["qc"][(r.get("qc") or {}).get("outcome", "?")] += 1
        if d.get("feed_pct") is not None:
            g["ovr"].append(d["feed_pct"])
    out = []
    for (mat, tt, dia, feat), g in sorted(G.items(), key=lambda kv: -kv[1]["n"]):
        out.append({
            "material": mat, "tool_type": tt, "dia_mm": dia, "feature": feat,
            "records": g["n"], "parts": len(g["parts"]),
            "S_posted": sorted(g["S_posted"]), "F_posted": sorted(g["F_posted"]),
            "S_run": _rng(g["S_run"]), "F_run": _rng(g["F_run"]),
            "vc_des": sorted(g["vc_des"]), "fz_des": sorted(g["fz_des"]),
            "strategy": sorted(g["strategy"]),
            "feed_override_pct": _rng(g["ovr"]),
            "qc": dict(g["qc"]),
        })
    return out


def _count_lines(p: Path) -> int:
    try:
        return sum(1 for _ in p.open(encoding="utf-8"))
    except OSError:
        return 0


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
