#!/usr/bin/env python3
"""Solar / geomagnetic conditions lookup for logan.

Source: GFZ Potsdam combined file `Kp_ap_Ap_SN_F107_since_1932.txt`, which gives
per UT day the 10.7 cm solar flux (SFI = F10.7), the international sunspot
number (SN), the daily geomagnetic A index (Ap), and Kp in eight 3-hour blocks
(so a QSO's K index is matched to the actual time of day, not just the date).

A snapshot lives in data/kp_ap_f107.txt (since 2000). `update()` refreshes it
from GFZ when the network is available; logan works offline from the snapshot.

File columns (whitespace separated, missing = -1):
  YYYY MM DD days days_m Bsr dB  Kp1..Kp8  ap1..ap8  Ap  SN  F10.7obs F10.7adj D
"""

import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "kp_ap_f107.txt"
# Two GFZ files: the full archive (since 1932, ~5.5 MB) and a small rolling
# "nowcast" of roughly the last 30 days (~8 KB, refreshed several times a day).
URL_FULL = "https://kp.gfz.de/app/files/Kp_ap_Ap_SN_F107_since_1932.txt"
URL_NOWCAST = "https://kp.gfz.de/app/files/Kp_ap_Ap_SN_F107_nowcast.txt"

# Kp block i covers UT hours [3i, 3i+3); a QSO's block is hour // 3.
_cache = None


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "logan/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _row_date(line):
    t = line.split()
    if len(t) < 27 or line.startswith("#"):
        return None
    try:
        return f"{int(t[0]):04d}-{int(t[1]):02d}-{int(t[2]):02d}"
    except ValueError:
        return None


def _pd(s):
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def update(full=False, timeout=40):
    """Refresh the local snapshot from GFZ. Returns (ok, message).

    Normally downloads only the small nowcast file (~8 KB, last ~30 days) and
    merges it in — a tiny routine refresh. But the nowcast can only EXTEND an
    already-current snapshot: if the local data ends more than a day before the
    nowcast window starts (e.g. the app sat unused for two months), merging
    would leave a gap, so the full archive (~5.5 MB) is pulled instead to keep
    coverage continuous. full=True forces the archive."""
    global _cache
    try:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        now = _fetch(URL_NOWCAST, timeout)
        new_rows = [ln for ln in now.splitlines() if _row_date(ln)]
        if not new_rows:
            return False, "no rows in nowcast file"
        cutoff = min(_row_date(ln) for ln in new_rows)
        latest = max(_row_date(ln) for ln in new_rows)

        old = (DATA.read_text(encoding="utf-8", errors="replace")
               if DATA.exists() else "")
        old_dates = [d for ln in old.splitlines() if (d := _row_date(ln))]
        existing_latest = max(old_dates) if old_dates else None
        gap = (existing_latest is None
               or _pd(existing_latest) < _pd(cutoff) - timedelta(days=1))

        note = "nowcast merged"
        if full or gap:
            # Need the full archive as the base so there's no missing stretch.
            arch = _fetch(URL_FULL, timeout)
            if "F10.7" not in arch or len(arch) < 10000:
                return False, "unexpected response from GFZ"
            old = arch
            note = "full archive + nowcast"

        # Keep header + archived rows before the nowcast window, then append
        # the fresh nowcast rows (which carry coverage through today).
        kept = [ln for ln in old.splitlines()
                if (d := _row_date(ln)) is None or d < cutoff]
        DATA.write_text("\n".join(kept + new_rows) + "\n", encoding="utf-8")
        _cache = None
        return True, f"{note} → {latest}"
    except Exception as e:
        return False, f"update failed: {e}"


def _parse():
    """Parse the data file into {date: record}. Cached after first call."""
    global _cache
    if _cache is not None:
        return _cache
    by_date = {}
    if DATA.exists():
        for line in DATA.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith("#"):
                continue
            t = line.split()
            if len(t) < 27:
                continue
            try:
                y, mo, d = int(t[0]), int(t[1]), int(t[2])
            except ValueError:
                continue
            kp = [float(x) for x in t[7:15]]
            ap = [int(x) for x in t[15:23]]
            ApV, SN = int(t[23]), int(t[24])
            f_obs, f_adj = float(t[25]), float(t[26])
            by_date[date(y, mo, d)] = {
                "kp": [None if v < 0 else v for v in kp],
                "ap": [None if v < 0 else v for v in ap],
                "a": None if ApV < 0 else ApV,
                "ssn": None if SN < 0 else SN,
                "sfi": None if f_obs < 0 else f_obs,
                "sfi_adj": None if f_adj < 0 else f_adj,
                "definitive": t[27] == "1" if len(t) > 27 else False,
            }
    _cache = by_date
    return by_date


def available():
    """Return (has_data, earliest, latest) for the loaded snapshot."""
    data = _parse()
    if not data:
        return False, None, None
    keys = sorted(data)
    return True, keys[0].isoformat(), keys[-1].isoformat()


def conditions_at(dt):
    """SFI / SSN / A / K (and the 3-hour Kp block) for a UTC datetime, or None.

    Returns a dict {sfi, ssn, a, k, kp_block, definitive} — any field may be
    None if that value is missing for the day."""
    rec = _parse().get(dt.date())
    if rec is None:
        return None
    block = dt.hour // 3
    return {
        "sfi": rec["sfi"], "sfi_adj": rec["sfi_adj"], "ssn": rec["ssn"],
        "a": rec["a"], "k": rec["kp"][block], "kp_block": block,
        "definitive": rec["definitive"],
    }


def day_record(d):
    """The daily record for a date (datetime.date), or None."""
    return _parse().get(d)
