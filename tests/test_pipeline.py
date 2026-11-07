# -*- coding: utf-8 -*-
"""用假伺服器跑整條管線（不連外網）。執行：python -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_server  # noqa: E402
from pipeline import build, fetch, newsfeed, ratesprob, run  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mock_server.start()
        cls.env = mock_server.env_for(cls.srv)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "config", self.tmp / "config")
        (self.tmp / "data" / "series").mkdir(parents=True)  # 每個測試從空歷史開始，不受 repo 內資料影響
        self._saved = dict(os.environ)
        os.environ.update(self.env)
        os.environ["ECO_BACKOFF"] = "0"
        for k in ("MOCK_FAIL", "MOCK_YAHOO_DOWN", "MOCK_JUMP", "MOCK_NEWS_BIG", "MOCK_AI_FAIL", "ANTHROPIC_API_KEY", "MOCK_POLY_FAIL", "MOCK_ZQ_RATE"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def go(self, *extra):
        return run.main(["--root", str(self.tmp), "--pause", "0", *extra])

    def doc(self, name):
        return json.loads((self.tmp / "data" / "site" / (name + ".json")).read_text(encoding="utf-8"))


class TestCrashDay(Base):
    def test_full_daily_run(self):
        self.assertEqual(self.go("--mode", "daily"), 0)
        ov = self.doc("overview")
        self.assertEqual(ov["mood"]["label"], "恐慌")
        self.assertEqual([g["id"] for g in ov["groups"]], ["us", "ai", "asia", "twd", "twd_c", "fx", "rates", "risk", "cmd", "f_dev", "f_em", "f_theme", "f_bond", "f_mix", "f_cash"])
        tiles = {t["id"]: t for g in ov["groups"] for t in g["tiles"]}
        self.assertEqual(len(tiles), 90)
        self.assertIn("silver", tiles)
        self.assertEqual([t["id"] for t in next(g for g in ov["groups"] if g["id"] == "rates")["tiles"]], ["us3m", "us2y", "us5y", "us10y", "us30y"])
        self.assertTrue(all(t.get("q") for t in tiles.values()))  # 每個方塊都能查新聞
        grp = {g["id"]: [t["id"] for t in g["tiles"]] for g in ov["groups"]}
        self.assertIn("usdtwd", grp["twd"])
        self.assertTrue({"twd_eur", "twd_jpy", "twd_cny", "twd_aud", "twd_zar"} <= set(grp["twd"]))
        self.assertTrue({"vix", "vxn", "move", "skew", "ovx", "gvz", "hyoas"} <= set(grp["risk"]))
        self.assertTrue({"audusd", "gbpusd", "usdcny", "usdzar"} <= set(grp["fx"]))
        self.assertAlmostEqual(tiles["twd_eur"]["value"], 1.1231 * 32.876, places=2)  # 歐元兌台幣＝EURUSD×USDTWD
        self.assertAlmostEqual(tiles["twd_jpy"]["value"], 32.876 / 157.9, places=4)  # 1 日圓，不乘 100
        self.assertAlmostEqual(tiles["twd_btc"]["value"], 98000.0 * 32.876, delta=1)  # 加密貨幣＝美元報價×USDTWD
        self.assertAlmostEqual(tiles["twd_usdt"]["value"], 1.0003 * 32.876, places=3)
        self.assertEqual(set(grp["twd_c"]), {"twd_usdt", "twd_usdc", "twd_btc", "twd_eth", "twd_sol"})
        self.assertNotIn("x_raw", grp)
        tags = [h["tag"] for h in ov["highlights"]]
        for need in ("新台幣", "國際匯率", "市場風險", "亞歐股市", "基金類別", "美債利率"):
            self.assertIn(need, tags)
        self.assertTrue(all(h["link"].startswith("#") and h["sub"] for h in ov["highlights"]))
        self.assertAlmostEqual(tiles["sox"]["chg"], -12.25, places=1)
        self.assertAlmostEqual(tiles["twii"]["value"], 41574.72, places=1)
        self.assertFalse(any(t["stale"] for t in tiles.values()))
        text = " ".join(h["text"] for h in ov["highlights"])
        self.assertIn("費半 −12.25%", text)
        self.assertIn("VIX", text)
        self.assertIn("尚未反映", text)  # ADR 跌 11%，台股台積電還停在昨天
        # 新加入的標的會自動補抓兩年歷史
        daily = (self.tmp / "data" / "series" / "daily.csv").read_text(encoding="utf-8")
        self.assertGreater(daily.count("\nmu,"), 400)
        macro = {m["id"]: m for m in self.doc("macro")}
        self.assertEqual(len(macro), 37)
        self.assertEqual(sum(1 for m in macro.values() if m["country"] == "us"), 15)
        self.assertIsNotNone(macro["us_cpi"]["next"])
        self.assertEqual(self.doc("lights")["products"][0]["id"], "twii")
        meta = self.doc("meta")
        self.assertEqual(meta["sources"]["yahoo"]["fail"], 0)
        self.assertEqual(meta["problems"], [])
        self.assertTrue((self.tmp / "data" / "site" / "overview.js").exists())  # file:// 備援

    def test_snapshot_skips_fred_when_present(self):
        self.go("--mode", "daily")
        before = (self.tmp / "data" / "series" / "fred.csv").read_text(encoding="utf-8")
        self.assertEqual(self.go("--mode", "snapshot"), 0)
        self.assertEqual(before, (self.tmp / "data" / "series" / "fred.csv").read_text(encoding="utf-8"))

    def test_idempotent(self):
        self.go("--mode", "daily")
        a = (self.tmp / "data" / "series" / "daily.csv").read_text(encoding="utf-8")
        self.go("--mode", "daily")
        self.assertEqual(a, (self.tmp / "data" / "series" / "daily.csv").read_text(encoding="utf-8"))


class TestFailures(Base):
    def test_some_symbols_fail(self):
        os.environ["MOCK_FAIL"] = "MU"
        self.go("--mode", "daily")  # 先有一份歷史：sox 抓得到、mu 一直抓不到
        os.environ["MOCK_FAIL"] = "MU,^SOX"
        self.assertEqual(self.go("--mode", "daily"), 2)  # 網站有更新，但有警告
        tiles = {t["id"]: t for g in self.doc("overview")["groups"] for t in g["tiles"]}
        self.assertNotIn("mu", tiles)  # 從沒抓到過 → 不顯示
        self.assertTrue(tiles["sox"]["stale"])  # 有舊資料 → 保留並標示
        self.assertIn("error", tiles["sox"])
        self.assertTrue(self.doc("meta")["problems"])

    def test_everything_down_keeps_old_site(self):
        self.go("--mode", "daily")
        before = (self.tmp / "data" / "site" / "overview.json").read_text(encoding="utf-8")
        os.environ["MOCK_YAHOO_DOWN"] = "1"
        self.assertEqual(self.go("--mode", "snapshot"), 1)
        self.assertEqual(before, (self.tmp / "data" / "site" / "overview.json").read_text(encoding="utf-8"))

    def test_absurd_price_is_rejected(self):
        self.go("--mode", "daily")  # 先建立歷史，才有「合理範圍」可比
        os.environ["MOCK_JUMP"] = "^TWII=2.0"
        self.go("--mode", "snapshot")
        tw = next(t for g in self.doc("overview")["groups"] for t in g["tiles"] if t["id"] == "twii")
        self.assertTrue(tw["stale"])
        self.assertIn("疑似資料異常", tw["error"])
        self.assertLess(tw["value"], 50000)  # 沒被寫入

    def test_offline_rebuild(self):
        self.assertEqual(self.go("--offline"), 0)
        self.assertEqual(self.doc("meta")["mode"], "offline")


class TestFundsAndNews(Base):
    def test_funds_built(self):
        self.assertEqual(self.go("--mode", "daily"), 0)
        d = self.doc("funds")
        self.assertEqual([t["id"] for t in d["tiers"]], [1, 2, 3, 4, 5, 6])
        for co in ("tlife", "nanshan"):
            comp = d["companies"][co]
            self.assertEqual(sum(comp["counts"].values()), comp["count"])
            for tier in "123456":
                picks = comp["picks"].get(tier, [])
                self.assertLessEqual(len(picks), 3)
                self.assertTrue(all(p["tier"] == int(tier) for p in picks))
                bases = [p["k"].split("-")[0] for p in picks]
                self.assertEqual(len(bases), len(set(bases)))  # 同一檔基金不重複推薦
            self.assertGreaterEqual(len([t for t in "123456" if comp["picks"].get(t)]), 5)  # 高風險到貨幣型都有
        self.assertTrue(any(t["idx"] for t in d["themes"]))
        self.assertIn("idx", d)
        u = self.doc("funds_tlife")
        self.assertIn("theme", u["cols"])
        self.assertEqual(len(u["rows"]), d["companies"]["tlife"]["count"])
        self.assertTrue((self.tmp / "data" / "site" / "funds_tlife.js").exists())

    def test_ineligible_never_picked(self):
        self.go("--mode", "daily")
        d = self.doc("funds")
        names = [p["n"] for c in d["companies"].values() for ps in c["picks"].values() for p in ps]
        self.assertFalse(any("暫停" in n for n in names))

    def test_new_and_dropped_tracked(self):
        os.environ["MOCK_FUND_DROP"] = "0"
        self.go("--mode", "daily")
        first = self.doc("funds")["companies"]["tlife"]["count"]
        os.environ["MOCK_FUND_DROP"] = "3"
        self.go("--mode", "daily")
        ch = self.doc("funds")["companies"]["tlife"]["changes"]
        self.assertEqual(len([c for c in ch if c["type"] == "下架"]), 3)
        self.assertEqual(self.doc("funds")["companies"]["tlife"]["count"], first - 3)

    def test_fund_failure_keeps_other_company_and_exit_code(self):
        os.environ["MOCK_FUND_FAIL"] = "tlife"
        code = self.go("--mode", "daily")
        self.assertEqual(code, 2)
        d = self.doc("funds")
        self.assertIn("nanshan", d["companies"])
        self.assertTrue(any("基金" in p for p in self.doc("meta")["problems"]))

    def test_news_failure_is_not_fatal(self):
        os.environ["MOCK_NEWS_FAIL"] = "1"
        code = self.go("--mode", "daily")
        self.assertIn(code, (0, 2))
        self.assertTrue((self.tmp / "data" / "site" / "overview.json").exists())

    def test_news_has_items(self):
        self.go("--mode", "daily")
        n = self.doc("news")
        self.assertIn("tw2330", n["items"])
        it = n["items"]["tw2330"][0]
        self.assertTrue(it["t"] and it["u"].startswith("http"))

    def test_snapshot_does_not_refetch_funds(self):
        self.go("--mode", "daily")
        before = (self.tmp / "data" / "site" / "funds.json").read_text(encoding="utf-8")
        self.assertEqual(self.go("--mode", "snapshot"), 0)
        self.assertEqual(before, (self.tmp / "data" / "site" / "funds.json").read_text(encoding="utf-8"))

    def test_derived_charts(self):
        self.go("--mode", "daily")
        charts = {c["id"]: c for c in self.doc("trends")["charts"]}
        self.assertIn("spreads", charts)
        self.assertIn("gs_ratio", charts)
        self.assertTrue(self.doc("trends")["curve"])


class TestFundUnits(unittest.TestCase):
    def test_decode_cp950(self):
        from pipeline import funds
        self.assertEqual(funds.decode("基金".encode("cp950")), "基金")
        self.assertEqual(funds.decode("基金".encode("utf-8")), "基金")

    def test_clean_name_strips_disclaimer(self):
        from pipeline import funds
        n = funds.clean_name("貝萊德環球非投資等級債券基金A6美元(本基金有相當比重投資於非投資等級之高風險債券)")
        self.assertNotIn("高風險", n)
        self.assertIn("貝萊德", n)

    def test_classifier_on_fixtures(self):
        from pipeline import funds
        import fund_fixtures
        cfg = json.loads((REPO / "config" / "fund_themes.json").read_text(encoding="utf-8"))
        cls = funds.Classifier(cfg)
        for name, cat, ty, want in fund_fixtures.SEEDS + fund_fixtures.EXTRA:
            f = {"n": funds.clean_name(name), "cat": cat, "ty": ty}
            self.assertEqual(cls.theme(f)["id"], want, name)

    def test_html_in_names_is_stripped(self):
        from pipeline import funds
        for raw in ('景順永續性環球數據趨動基金A-穩定月配息股美元<span class="fundname-warning"></span>',
                    '富蘭克林坦伯頓全球投資系列穩定月收益基金美元A<span class="fundname-warning"></span>'):
            n = funds.clean_name(raw)
            self.assertNotIn("<", n)
            self.assertNotIn("fundname", n)
        f = funds.normalize({"V40": "NS1-F1", "V3": '測試基金<span class="fundname-warning"></span>', "V41": "B"}, "nanshan")
        self.assertEqual(f["n"], "測試基金")
        self.assertNotIn("<", f["raw"])

    def test_diff_ids(self):
        from pipeline import funds
        from datetime import date
        t = date(2026, 11, 6)
        new, ch = funds.diff_ids(None, [{"k": "A", "n": "a"}], t)
        self.assertEqual(ch, [])
        new2, ch2 = funds.diff_ids(new, [{"k": "B", "n": "b"}], t)
        self.assertEqual(sorted(c["type"] for c in ch2), ["下架", "新增"])


class TestMacroAndGroups(Base):
    def test_macro_fields(self):
        self.go("--mode", "daily")
        m = {x["id"]: x for x in self.doc("macro")}
        self.assertEqual(len(m), 37)
        for x in m.values():
            if x.get("empty"):
                continue
            self.assertIn(x["verdict"], ("好", "壞", "持平"))
            self.assertTrue(x["asof"] and x["rec"])
        us = [x for x in m.values() if x["country"] == "us"]
        self.assertEqual(sorted(x["rec"] for x in us), list(range(1, 16)))
        self.assertTrue(m["us_claims"]["weekly"])

    def test_macro_countries(self):
        self.go("--mode", "daily")
        m = {x["id"]: x for x in self.doc("macro")}
        self.assertEqual({x["country"] for x in m.values()}, {"us", "eu", "jp", "tw", "uk", "cn"})
        # FRED 直取
        self.assertEqual(m["eu_ecb"]["value"], 2.0)
        self.assertEqual(m["eu_ecb"]["prev"], 2.25)
        # 國發會 CSV
        self.assertEqual(m["tw_signal"]["src"], "ndc")
        self.assertEqual(m["tw_signal"]["value"], 29.0)
        # 行事曆累積：日本 CPI 實際值；尚無資料者為空白卡
        self.assertEqual(m["jp_cpi"]["value"], 2.1)
        self.assertEqual(m["jp_cpi"]["prev"], 1.9)
        self.assertTrue(m["jp_gdp"]["empty"])
        self.assertTrue(m["cn_cpi"].get("next"))
        self.assertEqual(self.doc("meta")["problems"], [])

    def test_ndc_failure_is_soft(self):
        os.environ["MOCK_NDC_FAIL"] = "1"
        try:
            self.assertEqual(self.go("--mode", "daily"), 2)  # 部分來源失敗＝2，但其他照常產出
        finally:
            os.environ.pop("MOCK_NDC_FAIL", None)
        m = {x["id"]: x for x in self.doc("macro")}
        self.assertTrue(m["tw_signal"]["empty"])
        self.assertEqual(m["eu_ecb"]["value"], 2.0)

    def test_manual_event_dedupe_against_auto(self):
        from datetime import date
        from pipeline import build
        auto = [{"date": "2026-12-10", "time": "21:30", "event": "CPI 年增", "title": "CPI y/y", "country": "美國", "importance": "H",
                 "previous": "", "forecast": "", "actual": "", "source": "auto"}]
        manual = [{"date": "2026-12-10", "time": "21:30", "event": "美國 11 月 CPI", "country": "美國", "importance": "H", "dedupe": "CPI"},
                  {"date": "2026-12-10", "event": "中國 CPI", "country": "中國", "importance": "M", "dedupe": "CPI"},  # 不同國家：保留
                  {"date": "2026-12-12", "event": "美國 PPI", "country": "美國", "importance": "M", "dedupe": "CPI"},  # 名稱不符：保留
                  {"date": "2026-12-20", "event": "美國 11 月 CPI", "country": "美國", "importance": "H"}]  # 沒寫 dedupe：保留
        out = build.merge_calendar(auto, manual, date(2026, 11, 7))
        names = [e["event"] for e in out]
        self.assertNotIn("美國 11 月 CPI", [e["event"] for e in out if e["date"] == "2026-12-10"])
        self.assertIn("中國 CPI", names)
        self.assertIn("美國 PPI", names)
        self.assertEqual(len([e for e in out if e["date"] == "2026-12-20"]), 1)
        self.assertEqual(len([e for e in out if e["date"] == "2026-12-10" and e["country"] == "美國"]), 1)

    def test_ndc_zip_download(self):
        from pipeline import fetch
        base = os.environ["ECO_URL_NDC_META"].rsplit("/ndc/", 1)[0]
        cols = {"signal": "景氣對策信號綜合分數", "unemp": "失業率"}
        os.environ["ECO_URL_NDC_CSV"] = base + "/ndc/zip"
        try:
            out = fetch.fetch_ndc(cols)
        finally:
            os.environ.pop("ECO_URL_NDC_CSV", None)
        self.assertIn("signal", out)
        self.assertIn("unemp", out)
        self.assertGreater(len(out["signal"]), 20)

    def test_ndc_zip_without_needed_columns(self):
        from pipeline import fetch
        base = os.environ["ECO_URL_NDC_META"].rsplit("/ndc/", 1)[0]
        os.environ["ECO_URL_NDC_CSV"] = base + "/ndc/zip"
        try:
            with self.assertRaises(fetch.SourceError) as cm:
                fetch.fetch_ndc({"x": "完全不存在的欄位"})
        finally:
            os.environ.pop("ECO_URL_NDC_CSV", None)
        self.assertIn("ndc.csv", str(cm.exception))

    def test_parse_ndc_and_num(self):
        from pipeline import fetch, build
        txt = "\ufeff統計期,景氣對策信號綜合分數,失業率(%)\n202509,24,3.4\n202510,25,3.3\n"
        out = fetch.parse_ndc(txt, {"景氣對策信號綜合分數": "景氣對策信號綜合分數", "失業率": "失業率"})
        self.assertEqual([v for _, v in out["景氣對策信號綜合分數"]], [24.0, 25.0])
        self.assertEqual(out["失業率"][-1][1], 3.3)
        cr = txt.replace("\n", "\r")  # 只有 CR 換行也要能讀
        self.assertEqual(fetch.parse_ndc(cr, {"失業率": "失業率"})["失業率"][-1][1], 3.3)
        self.assertEqual(build._num("-0.4%"), -0.4)
        self.assertEqual(build._num("1.2K"), 1.2)
        self.assertIsNone(build._num(""))

    def test_progress_lines(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            self.go("--mode", "daily")
        out = buf.getvalue()
        self.assertIn("[1/", out)
        self.assertIn("完成（daily", out)

    def test_fund_groups(self):
        self.go("--mode", "daily")
        d = self.doc("funds")
        self.assertEqual([g["id"] for g in d["groups"]], ["dev", "em", "theme", "bond", "mix", "cash"])
        self.assertTrue(all(t["grp"] in {g["id"] for g in d["groups"]} for t in d["themes"]))
        for g in d["groups"]:
            self.assertIn("g_" + g["id"], d["idx"]["series"])  # 大分類合成指數
            self.assertEqual(len(d["idx"]["series"]["g_" + g["id"]]), len(d["idx"]["dates"]))
        self.assertEqual(sum(g["count"]["total"] for g in d["groups"]), sum(t["count"]["total"] for t in d["themes"]))
        u = self.doc("funds_nanshan")
        self.assertIn("grp", u["cols"])
        gi = u["cols"].index("grp")
        self.assertTrue(all(r[gi] in {g["id"] for g in d["groups"]} for r in u["rows"]))
        comp = d["companies"]["nanshan"]
        self.assertEqual(sum(comp["gcounts"].values()), comp["count"])

    def test_news_keys_for_new_things(self):
        self.go("--mode", "daily")
        keys = set(self.doc("news")["items"])
        for k in ("twd_eur", "vxn", "eem", "g:dev", "t:em"):
            self.assertIn(k, keys)


class TestMacroUnits(unittest.TestCase):
    def test_verdict_direction(self):
        from datetime import date
        rows = lambda vals: [(date(2026, i + 1, 1), v) for i, v in enumerate(vals)]
        fred = {"UNRATE": rows([4.0, 4.5]), "PAYEMS": rows([100000, 100100, 100300]), "UMCSENT": rows([60, 50])}
        out = {m["id"]: m for m in build.build_macro(fred, [], date(2026, 11, 6))}
        self.assertEqual(out["us_unrate"]["verdict"], "壞")   # 失業率上升
        self.assertEqual(out["us_nfp"]["verdict"], "好")      # 新增就業 200 vs 100
        self.assertEqual(out["us_sent"]["verdict"], "壞")     # 信心下滑
        self.assertEqual(out["us_unrate"]["score"], -2.5)

    def test_derive_ops(self):
        from datetime import date
        d = date(2026, 11, 5)
        cfg = {"derived": [{"id": "a", "name": "a", "short": "a", "a": "x", "b": "y", "op": "mul"},
                           {"id": "b", "name": "b", "short": "b", "a": "x", "b": "y", "op": "div", "scale": 100}]}
        out, _ = build.derive(cfg, {"x": [(d, 6.0)], "y": [(d, 3.0)]})
        self.assertEqual(out["a"][0][1], 18.0)
        self.assertEqual(out["b"][0][1], 200.0)


class TestUnits(unittest.TestCase):
    def test_streak(self):
        self.assertEqual(build.streak([5, 4, 3, 2, 1]), -4)
        self.assertEqual(build.streak([1, 2, 3, 2, 3]), 1)
        self.assertEqual(build.streak([1, 1, 1]), 0)

    def test_format(self):
        self.assertEqual(build.sp(-3.2), "−3.20%")
        self.assertEqual(build.sp(1.5, 1), "+1.5%")
        self.assertEqual(build.md("2026-11-05"), "11/5")

    def test_pick_range(self):
        from datetime import date
        t = date(2026, 11, 6)
        self.assertEqual(fetch.pick_range(None, t), "2y")
        self.assertEqual(fetch.pick_range(date(2026, 11, 5), t), "5d")
        self.assertEqual(fetch.pick_range(date(2026, 10, 20), t), "1mo")
        self.assertEqual(fetch.pick_range(date(2025, 1, 1), t), "2y")

    def test_mood_levels(self):
        mk = lambda **kw: {k: {"short": k, "chg": v[0], "value": v[1]} for k, v in kw.items()}
        calm = build.build_mood(mk(vix=(0.5, 14), spx=(0.2, 1), ixic=(0.1, 1), sox=(0.3, 1), twii=(0.0, 1)))
        self.assertEqual(calm["label"], "平穩")
        rally = build.build_mood(mk(vix=(-5, 14), spx=(3.2, 1), ixic=(2, 1), sox=(1, 1), twii=(1, 1)))
        self.assertEqual(rally["label"], "樂觀")
        panic = build.build_mood(mk(vix=(60, 27), spx=(-4, 1), ixic=(-5.5, 1), sox=(-12, 1), twii=(-3, 1)))
        self.assertEqual(panic["label"], "恐慌")


if __name__ == "__main__":
    unittest.main()


class TestCalendarTags(Base):
    def test_full_run_tags_and_scope(self):
        self.assertEqual(self.go("--mode", "daily"), 0)
        cal = self.doc("calendar")
        self.assertTrue(all(e.get("cat") and e.get("catn") and "tags" in e and e.get("scope") in ("econ", "other") for e in cal))
        lol = [e for e in cal if "英雄聯盟" in e["event"]]
        if lol:  # 電競：經濟以外，預設不是主要事件
            self.assertEqual((lol[0]["cat"], lol[0]["scope"], lol[0]["main"]), ("esport", "other", False))
        fomc = next(e for e in cal if "聯準會利率決議" in e["event"])
        self.assertEqual((fomc["cat"], fomc["scope"], fomc["main"]), ("policy", "econ", True))
        ov = self.doc("overview")
        self.assertTrue(all("hot" in h for h in ov["highlights"]))
        self.assertTrue(any(h["hot"] for h in ov["highlights"]) and not all(h["hot"] for h in ov["highlights"]))
        self.assertIn("加密貨幣", [h["tag"] for h in ov["highlights"]])
        # 非經濟事件不會進「接下來」
        self.assertFalse(any("英雄聯盟" in e["event"] for e in ov["events"]))


class TestEventsUnits(unittest.TestCase):
    def setUp(self):
        from pipeline import events
        self.ev = events
        self.cfg = events.load_cfg(json.loads((REPO / "config" / "calendar.json").read_text(encoding="utf-8")))

    def test_classify_auto_titles(self):
        c = lambda t: self.ev.classify(t, self.cfg)[0]
        self.assertEqual(c("Core CPI m/m"), "infl")
        self.assertEqual(c("Unemployment Claims"), "job")
        self.assertEqual(c("Federal Funds Rate"), "policy")
        self.assertEqual(c("Building Permits"), "housing")
        self.assertEqual(c("Crude Oil Inventories"), "energy")
        self.assertEqual(c("30-y Bond Auction"), "fin")
        self.assertEqual(c("ISM Manufacturing PMI"), "growth")
        self.assertEqual(c("英雄聯盟全球總決賽"), "esport")
        self.assertEqual(c("金管會公布壽險業資本適足率"), "ins")
        self.assertIsNone(c("Something Unknown"))

    def test_scope_and_main(self):
        t = self.ev.tag_event
        e = t({"event": "英雄聯盟 決賽", "importance": "M"}, self.cfg, "manual")
        self.assertEqual((e["scope"], e["main"]), ("other", False))
        e = t({"event": "英雄聯盟 決賽", "importance": "M", "main": True}, self.cfg, "manual")
        self.assertTrue(e["main"])  # 升級成主要事件
        e = t({"event": "九合一選舉投票", "cat": "politics", "tags": ["選舉", "能源"]}, self.cfg, "manual")
        self.assertEqual((e["catn"], e["tags"], e["scope"], e["main"]), ("政治", ["選舉", "能源"], "econ", True))
        e = t({"event": "未知事件"}, self.cfg, "auto")
        self.assertEqual(e["cat"], "growth")  # 自動抓到的經濟數據，沒對上規則時當景氣成長

    def test_parse_ics(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261114T100000Z\r\nSUMMARY:決賽\\, 第一場\r\nDESCRIPTION:長長的\r\n 說明\r\nEND:VEVENT\r\n"
               "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261120\r\nSUMMARY:全天事件\r\nEND:VEVENT\r\n"
               "BEGIN:VEVENT\r\nDTSTART:20261121T100000Z\r\nSUMMARY:取消的\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\n"
               "BEGIN:VEVENT\r\nDTSTART:20261122T100000Z\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:重複的\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        evs = self.ev.parse_ics(ics)
        self.assertEqual([(e["date"], e["time"], e["event"]) for e in evs], [("2026-11-14", "18:00", "決賽, 第一場"), ("2026-11-20", "", "全天事件")])
        self.assertEqual(evs[0]["note"], "長長的說明")

    def test_feeds_window_failure_and_merge(self):
        from datetime import date
        today = date(2026, 11, 6)
        ics = "BEGIN:VEVENT\nDTSTART:20261114T100000Z\nSUMMARY:W 決賽\nEND:VEVENT\nBEGIN:VEVENT\nDTSTART:20300101T100000Z\nSUMMARY:太遠\nEND:VEVENT\n"
        feeds = [{"name": "a", "url": "x", "cat": "esport", "country": "全球"}, {"name": "b", "url": "bad"}]

        def fake(u):
            if u == "bad":
                raise fetch.SourceError("連不上")
            return ics
        evs, warns = self.ev.fetch_feeds(feeds, today, fetch=fake)
        self.assertEqual([e["event"] for e in evs], ["W 決賽"])
        self.assertEqual(len(warns), 1)
        out = build.merge_calendar([], [], today, self.cfg, evs)
        self.assertEqual((out[0]["cat"], out[0]["scope"], out[0]["main"]), ("esport", "other", False))

    def test_add_event_cli(self):
        from pipeline import add_event
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(REPO / "config", tmp / "config")
            add_event.main(["--root", str(tmp), "--date", "2026-12-01", "--event", "實價登錄新制說明會", "--importance", "M"])
            doc = json.loads((tmp / "config" / "events.json").read_text(encoding="utf-8"))
            self.assertIn("實價登錄新制說明會", [e["event"] for e in doc["events"]])
            with self.assertRaises(SystemExit):
                add_event.main(["--root", str(tmp), "--date", "2026-12-01", "--event", "實價登錄新制說明會"])
            with self.assertRaises(SystemExit):
                add_event.main(["--root", str(tmp), "--date", "2026-13-01", "--event", "x"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestNewsFeed(Base):
    def cfg(self):
        return newsfeed.load_cfg(REPO / "config" / "news_topics.json")

    def test_run_produces_feed(self):
        os.environ["MOCK_NEWS_BIG"] = "1"
        self.assertEqual(self.go("--mode", "daily"), 0)
        d = self.doc("newsfeed")
        self.assertEqual([t["id"] for t in d["topics"]][:2], ["fx", "mkt"])
        self.assertFalse(d["ai"])
        its = d["items"]
        self.assertGreater(len(its), 20)
        crash = [i for i in its if "道瓊" in i["t"] and "千點" in i["t"]]
        self.assertEqual(len(crash), 1, "同一事件的兩家報導要合併成一則")
        c = crash[0]
        self.assertGreaterEqual(c["n"], 2)
        self.assertEqual(len(c["also"]), 1)
        self.assertIn("mkt", c["tg"])
        self.assertTrue(c["hot"])
        fx = [i for i in its if "新台幣貶值" in i["t"]][0]
        self.assertIn("fx", fx["tg"])
        self.assertEqual(self.doc("meta")["sources"]["newsfeed"]["ok"], True)

    def test_retention_and_accumulate(self):
        from datetime import datetime, timedelta
        cfg, now = self.cfg(), datetime.now(newsfeed.TZ)
        old = [{"id": "a", "t": "三十五天前的舊新聞內容很長很長", "u": "https://o/1", "s": "甲", "d": (now - timedelta(days=35)).isoformat(timespec="minutes"), "tg": ["mkt"]},
               {"id": "b", "t": "十天前的新聞保留下來的內容", "u": "https://o/2", "s": "乙", "d": (now - timedelta(days=10)).isoformat(timespec="minutes"), "tg": ["re"]}]
        raw = [({"t": "全新的一則房市新聞標題在這裡", "u": "https://n/1", "s": "丙", "d": now.isoformat(timespec="minutes"), "sum": ""}, "re")]
        out = newsfeed.merge(old, raw, now, cfg)
        self.assertEqual({i["id"] for i in out} - {"b"} != set(), True)
        self.assertNotIn("a", [i["id"] for i in out])
        self.assertIn("b", [i["id"] for i in out])
        self.assertEqual(out[0]["tg"], ["re"])
        # 再抓一次同樣的：不重複
        again = newsfeed.merge(out, raw, now, cfg)
        self.assertEqual(len(again), len(out))

    def test_expired_news_is_archived_by_month(self):
        import json
        from datetime import datetime, timedelta
        cfg, now = self.cfg(), datetime.now(newsfeed.TZ)
        d1, d2 = now - timedelta(days=45), now - timedelta(days=40)
        old = [{"id": "a", "t": "四十五天前的舊新聞內容很長很長", "u": "https://o/1", "s": "甲", "d": d1.isoformat(timespec="minutes"), "tg": ["mkt"], "hot": True},
               {"id": "c", "t": "四十天前的另一則舊新聞內容也不短", "u": "https://o/3", "s": "乙", "d": d2.isoformat(timespec="minutes"), "tg": ["re"]},
               {"id": "b", "t": "十天前的新聞保留下來的內容", "u": "https://o/2", "s": "乙", "d": (now - timedelta(days=10)).isoformat(timespec="minutes"), "tg": ["re"]}]
        expired = []
        out = newsfeed.merge(old, [], now, cfg, expired)
        self.assertEqual([i["id"] for i in out], ["b"])
        self.assertEqual({i["id"] for i in expired}, {"a", "c"})
        self.assertNotIn("_t", expired[0])
        adir = self.tmp / "arch"
        topics = [{"id": "mkt", "name": "股市行情"}]
        newsfeed.update_archive(adir, expired, topics, now)
        newsfeed.update_archive(adir, expired, topics, now)  # 重複寫入不會重複
        idx = json.loads((adir / "index.json").read_text(encoding="utf-8"))
        total = sum(m["n"] for m in idx["months"])
        self.assertEqual(total, 2)
        self.assertEqual(idx["months"], sorted(idx["months"], key=lambda m: m["m"], reverse=True))
        m = idx["months"][0]["m"]
        mdoc = json.loads((adir / (m + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(mdoc["month"], m)
        self.assertTrue((adir / (m + ".js")).exists())
        # 沒有過期新聞時也要有（空的）索引，前端才不會 404
        empty = self.tmp / "arch2"
        newsfeed.update_archive(empty, [], topics, now)
        self.assertEqual(json.loads((empty / "index.json").read_text(encoding="utf-8"))["months"], [])

    def test_run_writes_archive_index(self):
        import json
        os.environ["MOCK_NEWS_BIG"] = "1"
        self.assertEqual(self.go("--mode", "daily"), 0)
        idx = self.tmp / "data" / "site" / "news-archive" / "index.json"
        self.assertTrue(idx.exists())
        self.assertIn("months", json.loads(idx.read_text(encoding="utf-8")))

    def test_cluster_swaps_to_better_source(self):
        from datetime import datetime
        cfg, now = self.cfg(), datetime.now(newsfeed.TZ)
        d = now.isoformat(timespec="minutes")
        raw = [({"t": "央行宣布升息一碼 房貸族衝擊", "u": "https://x/1", "s": "小報", "d": d, "sum": ""}, "macro"),
               ({"t": "央行宣布升息一碼 衝擊房貸族", "u": "https://x/2", "s": "中央社", "d": d, "sum": ""}, "re")]
        out = newsfeed.merge([], raw, now, cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["s"], "中央社")            # 權重較高的來源當代表
        self.assertEqual(out[0]["also"][0]["s"], "小報")
        self.assertEqual(set(out[0]["tg"]) >= {"macro", "re"}, True)

    def test_same_event_rules(self):
        se = lambda a, b: newsfeed.same_event(newsfeed.bigrams(newsfeed.norm(a)), a, newsfeed.bigrams(newsfeed.norm(b)), b)
        self.assertTrue(se("新台幣貶值 美元匯率升破32", "新台幣重貶 美元兌台幣升破32 創近半年新高"))
        self.assertFalse(se("台股收盤 加權指數上漲50點", "台股收盤 加權指數下跌30點"))  # 數字不同＝不同事件
        self.assertFalse(se("房市交易量萎縮 預售屋成交價創高", "央行信用管制 房貸利率調升"))

    def test_summary_filter_and_trim(self):
        self.assertEqual(newsfeed.good_summary("<a href=x>美股大跌 道瓊重挫</a>&nbsp;中央社", "美股大跌 道瓊重挫"), "")
        s = newsfeed.good_summary("<p>聯準會昨晚宣布維持利率不變，並暗示年底前仍有一次降息空間。市場解讀偏鴿。</p>", "Fed 決議")
        self.assertTrue(s.startswith("聯準會昨晚宣布維持利率不變"))
        self.assertLessEqual(len(newsfeed.trim("甲" * 200)), 71)

    def test_hot_cap_per_topic_day(self):
        from datetime import datetime
        cfg, now = self.cfg(), datetime.now(newsfeed.TZ)
        d = now.isoformat(timespec="minutes")
        cfg["hot"]["threshold"] = 2
        heads = ["台積電重挫逾百元 外資連賣", "聯發科崩跌 法人下修目標價", "鴻海大跌 伺服器出貨遞延", "廣達暴跌 AI訂單恐縮手", "緯創重挫 美國客戶砍單", "大立光崩 手機鏡頭需求疲軟"]
        raw = [({"t": h, "u": "https://h/%d" % i, "s": "中央社", "d": d, "sum": ""}, "mkt") for i, h in enumerate(heads)]
        out = newsfeed.merge([], raw, now, cfg)
        self.assertEqual(sum(1 for i in out if i["hot"]), cfg["hot"]["per_topic_day"])

    def test_ai_summary_optional(self):
        os.environ["MOCK_NEWS_BIG"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.assertEqual(self.go("--mode", "daily"), 0)
        d = self.doc("newsfeed")
        self.assertTrue(d["ai"])
        got = [i for i in d["items"] if i.get("ai")]
        self.assertGreaterEqual(len(got), 1)
        self.assertLessEqual(len(got[0]["sum"]), 75)
        self.assertIn("只能根據提供的標題", json.loads(mock_server.H.last_ai_prompt)["messages"][0]["content"])

    def test_ai_failure_is_soft(self):
        os.environ["MOCK_NEWS_BIG"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "wrong"
        self.assertEqual(self.go("--mode", "daily"), 2)  # 有警告，但新聞照常產出
        d = self.doc("newsfeed")
        self.assertGreater(len(d["items"]), 20)
        self.assertTrue(any("AI" in p for p in self.doc("meta")["problems"]))

    def test_news_source_down(self):
        os.environ["MOCK_NEWS_FAIL"] = "1"
        try:
            self.go("--mode", "daily")
        finally:
            os.environ.pop("MOCK_NEWS_FAIL", None)
        self.assertFalse((self.tmp / "data" / "site" / "newsfeed.json").exists())
        self.assertFalse(self.doc("meta")["sources"]["newsfeed"]["ok"])


class TestRates(Base):
    TODAY = date(2026, 11, 7)

    def cfg(self):
        return ratesprob.load_cfg({"fomc": ["2026-12-09", "2027-01-27", "2027-03-17"]})

    def test_classify(self):
        c = ratesprob.classify
        self.assertEqual(c("Will there be no change in Fed interest rates"), "hold")
        self.assertEqual(c("Fed decrease interest rates by 25 bps"), "cut")
        self.assertEqual(c("Fed cuts 50+ bps"), "cut")
        self.assertEqual(c("Will the Fed increase rates by 25+ bps"), "hike")
        self.assertIsNone(c("Will Bitcoin hit 150k"))

    def test_polymarket_parse_and_normalize(self):
        rows = ratesprob.fetch_polymarket(self.cfg(), self.TODAY)
        by = {r["date"]: r for r in rows}
        self.assertEqual(sorted(by), ["2026-12-09", "2027-01-27"])  # 比特幣、「今年降幾次」這類市場被忽略
        d = by["2026-12-09"]
        self.assertEqual((d["cut"], d["hold"], d["hike"]), (43.0, 55.0, 2.0))  # 25bp 降 40 ＋ 50bp 降 3
        j = by["2027-01-27"]  # 合計 90%：正規化
        self.assertAlmostEqual(j["cut"] + j["hold"] + j["hike"], 100, delta=0.2)
        self.assertEqual(j["hike"], 0.0)

    def test_polymarket_incomplete_market_has_no_numbers(self):
        ev = [{"id": "x", "title": "Fed decision in December?", "markets": [
            {"id": "1", "question": "Will the Fed decrease interest rates by 25 bps after the December 2026 meeting?",
             "outcomes": '["Yes","No"]', "outcomePrices": '["0.2","0.8"]', "closed": False}]}]
        acc = ratesprob.parse_polymarket(ev, self.cfg(), self.TODAY)
        self.assertEqual(acc["2026-12-09"]["cut"], 0.2)  # 只有一個選項：合計 20%，後續 fetch 會標「不完整」

    def test_futures_estimate(self):
        f = ratesprob.futures_estimate(self.cfg(), date(2026, 12, 9), 3.88)  # 預設 3.80：預期降 ~11bp
        self.assertEqual(f["symbol"], "ZQZ26.CBT")
        self.assertAlmostEqual(f["implied_bp"], -11.3, delta=0.2)
        self.assertAlmostEqual(f["cut"], 45.0, delta=0.3)
        self.assertAlmostEqual(f["cut"] + f["hold"], 100, delta=0.2)
        self.assertEqual(f["hike"], 0.0)

    def test_futures_end_of_month_meeting_uses_next_contract(self):
        f = ratesprob.futures_estimate(self.cfg(), date(2027, 1, 27), 3.88)
        self.assertEqual(f["symbol"], "ZQG27.CBT")  # 1/27 會議距月底只剩 4 天 → 改看 2 月合約
        self.assertAlmostEqual(f["implied_bp"], -8.0, delta=0.1)

    def test_build_gap_and_next_meeting(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=run.TZ)
        doc, warns = ratesprob.build(self.cfg(), [(date(2026, 11, 6), 4.0)], self.TODAY, now)
        self.assertEqual(warns, [])
        self.assertEqual(doc["next"], {"date": "2026-12-09", "days": 32})
        self.assertEqual((doc["current"]["upper"], doc["current"]["lower"], doc["current"]["effr"]), (4.0, 3.75, 3.88))
        self.assertTrue(doc["poly"]["ok"] and doc["futures"]["ok"])
        self.assertAlmostEqual(doc["gap"], 2.0, delta=0.5)
        self.assertFalse(doc["gap_warn"])

    def test_gap_warning_when_sources_disagree(self):
        os.environ["MOCK_ZQ_RATE"] = "3.60"  # 期貨預期降 ~40bp（上限 100%），Polymarket 只有 43%
        doc, _ = ratesprob.build(self.cfg(), [(date(2026, 11, 6), 4.0)], self.TODAY, datetime(2026, 11, 7, tzinfo=run.TZ))
        self.assertTrue(doc["gap_warn"])
        self.assertGreater(doc["gap"], 15)

    def test_one_source_down_still_builds(self):
        os.environ["MOCK_POLY_FAIL"] = "1"
        doc, warns = ratesprob.build(self.cfg(), [(date(2026, 11, 6), 4.0)], self.TODAY, datetime(2026, 11, 7, tzinfo=run.TZ))
        self.assertFalse(doc["poly"]["ok"])
        self.assertTrue(doc["futures"]["ok"])
        self.assertNotIn("gap", doc)
        self.assertEqual(len(warns), 1)

    def test_both_down_raises(self):
        os.environ["MOCK_POLY_FAIL"] = "1"
        os.environ["MOCK_FAIL"] = "ZQZ26.CBT"
        with self.assertRaises(ratesprob.SourceError):
            ratesprob.build(self.cfg(), [(date(2026, 11, 6), 4.0)], self.TODAY, datetime(2026, 11, 7, tzinfo=run.TZ))

    def test_expired_fomc_list_is_reported(self):
        with self.assertRaises(ratesprob.SourceError):
            ratesprob.build(ratesprob.load_cfg({"fomc": ["2020-01-29"]}), [], self.TODAY, datetime(2026, 11, 7, tzinfo=run.TZ))

    @unittest.skipIf(date.today() > date(2026, 12, 8), "config/rates.json 的下一次會議日期已過")
    def test_run_writes_rates_and_failure_keeps_old(self):
        self.assertEqual(self.go("--mode", "daily"), 0)
        r = self.doc("rates")
        self.assertTrue(r["poly"]["ok"] and r["futures"]["ok"])
        self.assertTrue((self.tmp / "data" / "site" / "rates.js").exists())
        os.environ["MOCK_POLY_FAIL"] = "1"
        os.environ["MOCK_FAIL"] = "ZQZ26.CBT"
        self.assertEqual(self.go("--mode", "snapshot"), 2)  # 利率預期失敗＝警告，網站其他資料照常更新
        self.assertEqual(self.doc("rates"), r)  # 沿用上一版
        self.assertTrue(any("利率預期" in p for p in self.doc("meta")["problems"]))
