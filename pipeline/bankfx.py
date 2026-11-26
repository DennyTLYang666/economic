# -*- coding: utf-8 -*-
"""銀行牌告匯率（玉山銀行）：換外幣＝銀行賣出價（現金／即期／網銀優惠）。
來源：玉山「外幣匯率」頁面實際呼叫的資料介面 POST https://www.esunbank.com/api/client/ExchangeRate/LastRateInfo（回傳 JSON，不用登入）。
欄位對照（2026-11-26 用瀏覽器對照頁面數字確認）：BBoardRate／SBoardRate＝即期買入／賣出；CashBBoardRate／CashSBoardRate＝現金買入／賣出；
BuyIncreaseRate／SellDecreaseRate＝網銀／App 優惠買入／賣出（已含加減碼）；CCY＝"USD/TWD"；UpdateTime＝"/Date(毫秒)/"。沒有報價的欄位是空字串或 "-"。
防呆：賣出低於買入、離市場價太遠（超過 8 個百分點）的幣別會被丟棄，不會顯示錯的數字；整個來源失敗時沿用上一次的結果。"""
import json
import re
from datetime import datetime

from .common import TZ, SourceError, http_get, num, url

DEFAULT = {"currencies": [{"code": "USD", "name": "美元", "dec": 3, "mid": ["usdtwd"]}, {"code": "JPY", "name": "日圓", "dec": 4, "mid": ["usdtwd", "/usdjpy"]}, {"code": "EUR", "name": "歐元", "dec": 3, "mid": ["usdtwd", "eurusd"]}, {"code": "CNY", "name": "人民幣", "dec": 3, "mid": ["usdtwd", "/usdcny"]}, {"code": "AUD", "name": "澳幣", "dec": 3, "mid": ["usdtwd", "audusd"]}, {"code": "HKD", "name": "港幣", "dec": 3, "mid": ["usdtwd", "/usdhkd"]}, {"code": "GBP", "name": "英鎊", "dec": 3, "mid": ["usdtwd", "gbpusd"]}, {"code": "SGD", "name": "新加坡幣", "dec": 3, "mid": ["usdtwd", "/usdsgd"]}]}  # config/bankfx.json 沒推上去或不見時的內建預設
MAX_GAP = 0.08  # 牌告賣出價和市場中間價差超過 8% 視為異常（現鈔價差通常 1～3%）


def _px(v):
    """數字或字串→浮點數；空字串、"-"、0 都當作沒有報價。"""
    x = num(str(v).replace(",", "")) if v not in (None, "", "-") else None
    return x if x and x > 0 else None


def _pair(b, s):
    b, s = _px(b), _px(s)
    return (b, s) if b and s and s >= b else None  # 賣出低於買入一定是欄位錯位


def _when(ms):
    m = re.search(r"-?\d{10,13}", str(ms or ""))
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(0)) / 1000, TZ).isoformat(timespec="seconds")


def parse(raw):
    """回傳 ({代號: {"cash": (買,賣), "spot": (買,賣), "online": (買,賣)}}, 玉山更新時間 ISO)；沒報價的欄位是 None。"""
    try:
        d = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
        rows = d["Rates"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, None
    out, when = {}, None
    for r in rows if isinstance(rows, list) else []:
        code = str(r.get("CCY") or "").split("/")[0].strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            continue
        out[code] = {"cash": _pair(r.get("CashBBoardRate"), r.get("CashSBoardRate")), "spot": _pair(r.get("BBoardRate"), r.get("SBoardRate")),
                     "online": _pair(r.get("BuyIncreaseRate"), r.get("SellDecreaseRate"))}
        when = when or _when(r.get("UpdateTime"))
    return out, when


def _peek(raw):
    """收到的不是預期的 JSON 時，回報它是什麼（標題與可見文字開頭），方便從 meta.json 判斷是被擋、導向還是改版。"""
    t = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    m = re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    return "%d 位元組；標題 %r；內文開頭 %r" % (len(raw), (m.group(1).strip()[:60] if m else None), body[:100])


def fetch():
    """回傳 (報價, 玉山更新時間)。"""
    raw = http_get(url("ESUN_FX"), post=True)
    got, when = parse(raw)
    if not got:
        raise SourceError("玉山牌告匯率解析不到幣別（%s）" % _peek(raw))
    return got, when


def _mid(expr, last):
    v = None
    for tok in expr:
        div = tok.startswith("/")
        x = last.get(tok.lstrip("/"))
        if x is None or x == 0:
            return None
        v = x if v is None else (v / x if div else v * x)
    return v


def build(cfg, quotes, daily, now, quoted_at=None):
    """quotes＝parse() 的結果；daily＝{標的id: [(date, close)]}，用來算和市場中間價的差。回傳 (doc, 警告清單)。"""
    last = {k: v[-1][1] for k, v in (daily or {}).items() if v}
    items, warns = [], []
    for c in cfg.get("currencies", []):
        q = quotes.get(c["code"])
        if not q or not (q["cash"] or q["spot"] or q["online"]):
            warns.append("牌告匯率沒有 %s" % c["code"])
            continue
        mid = _mid(c.get("mid") or [], last)
        ref = (q["spot"] or q["cash"] or q["online"])[1]
        if mid and abs(ref / mid - 1) > MAX_GAP:
            warns.append("牌告匯率 %s 離市場價 %.1f%%，已略過（欄位可能錯位）" % (c["code"], (ref / mid - 1) * 100))
            continue
        it = {"code": c["code"], "name": c["name"], "dec": c.get("dec", 3)}
        for k in ("cash", "spot", "online"):
            it[k] = {"buy": q[k][0], "sell": q[k][1]} if q[k] else None
        if mid:
            it["mid"] = round(mid, 6)
            for k in ("cash", "spot", "online"):
                if q[k]:
                    it[k + "_gap"] = round((q[k][1] / mid - 1) * 100, 2)  # 賣出價比市場價貴幾 %
        items.append(it)
    if not items:
        raise SourceError("牌告匯率：沒有任何幣別通過檢查")
    return {"source": "玉山銀行牌告匯率", "url": "https://www.esunbank.com/zh-tw/personal/deposit/rate/forex/foreign-exchange-rates",
            "updated": now.isoformat(timespec="seconds"), "quoted_at": quoted_at, "items": items}, warns
