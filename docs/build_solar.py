#!/usr/bin/env python3
"""Regenerate docs/solar.json — the bundled space-weather snapshot for the web
build — from the desktop app's data/kp_ap_f107.txt (GFZ Potsdam combined file).

Keeps only the fields logcore.js needs (per UT day: eight Kp blocks, Ap, sunspot
number, observed + adjusted F10.7, and the definitive flag), so the browser
never has to parse the full ~1.5 MB text file. Mirrors solar.py's _parse().

    python3 docs/build_solar.py        # run from the repo root
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "kp_ap_f107.txt"
OUT = ROOT / "docs" / "solar.json"


def main():
    rows = {}
    for line in SRC.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        t = line.split()
        if len(t) < 27:
            continue
        try:
            y, mo, d = int(t[0]), int(t[1]), int(t[2])
        except ValueError:
            continue
        kp = [float(x) for x in t[7:15]]
        ApV, SN = int(t[23]), int(t[24])
        f_obs, f_adj = float(t[25]), float(t[26])
        rows[f"{y:04d}-{mo:02d}-{d:02d}"] = {
            "kp": [None if v < 0 else v for v in kp],
            "a": None if ApV < 0 else ApV,
            "ssn": None if SN < 0 else SN,
            "sfi": None if f_obs < 0 else f_obs,
            "sfi_adj": None if f_adj < 0 else f_adj,
            "definitive": (t[27] == "1") if len(t) > 27 else False,
        }
    OUT.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    keys = sorted(rows)
    print(f"wrote {OUT}  ({len(rows)} days, {keys[0]} → {keys[-1]})")


if __name__ == "__main__":
    main()
