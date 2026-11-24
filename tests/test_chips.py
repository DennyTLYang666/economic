# -*- coding: utf-8 -*-
"""籌碼（三大法人）、夜盤、預估成績單。單元測試不連網；整合測試用 mock_server。"""
import json
import shutil
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_server  # noqa: E402
from test_pipeline import Base  # noqa: E402
from pipeline import chips, outlook  # noqa: E402
from pipeline.common import SourceError  # noqa: E402


def body(rows=None, date_="20261117", stat="OK"):
    rows = rows or [["自營商(自行買賣)", "1", "1", "+840,000,000"], ["自營商(避險)", "1", "1", "-15,000,000"], ["投信", "1", "1", "+783,000,000"],
                    ["外資及陸資(不含外資自營商)", "1", "1", "-38,094,000,000"], ["外資自營商", "1", "1", "-6,000,000"],
                    ["合計", "1", "1", "-36,486,000,000"]]
    return json.dumps({"stat": stat, "date": date_, "data": rows}, ensure_ascii=False)


class TestParseInst(unittest.TestCase):
    def test_normal(self):
        d, v = chips.parse_inst(body())
        self.assertEqual(d, date(2026, 11, 17))
        self.assertEqual(v["foreign"], -380.94)
        self.assertEqual(v["trust"], 7.83)
        self.assertAlmostEqual(v["dealer"], 8.25, 2)
        self.assertEqual(v["total"], -364.86)

    def test_holiday_or_not_ready(self):
        self.assertIsNone(chips.parse_inst(body(stat="很抱歉，沒有符合條件的資料!")))
        self.assertIsNone(chips.parse_inst(json.dumps({"stat": "OK", "date": "20261117", "data": []})))

    def test_sum_mismatch_is_rejected(self):  # 格式改版或欄位錯位時不能默默寫進去
        rows = [["自營商", "", "", "+100"], ["投信", "", "", "+100"], ["外資及陸資(不含外資自營商)", "", "", "-100"], ["合計", "", "", "+999,000,000"]]
        with self.assertRaises(SourceError):
            chips.parse_inst(body(rows))

    def test_wrong_unit_is_rejected(self):  # 單位若改成「千元」以外造成數字爆大
        rows = [["自營商", "", "", "+1"], ["投信", "", "", "+1"], ["外資及陸資", "", "", "-9000000000000"], ["合計", "", "", "-9000000000000"]]
        with self.assertRaises(SourceError):
            chips.parse_inst(body(rows))

    def test_missing_rows(self):
        with self.assertRaises(SourceError):
            chips.parse_inst(body([["投信", "", "", "+1"], ["合計", "", "", "+1"]]))

    def test_new_table_layout_and_roc_date(self):  # 假設新版改成 tables[]、民國日期
        raw = json.dumps({"stat": "OK", "date": "1151117", "tables": [{"data": json.loads(body())["data"]}]}, ensure_ascii=False)
        d, v = chips.parse_inst(raw)
        self.assertEqual(d, date(2026, 11, 17))

    def test_not_json(self):
        with self.assertRaises(SourceError):
            chips.parse_inst("<html>blocked</html>")


class TestUpdateInst(unittest.TestCase):
    def test_backfill_skips_weekend_and_waits_for_publish(self):
        calls = []

        def getter(d):
            calls.append(d)
            return (d, {"foreign": -100.0, "trust": 1.0, "dealer": 1.0, "total": -98.0})
        inst = {date(2026, 11, 12): {"foreign": 1.0, "trust": 0.0, "dealer": 0.0, "total": 1.0}}
        w = chips.update_inst(inst, date(2026, 11, 17), now=datetime(2026, 11, 17, 14, 0), getter=getter, pause=0)
        self.assertEqual([d.isoformat() for d in calls], ["2026-11-13", "2026-11-16"])  # 11/14、11/15 週末；今天 15:30 前不抓
        self.assertEqual(w, [])
        chips.update_inst(inst, date(2026, 11, 17), now=datetime(2026, 11, 17, 16, 0), getter=getter, pause=0)
        self.assertIn(date(2026, 11, 17), inst)

    def test_stops_after_three_failures(self):
        def getter(d):
            raise SourceError("HTTP 403")
        inst = {date(2026, 11, 2): {"foreign": 0.0, "trust": 0.0, "dealer": 0.0, "total": 0.0}}
        w = chips.update_inst(inst, date(2026, 11, 20), now=None, getter=getter, pause=0)
        self.assertEqual(len(w), 3)


NIGHT = [
    {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202611", "Last": "39100", "Open": "39200", "High": "39300", "Low": "38900",
     "Change": "-120", "%": "-0.31", "Volume": "88000", "TradingSession": "盤後"},
    {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202612", "Last": "39050", "TradingSession": "盤後"},
    {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202611/202612", "Last": "50", "TradingSession": "盤後"},
    {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202611", "Last": "39700", "TradingSession": "一般"},
    {"Date": "20261118", "Contract": "MTX", "ContractMonth(Week)": "202611", "Last": "39099", "TradingSession": "盤後"},
]


class TestNight(unittest.TestCase):
    def test_picks_near_month_after_hours_tx(self):
        n = chips.parse_night(json.dumps(NIGHT))
        self.assertEqual((n["date"], n["month"], n["last"], n["pct"]), ("2026-11-18", "202611", 39100.0, -0.31))

    def test_no_after_hours_rows(self):
        self.assertIsNone(chips.parse_night(json.dumps([r for r in NIGHT if r["TradingSession"] != "盤後"])))

    def test_field_names_changed_is_error(self):
        with self.assertRaises(SourceError):
            chips.parse_night(json.dumps([{"foo": 1}, {"foo": 2}]))

    def test_chinese_field_names(self):
        rows = [{"日期": "20261118", "契約": "TX", "到期月份(週別)": "202611", "最後成交價": "39,100", "交易時段": "盤後"}]
        self.assertEqual(chips.parse_night(json.dumps(rows, ensure_ascii=False))["last"], 39100.0)


class TestBuildChips(unittest.TestCase):
    def setUp(self):
        self.inst = {}
        for i, f in enumerate([-100, -50, 20, -300, -400, -200, -100, 30, -50, -60, -10, 5]):
            d = date(2026, 11, 2 + i + (2 * (i // 5)))  # 跳過週末
            self.inst[d] = {"foreign": float(f), "trust": 1.0, "dealer": 2.0, "total": f + 3.0}

    def test_numbers(self):
        twii = [(date(2026, 11, 2), 40000.0), (date(2026, 11, 3), 39000.0)]
        night = {"date": "2026-11-18", "last": 39100.0}
        doc = chips.build_chips(self.inst, twii, night, {"since": {"date": "2026-11-05", "label": "11 月中以來"}})
        i = doc["inst"]
        self.assertEqual(i["streak"], 1)  # 最後一天 +5，前一天 −10 → 連買 1 天
        self.assertEqual(i["sum"]["d5"]["foreign"], round(-100 + 30 - 50 - 60 - 10 + 5 - 30, 2) if False else sum(list(self.inst[d]["foreign"] for d in sorted(self.inst))[-5:]))
        self.assertEqual(i["since"]["label"], "11 月中以來")
        self.assertEqual(i["series"][0]["twii_pct"], None)
        self.assertIn("vs_close_pct", doc["night"])
        self.assertAlmostEqual(doc["night"]["vs_close_pct"], round((39100 / 39000 - 1) * 100, 2))

    def test_empty(self):
        self.assertIsNone(chips.build_chips({}, [], None))


class TestOutlook(unittest.TestCase):
    def test_judge(self):
        self.assertEqual(outlook.judge([40300, 47300], 44489, 39790, 0)[0], "below")
        self.assertIn("510", outlook.judge([40300, 47300], 44489, 39790, 0)[1])
        self.assertEqual(outlook.judge([32.60, 33.03], 33.015, 32.83, 2)[0], "in")
        self.assertEqual(outlook.judge([1, 2], 3, 0, 0)[0], "both")

    def test_scorecard_manual_and_computed(self):
        hl = {"twii": {date(2026, 11, 18): (41000.0, 39500.0), date(2026, 11, 19): (41500.0, 38800.0)}}
        cfg = {"enabled": True, "current": {"issued": "x", "period": ["2026-11-18", "2026-11-24"],
                                            "items": [{"id": "twii", "p80": [37300, 44700], "dec": 0}]},
               "past": [{"issued": "p", "period": ["2026-11-13", "2026-11-19"], "items": [
                   {"id": "twii", "p80": [40300, 47300], "dec": 0, "actual": {"high": 44489, "low": 39790}}]}]}
        doc = outlook.build_outlook(cfg, hl, date(2026, 11, 20))
        prog = doc["current"]["items"][0]["progress"]
        self.assertEqual((prog["actual"]["high"], prog["actual"]["low"], prog["status"]), (41500.0, 38800.0, "in"))
        self.assertFalse(doc["current"]["ended"])
        self.assertEqual(doc["scorecard"][0]["items"][0]["status"], "below")

    def test_disabled_or_empty(self):
        self.assertIsNone(outlook.build_outlook({"enabled": False, "current": {}}, {}, date(2026, 11, 20)))
        self.assertIsNone(outlook.build_outlook({}, {}, date(2026, 11, 20)))

    def test_no_data_message(self):
        cfg = {"enabled": True, "current": {"period": ["2026-11-18", "2026-11-24"], "items": [{"id": "twii", "p80": [1, 2]}]}}
        it = outlook.build_outlook(cfg, {}, date(2026, 11, 20))["current"]["items"][0]["progress"]
        self.assertIsNone(it["actual"])
        self.assertIn("尚無", it["text"])


class TestEndToEnd(Base):
    def test_daily_run_writes_chips_night_and_outlook(self):
        cfgp = self.tmp / "config" / "outlook.json"
        c = json.loads(cfgp.read_text(encoding="utf-8"))
        c["enabled"] = True
        import os
        last = date.fromisoformat(os.environ.get("MOCK_LAST_DATE", "2026-11-05"))
        nxt = last + __import__("datetime").timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += __import__("datetime").timedelta(days=1)
        c["current"]["period"] = [(last - __import__("datetime").timedelta(days=3)).isoformat(), nxt.isoformat()]
        cfgp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        self.assertIn(self.go("--mode", "daily"), (0, 2))
        ch = self.doc("chips")
        self.assertEqual(ch["inst"]["latest"]["date"], last.isoformat())  # mock 的最後交易日
        self.assertLess(ch["inst"]["latest"]["foreign"], 0)
        self.assertEqual(ch["night"]["last"], 39100.0)
        self.assertEqual(ch["night"]["date"], nxt.isoformat())
        self.assertTrue((self.tmp / "data" / "series" / "inst.csv").exists())
        hl = (self.tmp / "data" / "series" / "hl.csv").read_text(encoding="utf-8")
        self.assertIn("twii,%s," % last.isoformat(), hl)
        ol = self.doc("outlook")
        self.assertEqual(ol["current"]["items"][0]["progress"]["actual"]["days"] >= 1, True)
        self.assertEqual([i["status"] for i in ol["scorecard"][0]["items"]], ["below", "in"])
        # 第二次執行：已是最新就不再補抓（資料沒變）
        before = (self.tmp / "data" / "series" / "inst.csv").read_text(encoding="utf-8")
        self.assertIn(self.go("--mode", "snapshot"), (0, 2))
        self.assertEqual(before, (self.tmp / "data" / "series" / "inst.csv").read_text(encoding="utf-8"))

    def test_twse_down_does_not_break_run(self):
        import os
        os.environ["MOCK_TWSE_DOWN"] = "1"
        code = self.go("--mode", "daily")
        self.assertEqual(code, 2)  # 部分來源失敗，但網站照常更新
        self.assertTrue((self.tmp / "data" / "site" / "overview.json").exists())
        self.assertTrue(any("三大法人" in p for p in self.doc("meta")["problems"]))
        self.assertFalse((self.tmp / "data" / "site" / "chips.json").exists() and self.doc("chips").get("inst"))


if __name__ == "__main__":
    unittest.main()
