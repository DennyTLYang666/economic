# -*- coding: utf-8 -*-
"""銀行牌告匯率（臺灣銀行 fltxt 文字檔）：換外幣＝銀行賣出價（現金／即期）。
檔案格式（由搜尋摘要確認，未在這個環境直接抓過，見 probe.py）：每個幣別兩行，
「代號 本行買入 現金 即期 遠期10…180」「本行賣出 現金 即期 遠期10…180」，數字以空白分隔，0.00000 代表沒有報價，沒有日期。
解析只認文字標記（本行買入／本行賣出）不認位置；數字對不上（賣出低於買入、離市場價太遠）就丟棄那個幣別，不寫錯的數字。"""
import re
from datetime import datetime

from .common import SourceError, http_get, num, url

ROW = re.compile(r"([A-Z]{3})\s+本行買入((?:\s+[\d.,]+){2,9})\s+本行賣出((?:\s+[\d.,]+){2,9})")
MAX_GAP = 0.08  # 牌告賣出價和市場中間價差超過 8% 視為異常（現鈔、冷門幣別的價差通常 1～3%）


def parse(text):
    """回傳 {代號: {"cash": (買入, 賣出), "spot": (買入, 賣出)}}；沒有報價的欄位是 None。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", "replace")
    out = {}
    for m in ROW.finditer(text or ""):
        buy = [num(x) for x in m.group(2).split()]
        sell = [num(x) for x in m.group(3).split()]
        if len(buy) < 2 or len(sell) < 2:
            continue

        def pair(i):
            b, s = buy[i], sell[i]
            if not b or not s or b <= 0 or s <= 0:  # 0 或空＝沒報價
                return None
            return (b, s) if s >= b else None  # 賣出低於買入一定是欄位錯位
        out[m.group(1)] = {"cash": pair(0), "spot": pair(1)}
    return out


def fetch():
    raw = http_get(url("BOT_FX"))
    got = parse(raw)
    if not got:
        raise SourceError("臺灣銀行牌告匯率：解析不到任何幣別（格式可能改了）")
    return got


def _mid(expr, last):
    v = None
    for tok in expr:
        div = tok.startswith("/")
        x = last.get(tok.lstrip("/"))
        if x is None or x == 0:
            return None
        v = x if v is None else (v / x if div else v * x)
    return v


def build(cfg, quotes, daily, now):
    """quotes＝parse() 的結果；daily＝{標的id: [(date, close)]}，用來算和市場中間價的差。回傳 (doc, 警告清單)。"""
    last = {k: v[-1][1] for k, v in (daily or {}).items() if v}
    items, warns = [], []
    for c in cfg.get("currencies", []):
        q = quotes.get(c["code"])
        if not q or not (q["cash"] or q["spot"]):
            warns.append("牌告匯率沒有 %s" % c["code"])
            continue
        mid = _mid(c.get("mid") or [], last)
        ref = (q["spot"] or q["cash"])[1]
        if mid and abs(ref / mid - 1) > MAX_GAP:
            warns.append("牌告匯率 %s 離市場價 %.1f%%，已略過（欄位可能錯位）" % (c["code"], (ref / mid - 1) * 100))
            continue
        it = {"code": c["code"], "name": c["name"], "dec": c.get("dec", 3)}
        for k in ("cash", "spot"):
            it[k] = {"buy": q[k][0], "sell": q[k][1]} if q[k] else None
        if mid:
            it["mid"] = round(mid, 6)
            if q["spot"]:
                it["spot_gap"] = round((q["spot"][1] / mid - 1) * 100, 2)  # 即期賣出比市場價貴幾 %
            if q["cash"]:
                it["cash_gap"] = round((q["cash"][1] / mid - 1) * 100, 2)
        items.append(it)
    if not items:
        raise SourceError("牌告匯率：沒有任何幣別通過檢查")
    return {"source": "臺灣銀行牌告匯率", "url": "https://rate.bot.com.tw/xrt", "updated": now.isoformat(timespec="seconds"), "items": items}, warns
