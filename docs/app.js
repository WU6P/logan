// app.js — logan front-end (client-side, GitHub-Pages build).
//
// Ported from logan.py's PAGE string. All the Chart.js / d3-geo / canvas drawing
// is unchanged; the only difference from the server version is the data layer:
// the analysis engine (logcore.js) runs here in the browser, so there is no
// /analyze, /refilter or /solar/refresh round-trip. The parsed QSOs stay in
// memory so band/continent filters recompute instantly with no re-upload.

import { setData, analyzeUpload, analyze } from './logcore.js?v=1';

const $ = s => document.querySelector(s), drop = $('#drop'), fileIn = $('#file');
function cssv(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
let INK = '', MUT = '', TH = {};
let grid = { color: '#222c3a' }, tick = { color: MUT };
function readTheme() {
  INK = cssv('--ink'); MUT = cssv('--mut');
  grid.color = cssv('--grid'); tick.color = MUT;
  TH = { canvas: cssv('--canvas'), grid: cssv('--grid'), coast: cssv('--coast'),
    land: cssv('--land'), ocean: cssv('--ocean'), gratic: cssv('--gratic') };
  HEAT0 = hexToRgb(TH.canvas);
}
const AX = { x: { grid, ticks: tick }, y: { grid, ticks: tick, beginAtZero: true } };
let charts = {}, D = null;
let QSOS = [], SOURCES = [];          // parsed log, kept for instant refilter
function chart(id, cfg) { if (charts[id]) charts[id].destroy(); charts[id] = new Chart($(id), cfg); }
// Engine emits naive UTC ISO strings; force UTC parsing so the display isn't
// shifted by the browser's local timezone.
const asUTC = s => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z');
const fmt = s => asUTC(s).toISOString().slice(0, 16).replace('T', ' ') + 'Z';
const fmtmd = s => asUTC(s).toISOString().slice(5, 16).replace('T', ' ') + 'Z';
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---- load the lookup tables, then enable the drop zone ----
let READY = false;
async function boot() {
  $('#msg').textContent = 'Loading lookup tables…'; $('#msg').className = 'note';
  try {
    const [dxcc, itu, rare, solar] = await Promise.all(
      ['dxcc', 'itu', 'rare', 'solar'].map(n => fetch(n + '.json').then(r => r.json())));
    setData({ dxcc, itu, rare, solar });
    READY = true;
    $('#msg').textContent = '';
  } catch (e) {
    $('#msg').textContent = 'Could not load lookup tables (' + e + '). Serve this folder over HTTP.';
    $('#msg').className = 'note err';
  }
}
boot();

drop.onclick = () => fileIn.click();
fileIn.onchange = () => upload(fileIn.files);
['dragenter', 'dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('hot'); }));
['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('hot'); }));
drop.addEventListener('drop', ev => upload(ev.dataTransfer.files));

async function upload(files) {
  if (!files || !files.length) return;
  if (!READY) { $('#msg').textContent = 'Still loading lookup tables — try again in a moment.'; $('#msg').className = 'note err'; return; }
  $('#msg').textContent = 'Reading ' + files.length + ' file(s)…'; $('#msg').className = 'note';
  const parts = [];
  for (const f of files) parts.push('NAME:' + f.name + '\n' + await f.text());
  runAnalyze(parts, true);
}
function refilter() {
  if (!QSOS.length) return;
  const opts = { bands: checked('#bandChips'), conts: checked('#contChips') };
  compute(analyze(QSOS, SOURCES, opts), false);
}
function runAnalyze(parts, isUpload) {
  $('#msg').textContent = 'Analyzing…'; $('#msg').className = 'note';
  try {
    const parsed = analyzeUpload(parts);
    QSOS = parsed.qsos; SOURCES = parsed.sources;
    compute(analyze(QSOS, SOURCES), isUpload);
  } catch (e) { $('#msg').textContent = 'Error: ' + e; $('#msg').className = 'note err'; console.error(e); }
}
function compute(d, isUpload) {
  D = d;
  if (isUpload) buildControls(d);
  render(d);
}
function checked(sel) { return [...document.querySelectorAll(sel + ' input:checked')].map(c => c.value); }

function buildControls(d) {
  const m = d.meta;
  $('#bandChips').innerHTML = m.all_bands.map(b =>
    `<label class=chip><input type=checkbox value="${b}" checked>${b}</label>`).join('');
  $('#contChips').innerHTML = m.all_conts.map(c =>
    `<label class=chip><input type=checkbox value="${c}" checked>${c}</label>`).join('');
  document.querySelectorAll('#bandChips input,#contChips input').forEach(c => c.onchange = refilter);
  $('#tlMode').onchange = () => drawTimeline(D);
  $('#tlCum').onchange = () => drawTimeline(D);
  $('#tlK').onchange = () => drawTimeline(D);
  $('#topN').oninput = () => drawDxcc(D);
  $('#azGo').onclick = () => drawAz(D);
  $('#azCenter').addEventListener('keydown', e => { if (e.key === 'Enter') drawAz(D); });
  $('#azLines').onchange = () => drawAz(D);
  $('#worldGo').onclick = () => drawWorld(D);
  $('#worldCenter').addEventListener('keydown', e => { if (e.key === 'Enter') drawWorld(D); });
  $('#worldProj').onchange = () => drawWorld(D);
  $('#dirGo').onclick = () => drawDirections(D);
  $('#dirCenter').addEventListener('keydown', e => { if (e.key === 'Enter') drawDirections(D); });
  $('#dirBand').onchange = () => drawDirections(D);
  $('#dirMode').onchange = () => drawDirections(D);
  $('#dirDom').onchange = () => drawDirections(D);
  $('#dirBox').onchange = () => drawDirections(D);
  $('#dirTable').onchange = () => drawDirections(D);
  $('#reset').onclick = () => {
    document.querySelectorAll('#bandChips input,#contChips input').forEach(c => c.checked = true);
    refilter();
  };
}

function card(k, l, hl) { return `<div class="card${hl ? ' hl' : ''}"><div class=k>${k}</div><div class=l>${l}</div></div>`; }

function drawTimeline(d) {
  const t = d.timeline, mode = $('#tlMode').value, labels = t.map(x => x.label);
  let ds = [];
  if (mode === 'band') {
    ds = d.bandOrder.map(b => ({ label: b, data: t.map(x => x.bands[b] || 0), backgroundColor: d.bandColors[b], stack: 'q' }));
  } else if (mode === 'cont') {
    ds = d.continents.map(c => ({ label: c.name, data: t.map(x => x.conts[c.code] || 0), backgroundColor: c.color, stack: 'q' }));
  } else {
    ds = [{ label: 'QSOs', data: t.map(x => x.count), backgroundColor: '#2196F3', stack: 'q' }];
  }
  const scales = { x: { ...AX.x, stacked: true }, y: { ...AX.y, stacked: true, title: { display: true, text: 'QSOs / hour', color: MUT } } };
  if ($('#tlCum').checked) {
    ds.push({ type: 'line', label: 'Cumulative', data: t.map(x => x.cum), yAxisID: 'y1', borderColor: '#FFD54F',
      backgroundColor: '#FFD54F', borderWidth: 2, pointRadius: 0, tension: .25, order: -1 });
    scales.y1 = { position: 'right', grid: { drawOnChartArea: false }, ticks: tick, beginAtZero: true,
      title: { display: true, text: 'cumulative', color: MUT } };
  }
  if ($('#tlK').checked && t.some(x => x.k != null)) {
    ds.push({ type: 'line', label: 'K-index', data: t.map(x => x.k), yAxisID: 'yk', borderColor: '#FF6E6E',
      backgroundColor: '#FF6E6E', borderWidth: 2, borderDash: [4, 3], pointRadius: 0, tension: .2,
      spanGaps: true, order: -2 });
    scales.yk = { position: 'right', min: 0, max: 9, grid: { drawOnChartArea: false }, ticks: tick,
      title: { display: true, text: 'K-index', color: '#FF6E6E' } };
  }
  chart('#tlChart', { type: 'bar', data: { labels, datasets: ds },
    options: { responsive: true, plugins: { legend: { labels: { color: INK, boxWidth: 12 } } },
      interaction: { mode: 'index', intersect: false }, scales } });
}

function drawDxcc(d) {
  const n = Math.max(1, parseInt($('#topN').value) || 25);
  const rare = d.dxcc.filter(e => e.rank).sort((a, b) => a.rank - b.rank);
  const shown = d.dxcc.slice(0, n);
  const list = shown.concat(rare.filter(e => !shown.includes(e)));  // rare always shown
  $('#dxccNote').innerHTML = 'showing ' + shown.length + ' of ' + d.dxcc.length +
    (rare.length ? ' · <span class=warn>⚠ ' + rare.length + ' rare / most-wanted worked — verify the callsign(s)</span>' : '');
  $('#dxccTable tbody').innerHTML = list.map(e =>
    `<tr class="${e.rank ? 'rarerow' : ''}"><td>` +
      `${e.rank ? '<span class=rarebadge title="most-wanted #' + e.rank + ' — likely a busted call">⚠ #' + e.rank + '</span> ' : ''}${esc(e.entity)}</td>` +
    `<td><span class=swatch style="background:${e.color}"></span>${esc(e.cont)}</td>` +
    `<td class=n>${e.count}</td><td>${fmtmd(e.first.dt)} ${esc(e.first.call)}</td>` +
    `<td>${fmtmd(e.last.dt)} ${esc(e.last.call)}</td></tr>`).join('');
}

// ---- maps (d3-geo) ----
let WORLD = null;
async function world() {
  if (WORLD) return WORLD;
  const t = await (await fetch('https://cdn.jsdelivr.net/npm/world-atlas@2/land-110m.json')).json();
  WORLD = topojson.feature(t, t.objects.land);
  return WORLD;
}
function maiden(g) {
  g = g.trim().toUpperCase();
  if (!/^[A-R]{2}[0-9]{2}([A-X]{2})?$/.test(g)) return null;
  let lon = (g.charCodeAt(0) - 65) * 20 - 180, lat = (g.charCodeAt(1) - 65) * 10 - 90;
  lon += (+g[2]) * 2; lat += (+g[3]) * 1;
  if (g.length >= 6) { lon += (g.charCodeAt(4) - 65) * 5 / 60 + 2.5 / 60; lat += (g.charCodeAt(5) - 65) * 2.5 / 60 + 1.25 / 60; }
  else { lon += 1; lat += 0.5; }
  return [lon, lat];
}
function parseCenter(s) {
  if (!s) return null;
  s = s.trim();
  const m = s.match(/^(-?\d+(?:\.\d+)?)\s*[,/ ]\s*(-?\d+(?:\.\d+)?)$/);
  if (m) {
    const a = parseFloat(m[1]), b = parseFloat(m[2]);
    if (Math.abs(a) <= 90 && Math.abs(b) <= 180) return [b, a];          // lat,lon
    if (Math.abs(b) <= 90 && Math.abs(a) <= 180) return [a, b];          // tolerate lon,lat
    return null;
  }
  return maiden(s);   // Maidenhead grid: 4-char (CM87) or 6-char (CM87xi)
}
function gcKm(a, b) { // [lon,lat] great-circle km
  const R = 6371, r = Math.PI / 180, dl = (b[0] - a[0]) * r;
  const la1 = a[1] * r, la2 = b[1] * r;
  const c = Math.sin(la1) * Math.sin(la2) + Math.cos(la1) * Math.cos(la2) * Math.cos(dl);
  return R * Math.acos(Math.max(-1, Math.min(1, c)));
}
function mapsReady() { return typeof d3 !== 'undefined' && typeof topojson !== 'undefined'; }
async function drawWorld(d) {
  if (!mapsReady()) { $('#worldNote').textContent = '(map library unavailable — needs internet for d3)'; return; }
  const pts = d.dxcc.filter(e => e.lat != null);
  $('#worldNote').textContent = '(' + pts.length + ' of ' + d.dxcc.length + ' entities mapped · bubble = QSO count · color = continent)';
  const land = await world();
  const w = 960, h = 500, svg = d3.select('#worldMap'); svg.selectAll('*').remove();
  const c = parseCenter($('#worldCenter').value);
  const lon0 = c ? c[0] : 0;
  $('#worldCNote').textContent = c ? ('centred on ' + lon0.toFixed(0) + '° lon') : 'centred on 0° (Greenwich)';
  const kind = $('#worldProj').value;
  const proj = (kind === 'equirect' ? d3.geoEquirectangular() : d3.geoNaturalEarth1())
    .rotate([-lon0, 0]);
  proj.fitExtent([[6, 6], [w - 6, h - 6]], { type: 'Sphere' });
  const path = d3.geoPath(proj);
  svg.append('path').datum({ type: 'Sphere' }).attr('d', path).attr('fill', TH.ocean).attr('stroke', TH.coast);
  svg.append('path').datum(d3.geoGraticule10()).attr('d', path).attr('fill', 'none').attr('stroke', TH.gratic);
  svg.append('path').datum(land).attr('d', path).attr('fill', TH.land).attr('stroke', TH.coast);
  const rmax = Math.sqrt(Math.max(...pts.map(e => e.count), 1));
  svg.append('g').selectAll('circle').data(pts).join('circle')
    .attr('cx', e => proj([e.lon, e.lat])[0]).attr('cy', e => proj([e.lon, e.lat])[1])
    .attr('r', e => 3 + 10 * Math.sqrt(e.count) / rmax).attr('fill', e => e.color)
    .attr('fill-opacity', .72).attr('stroke', TH.canvas).attr('stroke-width', .6)
    .append('title').text(e => e.entity + ': ' + e.count + ' QSOs');
}
async function drawAz(d) {
  if (!mapsReady()) { $('#azNote').textContent = 'Map library unavailable — needs internet for d3.'; return; }
  const c = parseCenter($('#azCenter').value);
  if (!c) { $('#azNote').textContent = 'Enter a Maidenhead grid (CM87) or "lat,lon".'; $('#azNote').className = 'note err'; return; }
  $('#azNote').className = 'note';
  $('#azNote').textContent = 'center ' + c[1].toFixed(2) + ', ' + c[0].toFixed(2);
  const land = await world();
  const R = 270, rad = 258, scale = rad / Math.PI, svg = d3.select('#azMap'); svg.selectAll('*').remove();
  const proj = d3.geoAzimuthalEquidistant().rotate([-c[0], -c[1]]).translate([R, R]).scale(scale).clipAngle(179.9);
  const path = d3.geoPath(proj);
  svg.append('circle').attr('cx', R).attr('cy', R).attr('r', rad).attr('fill', TH.ocean).attr('stroke', TH.coast);
  svg.append('path').datum(d3.geoGraticule10()).attr('d', path).attr('fill', 'none').attr('stroke', TH.gratic);
  svg.append('path').datum(land).attr('d', path).attr('fill', TH.land).attr('stroke', TH.coast);
  [5000, 10000, 15000, 20000].forEach(km => { const rr = km / 6371 * scale;
    svg.append('circle').attr('cx', R).attr('cy', R).attr('r', rr).attr('fill', 'none').attr('stroke', TH.coast).attr('stroke-dasharray', '2,4');
    svg.append('text').attr('x', R).attr('y', R - rr - 2).attr('fill', MUT).attr('font-size', 9).attr('text-anchor', 'middle').text(km / 1000 + 'k km'); });
  ['N', 'E', 'S', 'W'].forEach((lbl, i) => { const a = i * Math.PI / 2;
    svg.append('text').attr('x', R + Math.sin(a) * (rad + 10)).attr('y', R - Math.cos(a) * (rad + 10) + 3).attr('fill', MUT).attr('font-size', 11).attr('text-anchor', 'middle').text(lbl); });
  const pts = d.dxcc.filter(e => e.lat != null), g = svg.append('g');
  if ($('#azLines').checked) pts.forEach(e => { const p = proj([e.lon, e.lat]); if (p)
    g.append('line').attr('x1', R).attr('y1', R).attr('x2', p[0]).attr('y2', p[1]).attr('stroke', e.color).attr('stroke-opacity', .35).attr('stroke-width', .8); });
  const rmax = Math.sqrt(Math.max(...pts.map(e => e.count), 1));
  pts.forEach(e => { const p = proj([e.lon, e.lat]); if (!p) return;
    g.append('circle').attr('cx', p[0]).attr('cy', p[1]).attr('r', 2.5 + 8 * Math.sqrt(e.count) / rmax)
      .attr('fill', e.color).attr('fill-opacity', .78).attr('stroke', TH.canvas).attr('stroke-width', .6)
      .append('title').text(e.entity + ': ' + e.count + ' QSOs · ' + Math.round(gcKm(c, [e.lon, e.lat])) + ' km'); });
  svg.append('circle').attr('cx', R).attr('cy', R).attr('r', 3.5).attr('fill', '#FFD54F');
}

// ---- QSO direction (beam-heading) analysis ----
function bearing(c, p) { // c,p = [lon,lat]; great-circle initial bearing, 0..360
  const r = Math.PI / 180, dl = (p[0] - c[0]) * r, la1 = c[1] * r, la2 = p[1] * r;
  const y = Math.sin(dl) * Math.cos(la2);
  const x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(dl);
  return (Math.atan2(y, x) / r + 360) % 360;
}
let HEAT0 = [12, 18, 27];
function hexToRgb(h) { h = (h || '').replace('#', '').trim();
  if (h.length === 3) h = h.split('').map(c => c + c).join('');
  return h.length >= 6 ? [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)] : [12, 18, 27]; }
function heatRGB(t) {
  const stops = [HEAT0, [33, 102, 172], [33, 188, 210], [120, 200, 80], [255, 213, 79], [229, 57, 53]];
  if (t <= 0) return HEAT0;
  const x = Math.min(1, t) * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(x)), f = x - i;
  const a = stops[i], b = stops[i + 1];
  return [a[0] + (b[0] - a[0]) * f | 0, a[1] + (b[1] - a[1]) * f | 0, a[2] + (b[2] - a[2]) * f | 0];
}
function heatColor(t) { const c = heatRGB(t); return `rgb(${c[0]},${c[1]},${c[2]})`; }
function wedge(ctx, cx, cy, r0, r1, degA, degB) { // compass degrees, N up, clockwise
  const a = (degA - 90) * Math.PI / 180, b = (degB - 90) * Math.PI / 180;
  ctx.beginPath(); ctx.arc(cx, cy, r1, a, b); ctx.arc(cx, cy, r0, b, a, true); ctx.closePath();
}
function compassLabels(ctx, cx, cy, R) {
  ctx.fillStyle = MUT; ctx.font = '12px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  [['N', 0], ['E', 90], ['S', 180], ['W', 270]].forEach(([t, deg]) => {
    const a = (deg - 90) * Math.PI / 180; ctx.fillText(t, cx + Math.cos(a) * (R + 12), cy + Math.sin(a) * (R + 12)); });
  ctx.strokeStyle = TH.grid;
  for (let deg = 0; deg < 360; deg += 30) { const a = (deg - 90) * Math.PI / 180;
    ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    ctx.lineTo(cx + Math.cos(a) * (R + 5), cy + Math.sin(a) * (R + 5)); ctx.stroke(); }
}
function drawRose(canvas, slots) {
  const ctx = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H); const cx = W / 2, cy = H / 2, R = Math.min(cx, cy) - 26;
  const mx = Math.max(1, ...slots);
  ctx.strokeStyle = TH.grid; ctx.fillStyle = MUT; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
  for (let g = 1; g <= 4; g++) { const rr = R * g / 4; ctx.beginPath(); ctx.arc(cx, cy, rr, 0, 2 * Math.PI); ctx.stroke();
    ctx.fillText(Math.round(mx * g / 4), cx + 2, cy - rr); }
  for (let j = 0; j < 72; j++) { if (!slots[j]) continue;
    const r = R * slots[j] / mx; wedge(ctx, cx, cy, 0, r, j * 5, (j + 1) * 5);
    ctx.fillStyle = 'rgba(33,150,243,.78)'; ctx.fill();
    ctx.strokeStyle = TH.canvas; ctx.lineWidth = .5; ctx.stroke(); }
  compassLabels(ctx, cx, cy, R);
}
function drawDirHeat(canvas, grid) {
  const ctx = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H); const cx = W / 2, cy = H / 2, R = Math.min(cx, cy) - 26, ri = 36, dr = (R - ri) / 24;
  let mx = 1; for (const row of grid) for (const v of row) if (v > mx) mx = v;
  for (let h = 0; h < 24; h++) { const r1 = R - h * dr, r0 = r1 - dr;
    for (let j = 0; j < 72; j++) { const v = grid[h][j];
      wedge(ctx, cx, cy, r0, r1, j * 5, (j + 1) * 5); ctx.fillStyle = heatColor(v / mx); ctx.fill(); }
  }
  ctx.fillStyle = MUT; ctx.font = '9px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  [0, 6, 12, 18, 23].forEach(h => { const rr = R - (h + 0.5) * dr; ctx.fillText(h + 'Z', cx + 6, cy - rr); });
  compassLabels(ctx, cx, cy, R);
  ctx.textAlign = 'left'; ctx.fillStyle = MUT; ctx.fillText('max ' + mx + '/slot', 8, H - 8);
}
function quantile(sorted, p) {
  const i = (sorted.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}
function boxStats(values) {
  const s = [...values].sort((a, b) => a - b);
  const q1 = quantile(s, .25), q2 = quantile(s, .5), q3 = quantile(s, .75), iqr = q3 - q1;
  const lo = q1 - 1.5 * iqr, hi = q3 + 1.5 * iqr;
  let wl = s[0], wh = s[s.length - 1]; const out = [];
  for (const v of s) { if (v < lo || v > hi) out.push(v); }
  wl = s.find(v => v >= lo); wh = [...s].reverse().find(v => v <= hi);
  return { q1, q2, q3, wl, wh, out, n: s.length, min: s[0], max: s[s.length - 1] };
}
function drawBox(canvas, groups) {
  const ctx = canvas.getContext('2d'), W = canvas.width, padL = 64, padR = 70, padT = 14, padB = 28;
  const rows = groups.filter(g => g.values.length);
  canvas.height = padT + padB + rows.length * 38 + 6;
  const H = canvas.height; ctx.clearRect(0, 0, W, H);
  if (!rows.length) { ctx.fillStyle = MUT; ctx.font = '13px sans-serif'; ctx.fillText('No QSOs to summarise.', padL, padT + 20); return; }
  const maxD = Math.max(...rows.map(g => Math.max(...g.values))) || 1;
  const x = v => padL + (W - padL - padR) * v / maxD;
  ctx.strokeStyle = TH.grid; ctx.fillStyle = MUT; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
  const step = maxD > 16000 ? 5000 : maxD > 8000 ? 2500 : 1000;
  for (let v = 0; v <= maxD; v += step) { ctx.beginPath(); ctx.moveTo(x(v), padT); ctx.lineTo(x(v), H - padB); ctx.stroke();
    ctx.fillText((v / 1000) + 'k', x(v), H - padB + 14); }
  ctx.textAlign = 'left'; ctx.fillText('km →', W - padR + 6, H - padB + 14);
  rows.forEach((g, i) => {
    const cy = padT + 18 + i * 38, b = boxStats(g.values), h = 11;
    ctx.fillStyle = INK; ctx.font = '12px sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillText(g.label, 6, cy);
    ctx.strokeStyle = g.color; ctx.fillStyle = g.color + '55'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x(b.wl), cy); ctx.lineTo(x(b.q1), cy); ctx.moveTo(x(b.q3), cy); ctx.lineTo(x(b.wh), cy); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x(b.wl), cy - 5); ctx.lineTo(x(b.wl), cy + 5); ctx.moveTo(x(b.wh), cy - 5); ctx.lineTo(x(b.wh), cy + 5); ctx.stroke();
    ctx.beginPath(); ctx.rect(x(b.q1), cy - h, x(b.q3) - x(b.q1), h * 2); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x(b.q2), cy - h); ctx.lineTo(x(b.q2), cy + h); ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = g.color;
    b.out.forEach(v => { ctx.beginPath(); ctx.arc(x(v), cy, 1.8, 0, 2 * Math.PI); ctx.fill(); });
    ctx.fillStyle = MUT; ctx.font = '10px sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillText('n=' + b.n + '  med ' + Math.round(b.q2) + 'km', W - padR + 4, cy);
  });
}

function compass8(deg) {
  return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(deg / 45) % 8];
}
function drawDirTable(c, qsos) {
  const grid = Array.from({ length: 72 }, () => new Array(24).fill(0));
  for (const q of qsos) {
    const j = Math.floor(bearing(c, [q.lon, q.lat]) / 5) % 72;
    grid[j][q.h]++;
  }
  let mx = 1; for (const row of grid) for (const v of row) if (v > mx) mx = v;
  let head = '<tr><th>UTC</th>';
  for (let j = 0; j < 72; j++) { const deg = j * 5, lab = deg % 10 === 0 ? deg.toString().padStart(3, '0') : '';
    head += `<th class=vh title="${deg}° ${compass8(deg)}">${lab}</th>`; }
  $('#dirHeatTable thead').innerHTML = head + '</tr>';
  let body = '';
  for (let h = 0; h < 24; h++) {
    body += `<tr class="${h % 2 ? 'zebra' : ''}"><td class=lab>${String(h).padStart(2, '0')}Z</td>`;
    for (let j = 0; j < 72; j++) { const v = grid[j][h], ti = `title="${j * 5}° ${String(h).padStart(2, '0')}Z: ${v} QSO"`;
      if (v) { const cc = heatRGB(v / mx), lum = 0.2126 * cc[0] + 0.7152 * cc[1] + 0.0722 * cc[2];
        body += `<td class=cell style="background:rgb(${cc[0]},${cc[1]},${cc[2]});color:${lum > 140 ? '#000' : '#fff'}" ${ti}>${v}</td>`;
      } else body += `<td class=cell ${ti}></td>`; }
    body += '</tr>';
  }
  $('#dirHeatTable tbody').innerHTML = body;
}

function drawDirections(d) {
  const sel = $('#dirCenter').value, c = parseCenter(sel);
  if (!c) { $('#dirNote').textContent = 'Enter a grid (CM87) or "lat,lon" for your station.'; $('#dirNote').className = 'note err';
    drawRose($('#roseChart'), new Array(72).fill(0)); $('#dirHeats').innerHTML = ''; return; }
  $('#dirNote').className = 'note';
  const band = $('#dirBand').value, mode = $('#dirMode').value, dom = $('#dirDom').checked;
  const newGrid = () => Array.from({ length: 24 }, () => new Array(72).fill(0));
  const slots = new Array(72).fill(0), gridAll = newGrid(), byDate = {}, dirFiltered = [];
  let n = 0, nd = 0;
  for (const q of d.dirqsos) {
    if (band !== '*' && q.band !== band) continue;
    if (mode !== '*' && q.mode !== mode) continue;
    if (q.dom) { nd++; if (!dom) continue; }
    dirFiltered.push(q);
    const j = Math.floor(bearing(c, [q.lon, q.lat]) / 5) % 72;
    slots[j]++; gridAll[q.h][j]++; n++;
    (byDate[q.d] = byDate[q.d] || newGrid())[q.h][j]++;
  }
  $('#dirNote').textContent = 'center ' + c[1].toFixed(2) + ', ' + c[0].toFixed(2) + ' · ' + n + ' QSOs · 5° slots' +
    (nd ? ' · ' + nd + ' US/Canada ' + (dom ? '(call-area est.)' : 'excluded') : '');
  drawRose($('#roseChart'), slots);

  const dates = Object.keys(byDate).sort();
  let panels;
  if (dates.length === 2)
    panels = [['Day 1 — ' + dates[0], byDate[dates[0]]], ['Day 2 — ' + dates[1], byDate[dates[1]]],
      ['Combined', gridAll]];
  else if (dates.length > 2)
    panels = [['Per UTC hour-of-day · ' + dates.length + ' days folded', gridAll]];
  else
    panels = [['Per UTC hour-of-day' + (dates.length ? ' — ' + dates[0] : ''), gridAll]];
  const host = $('#dirHeats'); host.innerHTML = '';
  panels.forEach(([label, grid]) => {
    const fig = document.createElement('figure'); fig.className = 'dirfig';
    fig.innerHTML = '<figcaption class=note>' + label + '<br>(outer 00Z → inner 23Z · color = QSO count)</figcaption>';
    const cv = document.createElement('canvas'); cv.width = 440; cv.height = 440;
    fig.appendChild(cv); host.appendChild(fig); drawDirHeat(cv, grid);
  });

  $('#boxPanel').style.display = $('#dirBox').checked ? '' : 'none';
  if ($('#dirBox').checked) {
    const byBand = {};
    for (const q of d.dirqsos) {
      if (mode !== '*' && q.mode !== mode) continue;
      if (q.dom && !dom) continue;
      (byBand[q.band] = byBand[q.band] || []).push(gcKm(c, [q.lon, q.lat]));
    }
    const groups = d.bandOrder.filter(b => byBand[b]).map(b => ({ label: b, color: d.bandColors[b], values: byBand[b] }));
    drawBox($('#distBox'), groups);
  }

  $('#dirTablePanel').style.display = $('#dirTable').checked ? '' : 'none';
  if ($('#dirTable').checked) drawDirTable(c, dirFiltered);
}

function render(d) {
  readTheme();
  const m = d.meta;
  if (!m.total) { $('#msg').textContent = 'No QSOs match the current filters.'; $('#app').classList.remove('hide'); return; }
  $('#msg').textContent = ''; $('#app').classList.remove('hide');
  $('#hsub').textContent = m.sources.join(', ') + '  ·  ' + fmt(m.start) + ' → ' + fmt(m.end);
  const filt = []; if (m.filter_bands.length) filt.push(m.filter_bands.join('/'));
  if (m.filter_conts.length) filt.push(m.filter_conts.join('/'));
  $('#filtNote').textContent = filt.length ? ('active: ' + filt.join('  ·  ')) : 'all QSOs';

  const eu = d.continents.find(c => c.code === 'EU');
  const cs = [];
  cs.push(card(m.total, 'QSOs' + (m.skipped ? ` (+${m.skipped} undated)` : '')));
  cs.push(card(m.unique_calls, 'Unique calls'));
  cs.push(card(m.qph + '/hr', 'Avg rate'));
  cs.push(card(m.best60.n, 'Best 60-min' + (m.best60.at ? ' @ ' + fmtmd(m.best60.at) : '')));
  cs.push(card(m.best10.n + ' (' + (m.best10.n * 6) + '/hr)', 'Best 10-min'));
  cs.push(card(m.span, 'Active span'));
  cs.push(card(m.days, 'Operating days'));
  cs.push(card(m.n_dxcc, 'DXCC entities'));
  cs.push(card(m.n_cont, 'Continents'));
  cs.push(card(m.n_cq, 'CQ zones'));
  cs.push(card(m.n_itu, 'ITU zones'));
  if (m.points != null) cs.push(card(m.points, 'Points (' + m.points_per_q + '/Q)'));
  const sw = m.solar;
  if (sw && sw.cond_qsos) {
    if (sw.sfi_avg != null) cs.push(card(sw.sfi_avg, 'Avg SFI (' + sw.sfi_min + '–' + sw.sfi_max + ')'));
    if (sw.ssn_avg != null) cs.push(card(sw.ssn_avg, 'Avg sunspot # (' + sw.ssn_min + '–' + sw.ssn_max + ')'));
    if (sw.a_avg != null) cs.push(card(sw.a_avg, 'Avg A-index (max ' + sw.a_max + ')'));
    if (sw.k_avg != null) cs.push(card(sw.k_avg, 'Avg K-index (max ' + sw.k_max + ')'));
  }
  if (eu) cs.push(card(fmtmd(eu.first.dt), 'First EU: ' + esc(eu.first.call), true));
  if (eu) cs.push(card(fmtmd(eu.last.dt), 'Last EU: ' + esc(eu.last.call), true));
  $('#cards').innerHTML = cs.join('');

  const sav = sw && sw.available;
  const unm = sav ? (m.total - sw.cond_qsos) : 0;
  $('#solarNote').innerHTML = sav
    ? ('Space weather: GFZ Potsdam snapshot · ' + sw.cond_qsos + '/' + m.total + ' QSOs matched · data ' + sw.earliest + ' → ' + sw.latest
        + (unm > 0 ? ' <span class=err>· ' + unm + ' QSO(s) fall outside the bundled data window</span>' : ''))
    : 'Space-weather data unavailable.';

  drawTimeline(d);

  const hours = d.hours.map(h => String(h.hour).padStart(2, '0') + 'Z');
  const dsets = d.bandOrder.map(b => ({ label: b, data: d.hours.map(h => h.bands[b] || 0), backgroundColor: d.bandColors[b] }));
  chart('#hourChart', { type: 'bar', data: { labels: hours, datasets: dsets },
    options: { plugins: { legend: { labels: { color: INK, boxWidth: 12 } } },
      scales: { x: { ...AX.x, stacked: true }, y: { ...AX.y, stacked: true } } } });

  chart('#bandChart', { type: 'bar', data: { labels: d.bands.map(b => b.band),
    datasets: [{ data: d.bands.map(b => b.count), backgroundColor: d.bands.map(b => d.bandColors[b.band]) }] },
    options: { indexAxis: 'y', plugins: { legend: { display: false } }, scales: AX } });

  chart('#contChart', { type: 'doughnut', data: { labels: d.continents.map(c => c.name),
    datasets: [{ data: d.continents.map(c => c.count), backgroundColor: d.continents.map(c => c.color) }] },
    options: { plugins: { legend: { position: 'right', labels: { color: INK, boxWidth: 12 } } } } });
  $('#srcNote').textContent = '· ' + m.src_pdf + ' DXCC' + (m.src_itu ? ', ' + m.src_itu + ' ITU' : '') +
    (m.src_log ? ', ' + m.src_log + ' logger' : '') + (m.cont_unknown ? ', ' + m.cont_unknown + ' unknown' : '');

  $('#contTable tbody').innerHTML = d.continents.map(c => {
    const cell = x => `${fmt(x.dt)} &nbsp; ${esc(x.call)} <span class=note>(${esc(x.band)}${x.entity ? ' · ' + esc(x.entity) : ''})</span>`;
    return `<tr><td><span class=swatch style="background:${c.color}"></span><b>${c.name}</b></td>` +
      `<td class=n>${c.count}</td><td class=n>${c.pct}%</td><td>${cell(c.first)}</td><td>${cell(c.last)}</td></tr>`;
  }).join('');

  const zbar = (id, arr, lbl) => chart(id, { type: 'bar', data: { labels: arr.map(z => lbl + z.zone),
    datasets: [{ data: arr.map(z => z.count), backgroundColor: '#2196F3' }] },
    options: { plugins: { legend: { display: false } }, scales: AX } });
  zbar('#cqChart', d.cq, 'CQ ');
  zbar('#ituChart', d.itu, 'ITU ');

  if (d.runsp && d.runsp.length) { $('#runspPanel').classList.remove('hide');
    chart('#runspChart', { type: 'doughnut', data: { labels: d.runsp.map(r => r.kind),
      datasets: [{ data: d.runsp.map(r => r.count), backgroundColor: ['#4CAF50', '#FF9800'] }] },
      options: { plugins: { legend: { position: 'right', labels: { color: INK, boxWidth: 12 } } } } });
  } else $('#runspPanel').classList.add('hide');

  chart('#modeChart', { type: 'doughnut', data: { labels: d.modes.map(x => x.mode),
    datasets: [{ data: d.modes.map(x => x.count), backgroundColor: ['#2196F3', '#E91E63', '#FFD54F', '#4CAF50', '#00BCD4', '#9370DB'] }] },
    options: { plugins: { legend: { position: 'right', labels: { color: INK, boxWidth: 12 } } } } });

  if (d.operators && d.operators.length > 1) {
    $('#opsTabBtn').classList.remove('hide');     // reveal the Multi-operator tab
    const ops = d.operators, bands = d.bandOrder;
    chart('#opChart', { type: 'bar', data: { labels: ops.map(o => o.op),
      datasets: bands.map(b => ({ label: b, data: ops.map(o => o.bands[b] || 0), backgroundColor: d.bandColors[b] })) },
      options: { plugins: { legend: { labels: { color: INK, boxWidth: 12 } } },
        scales: { x: { ...AX.x, stacked: true }, y: { ...AX.y, stacked: true, title: { display: true, text: 'QSOs', color: MUT } } } } });
    $('#opTable tbody').innerHTML = ops.map(o =>
      `<tr><td><b>${esc(o.op)}</b></td><td class=n>${o.count}</td><td class=n>${o.unique}</td><td class=n>${o.ndxcc}</td>` +
      `<td class=n>${o.qph}</td><td class=n>${o.best10}</td><td class=n>${o.best60}</td><td class=n>${o.on_h}h</td>` +
      `<td>${o.span}</td><td>${fmtmd(o.first)}</td><td>${fmtmd(o.last)}</td></tr>`).join('');
    $('#opMatrix thead').innerHTML = '<tr><th class=op>Operator</th>' +
      bands.map(b => `<th>${b}</th>`).join('') + '<th class=tot>Total</th></tr>';
    let mb = ops.map(o => `<tr><td class=op>${esc(o.op)}</td>` +
      bands.map(b => { const v = o.bands[b] || 0; return `<td class="${v ? '' : 'z'}">${v || '·'}</td>`; }).join('') +
      `<td class=tot>${o.count}</td></tr>`).join('');
    mb += '<tr><td class=op>All ops</td>' + bands.map(b => { const t = ops.reduce((s, o) => s + (o.bands[b] || 0), 0);
      return `<td class=tot>${t || '·'}</td>`; }).join('') + `<td class=tot>${ops.reduce((s, o) => s + o.count, 0)}</td></tr>`;
    $('#opMatrix tbody').innerHTML = mb;
    const metrics = [['Top QSO count', o => o.count, ''], ['Top hours-on', o => o.on_h, 'h'],
      ['Top 10-min rate', o => o.best10, ''], ['Top 1-hour rate', o => o.best60, ''], ['Top DXCCs', o => o.ndxcc, '']];
    $('#lbGrid').innerHTML = metrics.map(([title, fn, suf]) => {
      const sorted = [...ops].sort((a, b) => fn(b) - fn(a)).slice(0, 10);
      return `<div class=lbcol><h3>${title}</h3><ol>` +
        sorted.map(o => `<li>${esc(o.op)} <b>${fn(o)}${suf}</b></li>`).join('') + '</ol></div>';
    }).join('');
  } else {
    $('#opsTabBtn').classList.add('hide');        // single-op: no Multi-operator tab
    if (!$('#tabOps').classList.contains('hide')) switchTab('tabOverall');
  }

  drawDxcc(d);

  if (m.home) {
    if (!$('#azCenter').value) $('#azCenter').value = m.home.lat + ',' + m.home.lon;
    if (!$('#worldCenter').value) $('#worldCenter').value = m.home.lat + ',' + m.home.lon;
  }
  $('#azNote').textContent = m.home ? ('default: ' + m.home.call + ' (' + m.home.entity + ')') : '';
  drawWorld(d);
  drawAz(d);

  if (m.home && !$('#dirCenter').value) $('#dirCenter').value = m.home.lat + ',' + m.home.lon;
  $('#dirBand').innerHTML = '<option value="*">All</option>' +
    d.bandOrder.map(b => `<option value="${b}">${b}</option>`).join('');
  $('#dirMode').innerHTML = '<option value="*">All</option>' +
    d.modes.map(x => `<option value="${x.mode}">${x.mode}</option>`).join('');
  drawDirections(d);

  const dl = d.days.map(x => x.date.slice(5)), sw2 = m.solar;
  if (sw2 && sw2.available && d.days.some(x => x.sfi != null)) {
    $('#swPanel').classList.remove('hide');
    chart('#swChart', { data: { labels: dl, datasets: [
      { type: 'bar', label: 'QSOs', data: d.days.map(x => x.count), backgroundColor: '#2b3e5c', yAxisID: 'yq', order: 3 },
      { type: 'line', label: 'SFI', data: d.days.map(x => x.sfi), borderColor: '#FFD54F', backgroundColor: '#FFD54F', borderWidth: 2, pointRadius: 0, tension: .25, yAxisID: 'yf', order: 0 },
      { type: 'line', label: 'Sunspot #', data: d.days.map(x => x.ssn), borderColor: '#4CAF50', backgroundColor: '#4CAF50', borderWidth: 2, pointRadius: 0, tension: .25, yAxisID: 'yf', order: 1 },
      { type: 'line', label: 'A-index', data: d.days.map(x => x.a), borderColor: '#FF6E6E', backgroundColor: '#FF6E6E', borderWidth: 2, borderDash: [4, 3], pointRadius: 0, tension: .25, yAxisID: 'ya', order: 2 }] },
      options: { interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: INK, boxWidth: 12 } } },
        scales: { x: { grid, ticks: tick },
          yq: { position: 'left', grid, ticks: tick, beginAtZero: true, title: { display: true, text: 'QSOs', color: MUT } },
          yf: { position: 'right', grid: { drawOnChartArea: false }, ticks: tick, beginAtZero: true, title: { display: true, text: 'SFI / sunspot', color: MUT } },
          ya: { position: 'right', grid: { drawOnChartArea: false }, ticks: tick, beginAtZero: true, title: { display: true, text: 'A', color: '#FF6E6E' } } } } });
  } else $('#swPanel').classList.add('hide');

  if (sw2 && sw2.available && d.kdist && d.kdist.some(x => x.count)) {
    $('#kPanel').classList.remove('hide');
    const kcol = k => k <= 1 ? '#4CAF50' : k <= 3 ? '#FFD54F' : k <= 4 ? '#FF9800' : '#FF6E6E';
    chart('#kChart', { type: 'bar', data: { labels: d.kdist.map(x => 'K' + x.k),
      datasets: [{ data: d.kdist.map(x => x.count), backgroundColor: d.kdist.map(x => kcol(x.k)) }] },
      options: { plugins: { legend: { display: false } }, scales: AX } });
  } else $('#kPanel').classList.add('hide');

  chart('#dayChart', { type: 'bar', data: { labels: dl,
    datasets: [{ data: d.days.map(x => x.count), backgroundColor: '#2196F3' }] },
    options: { plugins: { legend: { display: false } }, scales: AX } });

  if (d.cbs) { $('#cbsPanel').classList.remove('hide'); $('#cbsPre').textContent = d.cbs; }
  else $('#cbsPanel').classList.add('hide');
}

$('#cbsSave').onclick = () => { if (!D || !D.cbs) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([D.cbs], { type: 'text/plain' }));
  a.download = 'logan_cbs_report.txt'; a.click(); URL.revokeObjectURL(a.href); };

// ---- light / dark theme ----
function applyTheme(light) {
  document.documentElement.classList.toggle('light', light);
  try { localStorage.setItem('logan-theme', light ? 'light' : 'dark'); } catch (e) {}
  $('#themeBtn').textContent = light ? '☾ Dark' : '☀︎ Light';
  readTheme();
  if (D) render(D);
}
(function () {
  let light = false;
  try { light = localStorage.getItem('logan-theme') === 'light'; } catch (e) {}
  applyTheme(light);
  $('#themeBtn').onclick = () => applyTheme(!document.documentElement.classList.contains('light'));
})();

// ---- tabs ----
function switchTab(id) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('hide', t.id !== id));
  document.querySelectorAll('.tabbtn').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
}
document.querySelectorAll('.tabbtn').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
