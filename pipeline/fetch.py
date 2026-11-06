# -*- coding: utf-8 -*-
"""資料抓取：Yahoo 報價、FRED 總經、經濟日曆。每個來源獨立失敗，不影響其他來源。"""
import csv
import io
import json
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from .common import SourceError, TZ, http_get, num, parse_d, url

# 暴跌／暴漲的合理上限；超過視為資料異常（例如代號換了、除權資料錯誤），不寫入。VIX 與殖利率不檢查。
MAX_JUMP = 0.40
SKIP_JUMP = {"vix"}  # 殖利率（kind＝rate）也不檢查


# ---------- Yahoo ----------
def pick_range(last_date, today):
    """依缺口天數選最小的抓取範圍（5d 就夠日常更新；第一次或中斷很久會自動補抓）。"""
    if last_date is None:
        return "2y"
    gap = (today - last_date).days
    for limit, rng in ((3, "5d"), (20, "1mo"), (80, "3mo"), (170, "6mo"), (350, "1y")):
        if gap <= limit:
            return rng
    return "2y"


def yahoo_bars(sym, rng):
    """回傳 (bars, meta)。bars＝[(交易所當地日期, 收盤價)]（原始收盤價，不調整股利）。"""
    body = json.loads(http_get("%s/%s?range=%s&interval=1d&includePrePost=false" % (url("YAHOO"), urllib.parse.quote(sym), rng)))
    chart = body.get("chart") or {}
    res = chart.get("result")
    if not res:
        raise SourceError("%s 沒有資料（%s）" % (sym, (chart.get("error") or {}).get("description") or chart.get("error")))
    r = res[0]
    meta = r.get("meta") or {}
    off = meta.get("gmtoffset", 0) or 0
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    ts = r.get("timestamp") or []
    bars = []
    for t, v in zip(ts, closes):
        if v is not None and v == v:
            bars.append((datetime.fromtimestamp(t + off, timezone.utc).date(), float(v)))
    # 盤中最後一根有時 close 為空，改用即時價
    px, pt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if px is not None and pt:
        d = datetime.fromtimestamp(pt + off, timezone.utc).date()
        if not bars or d > bars[-1][0]:
            bars.append((d, float(px)))
        elif d == bars[-1][0]:
            bars[-1] = (d, float(px))
    if not bars:
        raise SourceError("%s 沒有可用收盤價" % sym)
    info = {"day_high": meta.get("regularMarketDayHigh"), "day_low": meta.get("regularMarketDayLow"),
            "market_time": datetime.fromtimestamp(pt, TZ).isoformat(timespec="minutes") if pt else None}
    return bars, info


def fetch_symbol(cfg, rows, today):
    """抓一個標的；依序嘗試候選代號。回傳 (新 bars, info, 使用的代號)。"""
    rng = pick_range(rows[-1][0] if rows else None, today)
    if rows and len(rows) < 200:
        rng = "2y"  # 歷史不足（例如新加入的標的）→ 補抓
    errs = []
    for sym in cfg["yahoo"]:
        try:
            bars, info = yahoo_bars(sym, rng)
            if rows and cfg["id"] not in SKIP_JUMP and cfg.get("kind") != "rate":
                prev = rows[-1][1]
                jump = abs(bars[-1][1] / prev - 1) if prev else 0
                if jump > MAX_JUMP:
                    raise SourceError("%s 最新價與前一筆差 %.0f%%，疑似資料異常，未寫入" % (sym, jump * 100))
            return bars, info, sym
        except (SourceError, ValueError, KeyError, TypeError) as e:
            errs.append(str(e))
    raise SourceError("；".join(errs))


# ---------- FRED ----------
def fred_series(sid, start):
    text = http_get("%s?id=%s&cosd=%s" % (url("FRED"), urllib.parse.quote(sid), start)).decode("utf-8-sig")
    obs = []
    for i, row in enumerate(csv.reader(text.splitlines())):
        if i == 0 or len(row) < 2:
            continue
        d, v = parse_d(row[0]), num(row[1])  # FRED 以 "." 表示無資料
        if d is not None and v is not None:
            obs.append((d, v))
    if not obs:
        raise SourceError("FRED %s 沒有可用數值" % sid)
    return sorted(obs)


# ---------- 經濟日曆（Forex Factory 免費週 JSON） ----------
CAL_IMPACTS = {"High": "H", "Medium": "M"}
CAL_COUNTRIES = {"USD": "美國", "EUR": "歐元區", "JPY": "日本", "CNY": "中國", "GBP": "英國"}
EVENT_ZH = {
    "Non-Farm Employment Change": "非農就業", "ADP Non-Farm Employment Change": "ADP 就業報告",
    "Unemployment Rate": "失業率", "Unemployment Claims": "初請失業金人數", "CPI y/y": "CPI 年增",
    "CPI m/m": "CPI 月增", "Core CPI m/m": "核心 CPI 月增", "Core CPI y/y": "核心 CPI 年增",
    "Core PCE Price Index m/m": "核心 PCE 月增", "PPI m/m": "PPI 月增", "PPI y/y": "PPI 年增",
    "Core PPI m/m": "核心 PPI 月增", "Federal Funds Rate": "聯邦資金利率決議", "FOMC Statement": "FOMC 聲明",
    "FOMC Press Conference": "FOMC 記者會", "FOMC Meeting Minutes": "FOMC 會議紀要",
    "ISM Manufacturing PMI": "ISM 製造業 PMI", "ISM Services PMI": "ISM 服務業 PMI",
    "Retail Sales m/m": "零售銷售月增", "Advance GDP q/q": "GDP 季增（初值）", "Prelim GDP q/q": "GDP 季增（修正值）",
    "Final GDP q/q": "GDP 季增（終值）", "Main Refinancing Rate": "歐洲央行主要再融通利率",
    "Monetary Policy Statement": "貨幣政策聲明", "ECB Press Conference": "歐洲央行記者會",
    "BOJ Policy Rate": "日銀政策利率", "BOJ Press Conference": "日銀記者會", "Caixin Manufacturing PMI": "財新製造業 PMI",
    "Official Manufacturing PMI": "官方製造業 PMI", "Flash Manufacturing PMI": "製造業 PMI 初值",
    "Flash Services PMI": "服務業 PMI 初值", "Philly Fed Manufacturing Index": "費城聯儲製造業指數",
    "Empire State Manufacturing Index": "紐約聯儲製造業指數", "JOLTS Job Openings": "JOLTS 職缺數",
    "Prelim UoM Consumer Sentiment": "密大消費者信心初值", "German ifo Business Climate": "德國 ifo 企業景氣",
    "Tankan Manufacturing Index": "日銀短觀製造業", "GDP q/q": "GDP 季增", "Trade Balance": "貿易收支",
    "Average Hourly Earnings m/m": "平均時薪月增", "Official Bank Rate": "英國央行利率決議",
}


def _cal_feed(u):
    data = json.loads(http_get(u))
    if not isinstance(data, list):
        raise SourceError("日曆回應格式不符（預期為陣列）")
    return data


def fetch_calendar():
    """本週＋下週的高／中重要度事件，轉台北時間。下週取不到只算警告（週中常常還沒發布）。"""
    data = _cal_feed(url("CALENDAR"))
    note = None
    try:
        data += _cal_feed(url("CALENDAR_NEXT"))
    except (SourceError, ValueError, OSError) as e:
        note = "下週資料取得失敗：%s" % e
    out, seen = [], set()
    for e in data:
        imp = CAL_IMPACTS.get(e.get("impact"))
        if not imp or e.get("country") not in CAL_COUNTRIES:
            continue
        try:
            dt = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if dt.tzinfo is None:
            continue
        dt = dt.astimezone(TZ)
        title = (e.get("title") or "").strip()
        key = (dt, title, e["country"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M"), "event": EVENT_ZH.get(title, title), "title": title,
                    "country": CAL_COUNTRIES[e["country"]], "importance": imp, "previous": (e.get("previous") or "").strip(),
                    "forecast": (e.get("forecast") or "").strip(), "actual": (e.get("actual") or "").strip(), "source": "auto"})
    if not out:
        raise SourceError("沒有符合條件（高／中重要度）的事件")
    out.sort(key=lambda x: (x["date"], x["time"]))
    return out, note


# ---------- 國發會「景氣指標及燈號」（data.gov.tw 資料集 6099；台灣總經一次取得） ----------
def _ndc_month(s):
    """'202509'、'2025/09'、'2025-09'、'2025-09-01' → date(2025, 9, 1)；看不懂回傳 None。"""
    m = re.match(r"^\s*(\d{4})[-/.]?(\d{1,2})", str(s))
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return date(int(m.group(1)), int(m.group(2)), 1)


def parse_ndc(text, cols):
    """text＝CSV 全文；cols＝{key: 欄名關鍵字}。回傳 {key: [(date, value)]}；找不到的欄就沒有該 key。"""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")  # 有些檔只用 CR 換行
    rows = list(csv.reader(io.StringIO(text, newline="")))
    DATE_COL = r"date|日期|時間|月份|統計期|期別|年月|period"
    hi = next((i for i, r in enumerate(rows[:6]) if any(re.search(DATE_COL, c, re.I) for c in r)), None)
    if hi is None:  # 欄名不認得：第一列當標題、第一欄當日期
        hi = 0
    head = [re.sub(r"[\s（）()\ufeff]", "", c) for c in (rows[hi] if rows else [])]
    di = next((i for i, c in enumerate(head) if re.search(DATE_COL, c, re.I)), 0)
    if not head:
        raise SourceError("國發會 CSV 是空的")
    out = {}
    for key, name in cols.items():
        want = re.sub(r"[\s（）()]", "", name)
        ci = next((i for i, c in enumerate(head) if want in c), None)
        if ci is None:
            continue
        pts = []
        for r in rows[hi + 1:]:
            if len(r) <= max(ci, di):
                continue
            d, v = _ndc_month(r[di]), num(r[ci].replace(",", ""))
            if d and v is not None:
                pts.append((d, v))
        if pts:
            out[key] = sorted(pts)
    return out


def fetch_ndc(cols, csv_url=None):
    """先用設定／環境變數給的 CSV 網址；沒有就問 data.gov.tw 的資料集 API 取得下載連結。"""
    u = os.environ.get("ECO_URL_NDC_CSV") or csv_url
    if not u:
        meta = json.loads(http_get(url("NDC_META")))
        res = (meta.get("result") or meta) if isinstance(meta, dict) else {}
        dist = res.get("distribution") or []
        cand = [d for d in dist if isinstance(d, dict) and (d.get("resourceDownloadUrl") or d.get("downloadURL"))]
        pick = next((d for d in cand if "csv" in str(d.get("format") or d.get("resourceFormat") or "").lower()), cand[0] if cand else None)
        if not pick:
            raise SourceError("國發會資料集沒有可下載的檔案")
        u = pick.get("resourceDownloadUrl") or pick.get("downloadURL")
    raw = http_get(u)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp950", errors="replace")
    try:
        got = parse_ndc(text, cols)
    except csv.Error as e:
        raise SourceError("國發會檔案不是 CSV（%s）；開頭：%r" % (e, text[:80]))
    if not got:
        first = [c.strip() for c in next(csv.reader(io.StringIO(text.replace("\r", "\n"), newline="")), [])]
        raise SourceError("國發會 CSV 沒有找到需要的欄位；實際欄位：%s" % "｜".join(first)[:200])
    return got
