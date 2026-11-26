# -*- coding: utf-8 -*-
"""銀行牌告匯率（臺灣銀行 fltxt 文字檔）：換外幣＝銀行賣出價（現金／即期）。
檔案格式（由搜尋摘要確認，未在這個環境直接抓過，見 probe.py）：每個幣別兩行，
「代號 本行買入 現金 即期 遠期10…180」「本行賣出 現金 即期 遠期10…180」，數字以空白分隔，0.00000 代表沒有報價，沒有日期。
解析只認文字標記（本行買入／本行賣出）不認位置；數字對不上（賣出低於買入、離市場價太遠）就丟棄那個幣別，不寫錯的數字。"""
import re
from datetime import datetime

from .common import SourceError, http_get, num, url

DEFAULT = {"currencies": [{"code": "USD", "name": "美元", "dec": 3, "mid": ["usdtwd"]}, {"code": "JPY", "name": "日圓", "dec": 4, "mid": ["usdtwd", "/usdjpy"]}, {"code": "EUR", "name": "歐元", "dec": 3, "mid": ["usdtwd", "eurusd"]}, {"code": "CNY", "name": "人民幣", "dec": 3, "mid": ["usdtwd", "/usdcny"]}, {"code": "AUD", "name": "澳幣", "dec": 3, "mid": ["usdtwd", "audusd"]}, {"code": "HKD", "name": "港幣", "dec": 3, "mid": ["usdtwd", "/usdhkd"]}, {"code": "GBP", "name": "英鎊", "dec": 3, "mid": ["usdtwd", "gbpusd"]}, {"code": "KRW", "name": "韓元", "dec": 4, "mid": ["usdtwd", "/usdkrw"]}, {"code": "SGD", "name": "新加坡幣", "dec": 3, "mid": ["usdtwd", "/usdsgd"]}]}  # config/bankfx.json 沒推上去或不見時的內建預設（這樣漏推設定檔也不會整塊靜悄悄消失）

MAX_GAP = 0.08  # 牌告賣出價和市場中間價差超過 8% 視為異常（現鈔、冷門幣別的價差通常 1～3%）


def _decode(raw):
    """文字檔可能是 UTF-8（含 BOM）或 Big5；先試 UTF-8，不行再試 Big5（cp950）。"""
    if not isinstance(raw, bytes):
        return raw or ""
    for enc in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _nums(tokens, i):
    """從 tokens[i] 起連續的數字；回傳 (數字清單, 下一個位置)。"""
    out = []
    while i < len(tokens) and re.fullmatch(r"-?[\d,]*\.?\d+", tokens[i]):
        out.append(num(tokens[i].replace(",", "")))
        i += 1
    return out, i


def parse(text):
    """回傳 {代號: {"cash": (買入, 賣出), "spot": (買入, 賣出)}}；沒有報價的欄位是 None。
    用「詞」解析、不依賴換行與欄寬：看到含「買入」的詞，往前找最近的三個大寫字母當幣別，後面接數字；接著含「賣出」的詞再接數字。"""
    toks = _decode(text).replace("\ufeff", " ").replace("\u3000", " ").replace(",", " ").split()  # CSV 版（flcsv）每個幣別一行、逗號分隔，一併處理
    out, code = {}, None
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[A-Z]{3}", t):
            code = t
        elif "買入" in t and code:
            buy, k = _nums(toks, i + 1)
            if k < len(toks) and "賣出" in toks[k]:
                sell, k = _nums(toks, k + 1)
                if len(buy) >= 2 and len(sell) >= 2:
                    def pair(n):
                        b, s_ = buy[n], sell[n]
                        if not b or not s_ or b <= 0 or s_ <= 0:  # 0 或空＝沒報價
                            return None
                        return (b, s_) if s_ >= b else None  # 賣出低於買入一定是欄位錯位
                    out[code] = {"cash": pair(0), "spot": pair(1)}
                i = k - 1
            code = None
        i += 1
    return out


def parse_html(text):
    """解析 https://rate.bot.com.tw/xrt?Lang=zh-TW 網頁。回傳 (報價, 掛牌時間 ISO 字串或 None)。
    順序和文字檔不同：每個幣別是 現金買入、現金賣出、即期買入、即期賣出（「-」＝沒報價）；幣別名稱與價格在頁面裡各出現兩次（手機版／桌機版），取第一組。
    只看可見文字，不依賴 class 或 data 屬性。"""
    t = _decode(text)
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t).replace("&nbsp;", " ")
    toks = t.split()
    out, i = {}, 0
    while i < len(toks):
        m = re.fullmatch(r"\(([A-Z]{3})\)", toks[i])
        if m and m.group(1) not in out:
            code, vals, k = m.group(1), [], i + 1
            while k < len(toks) and len(vals) < 4:
                if re.fullmatch(r"\(([A-Z]{3})\)", toks[k]):
                    if toks[k] != "(%s)" % code:
                        break  # 下一個幣別了，這個幣別資料不完整
                elif re.fullmatch(r"-?[\d,]*\.?\d+", toks[k]):
                    vals.append(num(toks[k].replace(",", "")))
                elif toks[k] == "-":
                    vals.append(None)
                elif vals:
                    break
                k += 1
            if len(vals) == 4:
                def pair(b, s_):
                    return (b, s_) if b and s_ and b > 0 and s_ >= b else None
                out[code] = {"cash": pair(vals[0], vals[1]), "spot": pair(vals[2], vals[3])}
            i = k
            continue
        i += 1
    when = None
    m = re.search(r"牌價最新掛牌時間[：:]\s*(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", t)
    if m:
        when = "%s-%s-%sT%s:%s:00+08:00" % m.groups()  # 臺灣銀行掛牌時間是台北時間
    return out, when


def _peek(raw):
    """收到的不是匯率檔時，回報它是什麼（標題與可見文字開頭），方便從 meta.json 直接判斷是被擋、導向還是改版。"""
    t = _decode(raw)
    m = re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    return "%d 位元組；標題 %r；內文開頭 %r" % (len(raw), (m.group(1).strip()[:60] if m else None), body[:100])


def fetch():
    """回傳 (報價, 掛牌時間)。依序試：牌告匯率網頁（有掛牌時間）、文字檔 fltxt、官方 CSV flcsv；三個都失敗才報錯。"""
    errs = []
    for key in ("BOT_FX_HTML", "BOT_FX", "BOT_FX_CSV"):
        try:
            raw = http_get(url(key))
        except SourceError as e:
            errs.append(str(e))
            continue
        got, when = parse_html(raw) if key == "BOT_FX_HTML" else (parse(raw), None)
        if got:
            return got, when
        errs.append("%s 解析不到幣別（%s）" % (url(key).split("?")[0].rsplit("/", 3)[-3] if key != "BOT_FX_HTML" else "xrt 網頁", _peek(raw)))
    raise SourceError("臺灣銀行牌告匯率：" + "｜".join(errs))


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
    return {"source": "臺灣銀行牌告匯率", "url": "https://rate.bot.com.tw/xrt", "updated": now.isoformat(timespec="seconds"), "quoted_at": quoted_at, "items": items}, warns
