# -*- coding: utf-8 -*-
"""籌碼與夜盤：①三大法人買賣超（證交所「三大法人買賣金額統計表」，集中市場，即 Yahoo 股市「法人進出」頁的來源）
②台指期夜盤（期交所「盤後交易時段」，開放資料「期貨每日交易行情」）。
資料格式沒有在這個環境實測過，所以解析器只認「欄位名稱」不認位置、對不上就回報錯誤，不會寫入錯的數字（見 probe.py 驗證）。
只用標準函式庫；抓取函式可注入（測試用）。"""
import csv
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import SourceError, atomic_write, http_get, num, parse_d, url

UNIT = 1e8  # 證交所金額單位是「元」，網站用「億元」
SANITY = 5000.0  # 單日三大法人合計超過 5,000 億視為單位或格式有誤
KEEP = 420  # 保留約 1.5 年


def _to_date(s):
    s = str(s or "").strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", s):
        s = "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    if re.fullmatch(r"\d{7}", s):  # 民國 1151117
        s = "%s-%s-%s" % (s[:3], s[3:5], s[5:])
    m = re.fullmatch(r"(\d{3})-(\d{2})-(\d{2})", s)  # 民國年
    if m:
        s = "%d-%s-%s" % (int(m.group(1)) + 1911, m.group(2), m.group(3))
    return parse_d(s)


# ---------- 三大法人 ----------
def parse_inst(raw):
    """解析證交所 BFI82U 的 JSON。回傳 (日期, {foreign, trust, dealer, total}（億元）)；休市或尚未公布回傳 None。"""
    try:
        body = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise SourceError("三大法人：不是 JSON（%s）" % str(e)[:60])
    if not isinstance(body, dict):
        raise SourceError("三大法人：格式不是物件")
    stat = str(body.get("stat", "OK")).strip()
    rows = body.get("data")
    if rows is None and isinstance(body.get("tables"), list):
        rows = next((t.get("data") for t in body["tables"] if isinstance(t, dict) and t.get("data")), None)
    if stat.upper() != "OK" or not rows:
        return None
    d = _to_date(body.get("date"))
    if d is None:
        raise SourceError("三大法人：找不到日期欄位")
    found = {"foreign": None, "foreign_alt": None, "trust": None, "dealer_all": None, "dealer_parts": [], "total": None}
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 2:
            continue
        name = re.sub(r"\s+", "", str(r[0]))
        net = num(str(r[-1]))
        if net is None:
            continue
        v = net / UNIT
        if name.startswith("外資自營商"):
            continue  # 已含在自營商裡（期交所／櫃買說明），不重複計
        if name.startswith("外資及陸資"):
            if "不含" in name:
                found["foreign"] = v
            elif found["foreign_alt"] is None:
                found["foreign_alt"] = v
        elif name.startswith("投信"):
            found["trust"] = v
        elif name == "自營商":
            found["dealer_all"] = v
        elif name.startswith("自營商"):
            found["dealer_parts"].append(v)
        elif name.startswith("合計") or name.startswith("總計"):
            found["total"] = v
    foreign = found["foreign"] if found["foreign"] is not None else found["foreign_alt"]
    dealer = found["dealer_all"] if found["dealer_all"] is not None else (sum(found["dealer_parts"]) if found["dealer_parts"] else None)
    if foreign is None or found["trust"] is None or dealer is None:
        raise SourceError("三大法人：缺少外資／投信／自營商列（欄位名稱可能改版）")
    total = found["total"] if found["total"] is not None else foreign + found["trust"] + dealer
    if abs(foreign + found["trust"] + dealer - total) > 0.6:
        raise SourceError("三大法人：外資＋投信＋自營商與合計差 %.2f 億（格式或單位可能改版），未寫入" % (foreign + found["trust"] + dealer - total))
    if abs(total) > SANITY or abs(foreign) > SANITY:
        raise SourceError("三大法人：金額超出合理範圍（%.0f 億），單位可能改變，未寫入" % total)
    return d, {"foreign": round(foreign, 2), "trust": round(found["trust"], 2), "dealer": round(dealer, 2), "total": round(total, 2)}


def fetch_inst(day):
    """抓某一天；休市或尚未公布回傳 None。"""
    u = "%s?type=day&dayDate=%s&response=json" % (url("TWSE_INST"), day.strftime("%Y%m%d"))
    got = parse_inst(http_get(u))
    if got and got[0] != day:
        return None  # 回傳的是別天（有些網站遇休市會回最近一日）
    return got


COLS = ("foreign", "trust", "dealer", "total")


def load_inst(path):
    out = {}
    path = Path(path)
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = parse_d(r.get("date"))
            vals = [num(r.get(c)) for c in COLS]
            if d and None not in vals:
                out[d] = dict(zip(COLS, vals))
    return out


def save_inst(path, inst):
    lines = ["date," + ",".join(COLS)]
    for d in sorted(inst)[-KEEP:]:
        lines.append("%s,%s" % (d.isoformat(), ",".join("%.2f" % inst[d][c] for c in COLS)))
    new = "\n".join(lines) + "\n"
    path = Path(path)
    if path.exists() and path.read_text(encoding="utf-8") == new:
        return False
    atomic_write(path, new)
    return True


def update_inst(inst, today, now=None, getter=fetch_inst, pause=0.3, backfill_days=45, ready=(15, 30)):
    """補齊缺的交易日。回傳警告清單。今天在 ready 時間前不抓；連續失敗 3 次就停。"""
    warns = []
    start = (max(inst) + timedelta(days=1)) if inst else today - timedelta(days=backfill_days)
    d, fails = start, 0
    while d <= today:
        if d.weekday() < 5:
            if d == today and now is not None and (now.hour, now.minute) < ready:
                break
            try:
                got = getter(d)
                fails = 0
                if got:
                    inst[got[0]] = got[1]
            except SourceError as e:
                fails += 1
                warns.append("三大法人 %s：%s" % (d.isoformat(), str(e)[:120]))
                if fails >= 3:
                    break
            if pause:
                time.sleep(pause)
        d += timedelta(days=1)
    return warns[:3]


# ---------- 夜盤 ----------
ALIASES = {
    "date": ("Date", "日期", "交易日期"),
    "contract": ("Contract", "契約", "商品代號"),
    "session": ("TradingSession", "Session", "交易時段"),
    "month": ("ContractMonth(Week)", "ContractMonth", "Contract Month(Week)", "到期月份(週別)", "到期月份"),
    "open": ("Open", "開盤價"), "high": ("High", "最高價"), "low": ("Low", "最低價"),
    "last": ("Last", "最後成交價", "收盤價"), "change": ("Change", "漲跌價"), "pct": ("%", "漲跌%", "Change%"),
    "volume": ("Volume", "成交量", "Volume-Total"),
}


def _pick(row, key):
    for a in ALIASES[key]:
        if a in row and row[a] not in (None, ""):
            return row[a]
    return None


def parse_night(raw, contract="TX"):
    """從期貨每日交易行情找出「台指期近月、盤後時段」。回傳 dict 或 None（沒有盤後資料）。"""
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise SourceError("夜盤：不是 JSON（%s）" % str(e)[:60])
    if not isinstance(rows, list):
        raise SourceError("夜盤：格式不是陣列")
    if rows and not any(_pick(r, "contract") for r in rows[:5] if isinstance(r, dict)):
        raise SourceError("夜盤：找不到契約欄位（欄位名稱可能改版）")
    best = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        c = str(_pick(r, "contract") or "").strip()
        sess = str(_pick(r, "session") or "")
        month = str(_pick(r, "month") or "").strip()
        if c != contract or not re.fullmatch(r"\d{6}", month):
            continue
        if not ("盤後" in sess or "after" in sess.lower()):
            continue
        last = num(str(_pick(r, "last") or ""))
        d = _to_date(_pick(r, "date"))
        if last is None or d is None:
            continue
        if best is None or (d, -int(month)) > (best["_d"], -int(best["month"])):
            best = {"_d": d, "month": month, "last": last, "open": num(str(_pick(r, "open") or "")),
                    "high": num(str(_pick(r, "high") or "")), "low": num(str(_pick(r, "low") or "")),
                    "change": num(str(_pick(r, "change") or "")), "pct": num(str(_pick(r, "pct") or "").replace("%", "")),
                    "volume": num(str(_pick(r, "volume") or ""))}
    if not best:
        return None
    best["date"] = best.pop("_d").isoformat()  # 盤後交易歸屬「次一交易日」（期交所說明）
    return best


def fetch_night():
    return parse_night(http_get(url("TAIFEX_FUT")))


# ---------- 整理成網站資料 ----------
def _sum(vals):
    return round(sum(vals), 2)


def build_chips(inst, twii_rows, night, cfg=None):
    """回傳 chips.json 的內容；完全沒有資料時回傳 None。cfg：{since: {date, label}}。"""
    cfg = cfg or {}
    doc = {"unit": "億元", "source": "證交所三大法人買賣金額統計表（集中市場）；夜盤：期交所盤後交易時段", "inst": None, "night": None}
    if inst:
        days = sorted(inst)
        lastd = days[-1]
        f = [inst[d]["foreign"] for d in days]
        streak = 0
        for v in reversed(f):
            s = 1 if v > 0 else -1 if v < 0 else 0
            if s == 0 or (streak and (s > 0) != (streak > 0)):
                break
            streak += s
        close = {d: v for d, v in (twii_rows or [])}
        prev_close = {}
        prev = None
        for d, v in sorted(close.items()):
            prev_close[d] = prev
            prev = v

        def pct(d):
            c, p = close.get(d), prev_close.get(d)
            return round((c / p - 1) * 100, 2) if c and p else None

        doc["inst"] = {
            "latest": dict(inst[lastd], date=lastd.isoformat()),
            "streak": streak,  # 外資連續買超(+N)／賣超(−N)天數
            "sum": {k: {"foreign": _sum(f[-n:]), "total": _sum([inst[d]["total"] for d in days[-n:]]), "days": min(n, len(days))}
                    for k, n in (("d5", 5), ("d10", 10), ("d20", 20))},
            "avg": {"last5": round(sum(f[-5:]) / len(f[-5:]), 1), "prev5": round(sum(f[-10:-5]) / len(f[-10:-5]), 1)} if len(f) >= 10 else None,
            "series": [dict(date=d.isoformat(), twii_pct=pct(d), **inst[d]) for d in days[-30:]],
        }
        sd = _to_date((cfg.get("since") or {}).get("date"))
        if sd:
            part = [d for d in days if d >= sd]
            if part:
                doc["inst"]["since"] = {"label": (cfg["since"].get("label") or "自 %s 起" % sd.isoformat()), "date": sd.isoformat(),
                                        "days": len(part), "foreign": _sum([inst[d]["foreign"] for d in part]),
                                        "total": _sum([inst[d]["total"] for d in part])}
    if night:
        n = dict(night)
        nd = _to_date(n.get("date"))
        rows = sorted(twii_rows or [])
        if rows and nd and rows[-1][0] < nd:
            n["ref_close"], n["ref_date"] = round(rows[-1][1], 2), rows[-1][0].isoformat()
            n["vs_close_pct"] = round((n["last"] / rows[-1][1] - 1) * 100, 2)
        doc["night"] = n
    return doc if (doc["inst"] or doc["night"]) else None
