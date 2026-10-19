# OrcaSlicer bridge temperature post-processor

`bridge_temp_postprocess.py` is an experimental G-code post-processor for OrcaSlicer. On any layer containing a bridge feature, it also treats `;TYPE:Internal solid infill` sections for that same object as part of the reduced-temperature region. Thus adjacent bridge and internal solid infill stays at bridge temperature, avoiding a temperature cycle between those feature types. It uses Orca's `EXCLUDE_OBJECT_START` markers to associate each bridge with that object's outer-wall contour, computes the area centroid of the largest closed `;TYPE:Outer wall` loop for that object and layer, then uses that XY point to park while waiting for temperature changes. It retracts, Z-hops, parks, waits for the lower bridge temperature, returns to the toolpath, and unretracts. At the end of the combined region it parks and waits for the normal temperature before continuing.

If no `;TYPE:*Bridge*` feature comment exists in the file, the script succeeds without changing the G-code.

## Configure in OrcaSlicer

In **Print Settings → Output options → Post-processing scripts**, add a command like this, replacing the script path and park coordinates:

```text
py "C:\Users\YOURNAME\Documents\Codex\2026-10-12\pri\outputs\bridge_temp_postprocess.py" --bridge-temp 250
```

OrcaSlicer passes the generated G-code path to the script. If you prefer a fixed park point, pass both `--park-x X --park-y Y`; otherwise the script derives it from the active object's outer-wall contour. The G-code must contain Orca `EXCLUDE_OBJECT_START` markers for multi-object centroid selection. `--normal-temp` can be supplied explicitly if the script cannot infer your normal temperature from `M104`/`M109` or the `START_PRINT` arguments. A geometric centroid can land in a hole or sparse infill, so inspect the point in the slicer and confirm travel clearance before printing. If the matching object's contour cannot be identified, the script stops instead of choosing another object's point.

Optional settings: `--zhop 0.6`, `--retract 0.8`, `--travel-feed 6000` (feed is mm/min), and `--temp-tolerance 5` (default ±5°C). The script sets the target with `M104`, then uses Klipper's `TEMPERATURE_WAIT` to continue once the nozzle is inside the tolerance band. The default sensor name is `extruder`; set `--temperature-sensor NAME` if your Klipper configuration uses another heater name. This tolerance wait requires Klipper. Tune retraction and Z hop for the machine/filament. Back up the G-code before first use and inspect the modified file in OrcaSlicer's G-code viewer.

## Important limits

- This script rewrites the G-code file in place, as OrcaSlicer expects for classic post-processing scripts.
- It expects one tool, absolute XYZ moves, and standard Orca bridge type comments. It stops with an error if it detects relative XYZ movement or multiple tools.
- The temperature change uses blocking Klipper `TEMPERATURE_WAIT` commands with a configurable tolerance band. This can add significant time and may create a blob/string at the park point. The automatic point is a geometric outer-wall centroid; it is not guaranteed to be solid infill or clear of holes, clips, or other objects.
- Run a small, supervised test print first. Check your printer's G-code viewer for the inserted waits and travel, and verify that the hotend returns to the normal temperature before non-bridge extrusion.
- If the print starts with relative extrusion (`M83`), the script preserves that mode. The retract distance remains printer-specific.

