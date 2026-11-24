# -*- coding: utf-8 -*-
"""銀行牌告匯率（換外幣＝銀行賣出價）。單元測試不連網；整合測試用 mock_server。"""
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

N = " ".join(["32.20000"] * 7)
SAMPLE = ("﻿幣別 匯率 現金 即期 遠期10天\n"
          "USD 本行買入 31.89500 32.22000 " + N + "\n      本行賣出 32.56500 32.37000 " + N + "\n"
          "KRW 本行買入 0.00000 0.02270 " + N + "\n      本行賣出 0.00000 0.02450 " + N + "\n")


class TestParse(unittest.TestCase):
    def test_sell_is_what_you_pay_to_buy_foreign_currency(self):
        q = bankfx.parse(SAMPLE.encode("utf-8"))
        self.assertEqual(q["USD"]["cash"], (31.895, 32.565))  # (買入, 賣出)：換現鈔看賣出 32.565
        self.assertEqual(q["USD"]["spot"], (32.22, 32.37))    # 匯款／帳戶換匯看賣出 32.37

    def test_zero_means_no_quote(self):
        q = bankfx.parse(SAMPLE)
        self.assertIsNone(q["KRW"]["cash"])
        self.assertIsNotNone(q["KRW"]["spot"])

    def test_sell_below_buy_is_rejected(self):  # 欄位錯位時不能把買入當賣出
        bad = "USD 本行買入 33.0 33.0\n 本行賣出 32.0 32.0\n"
        q = bankfx.parse(bad)
        self.assertIsNone(q["USD"]["cash"])
        self.assertIsNone(q["USD"]["spot"])

    def test_other_layouts(self):  # 版面不同也要能解析：全在一行、Big5 編碼、欄位標記帶前後綴
        one = SAMPLE.replace("\n", " ")
        self.assertEqual(bankfx.parse(one)["USD"]["spot"], (32.22, 32.37))
        self.assertEqual(bankfx.parse(SAMPLE.encode("cp950", "replace").replace(b"\xef\xbb\xbf", b""))["USD"]["cash"], (31.895, 32.565))
        alt = "美金 USD 現金買入 31.0 32.0 現金賣出 31.5 32.5\n"
        self.assertEqual(bankfx.parse(alt)["USD"]["spot"], (32.0, 32.5))

    def test_garbage(self):
        self.assertEqual(bankfx.parse("<html>blocked</html>"), {})
        self.assertEqual(bankfx.parse(None), {})


class TestBuild(unittest.TestCase):
    cfg = {"currencies": [{"code": "USD", "name": "美元", "dec": 3, "mid": ["usdtwd"]},
                          {"code": "JPY", "name": "日圓", "dec": 4, "mid": ["usdtwd", "/usdjpy"]}]}
    daily = {"usdtwd": [(None, 32.3)], "usdjpy": [(None, 155.0)]}
    now = datetime(2026, 11, 24, 18, 0, tzinfo=TZ)

    def test_gap_vs_market_and_cross_rate(self):
        q = {"USD": {"cash": (31.9, 32.565), "spot": (32.2, 32.37)}, "JPY": {"cash": None, "spot": (0.2, 0.2112)}}
        doc, warns = bankfx.build(self.cfg, q, self.daily, self.now)
        usd, jpy = doc["items"]
        self.assertEqual(usd["spot"]["sell"], 32.37)
        self.assertAlmostEqual(usd["spot_gap"], 0.22, 2)
        self.assertAlmostEqual(jpy["mid"], 32.3 / 155.0, 5)
        self.assertIsNone(jpy["cash"])

    def test_far_from_market_is_dropped(self):
        q = {"USD": {"cash": (40.0, 45.0), "spot": (40.0, 45.0)}, "JPY": {"cash": None, "spot": (0.2, 0.2112)}}
        doc, warns = bankfx.build(self.cfg, q, self.daily, self.now)
        self.assertEqual([i["code"] for i in doc["items"]], ["JPY"])
        self.assertTrue(any("USD" in w for w in warns))


class TestEndToEnd(Base):
    def test_daily_run_writes_bankfx_and_keeps_cache_when_source_fails(self):
        self.assertEqual(self.go("--mode", "daily"), 0)
        doc = self.doc("bankfx")
        codes = [i["code"] for i in doc["items"]]
        self.assertEqual(codes, ["USD", "JPY", "EUR", "CNY", "AUD", "HKD", "GBP", "KRW", "SGD"])
        self.assertIsNone([i for i in doc["items"] if i["code"] == "KRW"][0]["cash"])  # 現鈔沒報價就不顯示
        os.environ["MOCK_BOT_BAD"] = "1"
        try:
            self.assertEqual(self.go("--mode", "daily"), 2)
        finally:
            os.environ.pop("MOCK_BOT_BAD", None)
        self.assertNotIn("AUD", [i["code"] for i in self.doc("bankfx")["items"]])  # 賣出低於買入的幣別被丟棄
        self.assertTrue(any("AUD" in p for p in self.doc("meta")["problems"]))
        usd = doc["items"][0]
        self.assertEqual((usd["cash"]["sell"], usd["spot"]["sell"]), (33.2, 32.95))
        self.assertTrue((self.tmp / "data" / "series" / "bankfx_cache.json").exists())
        os.environ["MOCK_BOT_DOWN"] = "1"
        try:
            self.assertEqual(self.go("--mode", "snapshot"), 2)  # 失敗＝有警告，但整體還是產出
        finally:
            os.environ.pop("MOCK_BOT_DOWN", None)
        self.assertEqual([i["code"] for i in self.doc("bankfx")["items"]][:2], ["USD", "JPY"])  # 沿用上次


if __name__ == "__main__":
    unittest.main()
