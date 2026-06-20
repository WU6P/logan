# logan — ham radio log analyzer

A web-based analyzer for ADIF (`.adi`/`.adif`) amateur-radio logs. Drop one or
more logs onto the page and logan reports your operating activity: rate over
time, first/last contact per continent, band split, CQ/ITU zones, DXCC
countries worked, and more.

Pure Python standard library — **no third-party dependencies** to run.

## Run

```bash
python3 logan.py                 # serves http://127.0.0.1:8765 and opens it
python3 logan.py --port 9000     # pick a port
python3 logan.py --no-browser    # don't auto-open a browser
```

Then drag your ADIF file(s) onto the page. Everything is processed locally; no
data leaves your machine. A **light / dark theme** toggle sits in the top-right
corner (your choice is remembered).

The dashboard has two tabs: **Overall** (everything below) and **Multi-operator**
(per-operator analysis), the latter shown only when the log has 2+ operators.

## What it reports

- **Summary cards** — QSOs, unique calls, average rate, best rolling 60- and
  10-minute rates, active span, operating days, DXCC entities, continents,
  CQ/ITU zones, contest points, and the first & last European contact.
- **Contest timeline** (the multi-info chart) — QSOs per hour as bars, the
  running cumulative total as a line, with the bars optionally broken down by
  band or by continent.
- **Hourly rate** by UTC hour-of-day, stacked by band.
- **Band split** with each band's share and first/last QSO.
- **Continents** doughnut plus a first/last-contact-per-continent table.
- **CQ zones** and **ITU zones** bar charts.
- **Run vs S&P** and **modes** (shown when the log carries those fields).
- **Per-operator** breakdown (multi-op logs) — QSOs, unique calls, DXCCs, rate,
  best 10- and 60-minute runs, hours-on (active operating time), active span and
  first/last, a stacked QSOs-per-operator-by-band chart, an **operator × band
  matrix**, and an **operator leaderboard** (operators ranked by QSO count,
  hours-on, best 10-min rate, best 1-hour rate, and DXCCs worked).
- **DXCC entities (countries)** table with first/last per country. Rarest /
  most-wanted entities (e.g. P5 North Korea) are **flagged ⚠ with their rank** —
  a quick way to spot a busted callsign — and always shown even beyond the
  top-N cut.
- **World map** — every worked DXCC entity plotted as a bubble (size = QSO
  count, colour = continent), with a **custom center longitude** (recenter on
  any grid/`lat,lon`, e.g. to put your region in the middle instead of
  Greenwich) and a choice of projection (Natural Earth / Equirectangular /
  Mercator).
- **QSO directions (beam headings)** — the great-circle bearing from your
  station to each entity, binned into **5° slots** (72 of them) and drawn as a
  polar wind-rose: a **summary rose** over all hours, plus **per-hour polar
  heat-maps** (rings = UTC hour-of-day 00→23, colour = QSO count) so you can see
  where you were beaming through the period. A single-day log shows one heat-map;
  a **two-day (48 h) contest shows Day 1, Day 2 and Combined** (three); longer
  logs fold all days into one. Pick a **center** (your grid /
  `lat,lon`) and filter by **band** and **mode**. North = 0° at the top,
  clockwise; the 0–5° slot sits at the top, etc. **US/Canada** QSOs would all
  share one country-centroid bearing, so they're placed at a **call-area
  regional centroid** (W6→California, W1→New England, VE7→BC …) for a roughly
  correct heading; an *include US/Canada* toggle lets you drop them entirely.
  An optional **distance box plot** (toggle) summarises great-circle QSO
  distance per band (box = 25–75 %, median line, 1.5×IQR whiskers, outlier dots)
  — handy for seeing which bands carried DX versus local. An optional
  **direction × hour table** (toggle) lists every 5° heading with its first and
  last QSO time and a colour-coded count for each UTC hour-of-day (a Cartesian
  heat-map of when each direction was active).
- **Azimuthal map** — a great-circle (azimuthal-equidistant) map centred on a
  location you choose (Maidenhead grid like `CM87`, or `lat,lon`), defaulting to
  your logged station. Concentric rings mark 5/10/15/20 k km; optional
  great-circle lines run from the centre to each entity; hover for distance.
- **Space weather vs activity** — for each QSO logan looks up the solar &
  geomagnetic conditions *at that moment*: SFI (10.7 cm flux), sunspot number,
  the daily A-index, and the 3-hourly K-index. You get average/range cards, a
  per-day chart overlaying QSO counts with SFI / sunspot / A-index lines, a
  "QSOs by K-index" chart (how disturbed the bands were when you worked), and an
  optional **K-index overlay** on the contest-timeline chart.
- **Per-day totals.**

## Configurable

A controls panel lets you:

- filter by **band** and by **continent** (recomputes every statistic),
- switch the timeline breakdown (band / continent / total) and toggle the
  cumulative line,
- choose how many DXCC rows to show.

Filtering recomputes server-side from the already-uploaded log (cached by
session), so you don't re-drop the file.

## Where the country / zone data comes from

Each callsign's continent, ITU zone, CQ zone and DXCC entity are derived from
its prefix, using **three sources in priority order**:

1. **ARRL DXCC list** (`doc/2022_DXCC_Current.pdf` → `dxcc.json`) — full data
   (entity, continent, ITU & CQ zone, coordinates).
2. **ITU international call-sign-series table** (`doc/ITZ Callsign.pdf` →
   `itu.json`) — maps a prefix block to a country; continent and coordinates are
   borrowed from cty.dat. Catches prefixes the DXCC list doesn't file directly
   (e.g. `3G`=Chile, `XM`=Canada, `DS`=Korea).
3. The **logger's own `CONTINENT` field** — last resort.

The first match wins, so DXCC/ITU prefix data **overrides** the logger's field
when they disagree. Where the DXCC list gives a multi-zone range (e.g. the USA
spans CQ zones 3–5), logan uses the logger's per-QSO `CQZ`/`ITUZ` to pin the
exact zone when available.

Validated against a real 1,557-QSO log: prefix-derived continent agrees with the
logger's field on **98.8%** of QSOs; the rest are genuinely ambiguous DX /
portable cases that the override rule is intended to win.

Map coordinates (the ARRL list has none) come from **AD1C's `cty.dat`**
(`doc/cty.dat`); each entity gets a representative lat/lon. The two map panels
render with **d3-geo** and a world outline, both loaded from a CDN — so, like
the charts, the maps need an internet connection (your log data never leaves
your machine).

## Files

| file | purpose |
|------|---------|
| `logan.py` | the app: ADIF parser, DXCC resolver, analysis, web server + UI |
| `dxcc.json` | prefix → entity/continent/ITU/CQ lookup (committed; loaded at runtime) |
| `build_dxcc.py` | regenerates `dxcc.json` from the ARRL list |
| `itu.json` | ITU call-sign-series → country/continent (2nd-priority source) |
| `build_itu.py` | regenerates `itu.json` from `doc/ITZ Callsign.pdf` |
| `rare.json` | rarest/most-wanted DXCC entities by code → rank (bust flagging) |
| `build_rare.py` | regenerates `rare.json` (curated most-wanted list) |
| `solar.py` | space-weather lookup (SFI/SSN/A/K) from the GFZ data file |
| `data/kp_ap_f107.txt` | GFZ Kp/ap/Ap/SN/F10.7 snapshot (since 2000); refreshable in-app |
| `doc/cty.dat` | AD1C country file — source of per-entity lat/lon for the maps |
| `test_logan.py` | test suite (`python3 test_logan.py`) |
| `doc/` | the ARRL DXCC PDF, its extracted text, and sample logs |

## Space-weather data

Solar/geomagnetic values come from GFZ Potsdam (the authoritative source for
Kp/ap/Ap, the international sunspot number, and F10.7). The K-index is matched to
each QSO's 3-hour UT block, so the time-of-day of a contact picks the right
geomagnetic value.

**Loading new logs / refreshing.** A snapshot since 2000 is bundled in `data/`.
The **Refresh space-weather data** button (or `POST /solar/refresh`) normally
downloads only GFZ's small **nowcast** file — about **8 KB**, covering the last
~30 days — and merges it in, extending coverage through *today*. So if you
operate and log tomorrow, one click pulls ~8 KB and the new QSOs get their
conditions. Values for the current UT day fill in through the day as GFZ posts
them (later K-index blocks appear only after those hours have elapsed).

**Big gaps are handled automatically.** The nowcast can only *extend* an
already-current snapshot. If the app sat unused so long that the local data ends
more than a day before the nowcast window (e.g. you skip a couple of months),
merging the nowcast would leave a hole — so `update()` detects that and pulls
the **full archive** (since 1932, ~5.5 MB) instead, keeping coverage continuous.
You always get correct data; the only cost is a one-time larger download after a
long absence. (`solar.update(full=True)` forces the archive.) The UI also flags
when a log contains QSOs newer than the current data window.

## Regenerating the DXCC data

```bash
python3 build_dxcc.py     # reads doc/dxcc_raw.txt (or the PDF via pypdf) -> dxcc.json
```

`build_dxcc.py` parses from `doc/dxcc_raw.txt` (the extracted PDF text, already
committed). To re-extract from a newer PDF, install `pypdf` and delete
`dxcc_raw.txt` so the script reads the PDF directly.

## Tests

```bash
python3 test_logan.py
```
