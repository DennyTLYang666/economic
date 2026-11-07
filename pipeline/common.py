# -*- coding: utf-8 -*-
"""共用工具（只用標準函式庫）。"""
import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=8))  # 台北時間，固定 +08:00
UA = "Mozilla/5.0 (eco-dashboard/3.0; +https://github.com)"
KEEP_DAYS = 540  # 每個標的最多保留的日資料筆數（約 2 年）

URLS = {
    "YAHOO": "https://query1.finance.yahoo.com/v8/finance/chart",
    "FRED": "https://fred.stlouisfed.org/graph/fredgraph.csv",
    "CALENDAR": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "CALENDAR_NEXT": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
    "NEWS": "https://news.google.com/rss/search",
    "AI": "https://api.anthropic.com/v1/messages",
    "NDC_META": "https://data.gov.tw/api/v2/rest/dataset/6099",
    "FUND_TLIFE": "https://178.taiwanlife.com/w/djjson/CtbclifeSearchJSONNEW.djjson?P1=ctbclife&P2=False&P3=False&P4=0&P5=0&",
    "FUND_NANSHAN": "https://ilp.nanshanlife.com.tw/w/custom/djjson/SearchProductJSON.djjson?P1=nanshan&P2=False&P3=False&P4=False&P5=0&P=0",
}


class SourceError(Exception):
    """單一來源失敗；呼叫端決定要不要中止。"""


def url(name):
    """來源網址可用環境變數 ECO_URL_<名稱> 覆寫（測試用）。"""
    return os.environ.get("ECO_URL_" + name, URLS[name])


def http_get(u, retries=2, timeout=25, backoff=None):
    if backoff is None:
        backoff = float(os.environ.get("ECO_BACKOFF", "2.0"))  # 測試時設 0
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
        if i < retries:
            time.sleep(backoff * (i + 1))
    raise SourceError("%s：%s" % (u.split("?")[0], last))


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_pair(path, obj, key):
    """寫 .json（網站讀取）與同名 .js（雙擊 index.html 以 file:// 開啟時的備援載入）。"""
    path = Path(path)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    atomic_write(path, text + "\n")
    atomic_write(path.with_suffix(".js"), "window.ECO_JSONP&&window.ECO_JSONP(%s,%s);\n" % (json.dumps(key), text))


def parse_d(s):
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def num(s):
    try:
        v = float((s or "").strip().replace(",", ""))
        return v if v == v and abs(v) != float("inf") else None
    except ValueError:
        return None


# ---------- 序列檔：data/series/daily.csv（id,date,close）、fred.csv（id,date,value） ----------
def load_series(path, value_col):
    out = {}
    path = Path(path)
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d, v = parse_d(r.get("date")), num(r.get(value_col))
            if r.get("id") and d and v is not None:
                out.setdefault(r["id"], []).append((d, v))
    for k in out:
        out[k].sort()
    return out


def save_series(path, series, value_col, keep=KEEP_DAYS):
    lines = ["id,date,%s" % value_col]
    for k in sorted(series):
        for d, v in series[k][-keep:]:
            lines.append("%s,%s,%s" % (k, d.isoformat(), ("%.5f" % v).rstrip("0").rstrip(".")))
    new = "\n".join(lines) + "\n"
    path = Path(path)
    if path.exists() and path.read_text(encoding="utf-8") == new:
        return False  # 沒變化就不寫，避免無謂的 commit
    atomic_write(path, new)
    return True


def merge_bars(rows, bars, keep=KEEP_DAYS):
    """把新的 (date, value) 併入舊序列：同日期以新值為準。"""
    m = dict(rows or [])
    m.update(dict(bars))
    return sorted(m.items())[-keep:]
