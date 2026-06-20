#!/usr/bin/env python3
"""Parse the ITU 'Table of International Call Sign Series' into itu.json.

Source: doc/ITZ Callsign.pdf — rows like `AAA-ALZ  United States of America`
mapping a call-sign-series range to a country. logan uses this as the SECOND
prefix source (after the ARRL DXCC list, before the logger's own field).

Each range is expanded to the set of 2-character prefixes it covers (the third
character is just the A..Z series filler). Continent and representative
coordinates for each country are borrowed from AD1C's cty.dat by matching the
country name, so an ITU-only prefix (e.g. 3G = Chile, which the DXCC list files
under CE) still resolves to a continent and a map point.

Run once (regenerates logan/itu.json):
    python3 build_itu.py
"""

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "doc" / "itu_raw.txt"
PDF = HERE / "doc" / "ITZ Callsign.pdf"
CTY = HERE / "doc" / "cty.dat"
OUT = HERE / "itu.json"

ROW = re.compile(r"^\*?\s*([0-9A-Z]{3})-([0-9A-Z]{3})\s*(.+?)\s*$")
ALNUM = [chr(c) for c in range(48, 58)] + [chr(c) for c in range(65, 91)]  # 0-9 A-Z


def load_raw():
    if RAW.exists():
        return RAW.read_text(encoding="utf-8", errors="replace")
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(str(PDF)).pages)


def norm(name):
    """Normalise a country name for fuzzy matching (drop parentheticals)."""
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.sub(r"[^a-z ]", " ", name.lower())
    return " ".join(name.split())


# A few ITU-name -> cty-name hints where the two sources word things differently
# and the prefix isn't already covered by DXCC.
ALIASES = {
    "viet nam": "vietnam",
    "russian federation": "european russia",
    "korea": "republic of korea",          # parenthetical is stripped by norm()
    "united states of america": "united states",
    "germany": "fed rep of germany",
    "argentine republic": "argentina",
    "gambia": "the gambia",
    "syrian arab republic": "syria",
    "venezuela bolivarian republic of": "venezuela",
    "iran islamic republic of": "iran",
    "moldova republic of": "moldova",
    "tanzania united republic of": "tanzania",
    "brunei darussalam": "brunei darussalam",
    "lao peoples democratic republic": "laos",
}


def load_cty_names():
    """norm(country) -> (cont, lat, lon_east) from cty.dat."""
    out = {}
    if not CTY.exists():
        return out
    head = re.compile(r"^([^:]+):\s*\d+:\s*\d+:\s*(\w+):\s*"
                      r"(-?\d+\.?\d*):\s*(-?\d+\.?\d*):")
    for line in CTY.read_text(encoding="utf-8", errors="replace").splitlines():
        m = head.match(line)
        if m:
            out.setdefault(norm(m.group(1)),
                           (m.group(2), float(m.group(3)), -float(m.group(4))))
    return out


def match_country(country, cty):
    """Best (cont, lat, lon) for an ITU country name, or (None, None, None)."""
    n = norm(country)
    n = ALIASES.get(n, n)
    if n in cty:
        return cty[n]
    # startswith either direction (e.g. "united states" ⊂ "united states ...")
    for cn, val in cty.items():
        if len(cn) >= 4 and (n.startswith(cn) or cn.startswith(n)):
            return val
    # first significant word
    first = n.split()[0] if n.split() else ""
    if len(first) >= 4:
        for cn, val in cty.items():
            if cn.split() and cn.split()[0] == first:
                return val
    return (None, None, None)


def expand2(start, end):
    """All 2-char prefixes p with start[:2] <= p <= end[:2] (ASCII), valid."""
    s2, e2 = start[:2], end[:2]
    out = []
    for a in ALNUM:
        for b in ALNUM:
            p = a + b
            if s2 <= p <= e2:
                out.append(p)
    return out


def main():
    cty = load_cty_names()
    lookup = {}
    rows = 0
    for line in load_raw().splitlines():
        m = ROW.match(line.strip())
        if not m:
            continue
        start, end, country = m.group(1), m.group(2), m.group(3).strip()
        if not country or country.lower().startswith(("call sign", "page")):
            continue
        rows += 1
        cont, lat, lon = match_country(country, cty)
        disp = re.sub(r"\s*\(.*?\)", "", country).strip()    # drop parenthetical
        rec = {"country": disp, "cont": cont, "lat": lat, "lon": lon}
        for p in expand2(start, end):
            lookup.setdefault(p, rec)        # first range covering p wins
    OUT.write_text(json.dumps({"lookup": lookup}, ensure_ascii=False, indent=0),
                   encoding="utf-8")
    withc = sum(1 for r in lookup.values() if r["cont"])
    print(f"parsed {rows} ITU ranges -> {len(lookup)} 2-char prefixes "
          f"({withc} with continent) -> {OUT.name}")


if __name__ == "__main__":
    main()
