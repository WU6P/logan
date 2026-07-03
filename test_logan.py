#!/usr/bin/env python3
"""Tests for logan.  Run:  python3 -m pytest test_logan.py   (or python3 test_logan.py)"""

import json
import unittest
from datetime import datetime
from pathlib import Path

import logan
import solar

HERE = Path(__file__).resolve().parent
DOC = HERE / "doc"

SAMPLE = (
    "Some header text\n<EOH>\n"
    " <CALL:5>DL1AB <QSO_DATE:8>20260204 <TIME_ON:6>201500 <BAND:3>20M "
    "<MODE:2>CW <OPERATOR:4>WU6P <STATION_CALLSIGN:4>WU6P <APP_N1MM_CONTINENT:2>EU "
    "<APP_N1MM_RUN1RUN2:1>1 <APP_N1MM_POINTS:1>2 <CQZ:2>14 <EOR>\n"
    " <CALL:4>W1AW <QSO_DATE:8>20260204 <TIME_ON:6>203000 <BAND:3>20M "
    "<MODE:2>CW <OPERATOR:4>WU6P <STATION_CALLSIGN:4>WU6P <APP_N1MM_CONTINENT:2>NA "
    "<APP_N1MM_RUN1RUN2:1>2 <APP_N1MM_POINTS:1>1 <CQZ:1>5 <EOR>\n"
    " <CALL:5>JA1XY <QSO_DATE:8>20260204 <TIME_ON:6>211500 <BAND:3>15M "
    "<MODE:2>CW <OPERATOR:4>N6XX <APP_N1MM_POINTS:1>3 <EOR>\n"
    " <CALL:4>NODT <BAND:3>40M <EOR>\n"          # no date -> skipped
)


class TestAdifParsing(unittest.TestCase):
    def test_records_and_header_skip(self):
        recs = logan.parse_adif_records(SAMPLE)
        self.assertEqual(len(recs), 4)
        self.assertEqual(recs[0]["CALL"], "DL1AB")
        self.assertEqual(recs[0]["BAND"], "20M")

    def test_datetime(self):
        recs = logan.parse_adif_records(SAMPLE)
        self.assertEqual(logan.qso_datetime(recs[0]),
                         datetime(2026, 2, 4, 20, 15, 0))
        self.assertIsNone(logan.qso_datetime(recs[3]))  # missing date

    def test_tag_inside_value_not_confused(self):
        txt = "<EOH> <CALL:4>W1AW <COMMENT:11>a<EOR>b tag <BAND:3>20M <EOR>"
        recs = logan.parse_adif_records(txt)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["COMMENT"], "a<EOR>b tag")
        self.assertEqual(recs[0]["BAND"], "20M")


class TestDxcc(unittest.TestCase):
    def test_major_prefixes(self):
        cases = {
            "W1AW": ("NA", "United States of America"),
            "DL1AB": ("EU", "Fed. Rep. of Germany"),
            "G4XYZ": ("EU", "England"),
            "JA1XYZ": ("AS", "Japan"),
            "VK3ABC": ("OC", "Australia"),
            "9A1AA": ("EU", "Croatia"),
            "S59ABC": ("EU", "Slovenia"),
            "PY2XX": ("SA", "Brazil"),
        }
        for call, (cont, ent) in cases.items():
            rec = logan.resolve_dxcc(call)
            self.assertIsNotNone(rec, call)
            self.assertEqual(rec["cont"].split("/")[0], cont, call)
            self.assertEqual(rec["entity"], ent, call)

    def test_portable_core_extraction(self):
        self.assertEqual(logan._call_core("CT9/HA7GN"), "CT9")
        self.assertEqual(logan._call_core("W1AW/7"), "W1AW")
        self.assertEqual(logan._call_core("DL/W1AW/P"), "DL")

    def test_zones_present(self):
        rec = logan.resolve_dxcc("JA1XYZ")
        self.assertEqual(rec["itu"], "45")
        self.assertEqual(rec["cq"], "25")

    def test_coordinates_present_and_sane(self):
        # lat/lon attached from cty.dat; sign convention is +N / +E.
        de = logan.resolve_dxcc("DL1AB")
        self.assertGreater(de["lat"], 40)            # Germany northern hemi
        self.assertGreater(de["lon"], 0)             # east of Greenwich
        jp = logan.resolve_dxcc("JA1XYZ")
        self.assertGreater(jp["lon"], 100)           # Japan far east
        za = logan.resolve_dxcc("ZS6ABC")
        self.assertLess(za["lat"], 0)                # South Africa southern hemi


class TestDomestic(unittest.TestCase):
    def test_us_call_area_centroids_differ(self):
        e = "United States of America"
        w1 = logan.refine_domestic("K1ABC", e, 37.6, -91.87)
        w6 = logan.refine_domestic("W6XYZ", e, 37.6, -91.87)
        w7 = logan.refine_domestic("N7QQ", e, 37.6, -91.87)
        self.assertTrue(w1[2] and w6[2] and w7[2])      # all flagged domestic
        self.assertNotEqual((w1[0], w1[1]), (w6[0], w6[1]))
        self.assertLess(w6[1], w1[1])                   # California west of New England
        self.assertGreater(w7[0], w6[0])                # NW north of California

    def test_canada_districts(self):
        bc = logan.refine_domestic("VE7AA", "Canada", 0, 0)
        on = logan.refine_domestic("VA3AA", "Canada", 0, 0)   # VA -> VE provinces
        self.assertLess(bc[1], on[1])                   # BC west of Ontario
        self.assertTrue(bc[2])

    def test_dx_not_flagged_or_moved(self):
        lat, lon, dom = logan.refine_domestic("DL1AB", "Fed. Rep. of Germany",
                                              51.0, 10.0)
        self.assertFalse(dom)
        self.assertEqual((lat, lon), (51.0, 10.0))


class TestClassify(unittest.TestCase):
    def test_pdf_overrides_logger(self):
        # DL by prefix is EU; even if the logger said something else, PDF wins.
        q = {"CALL": "DL1AB", "APP_N1MM_CONTINENT": "AF"}
        cont, itu, cq, ent, src, lat, lon = logan.classify(q)
        self.assertEqual(cont, "EU")
        self.assertEqual(src, "pdf")
        self.assertEqual(ent, "Fed. Rep. of Germany")

    def test_itu_fallback_when_not_in_dxcc(self):
        # 3G (Chile) is in the ITU table but not the DXCC prefix list; the ITU
        # source resolves it before falling back to the logger's field.
        cont, itu, cq, ent, src, lat, lon = logan.classify(
            {"CALL": "3G0YR", "APP_N1MM_CONTINENT": "SA"})
        self.assertEqual(cont, "SA")
        self.assertEqual(src, "itu")
        self.assertEqual(ent, "Chile")
        self.assertIsNotNone(lat)

    def test_logger_fallback_when_neither_resolves(self):
        # A Q-series prefix is allocated to neither amateur DXCC nor the ITU
        # table, so the logger's CONTINENT is the last resort.
        cont, itu, cq, ent, src, lat, lon = logan.classify(
            {"CALL": "QZ1ABC", "APP_N1MM_CONTINENT": "EU"})
        self.assertEqual(cont, "EU")
        self.assertEqual(src, "log")

    def test_source_priority_dxcc_over_itu(self):
        # A prefix in both must come from DXCC (full zones/entity), not ITU.
        c = logan.classify({"CALL": "JA1XYZ", "APP_N1MM_CONTINENT": "EU"})
        self.assertEqual(c[4], "pdf")
        self.assertEqual(c[0], "AS")

    def test_zone_range_falls_back_to_logger_specific(self):
        # USA's PDF CQ zone is a range "3,4,5"; the logger's CQZ pins it to 5.
        q = {"CALL": "W1AW", "CQZ": "5"}
        cont, itu, cq, ent, src, lat, lon = logan.classify(q)
        self.assertEqual(cont, "NA")
        self.assertEqual(cq, "5")


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.recs = logan.parse_adif_records(SAMPLE)
        self.r = logan.analyze(self.recs, ["sample"])

    def test_meta_counts(self):
        m = self.r["meta"]
        self.assertEqual(m["total"], 3)         # 4 records, 1 undated
        self.assertEqual(m["skipped"], 1)
        self.assertEqual(m["unique_calls"], 3)
        self.assertEqual(m["n_bands"], 2)       # 20M, 15M

    def test_continents_and_first_last(self):
        conts = {c["code"]: c for c in self.r["continents"]}
        self.assertIn("EU", conts)
        self.assertEqual(conts["EU"]["first"]["call"], "DL1AB")
        self.assertEqual(conts["AS"]["count"], 1)

    def test_timeline_cumulative(self):
        tl = self.r["timeline"]
        self.assertTrue(tl)
        self.assertEqual(tl[-1]["cum"], 3)      # cumulative ends at total
        self.assertEqual(sum(b["count"] for b in tl), 3)

    def test_runsp_points_ops(self):
        self.assertEqual({r["kind"]: r["count"] for r in self.r["runsp"]},
                         {"Run": 1, "S&P": 1})
        self.assertEqual(self.r["meta"]["points"], 6)  # 2+1+3
        self.assertEqual(self.r["meta"]["n_ops"], 2)

    def test_dxcc_has_coordinates(self):
        dx = {e["entity"]: e for e in self.r["dxcc"]}
        self.assertIn("Fed. Rep. of Germany", dx)
        self.assertIsNotNone(dx["Fed. Rep. of Germany"]["lat"])
        self.assertIsNotNone(dx["Fed. Rep. of Germany"]["lon"])

    def test_home_station_from_station_callsign(self):
        home = self.r["meta"]["home"]
        self.assertIsNotNone(home)
        self.assertEqual(home["call"], "WU6P")
        self.assertIsNotNone(home["lat"])
        # WU6P (call area 6) should refine to California, not the US centroid.
        self.assertLess(home["lon"], -110)
        self.assertAlmostEqual(home["lon"], -120.0, places=1)

    def test_band_filter(self):
        r = logan.analyze(self.recs, ["s"], {"bands": ["20M"]})
        self.assertEqual(r["meta"]["total"], 2)
        self.assertEqual(r["meta"]["filter_bands"], ["20M"])

    def test_continent_filter(self):
        r = logan.analyze(self.recs, ["s"], {"conts": ["EU"]})
        self.assertEqual(r["meta"]["total"], 1)
        self.assertEqual(r["continents"][0]["code"], "EU")

    def test_rare_entity_flagged(self):
        # A stray P5 (North Korea) — the classic bust — is flagged most-wanted #1.
        recs = logan.parse_adif_records(
            "<EOH> <CALL:4>P55W <QSO_DATE:8>20260219 <TIME_ON:6>034256 "
            "<BAND:3>40M <MODE:2>CW <EOR>")
        r = logan.analyze(recs, ["s"])
        nk = [e for e in r["dxcc"] if e["entity"] == "DPR of Korea"]
        self.assertEqual(len(nk), 1)
        self.assertEqual(nk[0]["rank"], 1)
        self.assertEqual(r["meta"]["n_rare"], 1)
        # A common entity carries no rank.
        common = logan.analyze(logan.parse_adif_records(
            "<EOH> <CALL:4>W1AW <QSO_DATE:8>20260219 <TIME_ON:6>034200 "
            "<BAND:3>20M <EOR>"), ["s"])
        self.assertIsNone(common["dxcc"][0]["rank"])
        self.assertEqual(common["meta"]["n_rare"], 0)


CAB_SAMPLE = """START-OF-LOG: 3.0
CONTEST: CQ-WW-CW
CALLSIGN: N6RO
OPERATORS: WU6P N6RO
QSO: 28026 CW 2024-11-23 0000 N6RO 599 3 VK9DX 599 32
QSO: 14035 CW 2024-11-23 0001 N6RO 599 3 K3ATO 599 05
QSO: 14035 CW 2024-11-23 0102 N6RO 599 3 K3ATO 599 05
X-QSO: 7021 CW 2024-11-23 0203 N6RO 599 3 W1AW 599 05
END-OF-LOG:
"""


class TestCabrillo(unittest.TestCase):
    def test_parse_fields(self):
        recs = logan.parse_cabrillo_records(CAB_SAMPLE)
        self.assertEqual(len(recs), 3)               # X-QSO skipped
        q = recs[0]
        self.assertEqual(q["CALL"], "VK9DX")
        self.assertEqual(q["BAND"], "10M")
        self.assertEqual(q["MODE"], "CW")
        self.assertEqual(q["QSO_DATE"], "20241123")
        self.assertEqual(q["TIME_ON"], "000000")
        self.assertEqual(q["RST_RCVD"], "599")
        self.assertEqual(q["SRX_STRING"], "32")
        self.assertEqual(q["STATION_CALLSIGN"], "N6RO")
        self.assertEqual(q["CONTEST_ID"], "CQ-WW-CW")
        self.assertEqual(q["APP_LOGAN_OPS"], "WU6P N6RO")

    def test_khz_to_band(self):
        self.assertEqual(logan.khz_to_band("1830"), "160M")
        self.assertEqual(logan.khz_to_band("3512"), "80M")
        self.assertEqual(logan.khz_to_band("7021"), "40M")
        self.assertEqual(logan.khz_to_band("21007"), "15M")
        self.assertEqual(logan.khz_to_band("nope"), "")

    def test_records_from_text_detects_format(self):
        self.assertEqual(len(logan.records_from_text(CAB_SAMPLE)), 3)
        self.assertEqual(len(logan.records_from_text(SAMPLE)), 4)

    def test_analyze_accepts_cabrillo(self):
        r = logan.analyze(logan.parse_cabrillo_records(CAB_SAMPLE), ["cab"])
        self.assertEqual(r["meta"]["total"], 3)
        self.assertEqual(r["meta"]["home"]["call"], "N6RO")


class TestOverrides(unittest.TestCase):
    def test_uk_m_series(self):
        for call, ent in [("MD4K", "Isle of Man"), ("MM0T", "Scotland"),
                          ("MW4R", "Wales"), ("M5B", "England"),
                          ("2E0ABC", "England"), ("MI0AB", "Northern Ireland")]:
            cont, _, _, e, _, _, _ = logan.classify({"CALL": call})
            self.assertEqual((cont, e), ("EU", ent), call)

    def test_guantanamo_only_two_letter_kg4(self):
        self.assertEqual(logan.classify({"CALL": "KG4MA"})[3], "Guantanamo Bay")
        for call in ("KG4IGC", "KG1E", "KG5TA"):
            self.assertEqual(logan.classify({"CALL": call})[3],
                             "United States of America", call)

    def test_portugal_islands_and_9w(self):
        self.assertEqual(logan.classify({"CALL": "CR3DX"})[3], "Madeira Is.")
        self.assertEqual(logan.classify({"CALL": "CQ3W"})[3], "Madeira Is.")
        self.assertEqual(logan.classify({"CALL": "CT8AB"})[3], "Azores")
        self.assertEqual(logan.classify({"CALL": "CT1AB"})[3], "Portugal")
        self.assertEqual(logan.classify({"CALL": "9W2VGR"})[3], "West Malaysia")


class TestCbsReport(unittest.TestCase):
    def test_small_sample(self):
        recs = logan.parse_cabrillo_records(CAB_SAMPLE)
        rows = sorted(((logan.qso_datetime(q), q) for q in recs),
                      key=lambda r: r[0])
        rep = logan.cbs_report(rows)
        self.assertIn("Gross QSOs=3        Dupes=1        Net QSOs=2", rep)
        self.assertIn("Unique callsigns worked = 2", rep)
        self.assertIn("CONTEST: CQ-WW-CW", rep)
        self.assertIn("OPERATORS: WU6P N6RO", rep)

    def test_empty(self):
        self.assertEqual(logan.cbs_report([]), "")

    def test_analyze_carries_report(self):
        r = logan.analyze(logan.parse_adif_records(SAMPLE), ["s"])
        self.assertIn("Q S O   R a t e   S u m m a r y", r["cbs"])

    def test_n6ro_2024_matches_reference_cbs(self):
        """The 2024 CQWW CW N6RO public log, cross-checked against the actual
        CBS (Cabrillo Statistics 10g) output for the same log."""
        p = DOC / "n6ro_2024cw.log"
        if not p.exists():
            self.skipTest("n6ro_2024cw.log not bundled")
        recs = logan.records_from_text(p.read_text(encoding="utf-8",
                                                   errors="replace"))
        self.assertEqual(len(recs), 6592)
        rows = sorted(((logan.qso_datetime(q), q) for q in recs),
                      key=lambda r: r[0])
        rep = logan.cbs_report(rows)
        # headline counts
        self.assertIn("Gross QSOs=6592        Dupes=90        Net QSOs=6502",
                      rep)
        self.assertIn("Unique callsigns worked = 3814", rep)
        # best-window rates, incl. the exact windows CBS reports
        self.assertIn("The best 60 minute rate was 409/hour"
                      " from 0005 to 0104", rep)
        self.assertIn("The best 30 minute rate was 452/hour"
                      " from 0011 to 0040", rep)
        self.assertIn("The best 10 minute rate was 492/hour"
                      " from 0018 to 0027", rep)
        # per-minute histogram, first and last lines
        self.assertIn("11 QSOs/minute    2 times.", rep)
        self.assertIn(" 1 QSOs/minute  675 times.", rep)
        # first hour row and the per-band totals row of the rate table
        self.assertRegex(rep, r"0000 +0 +7 +74 +85 +124 +112 +402 +402")
        self.assertRegex(rep, r"Total +108 +593 +1129 +1455 +1576 +1641 +6502")
        # callsign-length histogram
        self.assertRegex(rep, r"   3 +80\n +4 +1675\n +5 +1656\n +6 +3008")
        # multiplier (received CQ zone) rows match CBS exactly
        self.assertRegex(rep, r"25 +45 +288 +459 +232 +516 +597 +2137")
        self.assertRegex(rep, r"14 +0 +31 +124 +315 +309 +331 +1110")
        # multi-band histogram + the 33 six-band stations
        for line in ("1 bands    2513", "2 bands     579", "3 bands     286",
                     "4 bands     240", "5 bands     163", "6 bands      33"):
            self.assertIn(line, rep)
        self.assertIn("The following stations were worked on 6 bands:", rep)
        for call in ("KH6J", "ZF1A", "PJ2T", "JS2MKU"):
            self.assertIn(call, rep)
        # single-band station counts per band
        self.assertRegex(rep, r"QSOs +45 +189 +312 +631 +590 +746")
        # continent totals: Africa/SA/OC agree with CBS exactly
        self.assertRegex(rep, r"Africa +0 +9 +14 +16 +22 +18 +79")
        self.assertRegex(rep, r"South America +5 +9 +25 +29 +53 +115 +236")
        self.assertRegex(rep, r"Oceania +2 +21 +58 +21 +50 +77 +229")


class TestRealLogs(unittest.TestCase):
    """Validate against the bundled real logs if present."""

    def _load(self, name):
        p = DOC / name
        if not p.exists():
            self.skipTest(f"{name} not bundled")
        return logan.parse_adif_records(p.read_text(encoding="utf-8",
                                                    errors="replace"))

    def test_jidx_is_all_japan(self):
        r = logan.analyze(self._load("WU6P_JIDX.adi"), ["jidx"])
        self.assertEqual(r["meta"]["total"], 385)
        self.assertEqual(r["meta"]["n_cont"], 1)
        self.assertEqual(r["continents"][0]["code"], "AS")
        self.assertEqual(r["dxcc"][0]["entity"], "Japan")

    def test_2026_pdf_agrees_with_logger(self):
        recs = self._load("WU6P_2026.adi")
        agree = disagree = 0
        for q in recs:
            rec = logan.resolve_dxcc(q.get("CALL", ""))
            n1 = q.get("APP_N1MM_CONTINENT", "")
            if rec and n1:
                if n1 in rec["cont"]:
                    agree += 1
                else:
                    disagree += 1
        self.assertGreater(agree / (agree + disagree), 0.97)  # >97% agreement


class TestSolar(unittest.TestCase):
    def test_snapshot_available(self):
        has, lo, hi = solar.available()
        self.assertTrue(has)
        self.assertLessEqual(lo, "2010-01-01")

    def test_conditions_with_time_matched_k(self):
        # 2026-02-05 is a known disturbed day; 04:00 UT is Kp block 1.
        c = solar.conditions_at(datetime(2026, 2, 5, 4, 0))
        self.assertIsNotNone(c)
        self.assertEqual(c["kp_block"], 1)
        self.assertGreater(c["sfi"], 100)        # high solar flux
        self.assertIsNotNone(c["ssn"])
        self.assertGreater(c["k"], 4)            # storm-level K that block
        # A quiet block of the same day has a lower K.
        c2 = solar.conditions_at(datetime(2026, 2, 5, 21, 0))
        self.assertLess(c2["k"], c["k"])

    def test_missing_date_returns_none(self):
        self.assertIsNone(solar.conditions_at(datetime(1900, 1, 1, 0, 0)))

    def test_refresh_uses_nowcast_when_current_else_full(self):
        # Offline simulation: stub the two GFZ downloads and watch which the
        # gap logic chooses, without touching the real snapshot file.
        import tempfile
        hdr = "# header line\n#YYY MM DD ... Ap SN F10.7obs F10.7adj D\n"

        def row(d, kp="1.000"):
            base = f"{d.year} {d.month:02d} {d.day:02d} 0 0 0 0 "
            return base + (kp + " ") * 8 + ("4 " * 8) + "4 100 150.0 145.0 1"

        def make(dates):
            return hdr + "\n".join(row(x) for x in dates) + "\n"

        from datetime import date, timedelta
        nc_days = [date(2026, 6, 1) + timedelta(days=i) for i in range(20)]
        nowcast = make(nc_days)
        # The real archive runs through ~yesterday; cover up to the nowcast
        # window. The full-archive guard wants F10.7 in the text and >10000 chars.
        archive = make([date(2024, 1, 1) + timedelta(days=i) for i in range(900)])

        fetched = []

        def fake_fetch(url, timeout):
            fetched.append("nowcast" if "nowcast" in url else "full")
            return nowcast if "nowcast" in url else archive

        orig_fetch, orig_data = solar._fetch, solar.DATA
        try:
            with tempfile.TemporaryDirectory() as td:
                solar._fetch = fake_fetch
                solar.DATA = Path(td) / "kp.txt"
                # current snapshot ending the day before the nowcast -> no gap
                solar.DATA.write_text(make([date(2026, 5, 30), date(2026, 5, 31)]))
                solar._cache = None
                fetched.clear()
                ok, _ = solar.update()
                self.assertTrue(ok)
                self.assertNotIn("full", fetched)        # tiny refresh only

                # stale snapshot (2 months behind) -> gap -> pulls full archive
                solar.DATA.write_text(make([date(2026, 4, 1)]))
                solar._cache = None
                fetched.clear()
                ok, _ = solar.update()
                self.assertTrue(ok)
                self.assertIn("full", fetched)
                solar._cache = None
                self.assertIsNotNone(
                    solar.conditions_at(datetime(2026, 5, 15, 12)))  # gap filled
        finally:
            solar._fetch, solar.DATA, solar._cache = orig_fetch, orig_data, None

    def test_analyze_includes_solar(self):
        recs = logan.parse_adif_records(SAMPLE)
        r = logan.analyze(recs, ["s"])
        sw = r["meta"]["solar"]
        self.assertTrue(sw["available"])
        self.assertEqual(sw["cond_qsos"], 3)     # all 3 datable QSOs matched
        self.assertIsNotNone(sw["sfi_avg"])
        self.assertEqual(sum(x["count"] for x in r["kdist"]), 3)
        self.assertIsNotNone(r["days"][0]["sfi"])


class TestDataFile(unittest.TestCase):
    def test_dxcc_json_sane(self):
        d = json.loads((HERE / "dxcc.json").read_text())
        self.assertGreater(len(d["entities"]), 300)
        self.assertGreater(len(d["lookup"]), 500)
        for key in ("W", "DL", "JA", "G", "VK"):
            self.assertIn(key, d["lookup"], key)

    def test_rare_json_sane(self):
        d = json.loads((HERE / "rare.json").read_text())["rare"]
        self.assertGreater(len(d), 50)
        self.assertEqual(d["344"], 1)        # DPR of Korea is most-wanted #1

    def test_itu_json_sane(self):
        d = json.loads((HERE / "itu.json").read_text())["lookup"]
        self.assertGreater(len(d), 800)
        self.assertEqual(d["3G"]["country"], "Chile")
        self.assertEqual(d["3G"]["cont"], "SA")
        self.assertEqual(d["XM"]["cont"], "NA")        # Canada
        self.assertEqual(d["DS"]["cont"], "AS")        # Korea


if __name__ == "__main__":
    unittest.main(verbosity=2)
