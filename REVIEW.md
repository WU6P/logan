# logan — deep review (fresh eyes)

Date: 2026-06-18. Reviewed `logan.py`, `solar.py`, `build_dxcc.py`, `test_logan.py`
end to end. Findings below, ordered by severity. Status updated as fixed.

## 1. [BUG — important] QSO times displayed in the wrong timezone  ✅ fixed
`fmt`/`fmtmd` do `new Date(isoString)` on the server's **naive UTC** strings
(e.g. `"2026-02-04T20:15:00"`, no `Z`). Per the JS spec a date-*time* string with
no zone is parsed as **local** time, then `.toISOString()` re-expresses it in
UTC — so every displayed timestamp (first/last EU, per-continent first/last,
DXCC first/last, best-run start times, header span) is shifted by the browser's
UTC offset. Only correct for a browser already in UTC.
**Fix:** treat the string as UTC (append `Z` before parsing).

## 2. [BUG] Mercator world-map projection renders blank/degenerate  ✅ fixed
`drawWorld` calls `proj.fitExtent([...], {type:'Sphere'})`. For `geoMercator`
the poles map to ±∞, so the sphere's projected bounds are infinite and the
fitted scale collapses → blank or broken map.
**Fix:** drop the Mercator option (area-distorting and the worst fit for a world
QSO map anyway); keep Natural Earth + Equirectangular, both of which fit a
sphere cleanly.

## 3. [IMPROVEMENT] Default map/direction center is the country centroid  ✅ fixed
`home` resolves the station callsign to its DXCC entity centroid — for a US
station that's the geographic middle of the country (37.6, -91.87), so the
default azimuthal/direction center is wrong (Japan comes out ~322° instead of
the ~305° you'd get from California). We already have `refine_domestic()` that
maps a US/Canada call to its call-area centroid.
**Fix:** run the home station through `refine_domestic()` so the default center
is the call-area centroid (WU6P → area 6 → California).

## 4. [ROBUSTNESS] `--port` with no value crashes  ✅ fixed
`main()` indexes `argv[i + 1]` and `int()`s it with no guard; `python3 logan.py
--port` raises IndexError, `--port abc` raises ValueError with a raw traceback.
**Fix:** validate and print a friendly message.

## 5. [MINOR / self-XSS] Unescaped log fields in innerHTML  ✅ fixed
Tables/cards interpolate log-derived `call`/`operator` into `innerHTML` without
escaping. Low severity (a local server over the user's own files — any injection
is self-inflicted), but a crafted ADIF callsign could inject markup.
**Fix:** add an `esc()` helper and use it for call/operator fields.

## 6. [DOC] Stale module docstring  ✅ fixed
`logan.py`'s docstring lists only the original features (no space weather, maps,
directions, distance box plot). Updated to match what the tool now does.

## 7. [CLEANUP] Stale comments / dead assignment  ✅ fixed
- `dir_qsos` comment said `{h, band, mode, lat, lon}` (now also `d`, `dom`).
- `kb = None` then reassigned only inside the guarded block — redundant.

## Checked and found OK
- ADIF parser (tag-in-value safety, missing header/EOR, length handling).
- DXCC prefix resolution and the PDF-overrides-logger rule (98.8% validated).
- Solar refresh: nowcast-vs-full gap detection is correct.
- Rolling best-rate window (two-pointer) is correct.
- Server binds to 127.0.0.1 only; bounded session cache; never 500s (errors
  returned as JSON). `classify` memo mutates the cached QSO dicts but the result
  is filter-independent, so refilter reuse is safe.
- Maidenhead and lat,lon center parsing; bearing/great-circle math (Japan from
  California = 305°, matches expectation).
