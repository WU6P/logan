# logan (web)

The same ham-radio log analyzer as the desktop `logan.py`, but **100 %
client-side** — it runs in the browser with no server and no install, so it can
be hosted for free on **GitHub Pages**. Your log file is read locally and never
leaves the browser.

It's a JavaScript port of the Python engine: `logcore.js` mirrors the analysis
core of `logan.py` (ADIF/Cabrillo parsing, ARRL-DXCC + ITU prefix resolution,
the call-area overrides, space-weather lookup, `analyze()` and the CBS-style
report), and `test_logcore.mjs` checks it against the Python engine so the two
stay in step — the parity test confirms **byte-identical** output (full report
JSON *and* the CBS text) on the 6592-QSO N6RO fixture.

## What it does

Drop one or more `.adi` / `.adif` / `.log` (Cabrillo) files on the page and it
reports, entirely in your browser:

* headline cards — QSOs, unique calls, average & best 60/10-minute rates, active
  span, DXCC/continent/zone counts, points, first/last EU, and QSO-weighted
  space-weather (SFI / sunspot / A / K);
* a **contest timeline** (per-hour bars + cumulative line + band/continent
  breakdown + optional K-index overlay), hourly-by-UTC and per-day charts;
* band split, continent doughnut with first/last-per-continent table, CQ/ITU
  zone bars, Run-vs-S&P, modes;
* the **DXCC entity** table with rare / most-wanted flagging (a stray P5 in a
  domestic contest is almost always a busted call);
* **maps** — a world bubble map and an azimuthal-equidistant great-circle map
  centered on your station (Maidenhead grid or lat,lon);
* **beam-heading direction** analysis — 5° wind-rose, per-UTC-hour heat rings, a
  distance box-plot by band, and a direction × hour activity table;
* **space-weather vs activity** and QSOs-by-K-index charts;
* a **CBS-style text report** (format after "Cabrillo Statistics" by K5KA &
  N6TV) with a one-click `.txt` download;
* filters (band / continent) that recompute instantly with no re-upload, plus a
  light / dark theme.

Multi-operator logs get an extra **Multi-operator** tab: per-op breakdown,
operator × band matrix, and a leaderboard.

## Notes on the static build

* **Charts & maps need internet.** Chart.js, d3-geo, topojson and the
  world-atlas land outline load from a CDN (jsDelivr / unpkg), exactly as the
  desktop version did. The analysis itself is fully local.
* **Space weather is a bundled snapshot** (`solar.json`, GFZ Potsdam, 2000 →
  mid-2026). The desktop app could refresh it live from GFZ; a browser can't
  (CORS), so the web build ships the snapshot and QSOs newer than the data
  window are flagged. Regenerate it from `data/kp_ap_f107.txt` if you want a
  newer cut.

## Run locally

ES modules and `fetch()` need a real HTTP origin (opening `index.html` from
`file://` is blocked by the browser), so serve the folder:

```sh
cd docs
python3 -m http.server 8000
# open http://localhost:8000/
```

There's an in-browser self-test that loads the real tables and checks the
engine: `http://localhost:8000/_selftest.html`.

Run the core unit + parity tests under Node (no browser, no dependencies):

```sh
node test_logcore.mjs        # unit tests + full parity vs logan.py (if present)
```

## Deploy to GitHub Pages

1. Push this repo to GitHub. This `docs/` folder already holds everything Pages
   needs: `index.html`, `app.js`, `logcore.js`, `styles.css`, and the data files
   `dxcc.json` / `itu.json` / `rare.json` / `solar.json`.
2. Repo **Settings → Pages → Build and deployment → Deploy from a branch**.
3. Pick your branch (e.g. `main`) and the **/docs** folder, then **Save**.
4. The app appears at `https://<user>.github.io/<repo>/` in a minute or two.

`_selftest.html`, `test_logcore.mjs` and `package.json` are dev-only; harmless
to publish, or delete them from the published copy.

## Files

| file | purpose |
|------|---------|
| `index.html` / `styles.css` | page + styling |
| `app.js`            | DOM glue: file load, charts, maps, direction analysis, filters, theme |
| `logcore.js`        | parsing + DXCC/ITU/rare resolution + space weather + `analyze()` + CBS report (port of `logan.py`) |
| `test_logcore.mjs`  | Node unit tests + byte-for-byte parity vs the Python engine |
| `_selftest.html`    | in-browser end-to-end smoke test (PASS/FAIL) |
| `dxcc/itu/rare.json`| lookup tables |
| `solar.json`        | bundled GFZ space-weather snapshot |
