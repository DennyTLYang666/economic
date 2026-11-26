# -*- coding: utf-8 -*-
"""銀行牌告匯率（玉山；換外幣＝銀行賣出價）。單元測試不連網；整合測試用 mock_server。"""
import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import bankfx  # noqa: E402
from pipeline.common import TZ  # noqa: E402
from test_pipeline import Base  # noqa: E402


def row(ccy, spot=("32.94", "33.04"), cash=("32.69", "33.24"), online=("32.971", "33.009"), ms=1795663520000):
    return {"Name": ccy, "BBoardRate": spot[0], "SBoardRate": spot[1], "CashBBoardRate": cash[0], "CashSBoardRate": cash[1],
            "BuyIncreaseRate": online[0], "SellDecreaseRate": online[1], "UpdateTime": "/Date(%d)/" % ms, "CCY": ccy + "/TWD"}


def body(*rows):
    return json.dumps({"DiscontFlag": "T", "Rates": list(rows), "Time": ["00:00:00"]}, ensure_ascii=False)


class TestParse(unittest.TestCase):
    def test_columns_map_to_sell_prices(self):  # 換外幣＝看賣出：現金賣出 33.24、即期賣出 33.04、網銀優惠賣出 33.009
        q, when = bankfx.parse(body(row("USD")))
        self.assertEqual(q["USD"]["cash"], (32.69, 33.24))
        self.assertEqual(q["USD"]["spot"], (32.94, 33.04))
        self.assertEqual(q["USD"]["online"], (32.971, 33.009))
        self.assertEqual(when, "2026-11-26T11:25:20+08:00")  # 玉山的更新時間（毫秒）→ 台北時間

    def test_missing_quotes_are_none(self):  # 空字串、"-"、0 都是沒報價
        q, _ = bankfx.parse(body(row("SGD", cash=("", ""), online=("-", "-"))))
        self.assertIsNone(q["SGD"]["cash"])
        self.assertIsNone(q["SGD"]["online"])
        self.assertIsNotNone(q["SGD"]["spot"])

    def test_sell_below_buy_is_rejected(self):  # 欄位錯位時不能把買入當賣出
        q, _ = bankfx.parse(body(row("USD", spot=("33.0", "32.0"))))
        self.assertIsNone(q["USD"]["spot"])

    def test_bytes_and_bom(self):
        q, _ = bankfx.parse(("\ufeff" + body(row("JPY", spot=("0.2053", "0.2093")))).encode("utf-8"))
        self.assertEqual(q["JPY"]["spot"], (0.2053, 0.2093))

    def test_challenge_page_is_reported_with_content(self):  # 被擋時 meta.json 要看得出收到什麼
        html = b"<!DOCTYPE html><html><head><title>Challenge Validation</title></head><body>Please verify</body></html>"
        self.assertEqual(bankfx.parse(html), ({}, None))
        info = bankfx._peek(html)
        self.assertIn("Challenge Validation", info)
        self.assertIn("Please verify", info)

    def test_garbage(self):
        self.assertEqual(bankfx.parse("<html>blocked</html>"), ({}, None))
        self.assertEqual(bankfx.parse(None), ({}, None))
        self.assertEqual(bankfx.parse('{"Rates": "oops"}'), ({}, None))


class TestBuild(unittest.TestCase):
    cfg = {"currencies": [{"code": "USD", "name": "美元", "dec": 3, "mid": ["usdtwd"]},
                          {"code": "JPY", "name": "日圓", "dec": 4, "mid": ["usdtwd", "/usdjpy"]}]}
    daily = {"usdtwd": [(None, 33.0)], "usdjpy": [(None, 157.0)]}
    now = datetime(2026, 11, 26, 18, 0, tzinfo=TZ)

    def test_gap_vs_market_and_cross_rate(self):
        q = {"USD": {"cash": (32.69, 33.24), "spot": (32.94, 33.04), "online": (32.971, 33.009)}, "JPY": {"cash": None, "spot": (0.2053, 0.2093), "online": None}}
        doc, warns = bankfx.build(self.cfg, q, self.daily, self.now, "2026-11-26T11:25:20+08:00")
        usd, jpy = doc["items"]
        self.assertEqual((usd["cash"]["sell"], usd["spot"]["sell"], usd["online"]["sell"]), (33.24, 33.04, 33.009))
        self.assertAlmostEqual(usd["spot_gap"], 0.12, 2)  # 即期賣出比市場中間價貴 0.12%
        self.assertAlmostEqual(jpy["mid"], 33.0 / 157.0, 5)
        self.assertIsNone(jpy["online"])
        self.assertEqual(doc["quoted_at"], "2026-11-26T11:25:20+08:00")

    def test_far_from_market_is_dropped(self):
        q = {"USD": {"cash": (40.0, 45.0), "spot": (40.0, 45.0), "online": None}, "JPY": {"cash": None, "spot": (0.2053, 0.2093), "online": None}}
        doc, warns = bankfx.build(self.cfg, q, self.daily, self.now)
        self.assertEqual([i["code"] for i in doc["items"]], ["JPY"])
        self.assertTrue(any("USD" in w for w in warns))


class TestEndToEnd(Base):
    def test_daily_run_writes_bankfx_and_keeps_cache_when_source_fails(self):
        self.assertIn(self.go("--mode", "daily"), (0, 2))
        doc = self.doc("bankfx")
        self.assertEqual(doc["quoted_at"], "2026-11-26T11:25:20+08:00")
        codes = [i["code"] for i in doc["items"]]
        self.assertEqual(codes, ["USD", "JPY", "EUR", "CNY", "AUD", "HKD", "GBP", "SGD"])
        usd = doc["items"][0]
        self.assertEqual((usd["cash"]["sell"], usd["spot"]["sell"], usd["online"]["sell"]), (33.24, 33.04, 33.009))
        self.assertIsNone([i for i in doc["items"] if i["code"] == "SGD"][0]["cash"])  # 現鈔沒報價就不顯示
        self.assertTrue((self.tmp / "data" / "series" / "bankfx_cache.json").exists())
        os.environ["MOCK_ESUN_DOWN"] = "1"  # 被擋（回驗證頁）：沿用上次，整體仍產出，meta 記錄原因
        try:
            self.assertEqual(self.go("--mode", "snapshot"), 2)
        finally:
            os.environ.pop("MOCK_ESUN_DOWN", None)
        self.assertEqual([i["code"] for i in self.doc("bankfx")["items"]][:2], ["USD", "JPY"])
        self.assertTrue(any("Challenge Validation" in p for p in self.doc("meta")["problems"]))

    def test_bad_row_is_dropped_with_warning(self):
        os.environ["MOCK_ESUN_BAD"] = "1"
        try:
            self.assertEqual(self.go("--mode", "daily"), 2)
        finally:
            os.environ.pop("MOCK_ESUN_BAD", None)
        self.assertNotIn("AUD", [i["code"] for i in self.doc("bankfx")["items"]])  # 賣出低於買入的幣別被丟棄
        self.assertTrue(any("AUD" in p for p in self.doc("meta")["problems"]))

    def test_disabled_skips_everything_silently(self):
        p = self.tmp / "config" / "bankfx.json"
        c = json.loads(p.read_text(encoding="utf-8"))
        c["enabled"] = False
        p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.go("--mode", "daily"), 0)
        self.assertFalse((self.tmp / "data" / "site" / "bankfx.json").exists())


if __name__ == "__main__":
    unittest.main()
