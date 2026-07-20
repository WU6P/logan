#!/usr/bin/env python3
"""Parse the ARRL DXCC entity list (doc/2022_DXCC_Current.pdf) into dxcc.json.

The PDF table has columns:  Prefix(es)  Entity  Continent  ITU  CQ  Code
Prefixes carry footnote digits and *, #, ^ markers, ranges ("DA-DR"), and
sometimes wrap onto a following line. We clean and expand them into a flat
prefix lookup so logan can resolve a callsign -> entity / continent / zones.

Run once (regenerates logan/dxcc.json):
    python3 build_dxcc.py

Reads doc/dxcc_raw.txt if present (extracted text), else extracts from the PDF
via pypdf. The committed dxcc.json means logan itself needs no dependencies.
"""

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "doc" / "dxcc_raw.txt"
PDF = HERE / "doc" / "2022_DXCC_Current.pdf"
OUT = HERE / "dxcc.json"

CONTS = ("AN", "AS", "AF", "EU", "NA", "OC", "SA")
# A complete table row ends with: CONT  ITU  CQ  CODE(3 digits).
# ITU/CQ may be ranges ("6,7,8"), dashes ("29-31"), or parenthesised ("(A)").
ROW_RE = re.compile(
    r"^(?P<pre>.*?)\s+"
    r"(?P<cont>(?:AN|AS|AF|EU|NA|OC|SA)(?:/(?:AN|AS|AF|EU|NA|OC|SA))?)\s+"
    r"(?P<zones>[\(\)A-I0-9,\-\s]+?)\s+"
    r"(?P<code>\d{3})\s*$"
)


def load_raw_text():
    if RAW.exists():
        return RAW.read_text(encoding="utf-8", errors="replace")
    from pypdf import PdfReader            # only needed when regenerating
    reader = PdfReader(str(PDF))
    return "\n".join(p.extract_text() for p in reader.pages)


def expand_range(tok):
    """Expand a prefix range like 'DA-DR' -> [DA, DB, ... DR]; trims footnotes."""
    parts = tok.split("-")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return [parts[0]] if parts and parts[0] else []
    a, b = parts[0], parts[1]
    if len(b) > len(a):                    # drop a trailing call-area/footnote
        b = b[:len(a)]
    if len(a) == len(b) and a[:-1] == b[:-1] and a[-1] <= b[-1]:
        return [a[:-1] + chr(c) for c in range(ord(a[-1]), ord(b[-1]) + 1)]
    return [a]


def clean_prefix(tok):
    """Turn one raw prefix token into zero or more clean lookup prefixes."""
    tok = tok.strip().strip("*#^").upper()
    if "," in tok:                         # e.g. "K,W,N" or "KH6,7" -> split
        out = []
        for sub in tok.split(","):
            out.extend(clean_prefix(sub))
        return out
    tok = tok.strip("*#^")
    if not tok or "." in tok or tok.isdigit():
        return []
    if "/" in tok:                         # compound (e.g. CE9/KC4) -> first part
        tok = tok.split("/")[0]
    if "-" in tok:
        return [p for p in expand_range(tok) if p]
    # Strip a trailing 2-digit footnote (10..55); single-digit tails are kept
    # because they're usually a real prefix digit (3A, 9A, T2, ...).
    m = re.match(r"^(.+?)(\d{2})$", tok)
    if m and 10 <= int(m.group(2)) <= 55 and m.group(1):
        tok = m.group(1)
    return [tok] if tok else []


def _name_start(toks):
    """Index where the entity name begins (earlier tokens are prefixes).

    Stops at the first lower-case word, and also at an all-caps word of 3+
    letters once a prefix has been seen — that's a name acronym such as the
    'DPR' in 'DPR of Korea', not a prefix (real prefixes are <=2 letters or
    carry a digit)."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.search(r"[a-z]", t):
            break
        if i > 0 and re.fullmatch(r"[A-Z]{3,}", t.strip("*#^,")):
            break
        i += 1
    return i


def split_prefixes(pre):
    """Pull the leading prefix tokens off the 'pre' field."""
    toks = pre.split()
    out = []
    for tok in toks[:_name_start(toks)]:
        out.extend(clean_prefix(tok))
    seen, res = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res


def entity_name(pre):
    """The entity name = pre with the leading prefix tokens removed."""
    toks = pre.split()
    return " ".join(toks[_name_start(toks):]).strip()


def parse(text):
    entities = []          # list of dicts
    last = None            # last completed entity (for orphan prefix lines)
    buffer = None          # an incomplete "start" line awaiting its continent row
    started = False        # skip the intro prose until the table header row

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if not started:
            if "Cont" in line and "ITU" in line and "Code" in line:
                started = True
            continue
        m = ROW_RE.match(line.strip())
        if m:
            pre = m.group("pre").strip()
            if buffer:                     # prepend a wrapped entity-start line
                pre = buffer + " " + pre
                buffer = None
            zones = re.sub(r",\s+", ",", m.group("zones").strip())
            ztoks = zones.split()
            itu = ztoks[0] if ztoks else ""
            cq = ztoks[-1] if len(ztoks) > 1 else (ztoks[0] if ztoks else "")
            prefixes = split_prefixes(pre)
            ent = {
                "prefixes": prefixes,
                "entity": entity_name(pre),
                "cont": m.group("cont"),
                "itu": itu,
                "cq": cq,
                "code": m.group("code"),
            }
            entities.append(ent)
            last = ent
            continue

        # Not a complete row.
        s = line.strip()
        first = s.split()[0]
        has_lower = bool(re.search(r"[a-z]", s))
        looks_prefix = bool(re.match(r"^[0-9]*[A-Z][0-9A-Z/,_*#^-]*$", first))
        if looks_prefix and not has_lower and last is not None:
            # Orphan prefix line: belongs to the previous entity (wrapped list).
            for p in split_prefixes(s):
                if p not in last["prefixes"]:
                    last["prefixes"].append(p)
        elif looks_prefix and has_lower:
            # Incomplete start (prefix + partial name); its continent row follows.
            buffer = s
        # else: entity-name spillover / notes -> ignore

    return entities


_REC = ("entity", "cont", "itu", "cq", "code", "lat", "lon")


def build_lookup(entities):
    """prefix -> {entity, cont, itu, cq, code, lat, lon}.

    First the exact cleaned prefixes (first entity wins a clash). Then, as a
    lower-priority fallback, the 2-char base of every 3-char prefix that ends
    in a digit (e.g. 9A6 -> 9A, S56 -> S5, KH6 -> KH): the PDF glues a
    single-digit footnote onto common prefixes, and longest-match still prefers
    the exact 3-char key when a real callsign carries it."""
    lut = {}
    for e in entities:
        for p in e["prefixes"]:
            lut.setdefault(p, {k: e.get(k) for k in _REC})
    for e in entities:
        for p in e["prefixes"]:
            if len(p) == 3 and p[2].isdigit():
                lut.setdefault(p[:2], {k: e.get(k) for k in _REC})
    return lut


# --------------------------------------------------------------------------
# Coordinates from AD1C's cty.dat (the ARRL PDF carries none).
# --------------------------------------------------------------------------

CTY = HERE / "doc" / "cty.dat"
# Header: Name: CQ: ITU: Cont: Lat: Lon: GMT: PrimaryPrefix:  (Lon positive=WEST)
_CTY_HEAD = re.compile(
    r"^(?P<name>[^:]+):\s*\d+:\s*\d+:\s*\w+:\s*"
    r"(?P<lat>-?\d+\.?\d*):\s*(?P<lon>-?\d+\.?\d*):\s*-?\d+\.?\d*:\s*"
    r"(?P<pfx>[A-Z0-9/]+):")


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


CTY_NAMES = {}                                # normalized name -> (lat, lon)


def parse_cty():
    """prefix -> (lat, lon_east) from cty.dat. Returns {} if the file is absent."""
    coords = {}
    CTY_NAMES.clear()
    if not CTY.exists():
        return coords
    cur = None
    for line in CTY.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _CTY_HEAD.match(line)
        if m:
            lat = float(m.group("lat"))
            lon = -float(m.group("lon"))      # cty is +west; store +east
            cur = (lat, lon)
            coords.setdefault(m.group("pfx").upper(), cur)
            CTY_NAMES.setdefault(_norm_name(m.group("name")), cur)
        elif cur and (line.startswith(" ") or line.startswith("\t")):
            # Alias prefixes; strip =exact-calls and (CQ)/[ITU]/<latlon>/{cont}.
            for tok in line.strip().rstrip(";").split(","):
                tok = re.sub(r"[(\[<{].*?[)\]>}]", "", tok).strip().lstrip("=")
                tok = tok.split("/")[0].upper()
                if tok and re.match(r"^[A-Z0-9]+$", tok):
                    coords.setdefault(tok, cur)
            if line.rstrip().endswith(";"):
                cur = None
    return coords


def enrich_coords(entities):
    """Attach (lat, lon) to each entity by matching its prefixes against cty.dat."""
    coords = parse_cty()
    if not coords:
        print("  (cty.dat not found — entities will have no coordinates)")
        return
    hit = 0
    for e in entities:
        # Exact cty.dat name match first: prefix matching lies when entities
        # share a prefix block (Asiatic Russia's UA... = European Russia's) or
        # when another entity's alias list claimed the prefix (Spratly's
        # "9M2/..." exact-call aliases swallow West Malaysia's 9M2).
        nm = _norm_name(e["entity"])
        got = CTY_NAMES.get(nm)
        cands = []
        for p in e["prefixes"]:
            cands.append(p)
            if len(p) == 3 and p[2].isdigit():   # 9A6 -> also try 9A
                cands.append(p[:2])
        if got is None:
            for p in cands:
                if p in coords:
                    got = coords[p]
                    break
        if got is None:                          # try a contained name (China...)
            for cn, c in CTY_NAMES.items():
                if len(cn) >= 4 and (cn in nm or nm in cn):
                    got = c
                    break
        if got is not None:
            e["lat"], e["lon"] = got
            hit += 1
    print(f"  coordinates attached to {hit}/{len(entities)} entities")


def main():
    text = load_raw_text()
    entities = parse(text)
    entities = [e for e in entities if e["prefixes"]]
    enrich_coords(entities)
    lut = build_lookup(entities)
    OUT.write_text(json.dumps({"entities": entities, "lookup": lut},
                              ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"parsed {len(entities)} entities, {len(lut)} prefixes -> {OUT.name}")


if __name__ == "__main__":
    main()
