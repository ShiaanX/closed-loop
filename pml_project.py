"""
pml_project.py — read what is reliably in a PowerMILL project for the closed loop.

PowerMILL 2019 "database" projects are a flat folder: an entity index
(`<name>.pmlprj`) plus hashed `*.pmlent` files. The `.pmlent` files are binary,
BUT each one carries an embedded JSON header with the settings we care about.
This module reads only that JSON — it does NOT decode the binary transforms, so
a workplane's exact origin/orientation is reported as "present, not decoded".

What it returns per toolpath (all straight from the JSON):
  workplane (resolved name), strategy, stepdown, tool axis,
  stock block limits (mm, in workplane coords),
  designed feeds — cutting / plunging / rapid (mm/min),
  designed cutting speed Vc (m/min) and feed-per-tooth fz (mm),
  CAM edit date.

Usage:
    from pml_project import load_project
    proj = load_project(Path(".../CAM Project_Krishna/SE-004-052/SE-004-052.pmlprj"))
    proj["toolpaths"]["1_10end_outer"]  ->  {...}
"""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path

_ENT_LINE = re.compile(r"(x[0-9a-f]+)\.pmlent\s+(pmlEnt\w+)\s+'([^']*)'")
_AXIS = ("X", "Y", "Z")


def _s(text: str, key: str):
    m = re.search(r'"' + key + r'"\s*:\s*"([^"]*)"', text)
    return m.group(1) if m else None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _block_limits(text: str):
    """The toolpath's stock Block, in workplane coordinates."""
    m = re.search(r'"Block"\s*:\s*\{.*?"Limits"\s*:\s*\{(.*?)\}', text, re.S)
    if not m:
        return None
    pairs = dict(re.findall(r'"([XYZ](?:Min|Max))"\s*:\s*"([-\d.eE+]+)"', m.group(1)))
    out = {}
    for ax in _AXIS:
        lo, hi = pairs.get(ax + "Min"), pairs.get(ax + "Max")
        if lo is None or hi is None:
            return None
        out[ax.lower()] = [round(float(lo), 3), round(float(hi), 3)]
    out["size_mm"] = [round(out[a][1] - out[a][0], 2) for a in ("x", "y", "z")]
    return out


def _feedrates(text: str):
    """FeedRate block — nested braces, so just slice from "FeedRate" to "Block"."""
    seg = re.search(r'"FeedRate"\s*:\s*\{(.*?)"Block"', text, re.S)
    seg = seg.group(1) if seg else ""

    def val(key):  # "<key>":{"Value":"123.4",...}
        m = re.search(r'"' + key + r'"\s*:\s*\{\s*"Value"\s*:\s*"([-\d.eE+]+)"', seg)
        return _f(m.group(1)) if m else None

    rapid = re.search(r'"Rapid"\s*:\s*"([-\d.eE+]+)"', seg)
    return {
        "feed_cut": val("Cutting"),
        "feed_plunge": val("Plunging"),
        "feed_rapid": _f(rapid.group(1)) if rapid else None,
        "vc_m_min": val("CuttingSpeed"),
        "fz_mm": val("FeedPerTooth"),
    }


def _decode_workplane(path: Path) -> dict | None:
    """Recover a workplane's origin + 3x3 rotation from the binary entity.

    Version-independent: scans 8-byte-aligned float64 windows for the first one
    whose last 9 doubles form an orthonormal matrix with det ~ +/-1 (a real
    rotation), taking the preceding 3 doubles as the origin (mm, in model space).
    Validated on axis-aligned setups; a general angled workplane assumes the
    layout is origin(3) then row-major matrix(9), contiguous.
    """
    b = path.read_bytes()
    for off in range(0, len(b) - 96, 8):
        d = struct.unpack_from("<12d", b, off)
        if any(not math.isfinite(x) for x in d):
            continue
        o, m = d[:3], d[3:]
        if any(abs(x) > 1e5 for x in o):
            continue
        R = (m[0:3], m[3:6], m[6:9])
        if not all(abs(math.sqrt(sum(v * v for v in r)) - 1) < 1e-6 for r in R):
            continue
        dot = lambda a, c: sum(x * y for x, y in zip(a, c))
        if max(abs(dot(R[0], R[1])), abs(dot(R[0], R[2])), abs(dot(R[1], R[2]))) > 1e-6:
            continue
        det = (R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
               - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
               + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0]))
        if abs(abs(det) - 1) > 1e-6:
            continue
        tr = R[0][0] + R[1][1] + R[2][2]
        angle = round(math.degrees(math.acos(max(-1.0, min(1.0, (tr - 1) / 2)))), 1)
        return {
            "byte_offset": off,
            "origin_model_mm": [round(x, 4) for x in o],
            "matrix": [[round(x, 6) for x in r] for r in R],
            "rotation_deg": angle,
            "z_axis_in_model": [round(x, 4) for x in R[2]],
        }
    return None


def _orientation_text(wp: dict | None) -> str | None:
    if not wp:
        return None
    z = wp["z_axis_in_model"]
    if z[2] > 0.99:
        face = "the model +Z (top) face"
    elif z[2] < -0.99:
        face = "the model -Z (bottom) face"
    else:
        face = f"a tilted face (workplane Z points {tuple(z)} in the model)"
    deg = wp["rotation_deg"]
    m = wp["matrix"]
    if deg < 1:
        rot = "part in its as-modelled orientation"
    elif abs(deg - 180) < 1:
        ax = ("X" if m[0][0] > 0.99 else "Y" if m[1][1] > 0.99
              else "Z" if m[2][2] > 0.99 else "?")
        rot = f"part flipped 180 deg about {ax}"
    else:
        rot = f"part rotated {deg} deg"
    o = wp["origin_model_mm"]
    return f"{rot} - tool works {face}; datum at model ({o[0]}, {o[1]}, {o[2]}) mm"


def load_project(pmlprj: Path) -> dict | None:
    pmlprj = Path(pmlprj)
    if not pmlprj.exists():
        return None
    folder = pmlprj.parent

    idx = {}
    for line in pmlprj.read_text(errors="ignore").splitlines():
        m = _ENT_LINE.match(line.strip())
        if m:
            idx[m.group(1)] = (m.group(2), m.group(3))   # key incl. leading 'x'
    # workplane id (as written inside toolpath JSON — no 'x' prefix) → name
    wp_name = {k[1:]: n for k, (t, n) in idx.items() if t == "pmlEntWorkplane"}

    # decode each workplane's origin + rotation from its binary entity
    wp_detail = {}
    for k, (t, n) in idx.items():
        if t != "pmlEntWorkplane":
            continue
        f = folder / (k + ".pmlent")
        if not f.exists():
            continue
        tr = _decode_workplane(f)
        wp_detail[n] = {**(tr or {}), "decoded": tr is not None,
                        "orientation": _orientation_text(tr)}

    toolpaths = {}
    for k, (typ, name) in idx.items():
        if typ != "pmlEntToolpath":
            continue
        f = folder / (k + ".pmlent")
        if not f.exists():
            continue
        text = f.read_bytes().decode("latin1")

        wid = re.search(r'"Workplane"\s*:\s*"&id:([0-9a-f]+)"', text)
        tax = re.search(r'"ToolAxis"\s*:\s*\{\s*"Type"\s*:\s*"([^"]*)"', text)
        eh = re.search(r'"EditHistory"\s*:\s*"([^"\r\n\\]*)', text)
        sd = _s(text, "Stepdown")

        toolpaths[name.lower()] = {
            "name": name,
            "workplane": wp_name.get(wid.group(1)) if wid else None,
            "strategy": _s(text, "Strategy"),
            "stepdown_mm": _f(sd),
            "tool_axis": tax.group(1) if tax else None,
            "stock_block_mm": _block_limits(text),
            **_feedrates(text),
            "cam_edited": eh.group(1).strip() if eh else None,
        }

    # group toolpaths by workplane → "setups"
    setups = {}
    for tp in toolpaths.values():
        setups.setdefault(tp["workplane"] or "(world)", []).append(tp["name"])

    n_decoded = sum(1 for v in wp_detail.values() if v.get("decoded"))
    return {
        "folder": str(folder),
        "project_file": pmlprj.name,
        "workplanes": sorted(wp_name.values()),
        "workplane_detail": wp_detail,
        "workplane_transforms": (
            f"Worked out where each setup sits on the part directly from the CAM file "
            f"({n_decoded}/{len(wp_detail)} setups figured out). Reliable for straightforward "
            f"flips between faces; a tilted/angled setup hasn't been double-checked yet."),
        "setups_by_workplane": setups,
        "toolpaths": toolpaths,
    }


if __name__ == "__main__":
    import json
    import sys

    p = Path(sys.argv[1]) if len(sys.argv) > 1 else next(
        Path(".").glob("**/*.pmlprj"))
    proj = load_project(p)
    print(json.dumps(proj, indent=2, default=str))
