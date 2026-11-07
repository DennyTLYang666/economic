# -*- coding: utf-8 -*-
"""聯準會利率預期：Polymarket 預測市場機率 ＋ Yahoo 聯邦基金期貨估算（交叉檢查）。

兩個來源都是「市場押注」，不是預測，也不是 CME FedWatch 官方數字：
  1. Polymarket（Gamma API，免金鑰）：每次 FOMC 會議的「降息／不動／升息」市場，價格就是隱含機率。
  2. Yahoo 30 天期聯邦基金期貨（ZQ）：隱含利率＝100−價格，換算下一次會議的降／不動／升機率（簡化版，25bp 一檔）。
任何一邊失敗都只是少一邊，不影響其他資料。
"""
import calendar
import json
import re
import urllib.parse
from datetime import date, datetime, timedelta

from . import fetch
from .common import SourceError, TZ, http_get, url

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
FUT_CODE = "FGHJKMNQUVXZ"  # 期貨月份代碼：1 月 F … 12 月 Z
DEFAULT = {
    "fomc": [],  # 美東決議日（YYYY-MM-DD），在 config/rates.json 維護
    "queries": ["tag_slug=fed&closed=false&limit=100",
                "tag_slug=fed-rates&closed=false&limit=100",
                "closed=false&active=true&limit=500&order=volume24hr&ascending=false"],
    "futures_symbol": "ZQ{code}{yy}.CBT",
    "step_bp": 25,
    "gap_warn": 15,  # 兩個來源差距超過幾個百分點就提醒
}
_MEET = re.compile(r"(%s)(?:\s+(\d{4}))?\s+(?:fomc\s+)?meeting" % "|".join(MONTHS))
_TITLE_MONTH = re.compile(r"(?:decision|meeting|rates?)\s+(?:in|after)\s+(?:the\s+)?(%s)(?:\s+(\d{4}))?" % "|".join(MONTHS))


def load_cfg(raw):
    cfg = dict(DEFAULT)
    for k in DEFAULT:
        if isinstance(raw, dict) and raw.get(k):
            cfg[k] = raw[k]
    cfg["fomc"] = sorted(d for d in cfg["fomc"] if _d(d))
    return cfg


def _d(s):
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        return None


def next_meeting(cfg, today):
    """今天（台北日期）之後的下一次會議（美東決議日 >= 今天；台北時間決議當天早上已公布，就算過了）。"""
    for s in cfg["fomc"]:
        if _d(s) >= today:
            return _d(s)
    return None


# ---------- Polymarket ----------
def classify(text):
    t = text.lower()
    if re.search(r"no change|unchanged|\bhold|maintain|stay the same", t):
        return "hold"
    if re.search(r"decreas|\bcut|lower|reduc", t):
        return "cut"
    if re.search(r"increas|\bhike|\braise|\braising", t):
        return "hike"
    return None


def _yes_price(m):
    try:
        outs, prices = m.get("outcomes"), m.get("outcomePrices")
        outs = json.loads(outs) if isinstance(outs, str) else (outs or ["Yes", "No"])
        prices = json.loads(prices) if isinstance(prices, str) else prices
        i = [str(o).lower() for o in outs].index("yes") if "yes" in [str(o).lower() for o in outs] else 0
        p = float(prices[i])
        return p if 0 <= p <= 1 else None
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _meeting_of(text, cfg, today):
    """從文字找出是哪一次會議（月份＋可選年份），對應到 config 的 FOMC 日期。"""
    t = text.lower()
    m = _MEET.search(t) or _TITLE_MONTH.search(t)
    if not m:
        return None
    mon = MONTHS.index(m.group(1)) + 1
    yr = int(m.group(2)) if m.group(2) else None
    cands = [_d(s) for s in cfg["fomc"] if _d(s).month == mon and (yr is None or _d(s).year == yr) and _d(s) >= today - timedelta(days=1)]
    return min(cands).isoformat() if cands else None


def parse_polymarket(events, cfg, today):
    """events＝Gamma /events 的 JSON 清單。回傳 {會議日: {cut, hold, hike, vol, n}}（機率 0～1，未正規化）。"""
    acc = {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        title = str(ev.get("title") or "")
        for m in ev.get("markets") or []:
            if not isinstance(m, dict) or m.get("closed") or m.get("active") is False:
                continue
            q = "%s %s" % (m.get("question") or "", m.get("groupItemTitle") or "")
            full = "%s %s" % (title, q)
            if not re.search(r"\bfed\b|federal reserve|fomc", full.lower()) or not re.search(r"rate|bps|meeting|decision", full.lower()):
                continue
            meet = _meeting_of(full, cfg, today)
            kind = classify(q) or classify(title)
            p = _yes_price(m)
            if not meet or not kind or p is None:
                continue
            a = acc.setdefault(meet, {"cut": 0.0, "hold": 0.0, "hike": 0.0, "vol": 0.0, "n": 0, "seen": set()})
            key = (m.get("id") or m.get("slug") or q)
            if key in a["seen"]:
                continue
            a["seen"].add(key)
            a[kind] += p
            a["n"] += 1
            try:
                a["vol"] += float(m.get("volume") or m.get("volumeNum") or 0)
            except (TypeError, ValueError):
                pass
    for a in acc.values():
        a.pop("seen", None)
    return acc


def fetch_polymarket(cfg, today):
    """回傳 (meetings 清單, 說明)。meetings＝[{date, cut, hold, hike (百分比), vol, note}]，依日期排序。"""
    base, events, errs, seen = url("POLYMARKET"), [], [], set()
    for qs in cfg["queries"]:
        try:
            body = json.loads(http_get("%s%s%s" % (base, "&" if "?" in base else "?", qs)))
        except (SourceError, ValueError) as e:
            errs.append(str(e)[:100])
            continue
        if isinstance(body, dict):
            body = body.get("events") or body.get("data") or []
        for ev in body if isinstance(body, list) else []:
            k = ev.get("id") or ev.get("slug") or ev.get("title")
            if k not in seen:
                seen.add(k)
                events.append(ev)
        if parse_polymarket(events, cfg, today):
            break  # 已經找到會議市場，不必再試後面的查詢
    acc = parse_polymarket(events, cfg, today)
    if not acc:
        raise SourceError("Polymarket 找不到 FOMC 利率市場%s" % ("（" + "；".join(errs[:2]) + "）" if errs else "（查詢成功但沒有符合的市場，可能市場名稱改了）"))
    out = []
    for d in sorted(acc):
        a = acc[d]
        tot = a["cut"] + a["hold"] + a["hike"]
        row = {"date": d, "vol": round(a["vol"]), "markets": a["n"]}
        if 0.8 <= tot <= 1.2:  # 各選項合計應接近 100%；略有偏差就正規化
            row.update(cut=round(a["cut"] / tot * 100, 1), hold=round(a["hold"] / tot * 100, 1), hike=round(a["hike"] / tot * 100, 1))
        else:
            row.update(cut=None, hold=None, hike=None, note="市場選項不完整（合計 %d%%），不顯示機率" % round(tot * 100))
        out.append(row)
    return out


# ---------- Yahoo 聯邦基金期貨 ----------
def _price(cfg, y, m):
    sym = cfg["futures_symbol"].format(code=FUT_CODE[m - 1], yy="%02d" % (y % 100))
    bars, _ = fetch.yahoo_bars(sym, "5d")
    return bars[-1][1], sym


def futures_estimate(cfg, meet, r_pre):
    """依下一次會議當月的期貨價，推算會後隱含利率與 降／不動／升 機率（簡化：單一會議、25bp 一檔）。"""
    D = calendar.monthrange(meet.year, meet.month)[1]
    d, after = meet.day, D - meet.day  # 會議當天起利率仍是舊的（新利率隔天生效）
    p, sym = _price(cfg, meet.year, meet.month)
    if after <= 7:  # 月底會議：當月價格幾乎只反映舊利率，改看下個月的合約
        y, m = (meet.year + 1, 1) if meet.month == 12 else (meet.year, meet.month + 1)
        p2, sym = _price(cfg, y, m)
        r_post = 100 - p2  # 假設下個月沒有會議
    else:
        r_post = ((100 - p) - r_pre * d / D) * D / after
    e = (r_post - r_pre) * 100  # 預期變動（bp）
    step = float(cfg["step_bp"])
    cut = min(1.0, max(0.0, -e / step))
    hike = min(1.0, max(0.0, e / step))
    return {"symbol": sym, "implied_bp": round(e, 1), "cut": round(cut * 100, 1), "hold": round((1 - cut - hike) * 100, 1), "hike": round(hike * 100, 1)}


def current_rate(fred_upper, today):
    """目前利率：目標區間上限來自 FRED（DFEDTARU），有效聯邦基金利率 DFF 另抓，失敗就用上限減 0.10 估。"""
    upper = fred_upper[-1][1] if fred_upper else None
    effr, note = None, None
    try:
        obs = fetch.fred_series("DFF", (today - timedelta(days=30)).isoformat())
        effr = obs[-1][1]
    except SourceError:
        if upper is not None:
            effr, note = upper - 0.10, "有效利率以目標上限估算"
    return upper, effr, note


def build(cfg, fred_upper, today, now):
    """回傳 (doc, 警告清單)。兩邊都失敗時 raise SourceError（呼叫端沿用上一版）。"""
    warns, doc = [], {"updated": now.isoformat(timespec="seconds")}
    meet = next_meeting(cfg, today)
    if meet is None:
        raise SourceError("config/rates.json 的 fomc 日期都已過期，請補上新的會議日期")
    upper, effr, rnote = current_rate(fred_upper, today)
    doc["current"] = {"upper": upper, "lower": None if upper is None else round(upper - 0.25, 2), "effr": None if effr is None else round(effr, 2), "note": rnote}
    doc["next"] = {"date": meet.isoformat(), "days": (meet - today).days}
    doc["poly"], doc["futures"] = {"ok": False}, {"ok": False}
    try:
        rows = fetch_polymarket(cfg, today)
        doc["poly"] = {"ok": True, "meetings": rows[:4]}
    except (SourceError, ValueError, KeyError, TypeError) as e:
        doc["poly"]["error"] = str(e)[:160]
        warns.append("利率預期（Polymarket）：%s" % doc["poly"]["error"])
    if effr is None:
        doc["futures"]["error"] = "取不到目前利率，無法由期貨換算"
    else:
        try:
            doc["futures"] = dict(futures_estimate(cfg, meet, effr), ok=True)
        except (SourceError, ValueError, KeyError, TypeError, IndexError) as e:
            doc["futures"]["error"] = str(e)[:160]
        if not doc["futures"]["ok"]:
            warns.append("利率預期（Yahoo 聯邦基金期貨）：%s" % doc["futures"].get("error"))
    if not doc["poly"]["ok"] and not doc["futures"]["ok"]:
        raise SourceError("；".join(warns)[:300])
    # 交叉檢查：兩邊都有時，比較下一次會議的降／不動／升機率
    nxt = next((r for r in (doc["poly"].get("meetings") or []) if r["date"] == meet.isoformat() and r.get("cut") is not None), None)
    if nxt and doc["futures"]["ok"]:
        gap = max(abs(nxt[k] - doc["futures"][k]) for k in ("cut", "hold", "hike"))
        doc["gap"] = round(gap, 1)
        doc["gap_warn"] = gap >= cfg["gap_warn"]
    return doc, warns
