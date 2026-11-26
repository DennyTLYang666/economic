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

    def test_csv_layout(self):  # 官方 CSV（flcsv）：每個幣別一行，欄位 0 幣別、1 標記、2 現金買入、3 即期買入…、11 標記、12 現金賣出、13 即期賣出
        row = ["USD", "本行買入", "31.895", "32.22"] + ["32.2"] * 7 + ["本行賣出", "32.565", "32.37"] + ["32.3"] * 7
        q = bankfx.parse("幣別,匯率,現金,即期\n" + ",".join(row) + "\n")
        self.assertEqual(q["USD"]["cash"], (31.895, 32.565))
        self.assertEqual(q["USD"]["spot"], (32.22, 32.37))

    def test_block_page_is_reported_with_content(self):  # 被擋時 meta.json 要看得出收到什麼
        html = b"<!DOCTYPE html><html><head><title>Access Denied</title></head><body><h1>Blocked</h1>Your IP is not allowed</body></html>"
        self.assertEqual(bankfx.parse(html), {})
        info = bankfx._peek(html)
        self.assertIn("Access Denied", info)
        self.assertIn("Your IP is not allowed", info)

    def test_html_page_order_and_timestamp(self):  # 網頁版順序是 現金買入、現金賣出、即期買入、即期賣出；每個幣別名稱與價格出現兩次
        row = lambda nm, c, v: ('<tr><td><div class="visible-phone">%s (%s)</div><div>%s (%s)</div></td>' % (nm, c, nm, c)) + "".join("<td>%s</td>" % x for x in v) + '<td><a>查詢</a></td><td><a>查詢</a></td>' + "".join("<td>%s</td>" % x for x in v) + "</tr>"
        page = ("<html><script>var a=(USD);</script><h1>2026/11/26 本行營業時間牌告匯率</h1><p>牌價最新掛牌時間：2026/11/26 10:01</p><table>" +
                row("美金", "USD", ["32.66", "33.33", "33.01", "33.11"]) + row("韓元", "KRW", ["-", "-", "0.0227", "0.0245"]) + row("日圓", "JPY", ["0.2031", "0.2161", "0.2072", "0.2102"]) + "</table></html>")
        q, when = bankfx.parse_html(page)
        self.assertEqual(q["USD"]["cash"], (32.66, 33.33))   # 現金：買入 32.66、賣出 33.33
        self.assertEqual(q["USD"]["spot"], (33.01, 33.11))   # 即期：買入 33.01、賣出 33.11
        self.assertIsNone(q["KRW"]["cash"])
        self.assertEqual(q["JPY"]["spot"], (0.2072, 0.2102))
        self.assertEqual(when, "2026-11-26T10:01:00+08:00")

    def test_challenge_page_gives_nothing(self):
        q, when = bankfx.parse_html("<html><head><title>Challenge Validation</title></head><body>Challenge Validation</body></html>")
        self.assertEqual((q, when), ({}, None))

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
    def setUp(self):
        super().setUp()
        self.set_remote(True)  # 正式設定是 remote=false（Actions 不抓）；這裡測「由更新流程自己抓」的路徑

    def set_remote(self, flag):
        p = self.tmp / "config" / "bankfx.json"
        c = json.loads(p.read_text(encoding="utf-8"))
        c["remote"] = flag
        p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")

    def test_falls_back_to_text_file_when_page_is_challenged(self):  # 網頁被擋（驗證頁）→ 改抓文字檔，資料一樣，只是沒有掛牌時間
        os.environ["MOCK_BOT_HTML_DOWN"] = "1"
        try:
            self.assertEqual(self.go("--mode", "daily"), 0)
        finally:
            os.environ.pop("MOCK_BOT_HTML_DOWN", None)
        doc = self.doc("bankfx")
        self.assertIsNone(doc["quoted_at"])
        self.assertEqual((doc["items"][0]["cash"]["sell"], doc["items"][0]["spot"]["sell"]), (33.2, 32.95))

    def test_daily_run_writes_bankfx_and_keeps_cache_when_source_fails(self):
        self.assertEqual(self.go("--mode", "daily"), 0)
        doc = self.doc("bankfx")
        self.assertEqual(doc["quoted_at"], "2026-11-26T10:01:00+08:00")  # 優先用網頁，帶掛牌時間
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

    def test_remote_false_reads_cache_only(self):  # Actions 不抓：沒有檔案要提醒，有檔案就發布，太舊要提醒
        self.set_remote(False)
        self.assertEqual(self.go("--mode", "daily"), 2)
        self.assertTrue(any("sync_bankfx" in p for p in self.doc("meta")["problems"]))
        self.assertFalse((self.tmp / "data" / "site" / "bankfx.json").exists())
        cache = self.tmp / "data" / "series" / "bankfx_cache.json"
        bank, _ = bankfx.build(json.loads((self.tmp / "config" / "bankfx.json").read_text(encoding="utf-8")),
                               bankfx.parse(SAMPLE), {}, datetime.now(TZ), "2026-11-26T10:01:00+08:00")
        cache.write_text(json.dumps(bank, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.go("--mode", "daily"), 0)  # 沒有再打網路也成功
        self.assertEqual(self.doc("bankfx")["quoted_at"], "2026-11-26T10:01:00+08:00")
        old = dict(bank, updated="2026-11-01T10:00:00+08:00")
        cache.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.go("--mode", "daily"), 2)
        self.assertTrue(any("沒更新" in p for p in self.doc("meta")["problems"]))


class TestSyncLocal(Base):
    def test_sync_pushes_only_when_changed(self):
        import subprocess
        import tempfile
        from pipeline import sync_bankfx
        root = Path(tempfile.mkdtemp())
        git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        git("init", "--bare", "-b", "main", str(root / "remote.git"))
        git("clone", str(root / "remote.git"), str(root / "work"))
        work = root / "work"
        git("config", "user.email", "t@example.com", cwd=work)
        git("config", "user.name", "t", cwd=work)
        (work / "config").mkdir()
        shutil_copy = __import__("shutil").copy
        shutil_copy(Path(__file__).resolve().parent.parent / "config" / "bankfx.json", work / "config" / "bankfx.json")
        (work / "README").write_text("x", encoding="utf-8")
        git("add", "-A", cwd=work)
        git("commit", "-m", "init", cwd=work)
        git("push", "-u", "origin", "main", cwd=work)
        self.assertEqual(sync_bankfx.main(["--repo", str(work), "--push"]), 0)
        cache = work / "data" / "series" / "bankfx_cache.json"
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["items"][0]["cash"]["sell"], 33.2)
        n1 = git("rev-list", "--count", "main", cwd=work).stdout.strip()
        self.assertEqual(sync_bankfx.main(["--repo", str(work), "--push"]), 0)  # 價格與掛牌時間都沒變 → 不再 commit
        self.assertEqual(git("rev-list", "--count", "main", cwd=work).stdout.strip(), n1)
        self.assertEqual(git("rev-list", "--count", "origin/main", cwd=work).stdout.strip(), n1)  # 已推上去
        os.environ["MOCK_BOT_HTML_DOWN"] = "1"
        os.environ["MOCK_BOT_DOWN"] = "1"  # 全部被擋：回傳失敗碼，不動檔案
        try:
            self.assertEqual(sync_bankfx.main(["--repo", str(work), "--push"]), 1)
        finally:
            os.environ.pop("MOCK_BOT_HTML_DOWN", None)
            os.environ.pop("MOCK_BOT_DOWN", None)
        __import__("shutil").rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
