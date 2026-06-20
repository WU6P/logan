#!/usr/bin/env python3
"""logan — a web-based ham radio log analyzer.

Start the server, then drag one or more ADIF (.adi/.adif) logs onto the page in
your browser. logan reports operating statistics:

  * summary — QSOs, span, unique calls, bands, modes, DXCC entities, zones
  * contest timeline (rate + cumulative + K-index) and hourly rate by band
  * best rolling 60- and 10-minute rates (contest style)
  * first / last contact per continent (e.g. the first & last European QSO)
  * band split, continent / CQ-zone / ITU-zone / DXCC-entity breakdowns
  * space weather per QSO (SFI / sunspot / A / K from GFZ Potsdam)
  * world map + azimuthal great-circle map of worked entities
  * QSO beam-heading rose / per-hour heat-map and a distance box plot by band
  * per-day totals

Continent / ITU zone / CQ zone / country for each callsign come from the ARRL
DXCC list parsed into dxcc.json (see build_dxcc.py). Per the project rule, that
prefix-derived data OVERRIDES the logger's own CONTINENT field when they
disagree; the logger's field is used only as a fallback when no prefix matches.

Usage:
    python3 logan.py                 # serve on http://127.0.0.1:8765 and open it
    python3 logan.py --port 9000     # choose the port
    python3 logan.py --no-browser    # don't auto-open a browser

Pure standard library — no third-party dependencies.
"""

import sys
import re
import json
import webbrowser
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import solar

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# ADIF parsing
# --------------------------------------------------------------------------

# An ADIF record is a run of fields like <CALL:5>W1AW <BAND:3>20m ... <EOR>.
TAG_RE = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+))?(?::[^>]*)?>")


def parse_adif_records(text):
    """Parse ADIF text into a list of dicts (one per QSO). Header is skipped.

    Field names are upper-cased. Robust to a missing header, extra whitespace,
    fields with no length specifier, and tag-like text inside field values.
    """
    eoh = re.search(r"<EOH>", text, re.IGNORECASE)
    pos = eoh.end() if eoh else 0
    records, current = [], {}
    while True:
        m = TAG_RE.search(text, pos)
        if not m:
            break
        name = m.group(1).upper()
        length = m.group(2)
        pos = m.end()
        if name == "EOR":
            if current:
                records.append(current)
                current = {}
            continue
        if name == "EOH":
            continue
        if length is not None:
            ln = int(length)
            current[name] = text[pos:pos + ln]
            pos += ln
        else:
            current[name] = ""
    if current:
        records.append(current)
    return records


def qso_datetime(qso):
    """Return a datetime for a QSO (UTC), or None if date/time are missing."""
    d = (qso.get("QSO_DATE", "") or "").strip()
    t = (qso.get("TIME_ON", "") or "").strip()
    if len(d) != 8:
        return None
    t = (t + "000000")[:6]
    try:
        return datetime.strptime(d + t, "%Y%m%d%H%M%S")
    except ValueError:
        return None


# --------------------------------------------------------------------------
# DXCC prefix resolution (from dxcc.json, built by build_dxcc.py)
# --------------------------------------------------------------------------

CONTINENT_NAMES = {
    "NA": "North America", "SA": "South America", "EU": "Europe",
    "AF": "Africa", "AS": "Asia", "OC": "Oceania", "AN": "Antarctica",
}


def _load_lookup(name):
    path = HERE / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("lookup", {})


DXCC = _load_lookup("dxcc.json")
# Second-priority source: ITU international call-sign-series table, keyed by the
# first two chars of a call (built from doc/ITZ Callsign.pdf by build_itu.py).
ITU = _load_lookup("itu.json")


def _load_dxcc_codes():
    path = HERE / "dxcc.json"
    if not path.exists():
        return {}
    ents = json.loads(path.read_text(encoding="utf-8")).get("entities", [])
    return {e["entity"]: e.get("code") for e in ents}


# entity name -> ARRL code, and the rarest/most-wanted entities (code -> rank),
# used to flag a likely busted callsign (e.g. a stray P5 in a domestic contest).
DXCC_CODE = _load_dxcc_codes()
RARE = (json.loads((HERE / "rare.json").read_text(encoding="utf-8")).get("rare", {})
        if (HERE / "rare.json").exists() else {})


def _load_entity_recs():
    """entity name -> full record (cont/lat/lon/cq/itu), for the call-area
    prefix overrides below (so an override resolves to a *located* entity)."""
    path = HERE / "dxcc.json"
    if not path.exists():
        return {}
    ents = json.loads(path.read_text(encoding="utf-8")).get("entities", [])
    return {e["entity"]: e for e in ents}


ENTITY_REC = _load_entity_recs()


_SUFFIXES = {"P", "M", "MM", "AM", "QRP", "A", "B"}


def _call_cores(call):
    """Candidate DXCC-carrying tokens of a callsign, best first.

    For a portable call the location prefix is normally the shorter side
    ("DL/W1AW" -> Germany), so non-suffix tokens with a letter are tried
    shortest-first; pure call-area markers ("/7", "/P") are dropped.
    """
    call = (call or "").upper().strip()
    if "/" not in call:
        return [call] if call else []
    parts = [p for p in call.split("/")
             if p and p not in _SUFFIXES and any(c.isalpha() for c in p)]
    return sorted(parts, key=len) or [call.replace("/", "")]


def _leading_alpha(head):
    """How many leading letters a prefix head has ('TM2' -> 2)."""
    i = 0
    while i < len(head) and head[i].isalpha():
        i += 1
    return i


def _lookup_head(core):
    """Longest-prefix DXCC match for a call core: (rec, key, head) or Nones.

    Full-call exception keys (e.g. 'KH7K' = Kure I.) are honoured before the
    head-prefix search. `key` lets callers judge confidence (a match that
    dropped leading letters, like France 'TM' collapsing to the 'T' catch-all
    = Kiribati, is unreliable)."""
    if core in DXCC:
        return DXCC[core], core, core
    m = re.match(r"([A-Z0-9]+?\d)", core)
    head = m.group(1) if m else core
    for n in range(len(head), 0, -1):
        key = head[:n]
        if key in DXCC:
            return DXCC[key], key, head
    return None, None, None


# The shared dxcc.json (from the ARRL DXCC PDF) keeps only coarse prefix keys,
# so call-area-split families resolve to the wrong entity -> wrong continent,
# zone and bearing: Hawaii/Guam/Alaska map to "USA", Asiatic Russia and
# Kaliningrad to "European Russia", the Canaries/Ceuta to "Spain", and bare
# R<digit> Russian calls don't resolve. These overrides map each split to the
# right (located) entity. See the dxcc_prefix_resolution skill.
_PREFIX_OVERRIDES = [
    (re.compile(r"^KP[34]"), "Puerto Rico"),
    (re.compile(r"^KP2"), "Virgin Is."),
    (re.compile(r"^[AKNW]H6"), "Hawaii"),
    (re.compile(r"^[AKNW]H7(?!K)"), "Hawaii"),         # KH7K = rare Kure, leave it
    (re.compile(r"^[AKNW]H2"), "Guam"),
    (re.compile(r"^[AKNW]H0"), "Mariana Is."),
    (re.compile(r"^[AKNW]L\d"), "Alaska"),
    (re.compile(r"^E[A-H]8"), "Canary Is."),
    (re.compile(r"^E[A-H]9"), "Ceuta & Melilla"),
    (re.compile(r"^(?:R[A-Z]?|U[A-I])[890]"), "Asiatic Russia"),
    (re.compile(r"^(?:R[A-Z]?|U[A-I])2F"), "Kaliningrad"),
    (re.compile(r"^R1(?!F)"), "European Russia"),      # R1FJ stays Franz Josef
    (re.compile(r"^(?:R[A-Z]?|U[A-I])[2-7]"), "European Russia"),
]


def _override_entity(call):
    for core in _call_cores(call):
        for rx, ent in _PREFIX_OVERRIDES:
            if rx.match(core):
                return ent
    return ""


def _dxcc_match(call):
    """First DXCC match across a call's cores: (rec, confident).

    A curated override wins outright; otherwise confident == the matched key
    kept every leading prefix letter (so a multi-letter prefix can't be
    mis-resolved by a 1-letter catch-all)."""
    ent = _override_entity(call)
    if ent:
        return ENTITY_REC.get(ent, {"entity": ent}), True
    for core in _call_cores(call):
        rec, key, head = _lookup_head(core)
        if rec:
            return rec, len(key) >= _leading_alpha(head)
    return None, False


def resolve_dxcc(call):
    """Longest-prefix DXCC record for a callsign, or None (override-aware).

    Tries each candidate token (shortest first) and returns the first that
    resolves, so a prefix indicator like DL in "DL/W1AW" wins over the home
    call."""
    rec, _confident = _dxcc_match(call)
    return rec


def resolve_itu(call):
    """ITU call-sign-series record for a callsign, or None.

    Keyed by the first two characters of each candidate token; only returns a
    record that carries a continent (so it's a usable fallback)."""
    for core in _call_cores(call):
        rec = ITU.get(core[:2])
        if rec and rec.get("cont"):
            return rec
    return None


def _call_core(call):
    """The single best candidate token (for display/tests)."""
    for c in _call_cores(call):
        if _lookup_head(c)[0]:
            return c
    cores = _call_cores(call)
    return cores[0] if cores else (call or "").upper().strip()


# Representative centroids by US call-area digit and Canadian VE/VA/VO/VY
# district, used to give domestic QSOs a *roughly* correct bearing instead of
# one meaningless country-centroid spike. Coordinates are (lat_N, lon_E).
US_AREAS = {
    "0": (41.5, -96.0), "1": (43.5, -71.5), "2": (42.0, -75.0),
    "3": (40.0, -77.0), "4": (33.0, -82.0), "5": (32.0, -97.0),
    "6": (37.0, -120.0), "7": (44.0, -114.0), "8": (40.5, -82.5),
    "9": (42.0, -89.0),
}
CA_AREAS = {
    "VE1": (45.0, -63.0), "VE2": (49.0, -72.0), "VE3": (45.5, -80.0),
    "VE4": (53.0, -98.0), "VE5": (53.0, -106.0), "VE6": (54.0, -115.0),
    "VE7": (53.0, -123.0), "VE8": (64.0, -120.0), "VE9": (46.5, -66.0),
    "VO1": (47.6, -53.0), "VO2": (53.5, -60.0),
    "VY1": (63.0, -135.0), "VY2": (46.3, -63.1), "VY0": (64.0, -95.0),
}


def refine_domestic(call, entity, lat, lon):
    """For USA-mainland / Canada QSOs, replace the country centroid with a
    call-area regional centroid so the bearing is roughly right. Returns
    (lat, lon, is_domestic)."""
    core = _call_core(call)
    if entity == "United States of America":
        m = re.search(r"\d", core)
        if m and m.group() in US_AREAS:
            return US_AREAS[m.group()][0], US_AREAS[m.group()][1], True
        return lat, lon, True
    if entity == "Canada":
        m = re.match(r"(VE|VA|VO|VY)(\d)", core)
        if m:
            grp = "VE" if m.group(1) in ("VE", "VA") else m.group(1)
            key = grp + m.group(2)
            if key in CA_AREAS:
                return CA_AREAS[key][0], CA_AREAS[key][1], True
        return lat, lon, True
    return lat, lon, False


def _zone(pdf_zone, n1mm_zone):
    """Pick a zone to chart. Prefer a single PDF number; fall back to the
    logger's per-QSO zone when the PDF gives a range/note; else the raw PDF."""
    z = (pdf_zone or "").strip()
    if z.isdigit():
        return str(int(z))
    nz = (n1mm_zone or "").strip()
    if nz.isdigit():
        return str(int(nz))
    return z or (str(int(nz)) if nz.isdigit() else "")


def _log_zones(qso):
    cq = (qso.get("CQZ", "") or "").strip()
    itu = (qso.get("ITUZ", "") or "").strip()
    return (str(int(itu)) if itu.isdigit() else "",
            str(int(cq)) if cq.isdigit() else "")


def classify(qso):
    """Return (continent, itu, cq, entity, source, lat, lon) for a QSO.

    Source priority: the ARRL DXCC prefix table ("pdf") first, then the ITU
    call-sign-series table ("itu"), then the logger's own CONTINENT field
    ("log"); "none" when nothing resolves. The first two win over the logger on
    conflict. ITU/CQ zones come from the DXCC record when present, otherwise the
    logger's per-QSO fields."""
    call = qso.get("CALL", "")
    n1mm_cont = (qso.get("APP_N1MM_CONTINENT", "") or qso.get("CONTINENT", "")
                 or qso.get("CONT", "")).strip().upper()

    rec, confident = _dxcc_match(call)

    def _from_pdf(rec):
        cont = (rec.get("cont") or "").split("/")[0]   # combo like EU/AS -> first
        cq = _zone(rec.get("cq"), qso.get("CQZ"))
        itu = _zone(rec.get("itu"), qso.get("ITUZ"))
        return (cont, itu, cq, rec.get("entity"), "pdf",
                rec.get("lat"), rec.get("lon"))

    if rec and confident:
        return _from_pdf(rec)

    # Low-confidence DXCC (a multi-letter prefix that collapsed to a 1-letter
    # catch-all, e.g. France TM -> T = Kiribati): trust the ITU series first.
    irec = resolve_itu(call)
    if irec:
        itu, cq = _log_zones(qso)             # ITU table carries no zones
        return (irec["cont"], itu, cq, irec.get("country"), "itu",
                irec.get("lat"), irec.get("lon"))

    if rec:                                   # low-confidence pdf, better than nothing
        return _from_pdf(rec)

    if n1mm_cont in CONTINENT_NAMES:
        itu, cq = _log_zones(qso)
        return n1mm_cont, itu, cq, None, "log", None, None
    return None, None, None, None, "none", None, None


# --------------------------------------------------------------------------
# Band ordering / colors
# --------------------------------------------------------------------------

BAND_ORDER = ["160M", "80M", "60M", "40M", "30M", "20M", "17M",
              "15M", "12M", "10M", "6M", "4M", "2M", "70CM"]
BAND_COLORS = {
    "160M": "#8B0000", "80M": "#DC143C", "60M": "#FF6347", "40M": "#FF8C00",
    "30M": "#FFD700", "20M": "#2196F3", "17M": "#00CED1", "15M": "#32CD32",
    "12M": "#9370DB", "10M": "#FF69B4", "6M": "#A0522D", "4M": "#8FBC8F",
    "2M": "#708090", "70CM": "#556B2F",
}
CONT_COLORS = {"NA": "#2196F3", "SA": "#FF9800", "EU": "#4CAF50",
               "AF": "#795548", "AS": "#E91E63", "OC": "#00BCD4",
               "AN": "#9E9E9E"}


def band_sort_key(band):
    b = band.upper()
    return (BAND_ORDER.index(b) if b in BAND_ORDER else len(BAND_ORDER), b)


def _best_window(times, width):
    """Max QSOs within any window of `width` over time-sorted `times`.
    Returns (count, window_start_datetime)."""
    best, at, left = 0, None, 0
    for right in range(len(times)):
        while times[right] - times[left] >= width:
            left += 1
        c = right - left + 1
        if c > best:
            best, at = c, times[left]
    return best, at


def _rate(n, span):
    """QSOs per hour over a span (timedelta)."""
    sec = span.total_seconds()
    return round(n / (sec / 3600.0), 1) if n > 1 and sec > 0 else float(n)


def _on_minutes(times, gap_min=30):
    """Active operating minutes: sum inter-QSO gaps shorter than `gap_min`
    (a break of >= gap_min counts as off-time, per the usual contest rule)."""
    on = 0.0
    for a, b in zip(times, times[1:]):
        d = (b - a).total_seconds() / 60.0
        if 0 <= d < gap_min:
            on += d
    return on


def fmt_span(span):
    total_min = int(span.total_seconds() // 60)
    d, rem = divmod(total_min, 1440)
    h, m = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Analysis  ->  a JSON-serializable dict
# --------------------------------------------------------------------------

def classify_cached(q):
    """classify() with the result memoised on the QSO dict (filters re-run)."""
    if "_LOGAN" not in q:
        q["_LOGAN"] = classify(q)
    return q["_LOGAN"]


def analyze(qsos, sources, opts=None):
    """Analyze QSOs into a JSON-able report. `opts` may carry filters:
    {"bands": [...], "conts": [...]} — when present, only matching QSOs count."""
    opts = opts or {}
    fbands = set(b.upper() for b in opts.get("bands") or []) or None
    fconts = set(c.upper() for c in opts.get("conts") or []) or None

    rows = []
    skipped = 0
    bands_seen, conts_seen = set(), set()
    for q in qsos:
        dt = qso_datetime(q)
        if dt is None:
            skipped += 1
            continue
        band = (q.get("BAND", "") or "Unknown").upper()
        cont = classify_cached(q)[0]
        bands_seen.add(band)
        if cont:
            conts_seen.add(cont)
        if fbands and band not in fbands:
            continue
        if fconts and (cont or "??") not in fconts:
            continue
        rows.append((dt, q))
    rows.sort(key=lambda r: r[0])
    total = len(rows)

    out = {"meta": {"sources": sources, "total": total, "skipped": skipped,
                    "all_bands": sorted(bands_seen, key=band_sort_key),
                    "all_conts": sorted(conts_seen),
                    "filter_bands": sorted(fbands or [], key=band_sort_key),
                    "filter_conts": sorted(fconts or [])}}
    if not total:
        return out

    start, end = rows[0][0], rows[-1][0]
    span = end - start

    unique_calls = len({(q.get("CALL", "") or "").upper().strip()
                        for _, q in rows if q.get("CALL", "").strip()})

    band_counts = Counter()
    band_first, band_last = {}, {}
    hour_counts = Counter()
    hour_band = defaultdict(Counter)
    day_counts = Counter()
    mode_counts = Counter()
    cont_counts = Counter()
    cont_first, cont_last = {}, {}
    cq_counts = Counter()
    itu_counts = Counter()
    dxcc_counts = Counter()
    dxcc_cont = {}
    dxcc_coord = {}
    dxcc_first, dxcc_last = {}, {}
    src_counts = Counter()
    cont_unknown = 0
    runsp_counts = Counter()             # Run vs Search&Pounce (N1MM)
    op_stats = {}                        # per-operator breakdown (multi-op logs)
    station_counts = Counter()           # STATION_CALLSIGN (for the map center)
    points_total = 0
    points_n = 0
    # Space-weather conditions at each QSO's time (SFI/SSN/A daily, K 3-hourly).
    sfi_v, ssn_v, a_v, k_v = [], [], [], []
    kdist = Counter()                    # QSOs bucketed by floor(Kp), 0..9
    cond_n = 0
    dir_qsos = []                        # per-QSO {h, d, band, mode, lat, lon, dom} for beam headings
    # Contest timeline: one bucket per occupied (date, hour), with breakdowns.
    tl = {}                              # "YYYY-MM-DD HHZ" -> {...}

    for dt, q in rows:
        call = (q.get("CALL", "") or "").upper()
        band = (q.get("BAND", "") or "Unknown").upper()
        band_counts[band] += 1
        band_first.setdefault(band, dt)
        band_last[band] = dt
        hour_counts[dt.hour] += 1
        hour_band[dt.hour][band] += 1
        day_counts[dt.date().isoformat()] += 1
        mode_counts[(q.get("MODE", "") or "?").upper()] += 1

        cont, itu, cq, entity, source, lat, lon = classify_cached(q)
        src_counts[source] += 1
        info = {"dt": dt.isoformat(), "call": call, "band": band,
                "entity": entity or ""}
        if cont is None:
            cont_unknown += 1
        else:
            cont_counts[cont] += 1
            cont_first.setdefault(cont, info)
            cont_last[cont] = info
        if cq:
            cq_counts[cq] += 1
        if itu:
            itu_counts[itu] += 1
        if entity:
            dxcc_counts[entity] += 1
            dxcc_cont[entity] = cont
            dxcc_coord[entity] = (lat, lon)
            dxcc_first.setdefault(entity, info)
            dxcc_last[entity] = info

        if lat is not None and lon is not None:
            rlat, rlon, dom = refine_domestic(call, entity, lat, lon)
            dir_qsos.append({"h": dt.hour, "d": dt.date().isoformat(),
                             "band": band,
                             "mode": (q.get("MODE", "") or "?").upper(),
                             "lat": rlat, "lon": rlon, "dom": dom})

        rs = (q.get("APP_N1MM_RUN1RUN2", "") or "").strip()
        if rs in ("1", "2"):
            runsp_counts["Run" if rs == "1" else "S&P"] += 1
        pts = (q.get("APP_N1MM_POINTS", "") or "").strip()
        has_pts = pts.lstrip("-").isdigit()
        if has_pts:
            points_total += int(pts)
            points_n += 1
        op = (q.get("OPERATOR", "") or "").upper().strip()
        if op:
            s = op_stats.get(op)
            if s is None:
                s = op_stats[op] = {"count": 0, "bands": Counter(),
                                    "first": dt, "times": [], "calls": set(),
                                    "entities": set(), "pts": 0, "pts_n": 0}
            s["count"] += 1
            s["bands"][band] += 1
            s["last"] = dt
            s["times"].append(dt)
            if call.strip():
                s["calls"].add(call.strip())
            if entity:
                s["entities"].add(entity)
            if has_pts:
                s["pts"] += int(pts)
                s["pts_n"] += 1
        st = (q.get("STATION_CALLSIGN", "") or "").upper().strip()
        if st:
            station_counts[st] += 1

        cond = solar.conditions_at(dt)
        if cond:
            cond_n += 1
            if cond["sfi"] is not None:
                sfi_v.append(cond["sfi"])
            if cond["ssn"] is not None:
                ssn_v.append(cond["ssn"])
            if cond["a"] is not None:
                a_v.append(cond["a"])
            if cond["k"] is not None:
                k_v.append(cond["k"])
                kdist[min(9, int(cond["k"]))] += 1   # 0..9 bucket

        key = f"{dt.date().isoformat()} {dt.hour:02d}Z"
        b = tl.get(key)
        if b is None:
            b = tl[key] = {"label": key, "count": 0, "ksum": 0.0, "kn": 0,
                           "bands": Counter(), "conts": Counter()}
        b["count"] += 1
        b["bands"][band] += 1
        if cont:
            b["conts"][cont] += 1
        if cond and cond["k"] is not None:
            b["ksum"] += cond["k"]
            b["kn"] += 1

    # Best rolling-window rates.
    times = [dt for dt, _ in rows]

    def best_window(width):
        n, at = _best_window(times, width)
        return {"n": n, "at": at.isoformat() if at else None}

    qph = (total / (span.total_seconds() / 3600.0)
           if total > 1 and span.total_seconds() > 0 else float(total))

    bands_present = sorted(band_counts, key=band_sort_key)

    out["meta"].update({
        "unique_calls": unique_calls,
        "start": start.isoformat(), "end": end.isoformat(),
        "span": fmt_span(span), "days": len(day_counts),
        "qph": round(qph, 1),
        "best60": best_window(timedelta(minutes=60)),
        "best10": best_window(timedelta(minutes=10)),
        "n_bands": len(band_counts), "n_modes": len(mode_counts),
        "n_dxcc": len(dxcc_counts), "n_cont": len(cont_counts),
        "n_cq": len(cq_counts), "n_itu": len(itu_counts),
        "src_pdf": src_counts["pdf"], "src_itu": src_counts["itu"],
        "src_log": src_counts["log"],
        "cont_unknown": cont_unknown,
        "has_dxcc": bool(DXCC),
        "points": points_total if points_n else None,
        "points_per_q": round(points_total / points_n, 2) if points_n else None,
        "n_ops": len(op_stats),
    })

    # Home station (for the azimuthal-map default center): the most-used
    # STATION_CALLSIGN, placed at its DXCC entity's representative coordinates.
    home = None
    if station_counts:
        call = station_counts.most_common(1)[0][0]
        hrec = resolve_dxcc(call)
        if hrec and hrec.get("lat") is not None:
            # Refine US/Canada to the call-area centroid so the default map and
            # direction center is near the actual QTH, not the country middle.
            hlat, hlon, _ = refine_domestic(call, hrec.get("entity"),
                                            hrec["lat"], hrec["lon"])
            home = {"call": call, "lat": hlat, "lon": hlon,
                    "entity": hrec.get("entity")}
    out["meta"]["home"] = home

    # Space-weather summary (QSO-weighted: the conditions you actually worked in).
    def _avg(v):
        return round(sum(v) / len(v), 1) if v else None
    s_has, s_lo, s_hi = solar.available()
    out["meta"]["solar"] = {
        "available": s_has, "earliest": s_lo, "latest": s_hi,
        "cond_qsos": cond_n,
        "sfi_avg": _avg(sfi_v), "sfi_min": min(sfi_v) if sfi_v else None,
        "sfi_max": max(sfi_v) if sfi_v else None,
        "ssn_avg": _avg(ssn_v), "ssn_min": min(ssn_v) if ssn_v else None,
        "ssn_max": max(ssn_v) if ssn_v else None,
        "a_avg": _avg(a_v), "a_max": max(a_v) if a_v else None,
        "k_avg": _avg(k_v), "k_max": round(max(k_v), 1) if k_v else None,
    }
    out["kdist"] = [{"k": k, "count": kdist.get(k, 0)} for k in range(10)]

    # Contest timeline with a running cumulative total.
    tl_sorted = [tl[k] for k in sorted(tl)]
    cum = 0
    timeline = []
    for b in tl_sorted:
        cum += b["count"]
        timeline.append({"label": b["label"], "count": b["count"], "cum": cum,
                         "k": round(b["ksum"] / b["kn"], 2) if b["kn"] else None,
                         "bands": dict(b["bands"]), "conts": dict(b["conts"])})
    out["timeline"] = timeline
    out["runsp"] = [{"kind": k, "count": c} for k, c in runsp_counts.most_common()]
    op_out = []
    for op, s in sorted(op_stats.items(), key=lambda kv: -kv[1]["count"]):
        span_o = s["last"] - s["first"]
        b60, _ = _best_window(s["times"], timedelta(minutes=60))
        b10, _ = _best_window(s["times"], timedelta(minutes=10))
        op_out.append({
            "op": op, "count": s["count"], "unique": len(s["calls"]),
            "ndxcc": len(s["entities"]),
            "first": s["first"].isoformat(), "last": s["last"].isoformat(),
            "span": fmt_span(span_o), "qph": _rate(s["count"], span_o),
            "on_h": round(_on_minutes(s["times"]) / 60.0, 1),
            "best60": b60, "best10": b10,
            "bands": {b: s["bands"][b] for b in
                      sorted(s["bands"], key=band_sort_key)},
            "points": s["pts"] if s["pts_n"] else None,
        })
    out["operators"] = op_out
    out["dirqsos"] = dir_qsos
    out["hours"] = [{"hour": h, "total": hour_counts.get(h, 0),
                     "bands": dict(hour_band.get(h, {}))} for h in range(24)]
    out["bandOrder"] = bands_present
    out["bandColors"] = {b: BAND_COLORS.get(b, "#9E9E9E") for b in bands_present}
    out["bands"] = [{"band": b, "count": band_counts[b],
                     "pct": round(band_counts[b] / total * 100, 1),
                     "first": band_first[b].isoformat(),
                     "last": band_last[b].isoformat()}
                    for b in bands_present]
    out["modes"] = [{"mode": m, "count": c}
                    for m, c in mode_counts.most_common()]
    out["continents"] = [{
        "code": c, "name": CONTINENT_NAMES.get(c, c), "count": n,
        "pct": round(n / total * 100, 1),
        "color": CONT_COLORS.get(c, "#9E9E9E"),
        "first": cont_first[c], "last": cont_last[c],
    } for c, n in cont_counts.most_common()]
    out["cq"] = [{"zone": z, "count": n} for z, n in
                 sorted(cq_counts.items(), key=_zone_key)]
    out["itu"] = [{"zone": z, "count": n} for z, n in
                  sorted(itu_counts.items(), key=_zone_key)]
    out["dxcc"] = [{
        "entity": e, "cont": dxcc_cont.get(e, ""), "count": n,
        "color": CONT_COLORS.get(dxcc_cont.get(e, ""), "#9E9E9E"),
        "lat": dxcc_coord.get(e, (None, None))[0],
        "lon": dxcc_coord.get(e, (None, None))[1],
        "rank": RARE.get(DXCC_CODE.get(e)),     # most-wanted rank, or None
        "first": dxcc_first[e], "last": dxcc_last[e],
    } for e, n in dxcc_counts.most_common()]
    out["meta"]["n_rare"] = sum(1 for e in dxcc_counts
                                if RARE.get(DXCC_CODE.get(e)))
    days_out = []
    for d in sorted(day_counts):
        rec = solar.day_record(datetime.fromisoformat(d).date())
        days_out.append({"date": d, "count": day_counts[d],
                         "sfi": rec["sfi"] if rec else None,
                         "ssn": rec["ssn"] if rec else None,
                         "a": rec["a"] if rec else None})
    out["days"] = days_out
    return out


def _zone_key(item):
    z = item[0]
    return (0, int(z)) if z.isdigit() else (1, z)


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

# Parsed logs are cached by id so filter changes recompute without re-upload.
# Bounded so a long-running server can't grow without limit.
SESSIONS = {}
SESSION_ORDER = []


def cache_session(qsos, sources):
    import uuid
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = (qsos, sources)
    SESSION_ORDER.append(sid)
    while len(SESSION_ORDER) > 8:
        SESSIONS.pop(SESSION_ORDER.pop(0), None)
    return sid


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):           # quiet console
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8", errors="replace")
        try:
            if self.path == "/analyze":
                result = self._analyze(raw)
            elif self.path == "/refilter":
                result = self._refilter(raw)
            elif self.path == "/solar/refresh":
                ok, msg = solar.update()
                has, lo, hi = solar.available()
                result = {"ok": ok, "msg": msg, "earliest": lo, "latest": hi}
            else:
                self._send(404, "not found", "text/plain")
                return
        except Exception as e:               # never 500 silently
            self._send(200, json.dumps({"error": str(e)}), "application/json")
            return
        self._send(200, json.dumps(result), "application/json; charset=utf-8")

    def _analyze(self, raw):
        # Body: "NAME:<file>\n<adif>" chunks joined by a \x00FILE\x00 marker.
        sources, text = [], []
        for chunk in raw.split("\x00FILE\x00"):
            if not chunk.strip():
                continue
            if chunk.startswith("NAME:"):
                name, _, content = chunk.partition("\n")
                sources.append(name[5:].strip())
                text.append(content)
            else:
                sources.append("log")
                text.append(chunk)
        qsos = []
        for t in text:
            qsos.extend(parse_adif_records(t))
        if not sources:
            sources = ["log"]
        sid = cache_session(qsos, sources)
        result = analyze(qsos, sources)
        result["id"] = sid
        return result

    def _refilter(self, raw):
        req = json.loads(raw or "{}")
        cached = SESSIONS.get(req.get("id"))
        if not cached:
            return {"error": "session expired — please re-drop the file"}
        qsos, sources = cached
        result = analyze(qsos, sources, req.get("opts") or {})
        result["id"] = req.get("id")
        return result


def serve(port, open_browser):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"logan running at {url}")
    print(f"  DXCC prefixes loaded: {len(DXCC)}"
          if DXCC else "  WARNING: dxcc.json not found — run build_dxcc.py")
    print("  drop your .adi files on the page;  Ctrl-C to stop")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    port = 8765
    open_browser = True
    if "--no-browser" in argv:
        open_browser = False
        argv.remove("--no-browser")
    if "--port" in argv:
        i = argv.index("--port")
        try:
            port = int(argv[i + 1])
        except (IndexError, ValueError):
            print("error: --port needs a number, e.g. --port 9000",
                  file=sys.stderr)
            return 2
    serve(port, open_browser)
    return 0


# --------------------------------------------------------------------------
# Front-end (served at /)
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>logan — ham log analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/topojson-client@3/dist/topojson-client.min.js"></script>
<style>
 :root{--bg:#0f1419;--card:#1a2230;--ink:#e6edf3;--mut:#8b98a9;--ac:#2196F3;--bd:#222c3a;
   --canvas:#0c121b;--grid:#222c3a;--land:#16202e;--ocean:#0e1826;--coast:#2c3b52;--gratic:#172234;
   --z1:#ffffff08;--z2:#ffffff16;--input:#0f1722;--btn:#22324a;--btnh:#2b3e5c;--chip:#0f1722;--hot:#16202e}
 :root.light{--bg:#f4f6f9;--card:#ffffff;--ink:#1a2230;--mut:#5a6776;--ac:#1976D2;--bd:#dce2ea;
   --canvas:#eef2f7;--grid:#dce2ea;--land:#d6e0ec;--ocean:#e9eef5;--coast:#a8b6c6;--gratic:#dde4ec;
   --z1:#00000006;--z2:#00000012;--input:#eef2f7;--btn:#e7ecf2;--btnh:#dbe2ea;--chip:#eef2f7;--hot:#e8f0fb}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
 header{padding:22px 28px;border-bottom:1px solid var(--bd);display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
 h1{margin:0;font-size:21px;letter-spacing:.5px}
 header .sub{color:var(--mut);font-size:13px}
 .wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
 #drop{border:2px dashed #33405264;border-radius:14px;padding:40px;text-align:center;color:var(--mut);
       background:var(--card);cursor:pointer;transition:.15s}
 #drop.hot{border-color:var(--ac);color:var(--ink);background:var(--hot)}
 #drop b{color:var(--ink)}
 input[type=file]{display:none}
 .note{color:var(--mut);font-size:12.5px}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:12px;margin-top:6px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:13px 15px}
 .card .k{font-size:19px;font-weight:700}
 .card .l{color:var(--mut);font-size:12px;margin-top:2px}
 .hl{outline:1px solid #4CAF5055}
 .panel{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:18px;margin-top:20px}
 .panel h2{margin:0 0 12px;font-size:13px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.05em;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
 .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
 .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px}
 .grid2>.panel,.grid3>.panel{margin-top:0}
 @media(max-width:820px){.grid2,.grid3{grid-template-columns:1fr}}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd);white-space:nowrap}
 th{color:var(--mut);font-weight:600}
 td.n,th.n{text-align:right}
 .swatch{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:middle}
 canvas{max-height:300px}
 .err{color:#ff6b6b}
 .warn{color:#ff9800}
 .tabbar{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--bd)}
 .tabbtn{background:none;border:none;border-bottom:2px solid transparent;color:var(--mut);padding:9px 18px;cursor:pointer;font:inherit;font-size:14px;font-weight:600}
 .tabbtn:hover{color:var(--ink)}
 .tabbtn.active{color:var(--ink);border-bottom-color:var(--ac)}
 table.matrix td,table.matrix th{text-align:center;padding:3px 8px;font-variant-numeric:tabular-nums}
 table.matrix td.op,table.matrix th.op{text-align:left;font-weight:600}
 table.matrix td.z{color:#55617340}
 table.matrix .tot{font-weight:700}
 .lbgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:16px}
 .lbcol h3{margin:0 0 6px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;font-weight:600}
 .lbcol ol{margin:0;padding-left:20px;font-size:13px}
 .lbcol li{padding:1px 0}
 .lbcol li b{color:var(--ac)}
 table tr.rarerow{background:#ff980018}
 .rarebadge{color:#ff9800;font-weight:700;font-size:11px;white-space:nowrap}
 .hide{display:none}
 .scroll{max-height:340px;overflow:auto}
 .dirwrap{display:flex;flex-wrap:wrap;gap:18px;justify-content:center;align-items:flex-start}
 .dirfig{margin:0;text-align:center}
 .dirfig canvas{width:300px;max-width:80vw;height:auto;display:block}
 .dirfig figcaption{margin-bottom:6px;line-height:1.3}
 table.dirheattable{font-size:11px;border-collapse:collapse}
 table.dirheattable th,table.dirheattable td{border:none;padding:1px 3px;white-space:nowrap}
 table.dirheattable thead th{color:var(--mut);font-weight:600;text-align:center;font-size:10px}
 table.dirheattable thead th.vh{writing-mode:vertical-rl;font-size:9px;padding:3px 0;line-height:1;height:30px}
 table.dirheattable td.lab{text-align:right;color:var(--ink);font-variant-numeric:tabular-nums}
 table.dirheattable td.fl{color:var(--mut);font-size:10px}
 table.dirheattable th,table.dirheattable td{padding:0 1px}
 table.dirheattable td.cell{width:12px;height:13px;text-align:center;font-size:8px}
 table.dirheattable tbody tr td{background:var(--z1)}        /* even hour rows */
 table.dirheattable tbody tr.zebra td{background:var(--z2)}  /* odd hours: lighter, to separate adjacent hours */
 table.dirheattable tbody tr.totrow td{background:none;border-top:1px solid #2c3b52}
 /* controls */
 #ctl{position:sticky;top:0;z-index:5}
 .ctlrow{display:flex;gap:22px;flex-wrap:wrap;align-items:center}
 .ctlrow .grp{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .chip{display:inline-flex;align-items:center;gap:5px;background:#0f1722;border:1px solid var(--bd);
       background:var(--chip);border-radius:999px;padding:3px 10px;font-size:12.5px;cursor:pointer;user-select:none}
 .chip input{accent-color:var(--ac);margin:0}
 .lbl{color:var(--mut);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
 select,input[type=number]{background:var(--input);color:var(--ink);border:1px solid var(--bd);border-radius:7px;padding:4px 8px;font:inherit;font-size:13px}
 button.btn{background:var(--btn);color:var(--ink);border:1px solid var(--bd);border-radius:7px;padding:5px 12px;cursor:pointer;font:inherit;font-size:13px}
 button.btn:hover{background:var(--btnh)}
</style></head>
<body>
<header><h1>LOGAN</h1><span class=sub id=hsub>Ham Radio Log Analysis — drop an ADIF file to begin</span>
  <button id=themeBtn class=btn style="margin-left:auto">☀︎ Light</button></header>
<div class=wrap>
  <div id=drop>
    <b>Drop ADIF logs here</b> or click to choose<br>
    <span class=note>.adi / .adif &nbsp;·&nbsp; multiple files allowed &nbsp;·&nbsp; everything stays on your machine</span>
    <input type=file id=file accept=".adi,.adif,text/plain" multiple>
  </div>

  <div id=app class=hide>
    <div class=tabbar>
      <button class="tabbtn active" data-tab=tabOverall>Overall</button>
      <button class="tabbtn hide" id=opsTabBtn data-tab=tabOps>Multi-operator</button>
    </div>
    <div id=tabOverall class=tab>
    <div class="panel" id=ctl><h2>Filters &amp; options <span class=note id=filtNote></span></h2>
      <div class=ctlrow>
        <div class=grp><span class=lbl>Bands</span><span id=bandChips></span></div>
        <div class=grp><span class=lbl>Continents</span><span id=contChips></span></div>
      </div>
      <div class=ctlrow style="margin-top:12px">
        <div class=grp><span class=lbl>Timeline breakdown</span>
          <select id=tlMode><option value=band>by band</option>
            <option value=cont>by continent</option><option value=none>total only</option></select></div>
        <div class=grp><span class=lbl>Overlay</span>
          <label class=chip><input type=checkbox id=tlCum checked> cumulative</label>
          <label class=chip><input type=checkbox id=tlK> K-index</label></div>
        <div class=grp><span class=lbl>DXCC rows</span>
          <input type=number id=topN value=25 min=1 max=400 style="width:70px"></div>
        <button class=btn id=reset>Reset filters</button>
      </div>
      <div class=ctlrow style="margin-top:10px">
        <span class=note id=solarNote></span>
        <button class=btn id=refreshSolar>Refresh space-weather data</button>
      </div>
    </div>

    <div class=cards id=cards></div>

    <div class=panel><h2>Contest timeline — rate + cumulative <span class=note>(one chart, several views: bars per hour, running total line, band/continent breakdown)</span></h2>
      <canvas id=tlChart></canvas></div>

    <div class=panel><h2>Hourly rate by UTC hour-of-day — stacked by band</h2><canvas id=hourChart></canvas></div>

    <div class=grid2>
      <div class=panel><h2>Band split</h2><canvas id=bandChart></canvas></div>
      <div class=panel><h2>Continents <span id=srcNote class=note></span></h2><canvas id=contChart></canvas></div>
    </div>

    <div class=panel><h2>First / last contact per continent</h2>
      <table id=contTable><thead><tr><th>Continent</th><th class=n>QSOs</th><th class=n>Share</th>
        <th>First contact</th><th>Last contact</th></tr></thead><tbody></tbody></table></div>

    <div class=grid2>
      <div class=panel><h2>CQ zones</h2><canvas id=cqChart></canvas></div>
      <div class=panel><h2>ITU zones</h2><canvas id=ituChart></canvas></div>
    </div>

    <div class=grid2>
      <div class=panel id=runspPanel><h2>Run vs S&amp;P</h2><canvas id=runspChart></canvas></div>
      <div class=panel><h2>Modes</h2><canvas id=modeChart></canvas></div>
    </div>

    <div class=panel><h2>DXCC entities (countries) <span class=note id=dxccNote></span></h2>
      <div class=scroll><table id=dxccTable><thead><tr><th>Entity</th><th>Cont</th><th class=n>QSOs</th>
        <th>First</th><th>Last</th></tr></thead><tbody></tbody></table></div></div>

    <div class=panel id=worldPanel><h2>Worked DXCC entities — world map <span class=note id=worldNote>(bubble size = QSO count · color = continent)</span></h2>
      <div class=ctlrow style="margin-bottom:12px">
        <div class=grp><span class=lbl>Center</span>
          <input id=worldCenter placeholder="grid CM87 / CM87xi, or lat,lon" style="width:185px">
          <button class=btn id=worldGo>Set</button></div>
        <div class=grp><span class=lbl>Projection</span>
          <select id=worldProj><option value=natural>Natural Earth</option>
            <option value=equirect>Equirectangular</option></select></div>
        <span class=note id=worldCNote></span>
      </div>
      <svg id=worldMap viewBox="0 0 960 500" style="width:100%;height:auto;background:var(--canvas);border-radius:8px"></svg></div>

    <div class=panel id=azPanel><h2>Azimuthal map — great-circle, centered on your station</h2>
      <div class=ctlrow style="margin-bottom:12px">
        <div class=grp><span class=lbl>Center</span>
          <input id=azCenter placeholder="grid CM87 / CM87xi, or lat,lon" style="width:185px">
          <button class=btn id=azGo>Set</button></div>
        <div class=grp><label class=chip><input type=checkbox id=azLines> great-circle lines</label></div>
        <span class=note id=azNote></span>
      </div>
      <svg id=azMap viewBox="0 0 540 540" style="width:100%;max-width:580px;height:auto;display:block;margin:0 auto;background:var(--canvas);border-radius:8px"></svg></div>

    <div class=panel id=dirPanel><h2>QSO directions — beam headings in 5° slots <span class=note>(bearing from your station to each entity; N=0° up, clockwise)</span></h2>
      <div class=ctlrow style="margin-bottom:12px">
        <div class=grp><span class=lbl>Center</span>
          <input id=dirCenter placeholder="grid CM87 / CM87xi, or lat,lon" style="width:175px">
          <button class=btn id=dirGo>Set</button></div>
        <div class=grp><span class=lbl>Band</span><select id=dirBand></select></div>
        <div class=grp><span class=lbl>Mode</span><select id=dirMode></select></div>
        <div class=grp><label class=chip title="US/Canada are placed at a call-area regional centroid — approximate"><input type=checkbox id=dirDom checked> include US/Canada</label></div>
        <div class=grp><label class=chip><input type=checkbox id=dirBox checked> distance box plot</label></div>
        <div class=grp><label class=chip><input type=checkbox id=dirTable checked> direction × hour table</label></div>
        <span class=note id=dirNote></span>
      </div>
      <div class=dirwrap>
        <figure class=dirfig><figcaption class=note>Summary — all hours combined<br>(radius = QSO count per 5° slot)</figcaption>
          <canvas id=roseChart width=440 height=440></canvas></figure>
        <div id=dirHeats class=dirwrap></div>
      </div>
      <div id=boxPanel style="margin-top:18px">
        <div class=note style="margin-bottom:4px">QSO distance by band — box = 25–75%, line = median, whiskers = 1.5×IQR, dots = outliers (great-circle km from center)</div>
        <canvas id=distBox width=900 height=300 style="width:100%;height:auto"></canvas>
      </div>
      <div id=dirTablePanel style="margin-top:18px">
        <div class=note style="margin-bottom:4px">Direction × UTC-hour activity — rows = UTC hour-of-day, columns = 5° heading; color = QSO count (rate)</div>
        <div class=scroll><table id=dirHeatTable class=dirheattable><thead></thead><tbody></tbody></table></div>
      </div>
    </div>

    <div class=panel id=swPanel><h2>Space weather vs activity — per day <span class=note>(QSOs as bars; SFI &amp; sunspot number &amp; A-index as lines)</span></h2>
      <canvas id=swChart></canvas></div>

    <div class=grid2>
      <div class=panel id=kPanel><h2>QSOs by K-index <span class=note>(geomagnetic conditions you worked through; 0 = quiet, 5+ = storm)</span></h2>
        <canvas id=kChart></canvas></div>
      <div class=panel><h2>Per-day QSO totals</h2><canvas id=dayChart></canvas></div>
    </div>
    </div><!-- /tabOverall -->

    <div id=tabOps class="tab hide">
    <div class=panel id=opPanel><h2>Per-operator <span class=note>multi-op breakdown — QSOs, bands, rate</span></h2>
      <canvas id=opChart></canvas>
      <div class=scroll style="margin-top:14px"><table id=opTable>
        <thead><tr><th>Operator</th><th class=n>QSOs</th><th class=n>Unique</th><th class=n>DXCCs</th>
          <th class=n>Rate/hr</th><th class=n>Best 10</th><th class=n>Best 60</th><th class=n>Hours-on</th>
          <th>Span</th><th>First</th><th>Last</th></tr></thead><tbody></tbody></table></div>
      <div class=note style="margin:18px 0 4px">Operator × band matrix (QSOs)</div>
      <div class=scroll><table id=opMatrix class=matrix><thead></thead><tbody></tbody></table></div></div>

    <div class=panel id=lbPanel><h2>Operator leaderboard</h2>
      <div id=lbGrid class=lbgrid></div></div>
    </div><!-- /tabOps -->
  </div>
  <div id=msg class=note style="margin-top:14px"></div>
</div>
<script>
const $=s=>document.querySelector(s), drop=$('#drop'), fileIn=$('#file');
function cssv(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
let INK='', MUT='', TH={};
let grid={color:'#222c3a'}, tick={color:MUT};
function readTheme(){
  INK=cssv('--ink'); MUT=cssv('--mut');
  grid.color=cssv('--grid'); tick.color=MUT;
  TH={canvas:cssv('--canvas'),grid:cssv('--grid'),coast:cssv('--coast'),
      land:cssv('--land'),ocean:cssv('--ocean'),gratic:cssv('--gratic')};
  HEAT0=hexToRgb(TH.canvas);
}
const AX={x:{grid,ticks:tick},y:{grid,ticks:tick,beginAtZero:true}};
let charts={}, D=null, SID=null;
function chart(id,cfg){ if(charts[id])charts[id].destroy(); charts[id]=new Chart($(id),cfg); }
// Server sends naive UTC ISO strings; force UTC parsing so the display isn't
// shifted by the browser's local timezone.
const asUTC=s=>new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s)?s:s+'Z');
const fmt=s=>asUTC(s).toISOString().slice(0,16).replace('T',' ')+'Z';
const fmtmd=s=>asUTC(s).toISOString().slice(5,16).replace('T',' ')+'Z';
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

drop.onclick=()=>fileIn.click();
fileIn.onchange=()=>upload(fileIn.files);
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('hot');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('hot');}));
drop.addEventListener('drop',ev=>upload(ev.dataTransfer.files));

async function upload(files){
  if(!files||!files.length) return;
  $('#msg').textContent='Reading '+files.length+' file(s)…'; $('#msg').className='note';
  const parts=[];
  for(const f of files){ parts.push('NAME:'+f.name+'\n'+await f.text()); }
  await post('/analyze', parts.join('\x00FILE\x00'), true);
}
async function refilter(){
  if(!SID) return;
  const opts={bands:checked('#bandChips'), conts:checked('#contChips')};
  await post('/refilter', JSON.stringify({id:SID, opts}), false, true);
}
async function post(url, body, isUpload, isJson){
  $('#msg').textContent='Analyzing…'; $('#msg').className='note';
  try{
    const r=await fetch(url,{method:'POST',headers:isJson?{'Content-Type':'application/json'}:{},body});
    const d=await r.json();
    if(d.error){ $('#msg').textContent='Error: '+d.error; $('#msg').className='note err'; return; }
    SID=d.id; D=d;
    if(isUpload) buildControls(d);
    render(d);
  }catch(e){ $('#msg').textContent='Error: '+e; $('#msg').className='note err'; }
}
function checked(sel){ return [...document.querySelectorAll(sel+' input:checked')].map(c=>c.value); }

function buildControls(d){
  const m=d.meta;
  $('#bandChips').innerHTML=m.all_bands.map(b=>
    `<label class=chip><input type=checkbox value="${b}" checked>${b}</label>`).join('');
  $('#contChips').innerHTML=m.all_conts.map(c=>
    `<label class=chip><input type=checkbox value="${c}" checked>${c}</label>`).join('');
  document.querySelectorAll('#bandChips input,#contChips input').forEach(c=>c.onchange=refilter);
  $('#tlMode').onchange=()=>drawTimeline(D);
  $('#tlCum').onchange=()=>drawTimeline(D);
  $('#tlK').onchange=()=>drawTimeline(D);
  $('#topN').oninput=()=>drawDxcc(D);
  $('#azGo').onclick=()=>drawAz(D);
  $('#azCenter').addEventListener('keydown',e=>{ if(e.key==='Enter') drawAz(D); });
  $('#azLines').onchange=()=>drawAz(D);
  $('#worldGo').onclick=()=>drawWorld(D);
  $('#worldCenter').addEventListener('keydown',e=>{ if(e.key==='Enter') drawWorld(D); });
  $('#worldProj').onchange=()=>drawWorld(D);
  $('#dirGo').onclick=()=>drawDirections(D);
  $('#dirCenter').addEventListener('keydown',e=>{ if(e.key==='Enter') drawDirections(D); });
  $('#dirBand').onchange=()=>drawDirections(D);
  $('#dirMode').onchange=()=>drawDirections(D);
  $('#dirDom').onchange=()=>drawDirections(D);
  $('#dirBox').onchange=()=>drawDirections(D);
  $('#dirTable').onchange=()=>drawDirections(D);
  $('#reset').onclick=()=>{
    document.querySelectorAll('#bandChips input,#contChips input').forEach(c=>c.checked=true);
    refilter();
  };
  $('#refreshSolar').onclick=async()=>{
    const b=$('#refreshSolar'); b.textContent='Downloading…'; b.disabled=true;
    try{
      const r=await fetch('/solar/refresh',{method:'POST'}); const j=await r.json();
      b.textContent=j.ok?('Updated → '+j.latest):('Failed: '+j.msg);
      if(j.ok&&SID) await refilter();          // recompute with fresh data
    }catch(e){ b.textContent='Failed: '+e; }
    setTimeout(()=>{b.textContent='Refresh space-weather data';b.disabled=false;},4000);
  };
}

function card(k,l,hl){ return `<div class="card${hl?' hl':''}"><div class=k>${k}</div><div class=l>${l}</div></div>`; }

function drawTimeline(d){
  const t=d.timeline, mode=$('#tlMode').value, labels=t.map(x=>x.label);
  let ds=[];
  if(mode==='band'){
    ds=d.bandOrder.map(b=>({label:b,data:t.map(x=>x.bands[b]||0),backgroundColor:d.bandColors[b],stack:'q'}));
  }else if(mode==='cont'){
    ds=d.continents.map(c=>({label:c.name,data:t.map(x=>x.conts[c.code]||0),backgroundColor:c.color,stack:'q'}));
  }else{
    ds=[{label:'QSOs',data:t.map(x=>x.count),backgroundColor:'#2196F3',stack:'q'}];
  }
  const scales={x:{...AX.x,stacked:true},y:{...AX.y,stacked:true,title:{display:true,text:'QSOs / hour',color:MUT}}};
  if($('#tlCum').checked){
    ds.push({type:'line',label:'Cumulative',data:t.map(x=>x.cum),yAxisID:'y1',borderColor:'#FFD54F',
      backgroundColor:'#FFD54F',borderWidth:2,pointRadius:0,tension:.25,order:-1});
    scales.y1={position:'right',grid:{drawOnChartArea:false},ticks:tick,beginAtZero:true,
      title:{display:true,text:'cumulative',color:MUT}};
  }
  if($('#tlK').checked && t.some(x=>x.k!=null)){
    ds.push({type:'line',label:'K-index',data:t.map(x=>x.k),yAxisID:'yk',borderColor:'#FF6E6E',
      backgroundColor:'#FF6E6E',borderWidth:2,borderDash:[4,3],pointRadius:0,tension:.2,
      spanGaps:true,order:-2});
    scales.yk={position:'right',min:0,max:9,grid:{drawOnChartArea:false},ticks:tick,
      title:{display:true,text:'K-index',color:'#FF6E6E'}};
  }
  chart('#tlChart',{type:'bar',data:{labels,datasets:ds},
    options:{responsive:true,plugins:{legend:{labels:{color:INK,boxWidth:12}}},
      interaction:{mode:'index',intersect:false},scales}});
}

function drawDxcc(d){
  const n=Math.max(1,parseInt($('#topN').value)||25);
  const rare=d.dxcc.filter(e=>e.rank).sort((a,b)=>a.rank-b.rank);
  const shown=d.dxcc.slice(0,n);
  const list=shown.concat(rare.filter(e=>!shown.includes(e)));  // rare always shown
  $('#dxccNote').innerHTML='showing '+shown.length+' of '+d.dxcc.length+
    (rare.length?' · <span class=warn>⚠ '+rare.length+' rare / most-wanted worked — verify the callsign(s)</span>':'');
  $('#dxccTable tbody').innerHTML=list.map(e=>
    `<tr class="${e.rank?'rarerow':''}"><td>`+
      `${e.rank?'<span class=rarebadge title="most-wanted #'+e.rank+' — likely a busted call">⚠ #'+e.rank+'</span> ':''}${esc(e.entity)}</td>`+
    `<td><span class=swatch style="background:${e.color}"></span>${esc(e.cont)}</td>`+
    `<td class=n>${e.count}</td><td>${fmtmd(e.first.dt)} ${esc(e.first.call)}</td>`+
    `<td>${fmtmd(e.last.dt)} ${esc(e.last.call)}</td></tr>`).join('');
}

// ---- maps (d3-geo) ----
let WORLD=null;
async function world(){
  if(WORLD) return WORLD;
  const t=await (await fetch('https://cdn.jsdelivr.net/npm/world-atlas@2/land-110m.json')).json();
  WORLD=topojson.feature(t,t.objects.land);
  return WORLD;
}
function maiden(g){
  g=g.trim().toUpperCase();
  if(!/^[A-R]{2}[0-9]{2}([A-X]{2})?$/.test(g)) return null;
  let lon=(g.charCodeAt(0)-65)*20-180, lat=(g.charCodeAt(1)-65)*10-90;
  lon+=(+g[2])*2; lat+=(+g[3])*1;
  if(g.length>=6){ lon+=(g.charCodeAt(4)-65)*5/60+2.5/60; lat+=(g.charCodeAt(5)-65)*2.5/60+1.25/60; }
  else { lon+=1; lat+=0.5; }
  return [lon,lat];
}
function parseCenter(s){
  if(!s) return null;
  s=s.trim();
  // Coordinates: lat then lon, separated by comma, slash or whitespace.
  const m=s.match(/^(-?\d+(?:\.\d+)?)\s*[,/ ]\s*(-?\d+(?:\.\d+)?)$/);
  if(m){
    const a=parseFloat(m[1]), b=parseFloat(m[2]);
    if(Math.abs(a)<=90 && Math.abs(b)<=180) return [b,a];          // lat,lon
    if(Math.abs(b)<=90 && Math.abs(a)<=180) return [a,b];          // tolerate lon,lat
    return null;
  }
  return maiden(s);   // Maidenhead grid: 4-char (CM87) or 6-char (CM87xi)
}
function gcKm(a,b){ // [lon,lat] great-circle km
  const R=6371, r=Math.PI/180, dl=(b[0]-a[0])*r;
  const la1=a[1]*r, la2=b[1]*r;
  const c=Math.sin(la1)*Math.sin(la2)+Math.cos(la1)*Math.cos(la2)*Math.cos(dl);
  return R*Math.acos(Math.max(-1,Math.min(1,c)));
}
function mapsReady(){ return typeof d3!=='undefined' && typeof topojson!=='undefined'; }
async function drawWorld(d){
  if(!mapsReady()){ $('#worldNote').textContent='(map library unavailable — needs internet for d3)'; return; }
  const pts=d.dxcc.filter(e=>e.lat!=null);
  $('#worldNote').textContent='('+pts.length+' of '+d.dxcc.length+' entities mapped · bubble = QSO count · color = continent)';
  const land=await world();
  const w=960,h=500, svg=d3.select('#worldMap'); svg.selectAll('*').remove();
  // Custom center: recenter the map on the chosen longitude (the equator stays
  // horizontal — a "normal" recentred world map, e.g. Pacific-centred).
  const c=parseCenter($('#worldCenter').value);
  const lon0=c?c[0]:0;
  $('#worldCNote').textContent=c?('centred on '+lon0.toFixed(0)+'° lon'):'centred on 0° (Greenwich)';
  const kind=$('#worldProj').value;
  const proj=(kind==='equirect'?d3.geoEquirectangular():d3.geoNaturalEarth1())
              .rotate([-lon0,0]);
  proj.fitExtent([[6,6],[w-6,h-6]], {type:'Sphere'});
  const path=d3.geoPath(proj);
  svg.append('path').datum({type:'Sphere'}).attr('d',path).attr('fill',TH.ocean).attr('stroke',TH.coast);
  svg.append('path').datum(d3.geoGraticule10()).attr('d',path).attr('fill','none').attr('stroke',TH.gratic);
  svg.append('path').datum(land).attr('d',path).attr('fill',TH.land).attr('stroke',TH.coast);
  const rmax=Math.sqrt(Math.max(...pts.map(e=>e.count),1));
  svg.append('g').selectAll('circle').data(pts).join('circle')
    .attr('cx',e=>proj([e.lon,e.lat])[0]).attr('cy',e=>proj([e.lon,e.lat])[1])
    .attr('r',e=>3+10*Math.sqrt(e.count)/rmax).attr('fill',e=>e.color)
    .attr('fill-opacity',.72).attr('stroke',TH.canvas).attr('stroke-width',.6)
    .append('title').text(e=>e.entity+': '+e.count+' QSOs');
}
async function drawAz(d){
  if(!mapsReady()){ $('#azNote').textContent='Map library unavailable — needs internet for d3.'; return; }
  const c=parseCenter($('#azCenter').value);
  if(!c){ $('#azNote').textContent='Enter a Maidenhead grid (CM87) or "lat,lon".'; $('#azNote').className='note err'; return; }
  $('#azNote').className='note';
  $('#azNote').textContent='center '+c[1].toFixed(2)+', '+c[0].toFixed(2);
  const land=await world();
  const R=270, rad=258, scale=rad/Math.PI, svg=d3.select('#azMap'); svg.selectAll('*').remove();
  const proj=d3.geoAzimuthalEquidistant().rotate([-c[0],-c[1]]).translate([R,R]).scale(scale).clipAngle(179.9);
  const path=d3.geoPath(proj);
  svg.append('circle').attr('cx',R).attr('cy',R).attr('r',rad).attr('fill',TH.ocean).attr('stroke',TH.coast);
  svg.append('path').datum(d3.geoGraticule10()).attr('d',path).attr('fill','none').attr('stroke',TH.gratic);
  svg.append('path').datum(land).attr('d',path).attr('fill',TH.land).attr('stroke',TH.coast);
  [5000,10000,15000,20000].forEach(km=>{ const rr=km/6371*scale;
    svg.append('circle').attr('cx',R).attr('cy',R).attr('r',rr).attr('fill','none').attr('stroke',TH.coast).attr('stroke-dasharray','2,4');
    svg.append('text').attr('x',R).attr('y',R-rr-2).attr('fill',MUT).attr('font-size',9).attr('text-anchor','middle').text(km/1000+'k km'); });
  ['N','E','S','W'].forEach((lbl,i)=>{ const a=i*Math.PI/2;
    svg.append('text').attr('x',R+Math.sin(a)*(rad+10)).attr('y',R-Math.cos(a)*(rad+10)+3).attr('fill',MUT).attr('font-size',11).attr('text-anchor','middle').text(lbl); });
  const pts=d.dxcc.filter(e=>e.lat!=null), g=svg.append('g');
  if($('#azLines').checked) pts.forEach(e=>{ const p=proj([e.lon,e.lat]); if(p)
    g.append('line').attr('x1',R).attr('y1',R).attr('x2',p[0]).attr('y2',p[1]).attr('stroke',e.color).attr('stroke-opacity',.35).attr('stroke-width',.8); });
  const rmax=Math.sqrt(Math.max(...pts.map(e=>e.count),1));
  pts.forEach(e=>{ const p=proj([e.lon,e.lat]); if(!p) return;
    g.append('circle').attr('cx',p[0]).attr('cy',p[1]).attr('r',2.5+8*Math.sqrt(e.count)/rmax)
      .attr('fill',e.color).attr('fill-opacity',.78).attr('stroke',TH.canvas).attr('stroke-width',.6)
      .append('title').text(e.entity+': '+e.count+' QSOs · '+Math.round(gcKm(c,[e.lon,e.lat]))+' km'); });
  svg.append('circle').attr('cx',R).attr('cy',R).attr('r',3.5).attr('fill','#FFD54F');
}

// ---- QSO direction (beam-heading) analysis ----
function bearing(c, p){ // c,p = [lon,lat]; great-circle initial bearing, 0..360
  const r=Math.PI/180, dl=(p[0]-c[0])*r, la1=c[1]*r, la2=p[1]*r;
  const y=Math.sin(dl)*Math.cos(la2);
  const x=Math.cos(la1)*Math.sin(la2)-Math.sin(la1)*Math.cos(la2)*Math.cos(dl);
  return (Math.atan2(y,x)/r+360)%360;
}
// Heat scale: the zero end is the (theme) canvas colour so empty cells blend
// in — light in light mode, dark in dark mode — then blue→cyan→green→yellow→red.
let HEAT0=[12,18,27];
function hexToRgb(h){ h=(h||'').replace('#','').trim();
  if(h.length===3) h=h.split('').map(c=>c+c).join('');
  return h.length>=6?[parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)]:[12,18,27]; }
function heatRGB(t){
  const stops=[HEAT0,[33,102,172],[33,188,210],[120,200,80],[255,213,79],[229,57,53]];
  if(t<=0) return HEAT0;
  const x=Math.min(1,t)*(stops.length-1), i=Math.min(stops.length-2,Math.floor(x)), f=x-i;
  const a=stops[i], b=stops[i+1];
  return [a[0]+(b[0]-a[0])*f|0,a[1]+(b[1]-a[1])*f|0,a[2]+(b[2]-a[2])*f|0];
}
function heatColor(t){ const c=heatRGB(t); return `rgb(${c[0]},${c[1]},${c[2]})`; }
function wedge(ctx,cx,cy,r0,r1,degA,degB){ // compass degrees, N up, clockwise
  const a=(degA-90)*Math.PI/180, b=(degB-90)*Math.PI/180;
  ctx.beginPath(); ctx.arc(cx,cy,r1,a,b); ctx.arc(cx,cy,r0,b,a,true); ctx.closePath();
}
function compassLabels(ctx,cx,cy,R){
  ctx.fillStyle=MUT; ctx.font='12px sans-serif'; ctx.textAlign='center'; ctx.textBaseline='middle';
  [['N',0],['E',90],['S',180],['W',270]].forEach(([t,deg])=>{
    const a=(deg-90)*Math.PI/180; ctx.fillText(t,cx+Math.cos(a)*(R+12),cy+Math.sin(a)*(R+12)); });
  ctx.strokeStyle=TH.grid;
  for(let deg=0;deg<360;deg+=30){ const a=(deg-90)*Math.PI/180;
    ctx.beginPath(); ctx.moveTo(cx+Math.cos(a)*R,cy+Math.sin(a)*R);
    ctx.lineTo(cx+Math.cos(a)*(R+5),cy+Math.sin(a)*(R+5)); ctx.stroke(); }
}
function drawRose(canvas, slots){
  const ctx=canvas.getContext('2d'), W=canvas.width, H=canvas.height;
  ctx.clearRect(0,0,W,H); const cx=W/2, cy=H/2, R=Math.min(cx,cy)-26;
  const mx=Math.max(1,...slots);
  // grid rings + value labels
  ctx.strokeStyle=TH.grid; ctx.fillStyle=MUT; ctx.font='10px sans-serif'; ctx.textAlign='left';
  for(let g=1;g<=4;g++){ const rr=R*g/4; ctx.beginPath(); ctx.arc(cx,cy,rr,0,2*Math.PI); ctx.stroke();
    ctx.fillText(Math.round(mx*g/4),cx+2,cy-rr); }
  for(let j=0;j<72;j++){ if(!slots[j]) continue;
    const r=R*slots[j]/mx; wedge(ctx,cx,cy,0,r,j*5,(j+1)*5);
    ctx.fillStyle='rgba(33,150,243,.78)'; ctx.fill();
    ctx.strokeStyle=TH.canvas; ctx.lineWidth=.5; ctx.stroke(); }
  compassLabels(ctx,cx,cy,R);
}
function drawDirHeat(canvas, grid){
  const ctx=canvas.getContext('2d'), W=canvas.width, H=canvas.height;
  ctx.clearRect(0,0,W,H); const cx=W/2, cy=H/2, R=Math.min(cx,cy)-26, ri=36, dr=(R-ri)/24;
  let mx=1; for(const row of grid) for(const v of row) if(v>mx) mx=v;
  // Time runs inward: hour 0 is the OUTERMOST ring, hour 23 the innermost.
  for(let h=0;h<24;h++){ const r1=R-h*dr, r0=r1-dr;
    for(let j=0;j<72;j++){ const v=grid[h][j];
      wedge(ctx,cx,cy,r0,r1,j*5,(j+1)*5); ctx.fillStyle=heatColor(v/mx); ctx.fill(); }
  }
  // hour ticks at North radial
  ctx.fillStyle=MUT; ctx.font='9px sans-serif'; ctx.textAlign='center'; ctx.textBaseline='middle';
  [0,6,12,18,23].forEach(h=>{ const rr=R-(h+0.5)*dr; ctx.fillText(h+'Z',cx+6,cy-rr); });
  compassLabels(ctx,cx,cy,R);
  // legend
  ctx.textAlign='left'; ctx.fillStyle=MUT; ctx.fillText('max '+mx+'/slot',8,H-8);
}
function quantile(sorted,p){
  const i=(sorted.length-1)*p, lo=Math.floor(i), hi=Math.ceil(i);
  return sorted[lo]+(sorted[hi]-sorted[lo])*(i-lo);
}
function boxStats(values){
  const s=[...values].sort((a,b)=>a-b);
  const q1=quantile(s,.25), q2=quantile(s,.5), q3=quantile(s,.75), iqr=q3-q1;
  const lo=q1-1.5*iqr, hi=q3+1.5*iqr;
  let wl=s[0], wh=s[s.length-1]; const out=[];
  for(const v of s){ if(v<lo||v>hi) out.push(v); }
  wl=s.find(v=>v>=lo); wh=[...s].reverse().find(v=>v<=hi);
  return {q1,q2,q3,wl,wh,out,n:s.length,min:s[0],max:s[s.length-1]};
}
function drawBox(canvas,groups){
  const ctx=canvas.getContext('2d'), W=canvas.width, padL=64, padR=70, padT=14, padB=28;
  const rows=groups.filter(g=>g.values.length);
  canvas.height=padT+padB+rows.length*38+6;
  const H=canvas.height; ctx.clearRect(0,0,W,H);
  if(!rows.length){ ctx.fillStyle=MUT; ctx.font='13px sans-serif'; ctx.fillText('No QSOs to summarise.',padL,padT+20); return; }
  const maxD=Math.max(...rows.map(g=>Math.max(...g.values)))||1;
  const x=v=>padL+(W-padL-padR)*v/maxD;
  // x grid + km labels
  ctx.strokeStyle=TH.grid; ctx.fillStyle=MUT; ctx.font='10px sans-serif'; ctx.textAlign='center';
  const step=maxD>16000?5000:maxD>8000?2500:1000;
  for(let v=0;v<=maxD;v+=step){ ctx.beginPath(); ctx.moveTo(x(v),padT); ctx.lineTo(x(v),H-padB); ctx.stroke();
    ctx.fillText((v/1000)+'k',x(v),H-padB+14); }
  ctx.textAlign='left'; ctx.fillText('km →',W-padR+6,H-padB+14);
  rows.forEach((g,i)=>{
    const cy=padT+18+i*38, b=boxStats(g.values), h=11;
    ctx.fillStyle=INK; ctx.font='12px sans-serif'; ctx.textAlign='left'; ctx.textBaseline='middle';
    ctx.fillText(g.label,6,cy);
    ctx.strokeStyle=g.color; ctx.fillStyle=g.color+'55'; ctx.lineWidth=1.5;
    // whiskers
    ctx.beginPath(); ctx.moveTo(x(b.wl),cy); ctx.lineTo(x(b.q1),cy); ctx.moveTo(x(b.q3),cy); ctx.lineTo(x(b.wh),cy); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x(b.wl),cy-5); ctx.lineTo(x(b.wl),cy+5); ctx.moveTo(x(b.wh),cy-5); ctx.lineTo(x(b.wh),cy+5); ctx.stroke();
    // box
    ctx.beginPath(); ctx.rect(x(b.q1),cy-h,x(b.q3)-x(b.q1),h*2); ctx.fill(); ctx.stroke();
    // median
    ctx.beginPath(); ctx.moveTo(x(b.q2),cy-h); ctx.lineTo(x(b.q2),cy+h); ctx.lineWidth=2; ctx.stroke();
    // outliers
    ctx.fillStyle=g.color;
    b.out.forEach(v=>{ ctx.beginPath(); ctx.arc(x(v),cy,1.8,0,2*Math.PI); ctx.fill(); });
    // count + median label
    ctx.fillStyle=MUT; ctx.font='10px sans-serif'; ctx.textAlign='left'; ctx.textBaseline='middle';
    ctx.fillText('n='+b.n+'  med '+Math.round(b.q2)+'km',W-padR+4,cy);
  });
}

function compass8(deg){
  return ['N','NE','E','SE','S','SW','W','NW'][Math.round(deg/45)%8];
}
function drawDirTable(c, qsos){
  // Transposed: rows = 24 UTC hours, columns = all 72 5° headings (0..355°).
  const grid=Array.from({length:72},()=>new Array(24).fill(0));
  for(const q of qsos){
    const j=Math.floor(bearing(c,[q.lon,q.lat])/5)%72;
    grid[j][q.h]++;
  }
  let mx=1; for(const row of grid) for(const v of row) if(v>mx) mx=v;
  // Sparse labels (every 10°) keep columns narrow while all 72 are shown.
  let head='<tr><th>UTC</th>';
  for(let j=0;j<72;j++){ const deg=j*5, lab=deg%10===0?deg.toString().padStart(3,'0'):'';
    head+=`<th class=vh title="${deg}° ${compass8(deg)}">${lab}</th>`; }
  $('#dirHeatTable thead').innerHTML=head+'</tr>';
  let body='';
  for(let h=0;h<24;h++){
    body+=`<tr class="${h%2?'zebra':''}"><td class=lab>${String(h).padStart(2,'0')}Z</td>`;
    for(let j=0;j<72;j++){ const v=grid[j][h], ti=`title="${j*5}° ${String(h).padStart(2,'0')}Z: ${v} QSO"`;
      if(v){ const cc=heatRGB(v/mx), lum=0.2126*cc[0]+0.7152*cc[1]+0.0722*cc[2];
        body+=`<td class=cell style="background:rgb(${cc[0]},${cc[1]},${cc[2]});color:${lum>140?'#000':'#fff'}" ${ti}>${v}</td>`;
      } else body+=`<td class=cell ${ti}></td>`; }
    body+='</tr>';
  }
  $('#dirHeatTable tbody').innerHTML=body;
}

function drawDirections(d){
  const sel=$('#dirCenter').value, c=parseCenter(sel);
  if(!c){ $('#dirNote').textContent='Enter a grid (CM87) or "lat,lon" for your station.'; $('#dirNote').className='note err';
    drawRose($('#roseChart'),new Array(72).fill(0)); $('#dirHeats').innerHTML=''; return; }
  $('#dirNote').className='note';
  const band=$('#dirBand').value, mode=$('#dirMode').value, dom=$('#dirDom').checked;
  const newGrid=()=>Array.from({length:24},()=>new Array(72).fill(0));
  const slots=new Array(72).fill(0), gridAll=newGrid(), byDate={}, dirFiltered=[];
  let n=0, nd=0;
  for(const q of d.dirqsos){
    if(band!=='*' && q.band!==band) continue;
    if(mode!=='*' && q.mode!==mode) continue;
    if(q.dom){ nd++; if(!dom) continue; }
    dirFiltered.push(q);
    const j=Math.floor(bearing(c,[q.lon,q.lat])/5)%72;
    slots[j]++; gridAll[q.h][j]++; n++;
    (byDate[q.d]=byDate[q.d]||newGrid())[q.h][j]++;
  }
  $('#dirNote').textContent='center '+c[1].toFixed(2)+', '+c[0].toFixed(2)+' · '+n+' QSOs · 5° slots'+
    (nd?' · '+nd+' US/Canada '+(dom?'(call-area est.)':'excluded'):'');
  drawRose($('#roseChart'),slots);

  // Per-hour heat-map panels: one per day, plus a Combined when 2 days
  // (a 48-hour contest -> Day 1, Day 2, Combined). >2 days folds into one.
  const dates=Object.keys(byDate).sort();
  let panels;
  if(dates.length===2)
    panels=[['Day 1 — '+dates[0],byDate[dates[0]]],['Day 2 — '+dates[1],byDate[dates[1]]],
            ['Combined',gridAll]];
  else if(dates.length>2)
    panels=[['Per UTC hour-of-day · '+dates.length+' days folded',gridAll]];
  else
    panels=[['Per UTC hour-of-day'+(dates.length?' — '+dates[0]:''),gridAll]];
  const host=$('#dirHeats'); host.innerHTML='';
  panels.forEach(([label,grid])=>{
    const fig=document.createElement('figure'); fig.className='dirfig';
    fig.innerHTML='<figcaption class=note>'+label+'<br>(outer 00Z → inner 23Z · color = QSO count)</figcaption>';
    const cv=document.createElement('canvas'); cv.width=440; cv.height=440;
    fig.appendChild(cv); host.appendChild(fig); drawDirHeat(cv,grid);
  });

  // Distance box plot by band (all bands; respects mode + US/Canada toggle).
  $('#boxPanel').style.display=$('#dirBox').checked?'':'none';
  if($('#dirBox').checked){
    const byBand={};
    for(const q of d.dirqsos){
      if(mode!=='*' && q.mode!==mode) continue;
      if(q.dom && !dom) continue;
      (byBand[q.band]=byBand[q.band]||[]).push(gcKm(c,[q.lon,q.lat]));
    }
    const groups=d.bandOrder.filter(b=>byBand[b]).map(b=>({label:b,color:d.bandColors[b],values:byBand[b]}));
    drawBox($('#distBox'),groups);
  }

  // Direction × hour activity table (first/last QSO + hourly rate by color).
  $('#dirTablePanel').style.display=$('#dirTable').checked?'':'none';
  if($('#dirTable').checked) drawDirTable(c, dirFiltered);
}

function render(d){
  readTheme();
  const m=d.meta;
  if(!m.total){ $('#msg').textContent='No QSOs match the current filters.'; $('#app').classList.remove('hide'); return; }
  $('#msg').textContent=''; $('#app').classList.remove('hide');
  $('#hsub').textContent=m.sources.join(', ')+'  ·  '+fmt(m.start)+' → '+fmt(m.end);
  const filt=[]; if(m.filter_bands.length) filt.push(m.filter_bands.join('/'));
  if(m.filter_conts.length) filt.push(m.filter_conts.join('/'));
  $('#filtNote').textContent=filt.length?('active: '+filt.join('  ·  ')):'all QSOs';

  const eu=d.continents.find(c=>c.code==='EU');
  const cs=[];
  cs.push(card(m.total,'QSOs'+(m.skipped?` (+${m.skipped} undated)`:'')));
  cs.push(card(m.unique_calls,'Unique calls'));
  cs.push(card(m.qph+'/hr','Avg rate'));
  cs.push(card(m.best60.n,'Best 60-min'+(m.best60.at?' @ '+fmtmd(m.best60.at):'')));
  cs.push(card(m.best10.n+' ('+(m.best10.n*6)+'/hr)','Best 10-min'));
  cs.push(card(m.span,'Active span'));
  cs.push(card(m.days,'Operating days'));
  cs.push(card(m.n_dxcc,'DXCC entities'));
  cs.push(card(m.n_cont,'Continents'));
  cs.push(card(m.n_cq,'CQ zones'));
  cs.push(card(m.n_itu,'ITU zones'));
  if(m.points!=null) cs.push(card(m.points,'Points ('+m.points_per_q+'/Q)'));
  const sw=m.solar;
  if(sw && sw.cond_qsos){
    if(sw.sfi_avg!=null) cs.push(card(sw.sfi_avg,'Avg SFI ('+sw.sfi_min+'–'+sw.sfi_max+')'));
    if(sw.ssn_avg!=null) cs.push(card(sw.ssn_avg,'Avg sunspot # ('+sw.ssn_min+'–'+sw.ssn_max+')'));
    if(sw.a_avg!=null) cs.push(card(sw.a_avg,'Avg A-index (max '+sw.a_max+')'));
    if(sw.k_avg!=null) cs.push(card(sw.k_avg,'Avg K-index (max '+sw.k_max+')'));
  }
  if(eu) cs.push(card(fmtmd(eu.first.dt),'First EU: '+esc(eu.first.call),true));
  if(eu) cs.push(card(fmtmd(eu.last.dt),'Last EU: '+esc(eu.last.call),true));
  $('#cards').innerHTML=cs.join('');

  const sav=sw&&sw.available;
  const unm=sav?(m.total-sw.cond_qsos):0;
  $('#solarNote').innerHTML=sav
    ? ('Space weather: GFZ Potsdam · '+sw.cond_qsos+'/'+m.total+' QSOs matched · data '+sw.earliest+' → '+sw.latest
        +(unm>0?' <span class=err>· '+unm+' QSO(s) outside the data window — click refresh to download up-to-date values</span>':''))
    : 'Space-weather data unavailable — click refresh to download from GFZ.';

  drawTimeline(d);

  const hours=d.hours.map(h=>String(h.hour).padStart(2,'0')+'Z');
  const dsets=d.bandOrder.map(b=>({label:b,data:d.hours.map(h=>h.bands[b]||0),backgroundColor:d.bandColors[b]}));
  chart('#hourChart',{type:'bar',data:{labels:hours,datasets:dsets},
    options:{plugins:{legend:{labels:{color:INK,boxWidth:12}}},
      scales:{x:{...AX.x,stacked:true},y:{...AX.y,stacked:true}}}});

  chart('#bandChart',{type:'bar',data:{labels:d.bands.map(b=>b.band),
    datasets:[{data:d.bands.map(b=>b.count),backgroundColor:d.bands.map(b=>d.bandColors[b.band])}]},
    options:{indexAxis:'y',plugins:{legend:{display:false}},scales:AX}});

  chart('#contChart',{type:'doughnut',data:{labels:d.continents.map(c=>c.name),
    datasets:[{data:d.continents.map(c=>c.count),backgroundColor:d.continents.map(c=>c.color)}]},
    options:{plugins:{legend:{position:'right',labels:{color:INK,boxWidth:12}}}}});
  $('#srcNote').textContent='· '+m.src_pdf+' DXCC'+(m.src_itu?', '+m.src_itu+' ITU':'')+
    (m.src_log?', '+m.src_log+' logger':'')+(m.cont_unknown?', '+m.cont_unknown+' unknown':'');

  $('#contTable tbody').innerHTML=d.continents.map(c=>{
    const cell=x=>`${fmt(x.dt)} &nbsp; ${esc(x.call)} <span class=note>(${esc(x.band)}${x.entity?' · '+esc(x.entity):''})</span>`;
    return `<tr><td><span class=swatch style="background:${c.color}"></span><b>${c.name}</b></td>`+
      `<td class=n>${c.count}</td><td class=n>${c.pct}%</td><td>${cell(c.first)}</td><td>${cell(c.last)}</td></tr>`;
  }).join('');

  const zbar=(id,arr,lbl)=>chart(id,{type:'bar',data:{labels:arr.map(z=>lbl+z.zone),
    datasets:[{data:arr.map(z=>z.count),backgroundColor:'#2196F3'}]},
    options:{plugins:{legend:{display:false}},scales:AX}});
  zbar('#cqChart',d.cq,'CQ ');
  zbar('#ituChart',d.itu,'ITU ');

  if(d.runsp && d.runsp.length){ $('#runspPanel').classList.remove('hide');
    chart('#runspChart',{type:'doughnut',data:{labels:d.runsp.map(r=>r.kind),
      datasets:[{data:d.runsp.map(r=>r.count),backgroundColor:['#4CAF50','#FF9800']}]},
      options:{plugins:{legend:{position:'right',labels:{color:INK,boxWidth:12}}}}});
  } else $('#runspPanel').classList.add('hide');

  chart('#modeChart',{type:'doughnut',data:{labels:d.modes.map(x=>x.mode),
    datasets:[{data:d.modes.map(x=>x.count),backgroundColor:['#2196F3','#E91E63','#FFD54F','#4CAF50','#00BCD4','#9370DB']}]},
    options:{plugins:{legend:{position:'right',labels:{color:INK,boxWidth:12}}}}});

  if(d.operators && d.operators.length>1){
    $('#opsTabBtn').classList.remove('hide');     // reveal the Multi-operator tab
    const ops=d.operators, bands=d.bandOrder;
    // stacked QSOs-per-operator by band
    chart('#opChart',{type:'bar',data:{labels:ops.map(o=>o.op),
      datasets:bands.map(b=>({label:b,data:ops.map(o=>o.bands[b]||0),backgroundColor:d.bandColors[b]}))},
      options:{plugins:{legend:{labels:{color:INK,boxWidth:12}}},
        scales:{x:{...AX.x,stacked:true},y:{...AX.y,stacked:true,title:{display:true,text:'QSOs',color:MUT}}}}});
    // detail table
    $('#opTable tbody').innerHTML=ops.map(o=>
      `<tr><td><b>${esc(o.op)}</b></td><td class=n>${o.count}</td><td class=n>${o.unique}</td><td class=n>${o.ndxcc}</td>`+
      `<td class=n>${o.qph}</td><td class=n>${o.best10}</td><td class=n>${o.best60}</td><td class=n>${o.on_h}h</td>`+
      `<td>${o.span}</td><td>${fmtmd(o.first)}</td><td>${fmtmd(o.last)}</td></tr>`).join('');
    // operator × band matrix (+ totals)
    $('#opMatrix thead').innerHTML='<tr><th class=op>Operator</th>'+
      bands.map(b=>`<th>${b}</th>`).join('')+'<th class=tot>Total</th></tr>';
    let mb=ops.map(o=>`<tr><td class=op>${esc(o.op)}</td>`+
      bands.map(b=>{const v=o.bands[b]||0; return `<td class="${v?'':'z'}">${v||'·'}</td>`;}).join('')+
      `<td class=tot>${o.count}</td></tr>`).join('');
    mb+='<tr><td class=op>All ops</td>'+bands.map(b=>{const t=ops.reduce((s,o)=>s+(o.bands[b]||0),0);
      return `<td class=tot>${t||'·'}</td>`;}).join('')+`<td class=tot>${ops.reduce((s,o)=>s+o.count,0)}</td></tr>`;
    $('#opMatrix tbody').innerHTML=mb;
    // leaderboard — operators ranked per metric
    const metrics=[['Top QSO count',o=>o.count,''],['Top hours-on',o=>o.on_h,'h'],
      ['Top 10-min rate',o=>o.best10,''],['Top 1-hour rate',o=>o.best60,''],['Top DXCCs',o=>o.ndxcc,'']];
    $('#lbGrid').innerHTML=metrics.map(([title,fn,suf])=>{
      const sorted=[...ops].sort((a,b)=>fn(b)-fn(a)).slice(0,10);
      return `<div class=lbcol><h3>${title}</h3><ol>`+
        sorted.map(o=>`<li>${esc(o.op)} <b>${fn(o)}${suf}</b></li>`).join('')+'</ol></div>';
    }).join('');
  } else {
    $('#opsTabBtn').classList.add('hide');        // single-op: no Multi-operator tab
    if(!$('#tabOps').classList.contains('hide')) switchTab('tabOverall');
  }

  drawDxcc(d);

  // Maps. Default both map centers to the home station if not set yet.
  if(m.home){
    if(!$('#azCenter').value) $('#azCenter').value=m.home.lat+','+m.home.lon;
    if(!$('#worldCenter').value) $('#worldCenter').value=m.home.lat+','+m.home.lon;
  }
  $('#azNote').textContent=m.home?('default: '+m.home.call+' ('+m.home.entity+')'):'';
  drawWorld(d);
  drawAz(d);

  // QSO directions: populate band/mode pickers, default center to home.
  if(m.home && !$('#dirCenter').value) $('#dirCenter').value=m.home.lat+','+m.home.lon;
  $('#dirBand').innerHTML='<option value="*">All</option>'+
    d.bandOrder.map(b=>`<option value="${b}">${b}</option>`).join('');
  $('#dirMode').innerHTML='<option value="*">All</option>'+
    d.modes.map(x=>`<option value="${x.mode}">${x.mode}</option>`).join('');
  drawDirections(d);

  // Space weather vs activity (per day): QSO bars + SFI/SSN/A lines.
  const dl=d.days.map(x=>x.date.slice(5)), sw2=m.solar;
  if(sw2 && sw2.available && d.days.some(x=>x.sfi!=null)){
    $('#swPanel').classList.remove('hide');
    chart('#swChart',{data:{labels:dl,datasets:[
      {type:'bar',label:'QSOs',data:d.days.map(x=>x.count),backgroundColor:'#2b3e5c',yAxisID:'yq',order:3},
      {type:'line',label:'SFI',data:d.days.map(x=>x.sfi),borderColor:'#FFD54F',backgroundColor:'#FFD54F',borderWidth:2,pointRadius:0,tension:.25,yAxisID:'yf',order:0},
      {type:'line',label:'Sunspot #',data:d.days.map(x=>x.ssn),borderColor:'#4CAF50',backgroundColor:'#4CAF50',borderWidth:2,pointRadius:0,tension:.25,yAxisID:'yf',order:1},
      {type:'line',label:'A-index',data:d.days.map(x=>x.a),borderColor:'#FF6E6E',backgroundColor:'#FF6E6E',borderWidth:2,borderDash:[4,3],pointRadius:0,tension:.25,yAxisID:'ya',order:2}]},
      options:{interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{color:INK,boxWidth:12}}},
        scales:{x:{grid,ticks:tick},
          yq:{position:'left',grid,ticks:tick,beginAtZero:true,title:{display:true,text:'QSOs',color:MUT}},
          yf:{position:'right',grid:{drawOnChartArea:false},ticks:tick,beginAtZero:true,title:{display:true,text:'SFI / sunspot',color:MUT}},
          ya:{position:'right',grid:{drawOnChartArea:false},ticks:tick,beginAtZero:true,title:{display:true,text:'A',color:'#FF6E6E'}}}}});
  } else $('#swPanel').classList.add('hide');

  // QSOs by K-index.
  if(sw2 && sw2.available && d.kdist && d.kdist.some(x=>x.count)){
    $('#kPanel').classList.remove('hide');
    const kcol=k=>k<=1?'#4CAF50':k<=3?'#FFD54F':k<=4?'#FF9800':'#FF6E6E';
    chart('#kChart',{type:'bar',data:{labels:d.kdist.map(x=>'K'+x.k),
      datasets:[{data:d.kdist.map(x=>x.count),backgroundColor:d.kdist.map(x=>kcol(x.k))}]},
      options:{plugins:{legend:{display:false}},scales:AX}});
  } else $('#kPanel').classList.add('hide');

  chart('#dayChart',{type:'bar',data:{labels:dl,
    datasets:[{data:d.days.map(x=>x.count),backgroundColor:'#2196F3'}]},
    options:{plugins:{legend:{display:false}},scales:AX}});
}

// ---- light / dark theme ----
function applyTheme(light){
  document.documentElement.classList.toggle('light', light);
  try{ localStorage.setItem('logan-theme', light?'light':'dark'); }catch(e){}
  $('#themeBtn').textContent = light ? '☾ Dark' : '☀︎ Light';
  readTheme();
  if(D) render(D);
}
(function(){
  let light=false;
  try{ light = localStorage.getItem('logan-theme')==='light'; }catch(e){}
  applyTheme(light);
  $('#themeBtn').onclick=()=>applyTheme(!document.documentElement.classList.contains('light'));
})();

// ---- tabs ----
function switchTab(id){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('hide', t.id!==id));
  document.querySelectorAll('.tabbtn').forEach(b=>b.classList.toggle('active', b.dataset.tab===id));
}
document.querySelectorAll('.tabbtn').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
</script>
</body></html>"""




if __name__ == "__main__":
    sys.exit(main())
