#!/usr/bin/env python3
"""Build rare.json — the rarest / most-wanted DXCC entities, by ARRL code.

A worked entity from this list is flagged in logan so you can eye-ball it for a
likely busted callsign (e.g. a stray P5 "North Korea" in a domestic sprint).

This is a *curated* approximation of the long-standing Clublog "Most Wanted"
ranking — membership matters more than the exact order, and it drifts a little
year to year with DXpeditions. Keyed by DXCC entity code (stable) because some
prefixes collide (Bouvet and Peter I are both 3Y).

    python3 build_rare.py        # validates against dxcc.json, writes rare.json
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Roughly most-wanted first; the index becomes the rank. Names are comments for
# review (the build cross-checks each code against dxcc.json).
CURATED = [
    "344",  # DPR of Korea (P5)
    "024",  # Bouvet
    "041",  # Crozet I.
    "506",  # Scarborough Reef
    "217",  # San Felix & San Ambrosio
    "505",  # Pratas I.
    "138",  # Kure I.
    "123",  # Johnston I.
    "199",  # Peter 1 I.
    "131",  # Kerguelen Is.
    "111",  # Heard I.
    "253",  # St. Peter & St. Paul Rocks
    "297",  # Wake I.
    "174",  # Midway I.
    "124",  # Juan de Nova, Europa
    "240",  # South Sandwich Is.
    "099",  # Glorioso Is.
    "303",  # Willis I.
    "197",  # Palmyra & Jarvis Is.
    "512",  # Chesterfield Is.
    "016",  # New Zealand Subantarctic Islands
    "171",  # Mellish Reef
    "280",  # Turkmenistan
    "017",  # Aves I.
    "273",  # Trindade & Martim Vaz Is.
    "180",  # Mount Athos
    "201",  # Prince Edward & Marion Is.
    "507",  # Temotu Province
    "031",  # C. Kiribati
    "051",  # Eritrea
    "182",  # Navassa I.
    "235",  # South Georgia I.
    "490",  # Banaba I. (Ocean I.)
    "010",  # Amsterdam & St. Paul Is.
    "509",  # Marquesas Is.
    "489",  # Conway Reef
    "020",  # Baker & Howland Is.
    "195",  # Annobon I.
    "513",  # Ducie I.
    "142",  # Lakshadweep Is.
    "172",  # Pitcairn I.
    "276",  # Tromelin I.
    "036",  # Clipperton I.
    "298",  # Wallis & Futuna Is.
    "167",  # Market Reef
    "246",  # Sov. Mil. Order of Malta
    "004",  # Agalega & St. Brandon Is.
    "153",  # Macquarie I.
    "515",  # Swains I.
    "177",  # Minami Torishima
    "061",  # Franz Josef Land
    "011",  # Andaman & Nicobar Is.
    "460",  # Rotuma I.
    "033",  # Chagos Is.
    "492",  # Yemen
    "219",  # Sao Tome & Principe
    "511",  # Timor - Leste
    "188",  # Niue
    "270",  # Tokelau Is.
    "302",  # Western Sahara
    "232",  # Somalia
    "510",  # Palestine
    "410",  # Chad
    "408",  # Central Africa
    "414",  # Dem. Rep. of Congo
    "404",  # Burundi
    "411",  # Comoros
    "204",  # Revillagigedo
    "161",  # Malpelo I.
    "216",  # San Andres & Providencia
    "125",  # Juan Fernandez Is.
    "519",  # Saba & St. Eustatius
    "043",  # Desecheo I.
    "157",  # Nauru
    "133",  # Kermadec Is.
    "048",  # E. Kiribati (Line Is.)
    "301",  # W. Kiribati (Gilbert Is.)
    "191",  # N. Cook Is.
    "234",  # S. Cook Is.
    "282",  # Tuvalu
    "306",  # Bhutan
    "175",  # French Polynesia
]


def main():
    d = json.loads((HERE / "dxcc.json").read_text(encoding="utf-8"))
    names = {e["code"]: e["entity"] for e in d["entities"]}
    rare, rank = {}, 0
    missing = []
    for code in CURATED:
        if code not in names:
            missing.append(code)
            continue
        rank += 1
        rare[code] = rank
    (HERE / "rare.json").write_text(
        json.dumps({"rare": rare}, ensure_ascii=False, indent=0),
        encoding="utf-8")
    print(f"wrote rare.json: {len(rare)} entities")
    if missing:
        print("  WARNING: codes not found in dxcc.json (dropped):", missing)
    print("  top 10:", [f"{c} {names[c]}" for c in CURATED[:10] if c in names])


if __name__ == "__main__":
    main()
