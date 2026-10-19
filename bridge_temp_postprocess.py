#!/usr/bin/env python3
"""Experimental OrcaSlicer G-code post-processor for bridge temperature changes.

Usage from OrcaSlicer Post-processing scripts (Windows example):
  py "C:\\path\\bridge_temp_postprocess.py" --bridge-temp 250 --park-x 200 --park-y 200

Intentionally conservative: only processes standard Orca ;TYPE:Bridge regions and
requires absolute XYZ coordinates, one tool, and a known normal nozzle temperature.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

WORD = re.compile(r"([A-Za-z])([-+]?(?:\d+(?:\.\d*)?|\.\d+))")


def words(line: str) -> dict[str, float]:
    return {m.group(1).upper(): float(m.group(2)) for m in WORD.finditer(line.split(";", 1)[0])}


def polygon_centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if len(points) < 4 or math.dist(points[0], points[-1]) > 0.15:
        return None
    pts = points[:-1] if math.dist(points[0], points[-1]) < 1e-9 else points
    area2 = cx = cy = 0.0
    for a, b in zip(pts, pts[1:] + pts[:1]):
        cross = a[0] * b[1] - b[0] * a[1]
        area2 += cross
        cx += (a[0] + b[0]) * cross
        cy += (a[1] + b[1]) * cross
    if abs(area2) < 1e-6:
        return None
    return cx / (3 * area2), cy / (3 * area2)


def object_marker(line: str) -> tuple[str, str] | None:
    start = re.search(r"\bEXCLUDE_OBJECT_START\s+NAME=([^\s;]+)", line, re.I)
    if start:
        return "start", start.group(1)
    if re.search(r"\bEXCLUDE_OBJECT_END\b", line, re.I):
        return "end", ""
    return None


def wall_centers(lines: list[str]) -> dict[tuple[str | None, float], tuple[float, float]]:
    """Find each object's area centroid from its largest closed outer-wall loop per layer."""
    z = None
    obj: str | None = None
    feature = ""
    x = y = None
    path: list[tuple[float, float]] = []
    loops: dict[tuple[str | None, float], list[list[tuple[float, float]]]] = {}
    in_wipe = False

    def finish() -> None:
        nonlocal path
        if z is not None and polygon_centroid(path) is not None:
            loops.setdefault((obj, z), []).append(path[:])
        path = []

    for line in lines:
        s = line.strip()
        if s.startswith(";WIPE_START"):
            in_wipe = True
            continue
        if s.startswith(";WIPE_END"):
            in_wipe = False
            continue
        if in_wipe:
            continue
        marker = object_marker(line)
        if marker:
            finish()
            obj = marker[1] if marker[0] == "start" else None
        zm = re.match(r";\s*Z\s*:\s*([-+\d.]+)", s, re.I)
        if zm:
            finish()
            z = float(zm.group(1))
        tm = re.search(r";\s*TYPE\s*:\s*([^;]+)", line, re.I)
        if tm:
            finish()
            feature = tm.group(1).strip().lower()
        cm = re.match(r"\s*(G\d+)\b", line, re.I)
        if not cm or cm.group(1).upper() not in {"G0", "G1"}:
            continue
        w = words(line)
        nx, ny = w.get("X", x), w.get("Y", y)
        if feature in {"outer wall", "external perimeter"} and "E" in w and nx is not None and ny is not None and (nx != x or ny != y):
            if x is not None and y is not None:
                if not path:
                    path.append((x, y))
                path.append((nx, ny))
        elif path and ("X" in w or "Y" in w):
            finish()
        x, y = nx, ny
    finish()
    centers: dict[tuple[str | None, float], tuple[float, float]] = {}
    for key, candidates in loops.items():
        poly = max(candidates, key=lambda pts: abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:]))))
        center = polygon_centroid(poly)
        if center is not None:
            centers[key] = center
    return centers


def bridge_object_layers(lines: list[str]) -> set[tuple[str | None, float]]:
    """Return (object, Z) pairs whose feature types include a bridge."""
    layers: set[tuple[str | None, float]] = set()
    z = None
    obj: str | None = None
    for line in lines:
        s = line.strip()
        marker = object_marker(line)
        if marker:
            obj = marker[1] if marker[0] == "start" else None
        zm = re.match(r";\s*Z\s*:\s*([-+\d.]+)", s, re.I)
        if zm:
            z = float(zm.group(1))
        tm = re.search(r";\s*TYPE\s*:\s*([^;]+)", line, re.I)
        if tm and "bridge" in tm.group(1).lower() and z is not None:
            layers.add((obj, z))
    return layers


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("gcode", type=Path, help="G-code file path (OrcaSlicer supplies this)")
    p.add_argument("--bridge-temp", type=float, required=True, help="Nozzle temperature during bridges")
    p.add_argument("--normal-temp", type=float, help="Normal nozzle temperature; inferred from M104/M109 if omitted")
    p.add_argument("--park-x", type=float, help="Override automatic outer-wall centroid X")
    p.add_argument("--park-y", type=float, help="Override automatic outer-wall centroid Y")
    p.add_argument("--zhop", type=float, default=0.6, help="Z hop in mm (default: 0.6)")
    p.add_argument("--retract", type=float, default=0.8, help="Retraction in mm (default: 0.8)")
    p.add_argument("--travel-feed", type=int, default=6000, help="Travel feed in mm/min")
    p.add_argument("--temp-tolerance", type=float, default=5.0, help="Allowed temperature band around each target in C (default: +/-5)")
    p.add_argument("--temperature-sensor", default="extruder", help="Klipper heater sensor name for TEMPERATURE_WAIT (default: extruder)")
    args = p.parse_args()

    raw = args.gcode.read_text(encoding="utf-8", errors="strict")
    lines = raw.splitlines()
    centers = wall_centers(lines)
    bridge_pairs = bridge_object_layers(lines)
    has_bridge_feature = any(
        (m := re.search(r";\s*TYPE\s*:\s*([^;]+)", line, re.I)) and "bridge" in m.group(1).lower()
        for line in lines
    )
    if not has_bridge_feature:
        print("No bridge features found; leaving G-code unchanged.")
        return 0
    if not bridge_pairs:
        raise SystemExit("Bridge feature comments were found, but no layer Z markers were available for them.")
    if (args.park_x is None) != (args.park_y is None):
        raise SystemExit("Pass both --park-x and --park-y, or neither for automatic placement.")
    normal = args.normal_temp
    if normal is None:
        # Take the last explicit target in the file, which is normally the active print temperature.
        targets = []
        for line in lines:
            if re.match(r"\s*M10[49]\b", line, re.I):
                w = words(line)
                if "S" in w and w["S"] > 0:
                    targets.append(w["S"])
        if targets:
            normal = max(targets)
    if normal is None:
        # Some printer profiles set the target in START_PRINT instead of M104/M109.
        for line in lines:
            match = re.search(r"\bEXTRUDER_TEMP\s*=\s*([-+\d.]+)", line, re.I)
            if match:
                normal = float(match.group(1))
                break
    if normal is None:
        raise SystemExit("Could not infer normal temperature; pass --normal-temp explicitly.")
    if args.bridge_temp >= normal:
        raise SystemExit("Bridge temperature must be below normal temperature.")
    if args.zhop <= 0 or args.retract < 0 or args.temp_tolerance < 0:
        raise SystemExit("--zhop must be positive; --retract and --temp-tolerance cannot be negative.")

    def set_and_wait(temp: float) -> list[str]:
        lo = max(0.0, temp - args.temp_tolerance)
        hi = temp + args.temp_tolerance
        return [
            f"M104 S{temp:.1f} ; set target without exact-target blocking wait",
            f"TEMPERATURE_WAIT SENSOR={args.temperature_sensor} MINIMUM={lo:.1f} MAXIMUM={hi:.1f}",
        ]

    # Determine the modes and position at each line. We reject non-absolute XYZ and
    # multiple tools because safely restoring those states needs printer-specific logic.
    x = y = z = None
    xyz_absolute = True
    e_absolute = True
    current_tool = 0
    reduced_region = False
    low_temp = False
    bridge_e_was_absolute = True
    layer_z = None
    active_object: str | None = None
    park_x, park_y = args.park_x, args.park_y
    out: list[str] = []
    starts = 0
    for line in lines:
        s = line.strip()
        zm = re.match(r";\s*Z\s*:\s*([-+\d.]+)", s, re.I)
        if zm:
            layer_z = float(zm.group(1))
        marker = object_marker(line)
        if marker:
            active_object = marker[1] if marker[0] == "start" else None
        if re.match(r"T\d+\b", s, re.I):
            t = int(re.match(r"T(\d+)", s, re.I).group(1))  # type: ignore[union-attr]
            if t != current_tool:
                raise SystemExit("Multiple tools detected; refusing to rewrite this file.")
        if re.match(r"G91\b", s, re.I):
            xyz_absolute = False
        elif re.match(r"G90\b", s, re.I):
            xyz_absolute = True
        elif re.match(r"M83\b", s, re.I):
            e_absolute = False
        elif re.match(r"M82\b", s, re.I):
            e_absolute = True

        m = re.search(r";\s*TYPE\s*:\s*([^;]+)", line, re.I)
        if m:
            tag = m.group(1).strip().lower()
            is_bridge = "bridge" in tag
            is_same_layer_solid_infill = "internal solid infill" in tag and (active_object, layer_z) in bridge_pairs
            now_reduced = is_bridge or is_same_layer_solid_infill
            if args.park_x is None and now_reduced:
                key = (active_object, layer_z)
                center = centers.get(key)
                if center is None and active_object is None and layer_z is not None:
                    # Object markers may be absent in some exports; allow an unambiguous single-object layer.
                    matches = [point for (obj, height), point in centers.items() if height == layer_z]
                    if len(matches) == 1:
                        center = matches[0]
                if center is None:
                    raise SystemExit(f"No outer-wall centroid for object {active_object!r} at bridge layer Z={layer_z}; use fixed --park-x and --park-y.")
                park_x, park_y = center
            if now_reduced != reduced_region:
                reduced_region = now_reduced
                if reduced_region:
                    starts += 1
                    if not xyz_absolute:
                        raise SystemExit("Relative XYZ mode encountered; refusing unsafe park insertion.")
                    if x is None or y is None or z is None:
                        raise SystemExit("Position is unknown at bridge start; ensure slicer emits XYZ moves.")
                    if not low_temp:
                        bridge_e_was_absolute = e_absolute
                        out.extend([
                            "; bridge-temp-postprocessor: park and cool before bridge",
                            "M83 ; temporary relative E mode",
                            f"G1 E-{args.retract:.3f} F1800",
                            "G90",
                            f"G1 Z{z + args.zhop:.3f} F{args.travel_feed}",
                            f"G1 X{park_x:.3f} Y{park_y:.3f} F{args.travel_feed}",
                            *set_and_wait(args.bridge_temp),
                            f"G1 X{x:.3f} Y{y:.3f} F{args.travel_feed}",
                            f"G1 Z{z:.3f} F{args.travel_feed}",
                            f"G1 E{args.retract:.3f} F1800",
                            ("M82" if bridge_e_was_absolute else "M83") + " ; restore prior E mode",
                        ])
                        low_temp = True
                elif low_temp:
                    # At bridge end, move away before waiting for the hotter temperature.
                    if x is None or y is None or z is None:
                        raise SystemExit("Position is unknown at bridge end.")
                    out.extend([
                        "; bridge-temp-postprocessor: park and reheat after bridge",
                        "M83 ; temporary relative E mode",
                        f"G1 E-{args.retract:.3f} F1800",
                        "G90",
                        f"G1 Z{z + args.zhop:.3f} F{args.travel_feed}",
                        f"G1 X{park_x:.3f} Y{park_y:.3f} F{args.travel_feed}",
                        *set_and_wait(normal),
                        f"G1 X{x:.3f} Y{y:.3f} F{args.travel_feed}",
                        f"G1 Z{z:.3f} F{args.travel_feed}",
                        f"G1 E{args.retract:.3f} F1800",
                        ("M82" if bridge_e_was_absolute else "M83") + " ; restore prior E mode",
                    ])
                    low_temp = False
        out.append(line)

        # Track absolute positions after the original line. G0/G1 XY moves are enough
        # for normal Orca output; G2/G3 arcs are rejected if they occur in a bridge.
        cmd = re.match(r"\s*(G\d+)\b", line, re.I)
        if cmd:
            code = cmd.group(1).upper()
            w = words(line)
            if code in {"G2", "G3"} and reduced_region:
                raise SystemExit("Arc move found in a bridge; refusing to insert temperature sequence.")
            if code in {"G0", "G1"}:
                if not xyz_absolute and any(k in w for k in ("X", "Y", "Z")):
                    raise SystemExit("Relative XYZ move encountered; refusing unsafe park insertion.")
                if "X" in w: x = w["X"]
                if "Y" in w: y = w["Y"]
                if "Z" in w: z = w["Z"]

    if low_temp:
        raise SystemExit("File ends inside a bridge region; no safe restore point found.")
    if starts == 0:
        raise SystemExit("No OrcaSlicer bridge type comments found; output left unchanged.")
    args.gcode.write_text("\n".join(out) + ("\n" if raw.endswith("\n") else ""), encoding="utf-8", newline="")
    print(f"Inserted temperature sequences for {starts} bridge region(s); normal={normal:g}C, bridge={args.bridge_temp:g}C")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError) as exc:
        print(f"bridge-temp-postprocessor: {exc}", file=sys.stderr)
        raise SystemExit(2)

