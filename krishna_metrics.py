"""
krishna_metrics.py — Krishna (STM) controller-telemetry metrics builder.

Pulls `cnc_telemetry` for machine_id=STM out of InfluxDB, segments the sample
stream into program runs, computes machine-level + per-run job-level metrics,
and writes:

  krishna_metrics.json   the numbers
  krishna_report.html    a self-contained report (open in a browser)

Design notes that are NOT obvious from the field names — all verified against
live data 2026-09-07, see audit/SPEC_krishna_telemetry_metrics.md §0:

  * `cycle_time` is MM·SS-PACKED (…758, 759, 800, 801…), so 741 = 7m41s.
    `cutting_time` is plain seconds. They are decoded differently.
  * `program_runtime` is NOT sent by this machine — run duration is wall-clock.
  * Run boundaries can NOT come from cycle_time resets: the counter climbs
    straight across multi-hour collector outages. We split on program change,
    a long wall-clock gap, or cycle_time going backwards.
  * `production_count` is an M30 (program-completion) counter, not a part
    counter — deliberately NOT reported as parts.
  * `tool_number` is always 0; tool identity is parsed from the program name.
  * The override pots and spindle load are not actually being read by Smart
    Connect (constants / all-zero). Rather than hide that, every expected field
    is health-checked and the dead ones are reported in their own section.

Usage:
  python krishna_metrics.py                     # the 4-Sep pilot part window
  python krishna_metrics.py --days 30
  python krishna_metrics.py --start 2026-09-02T00:00:00Z --end 2026-09-06T00:00:00Z
  python krishna_metrics.py --machine-id STM --factory-id krishna
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import statistics
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")


def _load_dotenv(path: str = ".env") -> None:
    """Tiny built-in .env loader — no extra dependency. Existing environment
    variables always win; a value already set (e.g. by the shell or CI) is
    never overridden by the file."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# Connection details come from the environment (or a local .env — see
# .env.example) so real credentials never live in source. Nothing here is
# required at import time — only when a live pull actually happens, so
# --no-telemetry / offline tests keep working without any of this set.
BASE_URL = os.environ.get("INFLUX_BASE_URL", "")
ORG = os.environ.get("INFLUX_ORG", "cnc-org")
BUCKET = os.environ.get("INFLUX_BUCKET", "cnc-data-v2")
MEASUREMENT = "cnc_telemetry"
USERNAME = os.environ.get("INFLUX_USERNAME", "")
PASSWORD = os.environ.get("INFLUX_PASSWORD", "")

# The pilot part: one physical part, 8 operations back to back.
PILOT_START = "2026-09-04T03:00:00Z"
PILOT_STOP = "2026-09-04T14:00:00Z"

RUN_GAP_SPLIT_S = 300      # wall-clock gap that starts a new run
SAMPLE_DT_CAP_S = 10       # ignore dt beyond this when bucketing time (collector gaps)
RUNNING_STATE = 3          # Mitsubishi: 3 = running (Fanuc machines use 1)
# machine_mode decoded 2026-09-11 (scratchpad/krishna_machine_mode_check.py): confirmed
# against two known manual-jog stretches vs two known auto-cutting stretches on 4-Sep.
# 0 = AUTO/MEM (normal program execution or a pause inside it) — every confirmed cutting
# sample read 0. Anything else means the operator is NOT running the program automatically:
# 10 = sustained JOG/MDI (operator moving the machine by hand — this is the mode seen for
# minutes at a stretch during the two known manual sessions); 6/7 = brief transitional
# blips around a mode switch; 12 = the G91 G28 reference-return rapid every program opens
# with. Only reclassifies samples that would otherwise be "stopped" — never touches a
# sample already counted as cutting/air.
MANUAL_MODES = {6, 7, 10, 12}

NUMERIC_FIELDS = [
    "spindle_speed", "feed_rate", "spindle_load", "spindle_override",
    "rapid_override", "feed_override", "cutting_time", "cycle_time",
    "program_runtime", "production_count", "machine_state", "machine_mode",
    "alarm_active", "axis_x", "axis_y", "axis_z",
]

# ---------------------------------------------------------------- influx ----


def get_session() -> str:
    if not BASE_URL or not USERNAME or not PASSWORD:
        raise RuntimeError(
            "InfluxDB connection not configured. Copy .env.example to .env in the repo "
            "root and fill in INFLUX_BASE_URL / INFLUX_USERNAME / INFLUX_PASSWORD "
            "(or export them as environment variables).")
    r = requests.post(f"{BASE_URL}/api/v2/signin", auth=(USERNAME, PASSWORD),
                      verify=False, timeout=30)
    r.raise_for_status()
    cookie = r.cookies.get("influxdb-oss-session")
    if not cookie:
        raise RuntimeError("signed in but no influxdb-oss-session cookie returned")
    return cookie


def query(session: str, flux: str) -> list[dict]:
    r = requests.post(f"{BASE_URL}/api/v2/query", params={"org": ORG},
                      headers={"Content-Type": "application/vnd.flux",
                               "Accept": "application/csv"},
                      cookies={"influxdb-oss-session": session},
                      data=flux, verify=False, timeout=180)
    if r.status_code == 401:
        session = get_session()
        return query(session, flux)
    if r.status_code >= 400:
        raise RuntimeError(f"influx {r.status_code}: {r.text[:500]}")
    # Flux annotated CSV re-emits the header row before each new table group;
    # csv.DictReader turns those into junk rows ({"_time": "_time", ...}). Drop them.
    rows = list(csv.DictReader(io.StringIO(r.text)))
    return [row for row in rows
            if row.get("result") != "result" and row.get("_time") != "_time"
            and not (row.get("_field") == "_field")]


def fetch_samples(session: str, factory_id: str, machine_id: str,
                  start: str, stop: str | None) -> list[dict]:
    rng = f"start: {start}" + (f", stop: {stop}" if stop else "")
    flux = f'''
from(bucket: "{BUCKET}")
  |> range({rng})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}"
        and r.factory_id == "{factory_id}" and r.machine_id == "{machine_id}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    rows = query(session, flux)
    out = []
    for row in rows:
        t = row.get("_time")
        if not t:
            continue
        s = {"_time": t, "t": _parse_ts(t), "program_name": row.get("program_name") or ""}
        for f in NUMERIC_FIELDS:
            s[f] = _num(row.get(f))
        s["block_number"] = _num(row.get("block_number"))  # stored as string on STM
        out.append(s)
    out.sort(key=lambda s: s["t"])
    return out


def _parse_ts(s: str) -> float:
    s = s.replace("Z", "+00:00")
    if "." in s:                                   # trim to microseconds
        head, rest = s.split(".", 1)
        frac, tz = re.match(r"(\d+)(.*)", rest).groups()
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


# ---------------------------------------------------------------- decode ----


def cycle_to_seconds(v):
    """cycle_time is MM·SS-packed: 741 -> 7m41s -> 461s. Verified 2026-09-07."""
    if v is None:
        return None
    v = int(v)
    return (v // 100) * 60 + (v % 100)


def _canon_state(raw):
    """Fold this machine's raw machine_state into 0=idle/reset / 1=running / 2=stop.
    STM: 3 -> running. (A Fanuc machine would already use 1.)"""
    if raw is None:
        return None
    s = int(raw)
    if RUNNING_STATE != 1:
        return 1 if s == RUNNING_STATE else (2 if s == 2 else 0)
    return s


TOOL_RE = re.compile(
    r"^(?P<dia>\d+(?:\.\d+)?)(?P<type>ENDMILL|END|BULLNOSE|BULL|BOLL|BALL|DRILL|CD|FACE|CHAM|TAP|REAM|SLOT)$",
    re.I)
TOOL_TYPE_NAMES = {
    "END": "endmill", "ENDMILL": "endmill", "BULL": "bull-nose", "BULLNOSE": "bull-nose",
    "BOLL": "ball-nose", "BALL": "ball-nose", "DRILL": "drill", "CD": "centre drill",
    "FACE": "face mill", "CHAM": "chamfer", "TAP": "tap", "REAM": "reamer", "SLOT": "slot mill",
}
PASS_NAMES = {"FIN": "finish", "FINISH": "finish", "FLAT": "flat", "OUTER": "outer profile",
              "TEST": "test", "SIZE": "sizing", "ROUGH": "rough"}


def parse_program_name(name: str) -> dict:
    """`1_16BULL_FIN.TAP` -> seq 1, Ø16 bull-nose, finish.

    NOTE: the leading number is the OPERATION SEQUENCE, not the setup number —
    verified on the 4-Sep pilot job where 1_..7_ all ran inside one setup. It is
    also not always present (`10END_FLAT.TAP` ran 5th with no prefix).
    """
    out = {"seq_no": None, "tool_dia_mm": None, "tool_type": None,
           "pass": None, "label": name}
    stem = re.sub(r"\.(TAP|PRG|NC|MPF)$", "", (name or "").strip(), flags=re.I)
    if not stem:
        return out
    tokens = [t for t in stem.split("_") if t]
    if tokens and tokens[0].isdigit():
        out["seq_no"] = int(tokens.pop(0))
    quals = []
    for tok in tokens:
        m = TOOL_RE.match(tok)
        if m and out["tool_dia_mm"] is None:
            out["tool_dia_mm"] = float(m.group("dia"))
            out["tool_type"] = TOOL_TYPE_NAMES.get(m.group("type").upper(), m.group("type").lower())
        else:
            quals.append(PASS_NAMES.get(tok.upper(), tok.lower()))
    out["pass"] = " ".join(quals) if quals else None
    bits = []
    if out["tool_dia_mm"] is not None:
        d = out["tool_dia_mm"]
        bits.append(f"Ø{int(d) if float(d).is_integer() else d} {out['tool_type']}")
    if out["pass"]:
        bits.append(out["pass"])
    out["label"] = " · ".join(bits) if bits else name
    return out


# ------------------------------------------------------------- segmenting ---


CYCLE_RESET_FRAC = 0.30   # a cycle_time reset must land below this fraction of the prior peak
CYCLE_RESET_PERSIST = 2   # ...and stay reset for this many consecutive samples


def segment_runs(samples: list[dict]) -> list[list[dict]]:
    """Split on program change, long wall-clock gap, or a real cycle_time reset.

    NOT on cycle_time reset alone — the counter climbs through multi-hour
    collector outages (spec §0c). And a *reset* must be genuine: a single
    glitchy MM·SS sample must not spawn a phantom cycle, so we require the
    decoded value to drop below CYCLE_RESET_FRAC of the run's running peak AND
    stay low for CYCLE_RESET_PERSIST samples (i.e. cycle-start really was
    pressed again, M30 -> new execution).
    """
    runs, cur = [], []
    peak = 0.0

    def real_reset(i):
        c = _cyc(samples[i])
        if c is None or peak <= 0 or c >= peak * CYCLE_RESET_FRAC:
            return False
        # confirm it persists (next samples also well below the peak)
        for j in range(i + 1, min(i + 1 + CYCLE_RESET_PERSIST, len(samples))):
            nc = _cyc(samples[j])
            if nc is not None and nc >= peak * CYCLE_RESET_FRAC:
                return False
        return True

    for i, s in enumerate(samples):
        if cur:
            prev = cur[-1]
            new_run = (
                s["program_name"] != prev["program_name"]
                or (s["t"] - prev["t"]) > RUN_GAP_SPLIT_S
                or real_reset(i)
            )
            if new_run:
                runs.append(cur)
                cur = []
                peak = 0.0
        cur.append(s)
        c = _cyc(s)
        if c is not None and c > peak:
            peak = c
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2]


def _cyc(s):
    return cycle_to_seconds(s.get("cycle_time"))


# ---------------------------------------------------------------- metrics ---


def _stats(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    vals_sorted = sorted(vals)
    return {
        "n": len(vals),
        "min": min(vals),
        "mean": round(statistics.fmean(vals), 1),
        "median": round(statistics.median(vals), 1),
        "p95": vals_sorted[min(len(vals_sorted) - 1, int(0.95 * len(vals_sorted)))],
        "max": max(vals),
    }


def _override_stats(samples, field, default):
    """Override-knob stats. A reading of 0 means the knob isn't feeding (rapids /
    between moves), not a 0% setting — so the 'active' numbers exclude 0 as well
    as the 100% default, to describe what the operator actually dialled in."""
    vals = [s[field] for s in samples if s.get(field) is not None]
    # an override knob is a percentage — keep 0..300; anything larger is the
    # controller reporting something else in that register on odd samples
    vals = [v for v in vals if isinstance(v, (int, float)) and 0 <= v <= 300]
    if not vals:
        return {"available": False, "field": field}
    active = [v for v in vals if v not in (default, 0)]
    return {
        "available": True, "field": field, "default": default,
        "n": len(vals), "events": len(active),
        "pct_time": round(100.0 * len(active) / len(vals), 2),
        "median_active": round(statistics.median(active), 1) if active else None,
        "min_active": min(active) if active else None,
        "max_active": max(active) if active else None,
        "distinct": sorted(set(vals))[:16],
        "constant": len({v for v in vals if v != 0}) <= 1,
    }


def _merge_override(runs, key):
    """Roll the per-run override stats (from _override_stats) up to an operation."""
    ov = [r[key]["override"] for r in runs if r[key].get("override", {}).get("available")]
    if not ov:
        return {"available": False}
    distinct = sorted({v for o in ov for v in o.get("distinct", [])})
    active = [v for v in distinct if v not in (100, 0)]
    n = sum(o["n"] for o in ov)
    ev = sum(o["events"] for o in ov)
    meds = [o["median_active"] for o in ov if o.get("median_active") is not None]
    return {
        "available": True, "field": ov[0]["field"], "default": ov[0].get("default", 100),
        "n": n, "events": ev, "pct_time": round(100.0 * ev / n, 1) if n else 0,
        "min_active": min(active) if active else None,
        "max_active": max(active) if active else None,
        "median_active": round(statistics.median(meds), 1) if meds else None,
        "distinct": distinct[:16],
        "constant": len(active) <= 1 and ev == 0,
    }


def bucket_time(samples: list[dict]) -> dict:
    """Walk consecutive pairs and split the elapsed wall-clock time into
    cutting / air / stopped / untracked. STATE-AWARE — a live cross-tab
    (2026-09-07, scratchpad/krishna_state_vs_bucket.py) showed the counter-only
    method calls ~1/3 of "paused" running (two samples in the same controller-
    second read equal counters). So: state 2/0 -> stopped; running -> split by
    whether cutting_time advanced; gap > cap -> untracked (collector not sampling).

    Also returns the machine_state % distribution over the classified intervals,
    so callers can see what state each bucket actually sat in.
    """
    cutting = air = stopped = manual = untracked = 0.0
    st_secs = {}  # canon state -> seconds
    for a, b in zip(samples, samples[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        if dt > SAMPLE_DT_CAP_S:
            untracked += dt
            continue
        st = _canon_state(b.get("machine_state"))
        if st is not None:
            st_secs[st] = st_secs.get(st, 0.0) + dt
        if st in (0, 2):
            if b.get("machine_mode") in MANUAL_MODES:
                manual += dt        # operator in JOG/MDI, not a program pause
            else:
                stopped += dt
        else:  # running, or state missing -> fall back to the counter
            ca, cb = a.get("cutting_time"), b.get("cutting_time")
            if ca is not None and cb is not None and cb > ca:
                cutting += dt
            else:
                air += dt
    tracked = cutting + air + stopped + manual or 1.0
    return {
        "cutting_s": round(cutting, 1), "air_s": round(air, 1),
        "stopped_s": round(stopped, 1), "manual_s": round(manual, 1),
        "untracked_s": round(untracked, 1),
        "state_secs": {str(k): round(v, 1) for k, v in sorted(st_secs.items())},
        "running_s": round(cutting + air, 1),
        "cutting_pct": round(100 * cutting / tracked, 1),
    }


def run_metrics(run: list[dict], override_field: str) -> dict:
    t0, t1 = run[0]["t"], run[-1]["t"]
    wallclock = round(t1 - t0, 1)

    cycs = [c for c in (_cyc(s) for s in run) if c is not None]
    cuts = [s["cutting_time"] for s in run if s.get("cutting_time") is not None]
    cycle_s = (max(cycs) - min(cycs)) if cycs else None
    cutting_s = (max(cuts) - min(cuts)) if cuts else None

    _b = bucket_time(run)
    cutting_b, air_b, stopped_b, manual_b, untracked_b = (
        _b["cutting_s"], _b["air_s"], _b["stopped_s"], _b["manual_s"], _b["untracked_s"])
    in_prog = cutting_b + air_b or 1.0

    states = Counter(s["machine_state"] for s in run if s.get("machine_state") is not None)
    n_state = sum(states.values()) or 1

    alarms = [s["alarm_active"] for s in run if s.get("alarm_active") is not None]
    transitions = sum(1 for a, b in zip(alarms, alarms[1:]) if a == 0 and b == 1)

    loads = [s["spindle_load"] for s in run if s.get("spindle_load") is not None]
    nonzero_loads = [v for v in loads if v]

    return {
        "program_name": run[0]["program_name"],
        "parsed": parse_program_name(run[0]["program_name"]),
        "start": run[0]["_time"], "end": run[-1]["_time"],
        "samples": len(run),
        "timing": {
            "wallclock_s": wallclock,
            "cycle_time_s": cycle_s,
            "cutting_time_s": cutting_s,
            "cutting_ratio": round(cutting_s / cycle_s, 3) if cycle_s else None,
            "cutting_s": round(cutting_b, 1),
            "air_s": round(air_b, 1),
            "stopped_s": round(stopped_b, 1),
            "manual_s": round(manual_b, 1),
            "untracked_s": round(untracked_b, 1),
            "cutting_pct": round(100 * cutting_b / in_prog, 1),
            "air_pct": round(100 * air_b / in_prog, 1),
            "state_secs": _b["state_secs"],
        },
        "spindle": {
            "rpm": _stats([s["spindle_speed"] for s in run if (s.get("spindle_speed") or 0) > 0]),
            "load_max": max(loads) if loads else None,
            "load_nonzero_samples": len(nonzero_loads),
            "override": _override_stats(run, "spindle_override", 100),
        },
        "feed": {
            "rate": _stats([s["feed_rate"] for s in run if (s.get("feed_rate") or 0) > 0]),
            "override": _override_stats(run, override_field,
                                        25 if override_field == "rapid_override" else 100),
        },
        "state_distribution": {str(k): round(100 * v / n_state, 1) for k, v in sorted(states.items())},
        "alarms": {"events": transitions,
                   "active_samples": sum(1 for a in alarms if a == 1),
                   "active_pct": round(100 * sum(1 for a in alarms if a == 1) / (len(alarms) or 1), 2)},
    }


UTILISATION_FORMULA = (
    "utilisation = samples with machine_state = {run} ÷ all samples with a machine_state, "
    "over the window. On this Mitsubishi controller state 3 = running (state 2 = hold/stop, "
    "0 = reset). NOTE: this is a share of LOGGED time, not calendar time — the collector only "
    "records while it is running, so hours when the Smart Connect PC was off are absent from "
    "both numerator and denominator. Read it next to the coverage figure."
)


def build_parts(samples: list[dict], ops: list[dict], gap_h: float,
                actual_window: tuple[str, str] | None = None) -> list[dict]:
    """Cluster operations into PARTS.

    Proper part identity would come from the controller's part counter, but on
    this machine `production_count` increments on M30 (program completion), not
    per part — so it can't be used. Until that is fixed or a job/part id is fed
    in from the job manifest, a part is inferred as a cluster of operations
    separated from the next cluster by more than `gap_h` hours.
    """
    if not ops:
        return []
    gap_s = gap_h * 3600
    clusters, cur = [], [ops[0]]
    for prev, op in zip(ops, ops[1:]):
        if (_parse_ts(op["start"]) - _parse_ts(prev["end"])) > gap_s:
            clusters.append(cur)
            cur = []
        cur.append(op)
    clusters.append(cur)

    parts = []
    for i, cl in enumerate(clusters, 1):
        t0, t1 = _parse_ts(cl[0]["start"]), _parse_ts(cl[-1]["end"])
        logged_span = t1 - t0
        window = [s for s in samples if t0 <= s["t"] <= t1]
        states = Counter(s["machine_state"] for s in window if s.get("machine_state") is not None)
        n_state = sum(states.values()) or 1
        running = states.get(RUNNING_STATE, 0)

        cutting = round(sum(o["timing"]["cutting_s"] for o in cl), 1)
        air = round(sum(o["timing"]["air_s"] for o in cl), 1)
        stopped = round(sum(o["timing"]["stopped_s"] for o in cl), 1)
        manual = round(sum(o["timing"].get("manual_s", 0) for o in cl), 1)
        untracked = round(sum(o["timing"]["untracked_s"] for o in cl), 1)
        setup = round(sum(o["timing"]["idle_before_s"] for o in cl), 1)
        setup_state = {}
        for o in cl:
            for st, sec in (o["timing"].get("idle_state_secs") or {}).items():
                setup_state[st] = round(setup_state.get(st, 0.0) + sec, 1)

        part = {
            "part_index": i,
            "label": f"Part {i}",
            "start": cl[0]["start"], "end": cl[-1]["end"],
            "logged_span_s": round(logged_span, 1),
            "logged_span_h": round(logged_span / 3600, 2),
            "operations": len(cl),
            "cycles": sum(o["cycles"] for o in cl),
            "samples": len(window),
            "coverage_pct": round(100 * len(window) / logged_span, 1) if logged_span else None,
            "utilisation_pct": round(100 * running / n_state, 1),
            "utilisation_formula": UTILISATION_FORMULA.format(run=RUNNING_STATE),
            "state_distribution": {str(k): round(100 * v / n_state, 1) for k, v in sorted(states.items())},
            "cutting_s": cutting, "air_s": air, "stopped_s": stopped, "manual_s": manual,
            "untracked_s": untracked, "setup_idle_s": setup,
            "setup_idle_state_secs": setup_state,
            "cutting_ratio": round(cutting / (cutting + air), 3) if (cutting + air) else None,
            "program_sequence": [o["program_name"] for o in cl],
            "operation_indexes": [o["index"] for o in cl],
            "actual": None,
        }

        # If the real shop-floor window is known, contrast it with what was logged.
        if actual_window:
            a0, a1 = _parse_ts(actual_window[0]), _parse_ts(actual_window[1])
            if not (t1 < a0 or t0 > a1):          # overlaps this cluster
                actual_span = a1 - a0
                part["actual"] = {
                    "start": actual_window[0], "end": actual_window[1],
                    "span_s": round(actual_span, 1), "span_h": round(actual_span / 3600, 2),
                    "logged_fraction_pct": round(100 * logged_span / actual_span, 1) if actual_span else None,
                    "missing_h": round((actual_span - logged_span) / 3600, 2),
                }
        parts.append(part)
    return parts


def build_operations(runs: list[dict]) -> list[dict]:
    """Roll consecutive same-program runs up into one OPERATION.

    Segmenting purely on the raw signals over-splits, and the split pieces are
    two genuinely different things:

      * runs where `cycle_time` never advanced (cycle_time_s == 0) are the
        program-loaded-but-not-started period — i.e. the inter-operation setup /
        tool-change dead time. Worth measuring, not worth showing as an operation.
      * runs where it did advance are real machining cycles. A program can
        legitimately run several (10END_FLAT ran 3 near-identical cycles on the
        pilot part).

    So: one row per operation, with `idle_before_s` split out from the cycles.
    """
    ops = []
    for r in runs:
        if ops and ops[-1]["program_name"] == r["program_name"]:
            ops[-1]["_runs"].append(r)
        else:
            ops.append({"program_name": r["program_name"], "_runs": [r]})

    out = []
    for i, op in enumerate(ops):
        rs = op["_runs"]
        cycles = [r for r in rs if (r["timing"]["cycle_time_s"] or 0) > 0]
        idle = [r for r in rs if (r["timing"]["cycle_time_s"] or 0) == 0]
        src = cycles or rs                      # if nothing ever started, fall back

        def _sum(key):
            return round(sum(r["timing"][key] for r in src), 1)

        cutting, air = _sum("cutting_s"), _sum("air_s")
        stopped, untracked = _sum("stopped_s"), _sum("untracked_s")
        manual = _sum("manual_s")
        idle_before = round(sum(r["timing"]["wallclock_s"] for r in idle), 1)
        in_prog = cutting + air or 1.0

        # what machine_state was the machine in DURING the setup/load gap?
        idle_state = {}
        for r in idle:
            for st, sec in (r["timing"].get("state_secs") or {}).items():
                idle_state[st] = round(idle_state.get(st, 0.0) + sec, 1)

        rpms = [r["spindle"]["rpm"]["median"] for r in cycles if r["spindle"]["rpm"]]
        feeds = [r["feed"]["rate"]["median"] for r in cycles if r["feed"]["rate"]]
        loads = [r["spindle"]["load_max"] for r in cycles if r["spindle"]["load_max"] is not None]

        out.append({
            "index": i + 1,
            "program_name": op["program_name"],
            "parsed": parse_program_name(op["program_name"]),
            "start": rs[0]["start"], "end": rs[-1]["end"],
            "cycles": len(cycles),
            "samples": sum(r["samples"] for r in rs),
            "timing": {
                "wallclock_s": round(sum(r["timing"]["wallclock_s"] for r in rs), 1),
                "idle_before_s": idle_before,
                "cycle_time_s": round(sum(r["timing"]["cycle_time_s"] or 0 for r in cycles), 1),
                "cutting_s": cutting, "air_s": air,
                "stopped_s": stopped, "manual_s": manual, "untracked_s": untracked,
                "cutting_pct": round(100 * cutting / in_prog, 1),
                "air_pct": round(100 * air / in_prog, 1),
                "cutting_ratio": round(cutting / in_prog, 3) if (cutting + air) else None,
                "idle_state_secs": idle_state,   # machine_state mix during setup/load
            },
            "spindle": {"rpm_median": round(statistics.median(rpms)) if rpms else None,
                        "load_max": max(loads) if loads else None,
                        "override": _merge_override(rs, "spindle")},
            "feed": {"rate_median": round(statistics.median(feeds)) if feeds else None,
                     "override": _merge_override(rs, "feed")},
            "alarms": {"events": sum(r["alarms"]["events"] for r in rs),
                       "active_pct": round(statistics.fmean(
                           [r["alarms"]["active_pct"] for r in rs]), 2) if rs else 0},
            "cycle_detail": [{
                "start": r["start"], "wallclock_s": r["timing"]["wallclock_s"],
                "cycle_time_s": r["timing"]["cycle_time_s"],
                "cutting_s": r["timing"]["cutting_s"], "air_s": r["timing"]["air_s"],
                "stopped_s": r["timing"]["stopped_s"],
                "rpm_median": r["spindle"]["rpm"]["median"] if r["spindle"]["rpm"] else None,
                "feed_median": r["feed"]["rate"]["median"] if r["feed"]["rate"] else None,
            } for r in cycles],
        })
    return out


def machine_metrics(samples, ops) -> dict:
    t0, t1 = samples[0]["t"], samples[-1]["t"]
    span = max(t1 - t0, 1)
    states = Counter(s["machine_state"] for s in samples if s.get("machine_state") is not None)
    n_state = sum(states.values()) or 1
    running = states.get(RUNNING_STATE, 0)

    cutting_total = sum(o["timing"]["cutting_s"] for o in ops)
    air_total = sum(o["timing"]["air_s"] for o in ops)
    stopped_total = sum(o["timing"]["stopped_s"] for o in ops)
    manual_total = sum(o["timing"].get("manual_s", 0) for o in ops)
    untracked_total = sum(o["timing"]["untracked_s"] for o in ops)
    setup_idle_total = sum(o["timing"]["idle_before_s"] for o in ops)
    setup_state = {}
    for o in ops:
        for st, sec in (o["timing"].get("idle_state_secs") or {}).items():
            setup_state[st] = round(setup_state.get(st, 0.0) + sec, 1)
    in_program = cutting_total + air_total
    alarms = [s["alarm_active"] for s in samples if s.get("alarm_active") is not None]

    return {
        "window_start": samples[0]["_time"], "window_end": samples[-1]["_time"],
        "window_span_s": round(span, 1),
        "samples": len(samples),
        # % of the window NOT lost to an untracked (collector/PC-off) gap — matches the
        # "Xh sampled · everything but untracked" tooltip. NOT samples-per-second: Krishna
        # samples well under 1 Hz even when perfectly healthy, so samples/span alone would
        # read ~60-70% on a gap-free window and be read as a data-quality problem it isn't.
        "uptime_coverage_pct": round(100 * (span - untracked_total) / span, 1) if span else None,
        "sample_rate_hz": round(len(samples) / span, 3) if span else None,
        "utilisation_logged_pct": round(100 * running / n_state, 1),
        "state_distribution": {str(k): round(100 * v / n_state, 1) for k, v in sorted(states.items())},
        "operations": len(ops),
        "cycles": sum(o["cycles"] for o in ops),
        "distinct_programs": len({o["program_name"] for o in ops}),
        "cutting_s": round(cutting_total, 1),
        "air_s": round(air_total, 1),
        "stopped_s": round(stopped_total, 1),
        "manual_s": round(manual_total, 1),
        "untracked_s": round(untracked_total, 1),
        "setup_idle_s": round(setup_idle_total, 1),
        "setup_idle_state_secs": setup_state,
        "cutting_ratio_of_runs": round(cutting_total / in_program, 3) if in_program else None,
        "alarm_active_pct": round(100 * sum(1 for a in alarms if a == 1) / (len(alarms) or 1), 2),
        "alarm_events": sum(1 for a, b in zip(alarms, alarms[1:]) if a == 0 and b == 1),
    }


# ----------------------------------------------------------- field health ---

FIELD_NOTES = {
    "feed_override": ("The operator's cutting-feed knob — the single richest closed-loop "
                      "signal (where the shop disagreed with the programmed feed).",
                      "Register 'Feed Override' in Smart Connect → Data Registration, then bind "
                      "it in Topic Settings. Needs the trial licence upgraded."),
    "spindle_override": ("Operator's spindle-speed knob — same role as feed override, for RPM.",
                         "Smart Connect appears not to be reading the pot. Re-register the data "
                         "point and confirm the register address."),
    "rapid_override": ("Rapid-traverse knob. Currently stands in for feed_override.",
                       "Same as above — confirm the register is actually being read."),
    "spindle_load": ("Cutting-intensity trace → tool wear, chatter and anomaly detection, and "
                     "the load half of any parameter-tuning reward.",
                     "Register address looks wrong — it reads 0 almost always. Ask the "
                     "Mitsubishi/Krishna team to verify the spindle-load register."),
    "tool_number": ("Would attribute every sample to a specific tool without relying on the "
                    "program-name convention.",
                    "Register 'Tool Number' in Smart Connect Data Registration."),
    "program_runtime": ("Controller's own program elapsed time — a cross-check on wall-clock.",
                        "Not sent. Low priority: wall-clock covers it."),
    "production_count": ("Would bound one physical part's telemetry so a QC result can be "
                         "attached to it.",
                         "It increments on M30 (program completion), not per part — so it can't "
                         "identify a part. Part identity needs to come from the job/setup "
                         "boundary plus the QC form recording its own time window."),
    "cutting_time": ("Time actually feeding — gives the cutting / air split within running time.", ""),
    "cycle_time": ("Program elapsed time (MM·SS packed).", ""),
    "feed_rate": ("Actual feed — compared against the CAM-programmed F.", ""),
    "spindle_speed": ("Actual RPM — compared against the CAM-programmed S.", ""),
    "machine_state": ("Running / hold / reset — utilisation and dead-time.", ""),
    "machine_mode": ("Auto vs manual (JOG/MDI) — now used to split Manual/MDI time out of "
                     "Stopped, instead of hand operation being invisible in the totals.",
                     "0 = automatic program running/paused; 6/7/10/12 = the operator is "
                     "jogging or in MDI. Decoded 2026-09-11 against known manual-jog and "
                     "auto-cutting stretches (scratchpad/krishna_machine_mode_check.py); "
                     "not from official Mitsubishi documentation, so keep re-checking it."),
    "alarm_active": ("Alarm signal per run.",
                     "Derived from the free-text Alarm1 field being non-blank — it is not "
                     "confirmed whether that means 'currently active' or 'last logged'. "
                     "Confirm with the Mitsubishi/Krishna team."),
    "block_number": ("G-code line number — the key to attributing samples to a specific "
                     "toolpath segment (per-operation analysis).", ""),
    "axis_x": ("Axis position.", ""), "axis_y": ("Axis position.", ""),
    "axis_z": ("Axis position.", ""),
}


def field_health(samples: list[dict]) -> list[dict]:
    """Classify every expected field so the report can show, honestly, what this
    machine does and does not give us."""
    out = []
    total = len(samples)
    for f in NUMERIC_FIELDS + ["block_number"]:
        vals = [s[f] for s in samples if s.get(f) is not None]
        why, fix = FIELD_NOTES.get(f, ("", ""))
        if not vals:
            status, detail = "absent", "Not sent by this controller at all."
        else:
            distinct = set(vals)
            nonzero = [v for v in vals if v]
            if len(distinct) == 1:
                status = "constant"
                detail = f"Present, but never changes — every one of {len(vals):,} samples reads {vals[0]}."
            elif len(nonzero) / len(vals) < 0.02:
                status = "sparse"
                detail = (f"Present but effectively empty — only {len(nonzero):,} of {len(vals):,} "
                          f"samples ({100*len(nonzero)/len(vals):.1f}%) are non-zero.")
            else:
                status = "ok"
                detail = (f"{len(vals):,} samples, {len(distinct):,} distinct values "
                          f"({min(vals)} … {max(vals)}).")
        if f == "production_count" and status == "ok":
            status, detail = "misleading", (
                "Increments on program completion (M30), not per part — "
                f"{len(set(vals))} steps across this window. Not a part counter.")
        out.append({"field": f, "status": status, "detail": detail,
                    "coverage_pct": round(100 * len(vals) / (total or 1), 1),
                    "why_it_matters": why, "how_to_fix": fix})
    order = {"ok": 0, "misleading": 1, "sparse": 2, "constant": 3, "absent": 4}
    out.sort(key=lambda d: (order.get(d["status"], 9), d["field"]))
    return out


# --------------------------------------------------------------- insights ---


def build_insights(machine: dict, ops: list[dict]) -> list[dict]:
    ins = []
    if not ops:
        return ins

    tot = machine["cutting_s"] + machine["air_s"]
    if tot:
        ins.append({
            "kind": "split",
            "text": f"In-program time splits {machine['cutting_s']/60:.0f} min cutting / "
                    f"{machine['air_s']/60:.0f} min air-positioning — "
                    f"{100*machine['air_s']/tot:.0f}% of running time the spindle is in a program "
                    f"but not removing metal. Plus {machine['stopped_s']/60:.0f} min stopped "
                    f"(feed-hold / M00) and {machine['untracked_s']/60:.0f} min the collector "
                    f"was not sampling."})

    if machine.get("setup_idle_s"):
        ins.append({
            "kind": "setup",
            "text": f"A further {machine['setup_idle_s']/60:.0f} min went on inter-operation setup — "
                    f"program loaded on the controller but cycle not started. That is on top of the "
                    f"in-program time above."})

    worst = min(ops, key=lambda o: o["timing"]["cutting_pct"])
    best = max(ops, key=lambda o: o["timing"]["cutting_pct"])
    ins.append({
        "kind": "spread",
        "text": f"Cutting share ranges from {worst['timing']['cutting_pct']:.0f}% "
                f"({worst['program_name']}) to {best['timing']['cutting_pct']:.0f}% "
                f"({best['program_name']}). The low end is where cycle time is being lost."})

    stopped_heavy = max(ops, key=lambda o: o["timing"]["stopped_s"])
    if stopped_heavy["timing"]["stopped_s"] > 120:
        ins.append({
            "kind": "stopped",
            "text": f"{stopped_heavy['program_name']} alone spent "
                    f"{stopped_heavy['timing']['stopped_s']/60:.0f} min in feed-hold / stop "
                    f"mid-operation. Worth asking the operator why."})

    repeats = [o for o in ops if o["cycles"] > 1]
    if repeats:
        r = max(repeats, key=lambda o: o["cycles"])
        ins.append({"kind": "repeat",
                    "text": f"{len(repeats)} program(s) ran more than one cycle — "
                            f"{r['program_name']} ran {r['cycles']}×. Repeat cycles of the same "
                            f"program are the cleanest run-to-run consistency check available."})

    rpms = [(o["program_name"], o["spindle"]["rpm_median"]) for o in ops if o["spindle"]["rpm_median"]]
    if rpms:
        lo, hi = min(rpms, key=lambda x: x[1]), max(rpms, key=lambda x: x[1])
        ins.append({"kind": "rpm",
                    "text": f"Spindle RPM spans {lo[1]:.0f} ({lo[0]}) to {hi[1]:.0f} ({hi[0]}) — "
                            f"compare against the CAM-programmed S once the .tap set is linked."})

    alarmy = [o for o in ops if o["alarms"]["active_pct"] > 1]
    if alarmy:
        ins.append({"kind": "alarm",
                    "text": f"{len(alarmy)} of {len(ops)} operations carry an alarm flag for >1% of "
                            f"samples (highest: "
                            f"{max(alarmy, key=lambda o: o['alarms']['active_pct'])['program_name']}). "
                            f"Alarm semantics are unconfirmed — treat as provisional."})
    return ins


def fetch_period_breakdown(session, factory_id, machine_id, days: int) -> dict:
    """Machine-level time breakdown over a rolling `-Nd` window. Light query —
    only machine_state / cutting_time / cycle_time — feeds the Summary tab's
    date-range dropdown."""
    flux = f'''
from(bucket: "{BUCKET}") |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}"
        and r.factory_id == "{factory_id}" and r.machine_id == "{machine_id}")
  |> filter(fn: (r) => r._field == "machine_state" or r._field == "cutting_time" or r._field == "cycle_time")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    try:
        rows = query(session, flux)
    except RuntimeError as e:
        print(f"  ! period {days}d query failed: {e}")
        return None
    samples = []
    for r in rows:
        t = r.get("_time")
        if not t:
            continue
        samples.append({"t": _parse_ts(t), "_time": t,
                        "machine_state": _num(r.get("machine_state")),
                        "cutting_time": _num(r.get("cutting_time")),
                        "cycle_time": _num(r.get("cycle_time"))})
    if len(samples) < 2:
        return {"label": f"Last {days} day{'s' if days > 1 else ''}", "days": days,
                "samples": len(samples), "window_span_s": 0, "coverage_pct": None,
                "utilisation_pct": None, "cutting_s": 0, "air_s": 0, "stopped_s": 0,
                "manual_s": 0, "untracked_s": 0, "state_distribution": {}}
    b = bucket_time(samples)
    span = samples[-1]["t"] - samples[0]["t"]
    states = Counter(s["machine_state"] for s in samples if s["machine_state"] is not None)
    n = sum(states.values()) or 1
    return {
        "label": f"Last {days} day{'s' if days > 1 else ''}", "days": days,
        "window_start": samples[0]["_time"], "window_end": samples[-1]["_time"],
        "samples": len(samples),
        "window_span_s": round(span, 1),
        "coverage_pct": round(100 * (span - b["untracked_s"]) / span, 1) if span else None,
        "sample_rate_hz": round(len(samples) / span, 3) if span else None,
        "utilisation_pct": round(100 * states.get(RUNNING_STATE, 0) / n, 1),
        "cutting_s": b["cutting_s"], "air_s": b["air_s"],
        "stopped_s": b["stopped_s"], "manual_s": b["manual_s"], "untracked_s": b["untracked_s"],
        "state_distribution": {str(k): round(100 * v / n, 1) for k, v in sorted(states.items())},
    }


# ------------------------------------------------------------------- main ---


def build(args) -> dict:
    print(f"signing in to InfluxDB …")
    session = get_session()

    if args.days:
        start, stop = f"-{args.days}d", None
    else:
        start, stop = args.start, args.end

    print(f"fetching {MEASUREMENT} for {args.factory_id}/{args.machine_id}  range={start} → {stop or 'now'}")
    samples = fetch_samples(session, args.factory_id, args.machine_id, start, stop)
    print(f"  {len(samples):,} samples")
    if not samples:
        raise SystemExit("no samples in that window — widen the range or check the machine id")

    runs = segment_runs(samples)
    override_field = args.override_field
    run_recs = [run_metrics(r, override_field) for r in runs]
    ops = build_operations(run_recs)
    print(f"  {len(runs)} raw runs → {len(ops)} operations "
          f"({sum(o['cycles'] for o in ops)} machining cycles)")

    actual_window = None
    if args.part_actual:
        try:
            a, b = [x.strip() for x in args.part_actual.split(",")]
            actual_window = (a, b)
        except ValueError:
            raise SystemExit('--part-actual must be "START,END" in RFC3339, e.g. '
                             '"2026-09-04T03:30:00Z,2026-09-04T13:30:00Z"')

    parts = build_parts(samples, ops, args.part_gap_hours, actual_window)
    print(f"  {len(parts)} part(s) inferred (operations clustered on a "
          f"{args.part_gap_hours} h gap)")

    machine = machine_metrics(samples, ops)
    machine["utilisation_formula"] = UTILISATION_FORMULA.format(run=RUNNING_STATE)
    health = field_health(samples)
    insights = build_insights(machine, ops)

    # rolling-window machine breakdowns for the Summary tab's date-range dropdown
    periods = []
    for d in args.windows:
        print(f"  period breakdown: last {d}d …")
        pd = fetch_period_breakdown(session, args.factory_id, args.machine_id, d)
        if pd:
            periods.append(pd)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"bucket": BUCKET, "measurement": MEASUREMENT,
                   "factory_id": args.factory_id, "machine_id": args.machine_id,
                   "range_start": start, "range_stop": stop},
        "config": {"override_field": override_field,
                   "override_is_proxy": override_field != "feed_override",
                   "running_state": RUNNING_STATE,
                   "run_gap_split_s": RUN_GAP_SPLIT_S,
                   "part_gap_hours": args.part_gap_hours},
        "machine": machine,
        "periods": periods,
        "parts": parts,
        "operations": ops,
        "runs": run_recs,
        "field_health": health,
        "insights": insights,
    }


def write_report(data: dict, template: Path, out: Path):
    if not template.exists():
        print(f"  ! template not found at {template} — skipping HTML report")
        return
    html = template.read_text(encoding="utf-8")
    html = html.replace("/*__DATA__*/null", json.dumps(data, indent=None, default=str))
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default=PILOT_START, help="RFC3339 start (default: 4-Sep pilot window)")
    p.add_argument("--end", default=PILOT_STOP, help="RFC3339 stop")
    p.add_argument("--days", type=int, help="instead of start/end: last N days")
    p.add_argument("--factory-id", default="krishna")
    p.add_argument("--machine-id", default="STM")
    p.add_argument("--override-field", default="feed_override",
                   choices=["rapid_override", "feed_override", "spindle_override"],
                   help="which field stands in for the feed-override metrics "
                        "(Krishna does not send feed_override yet)")
    p.add_argument("--part-gap-hours", type=float, default=2.0,
                   help="operations separated by more than this many hours are treated as "
                        "different parts (default 2)")
    p.add_argument("--part-actual", default=None,
                   help='real shop-floor window for the part, "START,END" RFC3339 — the report '
                        'then contrasts it with what was actually logged. e.g. '
                        '"2026-09-04T03:30:00Z,2026-09-04T13:30:00Z" for 09:00-19:00 IST')
    p.add_argument("--windows", default="1,7,30",
                   help="comma-separated day counts for the Summary date-range dropdown "
                        "(default 1,7,30). Pass '' to skip the rolling breakdowns.")
    p.add_argument("--out-dir", default=".")
    args = p.parse_args()
    args.windows = [int(x) for x in args.windows.split(",") if x.strip()]

    data = build(args)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "krishna_metrics.json"
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"wrote {json_path}")

    write_report(data, Path(__file__).with_name("krishna_report_template.html"),
                 out_dir / "krishna_report.html")

    m = data["machine"]
    print(f"\nutilisation {m['utilisation_logged_pct']}%  ·  {m['operations']} operations / "
          f"{m['cycles']} cycles  ·  cutting {m['cutting_s']/60:.0f}m / air {m['air_s']/60:.0f}m / "
          f"stopped {m['stopped_s']/60:.0f}m / setup-idle {m['setup_idle_s']/60:.0f}m / "
          f"untracked {m['untracked_s']/60:.0f}m")
    bad = [f for f in data["field_health"] if f["status"] != "ok"]
    print(f"{len(bad)} fields not usable: " + ", ".join(f"{f['field']}({f['status']})" for f in bad))


if __name__ == "__main__":
    main()
