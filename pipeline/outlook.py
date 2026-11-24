# -*- coding: utf-8 -*-
"""區間預估與成績單：config/outlook.json 手動填入經理報告的預估區間，系統每天累積實際的盤中最高／最低價，
期間內即時對照、期間結束後留下成績單（預估 vs 實際）。預估數字是「報告摘要」，不是本系統的預測，也不是投資建議。"""
import csv
from datetime import date
from pathlib import Path

from .common import atomic_write, num, parse_d

KEEP = 400


def load_hl(path):
    out = {}
    path = Path(path)
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d, h, l = parse_d(r.get("date")), num(r.get("high")), num(r.get("low"))
            if r.get("id") and d and h is not None and l is not None:
                out.setdefault(r["id"], {})[d] = (h, l)
    return out


def save_hl(path, hl):
    lines = ["id,date,high,low"]
    for k in sorted(hl):
        for d in sorted(hl[k])[-KEEP:]:
            h, l = hl[k][d]
            lines.append("%s,%s,%s,%s" % (k, d.isoformat(), ("%.5f" % h).rstrip("0").rstrip("."), ("%.5f" % l).rstrip("0").rstrip(".")))
    new = "\n".join(lines) + "\n"
    path = Path(path)
    if path.exists() and path.read_text(encoding="utf-8") == new:
        return False
    atomic_write(path, new)
    return True


def realized(hl_id, start, end):
    """期間內（含頭尾）累積到的最高、最低與天數；沒有資料回傳 None。"""
    pts = [(d, v) for d, v in (hl_id or {}).items() if start <= d <= end]
    if not pts:
        return None
    return {"high": max(v[0] for _, v in pts), "low": min(v[1] for _, v in pts), "days": len(pts)}


def judge(p80, high, low, dec):
    """回傳 (狀態碼, 一句話)。狀態：in／below／above／both。"""
    lo, hi = p80
    fmt = lambda x: format(x, ",.%df" % dec)
    below, above = low < lo, high > hi
    if below and above:
        return "both", "上下都突破：最低 %s（低於下緣 %s）、最高 %s（高於上緣 %s）" % (fmt(low), fmt(lo), fmt(high), fmt(hi))
    if below:
        return "below", "跌破下緣：最低 %s，比 8 成下緣 %s 低 %s" % (fmt(low), fmt(lo), fmt(lo - low))
    if above:
        return "above", "突破上緣：最高 %s，比 8 成上緣 %s 高 %s" % (fmt(high), fmt(hi), fmt(high - hi))
    return "in", "在區間內：最低 %s、最高 %s（8 成區間 %s～%s）" % (fmt(low), fmt(high), fmt(lo), fmt(hi))


def _score(item, period, hl, today, manual=None):
    """單一標的的對照結果；manual 是手動填的實際值（優先）。"""
    start, end = parse_d(period[0]), parse_d(period[1])
    dec = item.get("dec", 2)
    act = None
    if manual and manual.get("high") is not None and manual.get("low") is not None:
        act = {"high": manual["high"], "low": manual["low"], "days": None, "manual": True, "note": manual.get("note", "")}
    else:
        r = realized(hl.get(item["id"]), start, min(end, today))
        if r:
            act = dict(r, manual=False, note="")
    out = {"id": item["id"], "name": item.get("name", item["id"]), "dec": dec, "p80": item.get("p80"), "likely": item.get("likely"),
           "actual": None, "status": None, "text": None}
    if act and item.get("p80"):
        st, tx = judge(item["p80"], act["high"], act["low"], dec)
        out.update(actual=act, status=st, text=tx)
    elif not act:
        out["text"] = "尚無實際資料（系統從開始記錄盤中高低點的那天起才有）"
    return out


def build_outlook(cfg, hl, today):
    """cfg＝config/outlook.json。沒啟用或沒內容回傳 None。"""
    if not cfg or not cfg.get("enabled"):
        return None
    cur, past = cfg.get("current"), cfg.get("past") or []
    if not cur and not past:
        return None
    doc = {"note": cfg.get("note") or "預估區間整理自經理分享的報告，不是本系統的預測，也不是投資建議；8 成機率代表每 5 次約有 1 次會被突破。",
           "current": None, "scorecard": []}
    if cur:
        start, end = parse_d(cur["period"][0]), parse_d(cur["period"][1])
        doc["current"] = {"issued": cur.get("issued"), "source": cur.get("source"), "period": cur["period"], "note": cur.get("note"),
                          "ended": today > end, "started": today >= start,
                          "items": [dict(it, progress=_score(it, cur["period"], hl, today)) for it in cur.get("items", [])]}
    for p in sorted(past, key=lambda x: x.get("issued", ""), reverse=True):
        doc["scorecard"].append({"issued": p.get("issued"), "period": p["period"], "lesson": p.get("lesson"), "partial_to": p.get("partial_to"),
                                 "items": [_score(it, p["period"], hl, today, it.get("actual")) for it in p.get("items", [])]})
    return doc
