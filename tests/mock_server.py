# -*- coding: utf-8 -*-
"""測試用的假資料伺服器：模擬 Yahoo chart、FRED CSV、Forex Factory 日曆（不連外網）。

最後一天的收盤與漲跌幅照一份「暴跌日」情境設定，方便檢查氣氛標籤、今日重點等邏輯。
環境變數：
  MOCK_LAST_DATE   最後一個交易日（預設 2026-11-05）
  MOCK_FAIL        逗號分隔的 Yahoo 代號，這些回 404（例如 "MU,^SOX"）
  MOCK_YAHOO_DOWN  設為 1 則 Yahoo 全部回 500
  MOCK_JUMP        例如 "^TWII=2.0"：把該代號最後一天價格乘上倍數（測資料異常防呆）
"""
import hashlib
import json
import math
import os
import random
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAST = {  # 代號: (最後一天收盤, 最後一天漲跌 %)
    "^DJI": (51523.06, -3.44), "^GSPC": (7353.73, -4.36), "^IXIC": (24315.00, -5.52), "^SOX": (9011.14, -12.25),
    "TSM": (352.10, -11.03), "NVDA": (181.60, -9.26), "AMD": (404.40, -17.26), "MU": (656.39, -17.46),
    "2330.TW": (2240.0, -0.88), "^TWII": (41574.72, -3.20), "^KS11": (5383.65, -5.28), "^N225": (61077.90, -1.79),
    "^HSI": (24796.98, 3.67), "000001.SS": (3689.91, 0.10), "^BSESN": (79310.01, 2.60), "^GDAXI": (26283.28, -0.58),
    "TWD=X": (32.876, 0.35), "DX-Y.NYB": (101.3, -0.2), "JPY=X": (157.90, -0.4), "KRW=X": (1338.8, -0.3),
    "EURUSD=X": (1.1231, 0.2), "^TNX": (4.84, -0.05), "^VIX": (27.0, 60.0), "XAUUSD=X": (4100.0, 1.2),
    "BZ=F": (93.0, -3.0), "CL=F": (88.0, -2.5),
    "AUDUSD=X": (0.66, 0.3), "GBPUSD=X": (1.31, 0.1), "NZDUSD=X": (0.60, 0.2), "CNY=X": (7.18, -0.1), "ZAR=X": (18.2, 0.6),
    "CAD=X": (1.39, 0.1), "CHF=X": (0.88, -0.2), "HKD=X": (7.78, 0.0), "SGD=X": (1.32, 0.1),
    "^VXN": (31.0, 40.0), "^VVIX": (130.0, 30.0), "^MOVE": (118.0, 9.0), "^SKEW": (151.0, 3.0), "^OVX": (48.0, 12.0), "^GVZ": (24.0, 8.0),
    "^IRX": (4.30, -0.01), "^FVX": (4.35, -0.04), "^TYX": (4.95, -0.03), "XAGUSD=X": (52.0, 1.5),
    "USDT-USD": (1.0003, 0.01), "USDC-USD": (0.9999, 0.0), "BTC-USD": (98000.0, -2.1), "ETH-USD": (3450.0, -3.0), "SOL-USD": (162.0, -4.2),
}
ETF = {"SPY": 735, "EEM": 58, "ILF": 27, "INDA": 52, "MCHI": 55, "VNM": 14, "ASEA": 18, "EWJ": 85, "VGK": 78, "EWT": 62,
       "ACWI": 135, "AAXJ": 88, "XLK": 260, "XLV": 150, "XLE": 98, "GDX": 62, "GNR": 70, "VNQ": 92, "HYG": 79, "EMB": 91,
       "LQD": 110, "CWB": 81, "BIL": 91.5, "AOR": 70, "VT": 135, "IEF": 96}
for _k, _v in ETF.items():
    LAST[_k] = (float(_v), 0.4 if _k not in ("EEM", "GDX", "XLK") else -1.8)
RANGE_DAYS = {"5d": 5, "1mo": 22, "3mo": 66, "6mo": 130, "1y": 252, "2y": 504}
FRED_LEVEL = {"DTWEXBGS": 121.0, "DFII10": 1.9, "DGS2": 4.1, "T10YIE": 2.3, "VIXCLS": 20.0, "DFEDTARU": 4.0, "BAMLH0A0HYM2": 3.2}
FRED_MONTHLY = {"CPIAUCNS": (310.0, 0.003), "CPILFENS": (318.0, 0.0025), "PPIFID": (145.0, 0.004), "PCEPI": (126.0, 0.003),
                "PAYEMS": (159000.0, 90.0), "UNRATE": (4.2, 0.0),
                "PCEPILFE": (125.0, 0.0025), "ICSA": (210000.0, 400.0), "RSAFS": (700000.0, 0.003), "INDPRO": (103.0, 0.001),
                "A191RL1Q225SBEA": (2.0, 0.03), "UMCSENT": (62.0, 1.0), "HOUST": (1350.0, 6.0), "JTSJOL": (7200.0, 25.0),
                "CP0000EZCCM086NEST": (120.0, 0.002), "CLVMNACSCAB1GQEA": (2900000.0, 0.001), "LRUNTTTTJPM156S": (2.6, 0.0),
                "VALEXPTWM052N": (60000.0, 0.01)}


def bdays_back(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def rng_for(key):
    return random.Random(int(hashlib.md5(key.encode()).hexdigest()[:8], 16))


def yahoo_body(sym, rng_name, last_date):
    if sym not in LAST:
        return None
    n = RANGE_DAYS.get(rng_name, 22)
    last, chg = LAST[sym]
    jump = dict(x.split("=") for x in os.environ.get("MOCK_JUMP", "").split(",") if "=" in x)
    if sym in jump:
        last *= float(jump[sym])
    days = bdays_back(last_date, n)
    r = rng_for(sym)
    vol = 0.004 if sym not in ("^VIX", "^VXN", "^OVX", "^GVZ", "^MOVE", "^VVIX") else 0.04
    px = [last]
    px.insert(0, last / (1 + chg / 100))
    while len(px) < n:
        px.insert(0, px[0] / (1 + r.gauss(0.0003, vol)))
    px = px[-n:]
    ts = [int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp()) for d in days]
    return {"chart": {"result": [{"meta": {"gmtoffset": 0, "regularMarketPrice": px[-1], "regularMarketTime": ts[-1],
                                           "regularMarketDayHigh": px[-1] * 1.01, "regularMarketDayLow": px[-1] * 0.985},
                                  "timestamp": ts, "indicators": {"quote": [{"close": px}]}}], "error": None}}


def fred_csv(sid, start):
    last = date.fromisoformat(os.environ.get("MOCK_LAST_DATE", "2026-11-05"))
    rows = ["observation_date,%s" % sid]
    if sid in FRED_LEVEL:
        r = rng_for(sid)
        v = FRED_LEVEL[sid]
        days = bdays_back(last - timedelta(days=1), 520)
        vals = []
        for _ in days:
            v = max(0.05, v + r.gauss(0, 0.01 * (1 if v < 10 else v * 0.1)))
            vals.append(v)
        rows += ["%s,%.4f" % (d.isoformat(), x) for d, x in zip(days, vals)]
    elif sid == "ECBDFR":  # 利率階梯：約 300 個營業日前是 2.25，之後 2.00
        days = bdays_back(last - timedelta(days=1), 520)
        rows += ["%s,%.2f" % (d.isoformat(), 2.25 if i < 220 else 2.0) for i, d in enumerate(days)]
    elif sid in FRED_MONTHLY:
        base, step = FRED_MONTHLY[sid]
        months = []
        y, m = last.year, last.month - 1
        for _ in range(36):
            months.append(date(y, m, 1))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        months.reverse()
        for i, d in enumerate(months):
            v = base * (1 + step) ** i if step < 1 else base + step * i
            rows.append("%s,%.3f" % (d.isoformat(), v))
    else:
        return None
    return "\n".join(rows) + "\n"


def ndc_csv():
    head = "Date,工業生產指數(Index2021=100),領先指標綜合指數,領先指標不含趨勢指數,景氣對策信號綜合分數,失業率(%),五大銀行新承做放款平均利率(年息百分比)"
    rows = [head]
    y, m = 2024, 1
    for i in range(34):
        rows.append("%04d%02d,%.2f,%.2f,%.2f,%d,%.2f,%.3f" % (y, m, 100 + i * 0.8, 105 + i * 0.1, 100 + i * 0.05, 24 + (i % 7), 3.4 - 0.01 * (i % 5), 2.4 + 0.002 * i))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return ("\ufeff" + "\n".join(rows) + "\n").encode("utf-8")


def calendar_json():
    base = date.fromisoformat(os.environ.get("MOCK_LAST_DATE", "2026-11-05")) + timedelta(days=1)
    ev = [("Non-Farm Employment Change", "USD", "High", 0, "13:30", "65K", "29K"),
          ("Unemployment Rate", "USD", "High", 0, "13:30", "4.2%", "4.2%"),
          ("CPI y/y", "USD", "High", 4, "13:30", "3.7%", "3.4%"),
          ("Core CPI m/m", "USD", "High", 4, "13:30", "0.3%", "0.3%"),
          ("PPI m/m", "USD", "Medium", 5, "13:30", "0.2%", "0.1%"),
          ("Some Low Event", "USD", "Low", 1, "10:00", "", ""),
          ("National Core CPI y/y", "JPY", "High", -2, "07:30", "2.0%", "1.9%", "2.1%"),
          ("CPI y/y", "GBP", "High", -1, "07:00", "3.4%", "3.1%", "3.5%"),
          ("Unemployment Rate", "EUR", "High", -3, "10:00", "6.3%", "6.3%", "6.4%"),
          ("CPI y/y", "CNY", "High", 3, "09:30", "0.5%", "0.4%")]
    out = []
    for t, c, imp, off, hhmm, fc, pv, *act in ev:
        d = base + timedelta(days=off)
        h, m = map(int, hhmm.split(":"))
        out.append({"title": t, "country": c, "impact": imp, "forecast": fc, "previous": pv, "actual": act[0] if act else "",
                    "date": datetime(d.year, d.month, d.day, h, m, tzinfo=timezone(timedelta(hours=-5))).isoformat()})
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        last_date = date.fromisoformat(os.environ.get("MOCK_LAST_DATE", "2026-11-05"))
        if u.path.startswith("/yahoo/"):
            sym = urllib.parse.unquote(u.path[len("/yahoo/"):])
            if os.environ.get("MOCK_YAHOO_DOWN") == "1":
                return self._send(500, "{}")
            if sym in os.environ.get("MOCK_FAIL", "").split(","):
                return self._send(404, json.dumps({"chart": {"result": None, "error": {"description": "Not Found"}}}))
            body = yahoo_body(sym, (q.get("range") or ["5d"])[0], last_date)
            if body is None:
                return self._send(404, json.dumps({"chart": {"result": None, "error": {"description": "No data"}}}))
            return self._send(200, json.dumps(body))
        if u.path == "/fred":
            c = fred_csv((q.get("id") or [""])[0], (q.get("cosd") or [""])[0])
            return self._send(404, "not found") if c is None else self._send(200, c, "text/csv")
        if u.path == "/fund/tlife":
            if os.environ.get("MOCK_FUND_FAIL") in ("1", "tlife"):
                return self._send(500, "{}")
            import fund_fixtures as ff
            rows = ff.rows(1, date="2026/11/06", co="tlife")
            if os.environ.get("MOCK_FUND_DROP"):
                rows = rows[int(os.environ["MOCK_FUND_DROP"]):]
            body = json.dumps({"ResultSet": {"ExpireTime": "x", "DataLength": len(rows), "Result": rows}}, ensure_ascii=False)
            return self._send(200, body.encode("cp950", errors="replace"), "application/json; charset=big5")
        if u.path == "/fund/nanshan":
            if os.environ.get("MOCK_FUND_FAIL") in ("1", "nanshan"):
                return self._send(500, "{}")
            import fund_fixtures as ff
            part = ff.split_nanshan(ff.rows(2, per_seed=2, co="nanshan", with_extra=True))[int((q.get("m") or ["0"])[0])]
            return self._send(200, json.dumps({"ResultSet": {"ExpireTime": "x", "DataLength": len(part), "Result": part}}, ensure_ascii=False))
        if u.path == "/news":
            if os.environ.get("MOCK_NEWS_FAIL") == "1":
                return self._send(503, "down")
            qq = (q.get("q") or [""])[0]
            items = "".join("<item><title>%s 新聞%d - 測試報</title><link>https://news.example/%d</link><pubDate>Fri, 06 Nov 2026 08:%02d:00 GMT</pubDate><source url=\"https://x\">測試報</source></item>" % (qq.split(" when")[0], i, i, i) for i in range(1, 5))
            return self._send(200, '<?xml version="1.0" encoding="UTF-8"?><rss><channel>%s</channel></rss>' % items, "application/rss+xml")
        if u.path == "/ndc/meta":
            if os.environ.get("MOCK_NDC_FAIL") == "1":
                return self._send(500, "{}")
            base = "http://%s" % self.headers.get("Host")
            return self._send(200, json.dumps({"success": True, "result": {"distribution": [{"format": "JSON", "resourceDownloadUrl": base + "/nope"}, {"format": "CSV", "resourceDownloadUrl": base + "/ndc/csv"}]}}))
        if u.path == "/ndc/csv":
            return self._send(200, ndc_csv(), "text/csv; charset=utf-8")
        if u.path == "/cal/this":
            return self._send(200, json.dumps(calendar_json()))
        return self._send(404, "not found")


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def env_for(srv):
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    return {"ECO_URL_YAHOO": base + "/yahoo", "ECO_URL_FRED": base + "/fred", "ECO_URL_CALENDAR": base + "/cal/this",
            "ECO_URL_CALENDAR_NEXT": base + "/cal/next", "ECO_URL_NDC_META": base + "/ndc/meta", "ECO_URL_NEWS": base + "/news",
            "ECO_URL_FUND_TLIFE": base + "/fund/tlife", "ECO_URL_FUND_NANSHAN": base + "/fund/nanshan"}


if __name__ == "__main__":
    s = start(8765)
    print("mock on", s.server_address)
    threading.Event().wait()
