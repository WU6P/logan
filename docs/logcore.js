// logcore.js — browser + Node port of logan.py's analysis engine.
//
// A faithful JavaScript mirror of the Python core: ADIF/Cabrillo parsing, DXCC
// prefix resolution (with the curated call-area overrides), space-weather
// lookup, analyze() and the CBS-style text report. It produces the SAME JSON
// shape logan.py's /analyze endpoint returned, so the existing front-end
// (app.js, ported from the PAGE string) drops straight on top with no server.
//
// The lookup tables (dxcc / itu / rare / solar) are injected via setData() so
// this module stays environment-free (fetch in the browser, fs in Node).

// --------------------------------------------------------------------------
// Injected data
// --------------------------------------------------------------------------
let DXCC = {};        // prefix -> {entity,cont,itu,cq,code,lat,lon}
let ITU = {};         // 2-char -> {country,cont,lat,lon}
let RARE = {};        // ARRL code -> most-wanted rank
let DXCC_CODE = {};   // entity name -> ARRL code
let ENTITY_REC = {};  // entity name -> full record
let SOLAR = {};       // "YYYY-MM-DD" -> {kp:[8],a,ssn,sfi,sfi_adj,definitive}

export function setData({ dxcc, itu, rare, solar } = {}) {
  if (dxcc) {
    DXCC = dxcc.lookup || {};
    ENTITY_REC = {};
    DXCC_CODE = {};
    for (const e of dxcc.entities || []) {
      ENTITY_REC[e.entity] = e;
      DXCC_CODE[e.entity] = e.code;
    }
  }
  if (itu) ITU = itu.lookup || {};
  if (rare) RARE = rare.rare || {};
  if (solar) SOLAR = solar || {};
}
export function hasDxcc() { return Object.keys(DXCC).length > 0; }

// --------------------------------------------------------------------------
// Small helpers (Python parity: banker's rounding, str justification)
// --------------------------------------------------------------------------
export function pyround(x, nd = 0) {
  if (!isFinite(x)) return x;
  const m = Math.pow(10, nd);
  const y = x * m;
  const fl = Math.floor(y);
  const diff = y - fl;
  let r;
  if (Math.abs(diff - 0.5) < 1e-9) r = (fl % 2 === 0) ? fl : fl + 1;
  else r = Math.round(y);
  return r / m;
}
const rjust = (s, w) => { s = String(s); return s.length >= w ? s : ' '.repeat(w - s.length) + s; };
const ljust = (s, w) => { s = String(s); return s.length >= w ? s : s + ' '.repeat(w - s.length); };
const fmt1 = (v) => pyround(v, 1).toFixed(1);   // matches Python f"{v:.1f}" (ties→even)
const isDigits = (s) => s.length > 0 && /^\d+$/.test(s);

// --------------------------------------------------------------------------
// Counter (Map that preserves insertion order; most_common is a stable sort)
// --------------------------------------------------------------------------
class Counter extends Map {
  add(k, n = 1) { this.set(k, (this.get(k) || 0) + n); }
  gv(k) { return this.get(k) || 0; }
  total() { let s = 0; for (const v of this.values()) s += v; return s; }
  mostCommon() { return [...this.entries()].sort((a, b) => b[1] - a[1]); }
}
function dget(map, k) { let v = map.get(k); if (v === undefined) { v = new Counter(); map.set(k, v); } return v; }

// --------------------------------------------------------------------------
// Datetime (Python used naive UTC datetime; we mirror with UTC-based Date)
// --------------------------------------------------------------------------
const p2 = (n) => String(n).padStart(2, '0');
function isoNaive(d) {
  return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}T` +
    `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
}
function dateISO(d) { return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}`; }
function fromDateISO(s) { const [y, m, d] = s.split('-').map(Number); return new Date(Date.UTC(y, m - 1, d)); }
function floorHour(d) { return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours())); }
function floorMin(d) { return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours(), d.getUTCMinutes())); }
const HOUR = 3600e3, MIN = 60e3;

// --------------------------------------------------------------------------
// ADIF parsing
// --------------------------------------------------------------------------
const TAG_RE = /<([A-Za-z0-9_]+)(?::(\d+))?(?::[^>]*)?>/g;

export function parseAdifRecords(text) {
  const eoh = /<EOH>/i.exec(text);
  let pos = eoh ? eoh.index + eoh[0].length : 0;
  const records = [];
  let current = {};
  TAG_RE.lastIndex = pos;
  let m;
  while ((m = TAG_RE.exec(text))) {
    const name = m[1].toUpperCase();
    const length = m[2];
    pos = TAG_RE.lastIndex;
    if (name === 'EOR') {
      if (Object.keys(current).length) { records.push(current); current = {}; }
      continue;
    }
    if (name === 'EOH') continue;
    if (length !== undefined) {
      const ln = parseInt(length, 10);
      current[name] = text.slice(pos, pos + ln);
      pos += ln;
      TAG_RE.lastIndex = pos;
    } else {
      current[name] = '';
    }
  }
  if (Object.keys(current).length) records.push(current);
  return records;
}

export function qsoDatetime(qso) {
  const d = (qso.QSO_DATE || '').trim();
  let t = (qso.TIME_ON || '').trim();
  if (d.length !== 8) return null;
  t = (t + '000000').slice(0, 6);
  const y = +d.slice(0, 4), mo = +d.slice(4, 6), da = +d.slice(6, 8);
  const H = +t.slice(0, 2), M = +t.slice(2, 4), S = +t.slice(4, 6);
  if ([y, mo, da, H, M, S].some(Number.isNaN)) return null;
  const dt = new Date(Date.UTC(y, mo - 1, da, H, M, S));
  // Reject values that didn't round-trip (e.g. month 13, hour 25) like strptime.
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== da ||
      dt.getUTCHours() !== H || dt.getUTCMinutes() !== M || dt.getUTCSeconds() !== S) return null;
  return dt;
}

// --------------------------------------------------------------------------
// Cabrillo parsing
// --------------------------------------------------------------------------
const BAND_EDGES = [
  [1800, 2000, '160M'], [3500, 4000, '80M'], [5250, 5450, '60M'],
  [7000, 7300, '40M'], [10100, 10150, '30M'], [14000, 14350, '20M'],
  [18068, 18168, '17M'], [21000, 21450, '15M'], [24890, 24990, '12M'],
  [28000, 29700, '10M'], [50000, 54000, '6M'], [70000, 71000, '4M'],
  [144000, 148000, '2M'], [420000, 450000, '70CM'],
];
const CAB_MODE = { CW: 'CW', PH: 'SSB', SSB: 'SSB', RY: 'RTTY', RTTY: 'RTTY', FM: 'FM', DG: 'DATA', DI: 'DATA' };

export function khzToBand(khz) {
  const f = parseFloat(khz);
  if (Number.isNaN(f)) return '';
  for (const [lo, hi, name] of BAND_EDGES) if (f >= lo && f <= hi) return name;
  return '';
}
function isCallsign(tok) {
  const t = (tok || '').toUpperCase();
  return t.length >= 3 && t.length <= 12 && /[A-Z]/.test(t) && /\d/.test(t) && /^[A-Z0-9/]+$/.test(t);
}
function cabrilloSplit(body) {
  if (body.length < 7) return [null, []];
  let idx = 5 + Math.floor((body.length - 6) / 2);
  let cand = idx < body.length ? body[idx] : null;
  if (!(cand && isCallsign(cand))) {
    const shaped = body.slice(4).filter(isCallsign);
    if (shaped.length < 2) return [null, []];
    cand = shaped[1];
    idx = body.indexOf(cand);
  }
  return [cand, body.slice(idx + 1)];
}
export function parseCabrilloRecords(text) {
  let station = '', contest = '', ops = '';
  const records = [];
  for (const line of text.split(/\r?\n/)) {
    const s = line.trim();
    if (!s) continue;
    const u = s.toUpperCase();
    if (u.startsWith('CALLSIGN:')) { station = s.split(/:(.*)/s)[1].trim().toUpperCase(); continue; }
    if (u.startsWith('CONTEST:')) { contest = s.split(/:(.*)/s)[1].trim(); continue; }
    if (u.startsWith('OPERATORS:')) { ops = s.split(/:(.*)/s)[1].trim().toUpperCase(); continue; }
    if (!u.startsWith('QSO:')) continue;
    const body = s.split(/\s+/).slice(1);
    if (body.length < 6) continue;
    const freq = body[0], mode = body[1].toUpperCase(), d = body[2], t = body[3];
    const [call, rcvd] = cabrilloSplit(body);
    if (!call) continue;
    const ds = d.replace(/-/g, '');
    if (ds.length !== 8 || (t.length !== 4 && t.length !== 6)) continue;
    const rst = (rcvd.length && /^\d{2,3}$/.test(rcvd[0])) ? rcvd[0] : '';
    const exch = rst ? rcvd.slice(1) : rcvd;
    const rec = {
      CALL: call.toUpperCase(), QSO_DATE: ds,
      TIME_ON: t.length === 4 ? (t + '00').slice(0, 6) : t,
      BAND: khzToBand(freq), FREQ: freq,
      MODE: CAB_MODE[mode] || mode, RST_RCVD: rst,
      SRX_STRING: exch.join(' '),
    };
    if (station) rec.STATION_CALLSIGN = station;
    if (contest) rec.CONTEST_ID = contest;
    if (ops) rec.APP_LOGAN_OPS = ops;
    records.push(rec);
  }
  return records;
}
export function recordsFromText(text) {
  if (/<EOH>/i.test(text) || /<CALL/i.test(text)) return parseAdifRecords(text);
  if (/^\s*QSO:/im.test(text) || text.toUpperCase().includes('START-OF-LOG')) return parseCabrilloRecords(text);
  const recs = parseAdifRecords(text);
  return recs.length ? recs : parseCabrilloRecords(text);
}

// --------------------------------------------------------------------------
// DXCC prefix resolution
// --------------------------------------------------------------------------
export const CONTINENT_NAMES = {
  NA: 'North America', SA: 'South America', EU: 'Europe',
  AF: 'Africa', AS: 'Asia', OC: 'Oceania', AN: 'Antarctica',
};
const SUFFIXES = new Set(['P', 'M', 'MM', 'AM', 'QRP', 'A', 'B']);

function callCores(call) {
  call = (call || '').toUpperCase().trim();
  if (!call.includes('/')) return call ? [call] : [];
  const parts = call.split('/').filter(p => p && !SUFFIXES.has(p) && /[A-Z]/.test(p));
  if (parts.length) return parts.slice().sort((a, b) => a.length - b.length);
  return [call.replace(/\//g, '')];
}
function leadingAlpha(head) { let i = 0; while (i < head.length && /[A-Z]/i.test(head[i])) i++; return i; }
function lookupHead(core) {
  if (core in DXCC) return [DXCC[core], core, core];
  const m = /^([A-Z0-9]+?\d)/.exec(core);
  const head = m ? m[1] : core;
  for (let n = head.length; n > 0; n--) {
    const key = head.slice(0, n);
    if (key in DXCC) return [DXCC[key], key, head];
  }
  return [null, null, null];
}
const PREFIX_OVERRIDES = [
  [/^(?:MD|2D)/, 'Isle of Man'],
  [/^(?:MI|2I)/, 'Northern Ireland'],
  [/^(?:MJ|2J)/, 'Jersey'],
  [/^(?:MM|2M)/, 'Scotland'],
  [/^(?:MU|2U)/, 'Guernsey'],
  [/^(?:MW|2W)/, 'Wales'],
  [/^(?:M|2E)/, 'England'],
  [/^KG4[A-Z]{2}$/, 'Guantanamo Bay'],
  [/^KG/, 'United States of America'],
  [/^C[QRST][39]/, 'Madeira Is.'],
  [/^C[QRST]8/, 'Azores'],
  [/^9W[68]/, 'East Malaysia'],
  [/^9W/, 'West Malaysia'],
  [/^KP[34]/, 'Puerto Rico'],
  [/^KP2/, 'Virgin Is.'],
  [/^[AKNW]H6/, 'Hawaii'],
  [/^[AKNW]H7(?!K)/, 'Hawaii'],
  [/^[AKNW]H2/, 'Guam'],
  [/^[AKNW]H0/, 'Mariana Is.'],
  [/^[AKNW]L\d/, 'Alaska'],
  [/^E[A-H]8/, 'Canary Is.'],
  [/^E[A-H]9/, 'Ceuta & Melilla'],
  [/^(?:R[A-Z]?|U[A-I])[890]/, 'Asiatic Russia'],
  [/^(?:R[A-Z]?|U[A-I])2F/, 'Kaliningrad'],
  [/^R1(?!F)/, 'European Russia'],
  [/^(?:R[A-Z]?|U[A-I])[2-7]/, 'European Russia'],
];
function overrideEntity(call) {
  for (const core of callCores(call))
    for (const [rx, ent] of PREFIX_OVERRIDES)
      if (rx.test(core)) return ent;
  return '';
}
function dxccMatch(call) {
  const ent = overrideEntity(call);
  if (ent) return [ENTITY_REC[ent] || { entity: ent }, true];
  for (const core of callCores(call)) {
    const [rec, key, head] = lookupHead(core);
    if (rec) return [rec, key.length >= leadingAlpha(head)];
  }
  return [null, false];
}
export function resolveDxcc(call) { return dxccMatch(call)[0]; }
export function resolveItu(call) {
  for (const core of callCores(call)) {
    const rec = ITU[core.slice(0, 2)];
    if (rec && rec.cont) return rec;
  }
  return null;
}
function callCore(call) {
  for (const c of callCores(call)) if (lookupHead(c)[0]) return c;
  const cores = callCores(call);
  return cores.length ? cores[0] : (call || '').toUpperCase().trim();
}

const US_AREAS = {
  '0': [41.5, -96.0], '1': [43.5, -71.5], '2': [42.0, -75.0], '3': [40.0, -77.0],
  '4': [33.0, -82.0], '5': [32.0, -97.0], '6': [37.0, -120.0], '7': [44.0, -114.0],
  '8': [40.5, -82.5], '9': [42.0, -89.0],
};
const CA_AREAS = {
  VE1: [45.0, -63.0], VE2: [49.0, -72.0], VE3: [45.5, -80.0], VE4: [53.0, -98.0],
  VE5: [53.0, -106.0], VE6: [54.0, -115.0], VE7: [53.0, -123.0], VE8: [64.0, -120.0],
  VE9: [46.5, -66.0], VO1: [47.6, -53.0], VO2: [53.5, -60.0],
  VY1: [63.0, -135.0], VY2: [46.3, -63.1], VY0: [64.0, -95.0],
};
export function refineDomestic(call, entity, lat, lon) {
  const core = callCore(call);
  if (entity === 'United States of America') {
    const m = /\d/.exec(core);
    if (m && US_AREAS[m[0]]) return [US_AREAS[m[0]][0], US_AREAS[m[0]][1], true];
    return [lat, lon, true];
  }
  if (entity === 'Canada') {
    const m = /^(VE|VA|VO|VY)(\d)/.exec(core);
    if (m) {
      const grp = (m[1] === 'VE' || m[1] === 'VA') ? 'VE' : m[1];
      const key = grp + m[2];
      if (CA_AREAS[key]) return [CA_AREAS[key][0], CA_AREAS[key][1], true];
    }
    return [lat, lon, true];
  }
  return [lat, lon, false];
}
function zonePick(pdfZone, n1mmZone) {
  const z = (pdfZone || '').trim();
  if (isDigits(z)) return String(parseInt(z, 10));
  const nz = (n1mmZone || '').trim();
  if (isDigits(nz)) return String(parseInt(nz, 10));
  return z || (isDigits(nz) ? String(parseInt(nz, 10)) : '');
}
function logZones(qso) {
  const cq = (qso.CQZ || '').trim(), itu = (qso.ITUZ || '').trim();
  return [isDigits(itu) ? String(parseInt(itu, 10)) : '', isDigits(cq) ? String(parseInt(cq, 10)) : ''];
}
export function classify(qso) {
  const call = qso.CALL || '';
  const n1mmCont = ((qso.APP_N1MM_CONTINENT || qso.CONTINENT || qso.CONT || '')).trim().toUpperCase();
  const [rec, confident] = dxccMatch(call);
  const fromPdf = (r) => {
    const cont = (r.cont || '').split('/')[0];
    const cq = zonePick(r.cq, qso.CQZ);
    const itu = zonePick(r.itu, qso.ITUZ);
    return [cont, itu, cq, r.entity, 'pdf', r.lat ?? null, r.lon ?? null];
  };
  if (rec && confident) return fromPdf(rec);
  const irec = resolveItu(call);
  if (irec) {
    const [itu, cq] = logZones(qso);
    return [irec.cont, itu, cq, irec.country, 'itu', irec.lat ?? null, irec.lon ?? null];
  }
  if (rec) return fromPdf(rec);
  if (n1mmCont in CONTINENT_NAMES) {
    const [itu, cq] = logZones(qso);
    return [n1mmCont, itu, cq, null, 'log', null, null];
  }
  return [null, null, null, null, 'none', null, null];
}
function classifyCached(q) {
  if (q._LOGAN === undefined) q._LOGAN = classify(q);
  return q._LOGAN;
}

// --------------------------------------------------------------------------
// Band ordering / colors
// --------------------------------------------------------------------------
const BAND_ORDER = ['160M', '80M', '60M', '40M', '30M', '20M', '17M', '15M', '12M', '10M', '6M', '4M', '2M', '70CM'];
const BAND_COLORS = {
  '160M': '#8B0000', '80M': '#DC143C', '60M': '#FF6347', '40M': '#FF8C00',
  '30M': '#FFD700', '20M': '#2196F3', '17M': '#00CED1', '15M': '#32CD32',
  '12M': '#9370DB', '10M': '#FF69B4', '6M': '#A0522D', '4M': '#8FBC8F',
  '2M': '#708090', '70CM': '#556B2F',
};
const CONT_COLORS = { NA: '#2196F3', SA: '#FF9800', EU: '#4CAF50', AF: '#795548', AS: '#E91E63', OC: '#00BCD4', AN: '#9E9E9E' };

function bandIdx(b) { const i = BAND_ORDER.indexOf(b.toUpperCase()); return i < 0 ? BAND_ORDER.length : i; }
function bandSortCmp(a, b) {
  const ia = bandIdx(a), ib = bandIdx(b);
  if (ia !== ib) return ia - ib;
  const A = a.toUpperCase(), B = b.toUpperCase();
  return A < B ? -1 : A > B ? 1 : 0;
}
function sortBands(arr) { return [...arr].sort(bandSortCmp); }

function bestWindow(times, width) {
  let best = 0, at = null, left = 0;
  for (let right = 0; right < times.length; right++) {
    while (times[right] - times[left] >= width) left++;
    const c = right - left + 1;
    if (c > best) { best = c; at = times[left]; }
  }
  return [best, at];
}
function rate(n, spanMs) {
  const sec = spanMs / 1000;
  return (n > 1 && sec > 0) ? pyround(n / (sec / 3600), 1) : n;
}
function onMinutes(times, gapMin = 30) {
  let on = 0;
  for (let i = 1; i < times.length; i++) {
    const d = (times[i] - times[i - 1]) / MIN;
    if (d >= 0 && d < gapMin) on += d;
  }
  return on;
}
function fmtSpan(spanMs) {
  const totalMin = Math.floor(spanMs / MIN);
  const d = Math.floor(totalMin / 1440), rem = totalMin % 1440;
  const h = Math.floor(rem / 60), m = rem % 60;
  const parts = [];
  if (d) parts.push(d + 'd');
  if (h || d) parts.push(h + 'h');
  parts.push(m + 'm');
  return parts.join(' ');
}
function zoneKeyCmp(a, b) {
  const za = a[0], zb = b[0];
  const da = isDigits(za), db = isDigits(zb);
  if (da && db) return parseInt(za) - parseInt(zb);
  if (da !== db) return da ? -1 : 1;
  return za < zb ? -1 : za > zb ? 1 : 0;
}

// --------------------------------------------------------------------------
// Space weather
// --------------------------------------------------------------------------
export function solarAvailable() {
  const keys = Object.keys(SOLAR);
  if (!keys.length) return [false, null, null];
  keys.sort();
  return [true, keys[0], keys[keys.length - 1]];
}
export function solarDayRecord(dateStr) { return SOLAR[dateStr] || null; }
function conditionsAt(dt) {
  const rec = SOLAR[dateISO(dt)];
  if (!rec) return null;
  const block = Math.floor(dt.getUTCHours() / 3);
  return { sfi: rec.sfi, sfi_adj: rec.sfi_adj, ssn: rec.ssn, a: rec.a, k: rec.kp[block], kp_block: block, definitive: rec.definitive };
}

// --------------------------------------------------------------------------
// CBS-style text report
// --------------------------------------------------------------------------
const CBS_CONT_ORDER = ['NA', 'SA', 'EU', 'AS', 'AF', 'OC', 'AN'];
function cbsTitle(text, width) {
  const spaced = text.split(/\s+/).map(w => w.split('').join(' ')).join('   ');
  const pad = Math.max(0, width - spaced.length - 2);
  const left = Math.floor(pad / 2);
  return '-'.repeat(left) + ' ' + spaced + ' ' + '-'.repeat(pad - left);
}
function bestMinuteWindow(mins, widthMin) {
  const width = widthMin * MIN;
  let best = 0, at = null, left = 0;
  for (let right = 0; right < mins.length; right++) {
    while (mins[right] - mins[left] >= width) left++;
    if (right - left + 1 > best) { best = right - left + 1; at = mins[left]; }
  }
  return [best, at];
}
function rcvdZone(q) {
  for (const tok of (q.SRX_STRING || '').split(/\s+/)) {
    if (isDigits(tok)) { const v = parseInt(tok, 10); if (v >= 1 && v <= 40) return v; }
  }
  const cq = classifyCached(q)[2];
  const v = parseInt(cq, 10);
  return Number.isNaN(v) ? null : v;
}
const hhmm = (d) => p2(d.getUTCHours()) + p2(d.getUTCMinutes());

export function cbsReport(rows) {
  const gross = rows.length;
  if (!gross) return '';
  const seen = new Set(), net = [];
  for (const [dt, q] of rows) {
    const call = (q.CALL || '').toUpperCase().trim();
    const band = (q.BAND || 'Unknown').toUpperCase();
    const key = call + '\x00' + band + '\x00' + (q.MODE || '').toUpperCase();
    if (seen.has(key)) continue;
    seen.add(key);
    net.push([dt, q, call, band]);
  }
  const dupes = gross - net.length;
  const total = net.length;

  const bandSet = new Set(net.map(r => r[3]));
  const bands = sortBands(bandSet);
  const disp = {};
  for (const b of bands) disp[b] = (b.endsWith('M') && isDigits(b.slice(0, -1))) ? b.slice(0, -1) : b;
  const bandTot = new Counter();
  for (const r of net) bandTot.add(r[3]);

  const bandCells = (counter) => bands.map(b => rjust(counter.gv(b), 7)).join('');
  const matrixLine = (label, w1, counter, pct = false) => {
    const n = counter.total();
    let line = ljust(label, w1) + bandCells(counter) + rjust(n, 7);
    if (pct) line += rjust(fmt1(n / total * 100), 7);
    return line;
  };

  const L = [];
  const q0 = net[0][1];
  const contest = (q0.CONTEST_ID || '').trim();
  const station = new Counter();
  for (const r of net) station.add((r[1].STATION_CALLSIGN || '').toUpperCase().trim());
  station.delete('');
  let ops = (q0.APP_LOGAN_OPS || '').trim();
  if (!ops) {
    const set = new Set(net.map(r => (r[1].OPERATOR || '').toUpperCase().trim()));
    set.delete('');
    ops = [...set].sort().join(' ');
  }
  L.push('Contest statistics by logan');
  L.push('(format after "Cabrillo Statistics" by K5KA & N6TV)');
  L.push('');
  if (contest) L.push('CONTEST: ' + contest);
  if (station.size) L.push('CALLSIGN: ' + station.mostCommon()[0][0]);
  if (ops) L.push('OPERATORS: ' + ops);
  L.push('');

  // hourly rate table
  const hourBand = new Map();
  for (const [dt, , , b] of net) dget(hourBand, floorHour(dt).getTime()).add(b);
  const hourKeys = [...hourBand.keys()];
  const h0 = Math.min(...hourKeys), h1 = Math.max(...hourKeys);
  const nbuckets = Math.floor((h1 - h0) / HOUR) + 1;
  const full = nbuckets <= 170;
  const buckets = full ? Array.from({ length: nbuckets }, (_, k) => h0 + k * HOUR)
    : hourKeys.slice().sort((a, b) => a - b);
  let w1 = full ? 5 : 12;
  let width = w1 + 7 * bands.length + 21;
  L.push(cbsTitle('QSO Rate Summary', width));
  L.push(ljust('Hour', w1) + bands.map(b => rjust(disp[b], 7)).join('') + rjust('Rate', 7) + rjust('Total', 7) + rjust('Pct', 7));
  L.push('-'.repeat(width));
  let cum = 0;
  for (const hk of buckets) {
    const c = hourBand.get(hk) || new Counter();
    const n = c.total();
    cum += n;
    const hd = new Date(hk);
    const label = full ? hhmm(hd) : `${p2(hd.getUTCMonth() + 1)}-${p2(hd.getUTCDate())} ${p2(hd.getUTCHours())}Z`;
    L.push(ljust(label, w1) + bandCells(c) + rjust(n, 7) + rjust(cum, 7) + rjust(fmt1(cum / total * 100), 7));
  }
  L.push('-'.repeat(width - 14));
  L.push(ljust('Total', w1) + bandCells(bandTot) + rjust(total, 7));
  L.push('');
  L.push(`Gross QSOs=${gross}        Dupes=${dupes}        Net QSOs=${total}`);
  L.push('');
  L.push('Unique callsigns worked = ' + new Set(net.map(r => r[2])).size);
  L.push('');

  // best-window + per-minute
  const mins = net.map(r => floorMin(r[0]).getTime());
  for (const wmin of [60, 30, 10]) {
    const [n, at] = bestMinuteWindow(mins, wmin);
    if (at === null) continue;
    const end = new Date(at + (wmin - 1) * MIN);
    L.push(`The best ${wmin} minute rate was ${pyround(n * 60 / wmin)}/hour from ${hhmm(new Date(at))} to ${hhmm(end)}`);
  }
  L.push('');
  const perMin = new Counter();
  for (const m of mins) perMin.add(m);
  const hist = new Counter();
  for (const v of perMin.values()) hist.add(v);
  L.push('The best 1 minute rates were:');
  for (const qpm of [...hist.keys()].sort((a, b) => b - a))
    L.push(`${rjust(qpm, 2)} QSOs/minute ${rjust(hist.gv(qpm), 4)} times.`);
  L.push('');

  // continent / country / multiplier matrices
  const contBand = new Map(), entBand = new Map(), zoneBand = new Map();
  for (const [, q, , b] of net) {
    const [cont, , , ent] = classifyCached(q);
    dget(contBand, cont || '??').add(b);
    dget(entBand, ent || '(unresolved)').add(b);
    const z = rcvdZone(q);
    if (z !== null) dget(zoneBand, p2(z)).add(b);
  }

  w1 = 14; width = w1 + 7 * bands.length + 14;
  L.push(cbsTitle('Continent Summary', width));
  L.push(ljust('', w1) + bands.map(b => rjust(disp[b], 7)).join('') + rjust('Total', 7) + rjust('Pct', 7));
  L.push('-'.repeat(width));
  const order = CBS_CONT_ORDER.filter(c => contBand.has(c)).concat(contBand.has('??') ? ['??'] : []);
  for (const c of order) L.push(matrixLine(CONTINENT_NAMES[c] || 'Unknown', w1, contBand.get(c), true));
  L.push('-'.repeat(width - 7));
  L.push(ljust('Total', w1) + bandCells(bandTot) + rjust(total, 7));
  L.push('');

  L.push('Number of letters in callsigns');
  L.push('Letters  # worked');
  L.push('-----------------');
  const letters = new Counter();
  for (const r of net) letters.add(r[2].length);
  for (const ln of [...letters.keys()].sort((a, b) => a - b)) L.push(rjust(ln, 4) + rjust(letters.gv(ln), 10));
  L.push('');

  const entKeys = [...entBand.keys()];
  w1 = Math.min(Math.max(8, ...entKeys.map(e => e.length)) + 2, 30);
  width = w1 + 7 * bands.length + 14;
  L.push(cbsTitle('Country Summary', width));
  L.push(ljust('Country', w1) + bands.map(b => rjust(disp[b], 7)).join('') + rjust('Total', 7) + rjust('Pct', 7));
  L.push('-'.repeat(width));
  for (const e of entKeys.slice().sort()) L.push(matrixLine(e.slice(0, w1 - 1), w1, entBand.get(e), true));
  L.push('-'.repeat(width - 7));
  L.push(ljust('Total', w1) + bandCells(bandTot) + rjust(total, 7));
  L.push('');

  if (zoneBand.size) {
    w1 = 5; width = w1 + 7 * bands.length + 14;
    L.push(cbsTitle('Multiplier Summary', width));
    L.push(ljust('Mult', w1) + bands.map(b => rjust(disp[b], 7)).join('') + rjust('Total', 7) + rjust('Pct', 7));
    L.push('-'.repeat(width));
    const zk = [...zoneBand.keys()].sort((a, b) => {
      const d = zoneBand.get(b).total() - zoneBand.get(a).total();
      return d !== 0 ? d : (a < b ? -1 : a > b ? 1 : 0);
    });
    for (const z of zk) L.push(matrixLine(z, w1, zoneBand.get(z), true));
    L.push('-'.repeat(width - 7));
    let ztot = 0; const zt = new Counter();
    for (const c of zoneBand.values()) { ztot += c.total(); for (const [k, v] of c) zt.add(k, v); }
    L.push(ljust('Total', w1) + bandCells(zt) + rjust(ztot, 7));
    L.push('');
  }

  // multi / single band
  const callBands = new Map();
  for (const [, , c, b] of net) { let s = callBands.get(c); if (!s) { s = new Set(); callBands.set(c, s); } s.add(b); }
  const nbHist = new Counter();
  for (const v of callBands.values()) nbHist.add(v.size);
  L.push('Multi-band QSOs');
  L.push('---------------');
  for (const nb of [...nbHist.keys()].sort((a, b) => a - b)) L.push(`${nb} bands  ${rjust(nbHist.gv(nb), 6)}`);
  if (bands.length > 1) {
    const allband = [...callBands.entries()].filter(([, v]) => v.size === bands.length).map(([c]) => c).sort();
    if (allband.length) {
      L.push('');
      L.push(`The following stations were worked on ${bands.length} bands:`);
      L.push('');
      for (let i = 0; i < allband.length; i += 6) L.push(allband.slice(i, i + 6).map(c => ljust(c, 12)).join(''));
    }
  }
  L.push('');

  const single = new Counter();
  for (const [c, v] of callBands) if (v.size === 1) single.add([...v][0]);
  width = 5 + 7 * bands.length;
  L.push(cbsTitle('Single Band QSOs', width));
  L.push(ljust('Band', 5) + bands.map(b => rjust(disp[b], 7)).join(''));
  L.push('-'.repeat(width));
  L.push(ljust('QSOs', 5) + bandCells(single));
  L.push('');
  return L.join('\n');
}

// --------------------------------------------------------------------------
// analyze -> JSON-able report
// --------------------------------------------------------------------------
export function analyze(qsos, sources, opts = null) {
  opts = opts || {};
  const fbands = (opts.bands && opts.bands.length) ? new Set(opts.bands.map(b => b.toUpperCase())) : null;
  const fconts = (opts.conts && opts.conts.length) ? new Set(opts.conts.map(c => c.toUpperCase())) : null;

  const rows = [];
  let skipped = 0;
  const bandsSeen = new Set(), contsSeen = new Set();
  for (const q of qsos) {
    const dt = qsoDatetime(q);
    if (dt === null) { skipped++; continue; }
    const band = (q.BAND || 'Unknown').toUpperCase();
    const cont = classifyCached(q)[0];
    bandsSeen.add(band);
    if (cont) contsSeen.add(cont);
    if (fbands && !fbands.has(band)) continue;
    if (fconts && !fconts.has(cont || '??')) continue;
    rows.push([dt, q]);
  }
  rows.sort((a, b) => a[0] - b[0]);
  const total = rows.length;

  const out = {
    meta: {
      sources, total, skipped,
      all_bands: sortBands(bandsSeen),
      all_conts: [...contsSeen].sort(),
      filter_bands: sortBands(fbands || []),
      filter_conts: [...(fconts || [])].sort(),
    }
  };
  if (!total) return out;

  const start = rows[0][0], end = rows[rows.length - 1][0];
  const spanMs = end - start;

  const uniq = new Set();
  for (const [, q] of rows) { const c = (q.CALL || '').toUpperCase().trim(); if ((q.CALL || '').trim()) uniq.add(c); }

  const bandCounts = new Counter(), hourCounts = new Counter(), dayCounts = new Counter();
  const modeCounts = new Counter(), contCounts = new Counter(), cqCounts = new Counter();
  const ituCounts = new Counter(), dxccCounts = new Counter(), srcCounts = new Counter();
  const runspCounts = new Counter(), stationCounts = new Counter(), kdist = new Counter();
  const hourBand = new Map();
  const bandFirst = {}, bandLast = {}, contFirst = {}, contLast = {};
  const dxccCont = {}, dxccCoord = {}, dxccFirst = {}, dxccLast = {};
  const opStats = new Map();
  let contUnknown = 0, pointsTotal = 0, pointsN = 0, condN = 0;
  const sfiV = [], ssnV = [], aV = [], kV = [];
  const dirQsos = [];
  const tl = new Map();

  for (const [dt, q] of rows) {
    const call = (q.CALL || '').toUpperCase();
    const band = (q.BAND || 'Unknown').toUpperCase();
    bandCounts.add(band);
    if (!(band in bandFirst)) bandFirst[band] = dt;
    bandLast[band] = dt;
    hourCounts.add(dt.getUTCHours());
    dget(hourBand, dt.getUTCHours()).add(band);
    dayCounts.add(dateISO(dt));
    const mode = (q.MODE || '?').toUpperCase();
    modeCounts.add(mode);

    const [cont, itu, cq, entity, source, lat, lon] = classifyCached(q);
    srcCounts.add(source);
    const info = { dt: isoNaive(dt), call, band, entity: entity || '' };
    if (cont === null) contUnknown++;
    else { contCounts.add(cont); if (!(cont in contFirst)) contFirst[cont] = info; contLast[cont] = info; }
    if (cq) cqCounts.add(cq);
    if (itu) ituCounts.add(itu);
    if (entity) {
      dxccCounts.add(entity);
      dxccCont[entity] = cont;
      dxccCoord[entity] = [lat, lon];
      if (!(entity in dxccFirst)) dxccFirst[entity] = info;
      dxccLast[entity] = info;
    }
    if (lat !== null && lon !== null) {
      const [rlat, rlon, dom] = refineDomestic(call, entity, lat, lon);
      dirQsos.push({ h: dt.getUTCHours(), d: dateISO(dt), band, mode, lat: rlat, lon: rlon, dom });
    }
    const rs = (q.APP_N1MM_RUN1RUN2 || '').trim();
    if (rs === '1' || rs === '2') runspCounts.add(rs === '1' ? 'Run' : 'S&P');
    const pts = (q.APP_N1MM_POINTS || '').trim();
    const hasPts = isDigits(pts.replace(/^-/, ''));
    if (hasPts) { pointsTotal += parseInt(pts, 10); pointsN++; }
    const op = (q.OPERATOR || '').toUpperCase().trim();
    if (op) {
      let s = opStats.get(op);
      if (!s) { s = { count: 0, bands: new Counter(), first: dt, times: [], calls: new Set(), entities: new Set(), pts: 0, ptsN: 0 }; opStats.set(op, s); }
      s.count++; s.bands.add(band); s.last = dt; s.times.push(dt);
      if (call.trim()) s.calls.add(call.trim());
      if (entity) s.entities.add(entity);
      if (hasPts) { s.pts += parseInt(pts, 10); s.ptsN++; }
    }
    const st = (q.STATION_CALLSIGN || '').toUpperCase().trim();
    if (st) stationCounts.add(st);

    const cond = conditionsAt(dt);
    if (cond) {
      condN++;
      if (cond.sfi !== null) sfiV.push(cond.sfi);
      if (cond.ssn !== null) ssnV.push(cond.ssn);
      if (cond.a !== null) aV.push(cond.a);
      if (cond.k !== null) { kV.push(cond.k); kdist.add(Math.min(9, Math.floor(cond.k))); }
    }
    const key = `${dateISO(dt)} ${p2(dt.getUTCHours())}Z`;
    let b = tl.get(key);
    if (!b) { b = { label: key, count: 0, ksum: 0, kn: 0, bands: new Counter(), conts: new Counter() }; tl.set(key, b); }
    b.count++; b.bands.add(band);
    if (cont) b.conts.add(cont);
    if (cond && cond.k !== null) { b.ksum += cond.k; b.kn++; }
  }

  const times = rows.map(r => r[0]);
  const bw = (width) => { const [n, at] = bestWindow(times, width); return { n, at: at ? isoNaive(at) : null }; };
  const qph = (total > 1 && spanMs > 0) ? total / (spanMs / HOUR) : total;
  const bandsPresent = sortBands([...bandCounts.keys()]);

  const avg = (v) => v.length ? pyround(v.reduce((s, x) => s + x, 0) / v.length, 1) : null;

  Object.assign(out.meta, {
    unique_calls: uniq.size,
    start: isoNaive(start), end: isoNaive(end),
    span: fmtSpan(spanMs), days: dayCounts.size,
    qph: pyround(qph, 1),
    best60: bw(60 * MIN), best10: bw(10 * MIN),
    n_bands: bandCounts.size, n_modes: modeCounts.size,
    n_dxcc: dxccCounts.size, n_cont: contCounts.size,
    n_cq: cqCounts.size, n_itu: ituCounts.size,
    src_pdf: srcCounts.gv('pdf'), src_itu: srcCounts.gv('itu'), src_log: srcCounts.gv('log'),
    cont_unknown: contUnknown,
    has_dxcc: hasDxcc(),
    points: pointsN ? pointsTotal : null,
    points_per_q: pointsN ? pyround(pointsTotal / pointsN, 2) : null,
    n_ops: opStats.size,
  });

  let home = null;
  if (stationCounts.size) {
    const call = stationCounts.mostCommon()[0][0];
    const hrec = resolveDxcc(call);
    if (hrec && hrec.lat !== undefined && hrec.lat !== null) {
      const [hlat, hlon] = refineDomestic(call, hrec.entity, hrec.lat, hrec.lon);
      home = { call, lat: hlat, lon: hlon, entity: hrec.entity };
    }
  }
  out.meta.home = home;

  const [sHas, sLo, sHi] = solarAvailable();
  out.meta.solar = {
    available: sHas, earliest: sLo, latest: sHi, cond_qsos: condN,
    sfi_avg: avg(sfiV), sfi_min: sfiV.length ? Math.min(...sfiV) : null, sfi_max: sfiV.length ? Math.max(...sfiV) : null,
    ssn_avg: avg(ssnV), ssn_min: ssnV.length ? Math.min(...ssnV) : null, ssn_max: ssnV.length ? Math.max(...ssnV) : null,
    a_avg: avg(aV), a_max: aV.length ? Math.max(...aV) : null,
    k_avg: avg(kV), k_max: kV.length ? pyround(Math.max(...kV), 1) : null,
  };
  out.kdist = Array.from({ length: 10 }, (_, k) => ({ k, count: kdist.gv(k) }));

  const tlSorted = [...tl.keys()].sort().map(k => tl.get(k));
  let cum = 0; const timeline = [];
  for (const b of tlSorted) {
    cum += b.count;
    timeline.push({
      label: b.label, count: b.count, cum,
      k: b.kn ? pyround(b.ksum / b.kn, 2) : null,
      bands: Object.fromEntries(b.bands), conts: Object.fromEntries(b.conts),
    });
  }
  out.timeline = timeline;
  out.runsp = runspCounts.mostCommon().map(([kind, count]) => ({ kind, count }));

  const opSorted = [...opStats.entries()].sort((a, b) => b[1].count - a[1].count);
  out.operators = opSorted.map(([op, s]) => {
    const spanO = s.last - s.first;
    const [b60] = bestWindow(s.times, 60 * MIN);
    const [b10] = bestWindow(s.times, 10 * MIN);
    return {
      op, count: s.count, unique: s.calls.size, ndxcc: s.entities.size,
      first: isoNaive(s.first), last: isoNaive(s.last),
      span: fmtSpan(spanO), qph: rate(s.count, spanO),
      on_h: pyround(onMinutes(s.times) / 60, 1),
      best60: b60, best10: b10,
      bands: Object.fromEntries(sortBands([...s.bands.keys()]).map(b => [b, s.bands.gv(b)])),
      points: s.ptsN ? s.pts : null,
    };
  });
  out.dirqsos = dirQsos;
  out.hours = Array.from({ length: 24 }, (_, h) => ({ hour: h, total: hourCounts.gv(h), bands: hourBand.has(h) ? Object.fromEntries(hourBand.get(h)) : {} }));
  out.bandOrder = bandsPresent;
  out.bandColors = Object.fromEntries(bandsPresent.map(b => [b, BAND_COLORS[b] || '#9E9E9E']));
  out.bands = bandsPresent.map(b => ({
    band: b, count: bandCounts.gv(b), pct: pyround(bandCounts.gv(b) / total * 100, 1),
    first: isoNaive(bandFirst[b]), last: isoNaive(bandLast[b]),
  }));
  out.modes = modeCounts.mostCommon().map(([mode, count]) => ({ mode, count }));
  out.continents = contCounts.mostCommon().map(([c, n]) => ({
    code: c, name: CONTINENT_NAMES[c] || c, count: n, pct: pyround(n / total * 100, 1),
    color: CONT_COLORS[c] || '#9E9E9E', first: contFirst[c], last: contLast[c],
  }));
  out.cq = [...cqCounts.entries()].sort(zoneKeyCmp).map(([zone, count]) => ({ zone, count }));
  out.itu = [...ituCounts.entries()].sort(zoneKeyCmp).map(([zone, count]) => ({ zone, count }));
  out.dxcc = dxccCounts.mostCommon().map(([e, n]) => ({
    entity: e, cont: dxccCont[e] || '', count: n,
    color: CONT_COLORS[dxccCont[e] || ''] || '#9E9E9E',
    lat: (dxccCoord[e] || [null, null])[0], lon: (dxccCoord[e] || [null, null])[1],
    rank: RARE[DXCC_CODE[e]] ?? null,
    first: dxccFirst[e], last: dxccLast[e],
  }));
  out.meta.n_rare = [...dxccCounts.keys()].filter(e => RARE[DXCC_CODE[e]]).length;

  const days = [];
  for (const d of [...dayCounts.keys()].sort()) {
    const rec = solarDayRecord(d);
    days.push({ date: d, count: dayCounts.gv(d), sfi: rec ? rec.sfi : null, ssn: rec ? rec.ssn : null, a: rec ? rec.a : null });
  }
  out.days = days;
  out.cbs = cbsReport(rows);
  return out;
}

// Convenience: parse the multi-file upload body used by the old /analyze, then
// analyze. Kept so app.js can mirror the server's request handling exactly.
export function analyzeUpload(chunks) {
  const sources = [], texts = [];
  for (const chunk of chunks) {
    if (!chunk.trim()) continue;
    if (chunk.startsWith('NAME:')) {
      const nl = chunk.indexOf('\n');
      sources.push(chunk.slice(5, nl < 0 ? undefined : nl).trim());
      texts.push(nl < 0 ? '' : chunk.slice(nl + 1));
    } else { sources.push('log'); texts.push(chunk); }
  }
  const qsos = [];
  for (const t of texts) qsos.push(...recordsFromText(t));
  return { qsos, sources: sources.length ? sources : ['log'] };
}
