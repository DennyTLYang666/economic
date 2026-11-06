# -*- coding: utf-8 -*-
"""宏觀環境燈號（規則式，非預測）。

五個宏觀因子各看 20 日趨勢，再乘上「該因子與產品報酬近 250 日的實測相關方向」，
平均後 ≥ +0.34 偏多、≤ −0.34 偏空，其餘中性；相關絕對值低於 0.15 的因子不計分。
"""
import math

MIN_ABS_CORR = 0.15
LABEL_BULL, LABEL_BEAR = 0.34, -0.34


def _mean(v):
    return sum(v) / len(v)


def corr(x, y):
    n = min(len(x), len(y))
    if n < 8:
        return None
    x, y = x[-n:], y[-n:]
    mx, my = _mean(x), _mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None


def trend(rows, change):
    """20 日趨勢：現值高於 20 日均線且均線上揚＝上行(1)；反之下行(-1)；其餘 0。"""
    v = [r[1] for r in rows]
    n = len(v)
    if n < 30:
        return None
    sma = lambda e, w: sum(v[e - w:e]) / w
    now, prev, last = sma(n, 20), sma(n - 5, 20), v[-1]
    d = 1 if (last > now and now > prev) else -1 if (last < now and now < prev) else 0
    base = v[max(0, n - 21)]
    d20 = (last / base - 1) * 100 if change == "pct" else last - base
    return {"dir": d, "last": last, "d20": d20, "date": rows[-1][0]}


def aligned(prod, ser, change):
    """產品與因子在共同日期上的連續變化（產品報酬 vs 因子變化）。"""
    sm = dict(ser)
    pr, sr = [], []
    pc = sc = None
    for d, pv in prod:
        if d not in sm:
            continue
        sv = sm[d]
        if pc is not None and pc > 0 and (change != "pct" or sc > 0):
            pr.append(pv / pc - 1)
            sr.append(sv / sc - 1 if change == "pct" else sv - sc)
        pc, sc = pv, sv
    return pr, sr


def sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def light(prod, factors, fred):
    contrib, score, cnt = [], 0, 0
    for f in factors:
        ser = fred.get(f["id"])
        t = trend(ser, f["change"]) if ser else None
        if not t:
            continue
        pr, sr = aligned(prod, ser, f["change"])
        c250 = corr(pr[-250:], sr[-250:])
        imp = 0
        if c250 is not None and abs(c250) >= MIN_ABS_CORR:
            imp = t["dir"] * sign(c250)
            score += imp
            cnt += 1
        contrib.append({"id": f["id"], "imp": imp, "corr": None if c250 is None else round(c250, 2)})
    if not contrib:
        return None
    avg = score / cnt if cnt else 0.0
    label = "偏多" if avg >= LABEL_BULL else "偏空" if avg <= LABEL_BEAR else "中性"
    return {"score": round(avg, 2), "label": label, "n": cnt, "contrib": contrib}


def build(cfg, daily, fred):
    """回傳 {"factors": [...], "products": [...]}；資料不足時回傳 None。"""
    facs = [f for f in cfg["fred"] if f.get("light")]
    factors = []
    for f in facs:
        t = trend(fred.get(f["id"]) or [], f["change"]) if fred.get(f["id"]) else None
        if t:
            factors.append({"id": f["id"], "name": f["name"], "role": f["role"], "change": f["change"],
                            "value": round(t["last"], 3), "date": t["date"].isoformat(), "dir": t["dir"], "d20": round(t["d20"], 3)})
    prods = []
    for p in cfg["lights"]:
        rows = daily.get(p["id"])
        if not rows or len(rows) < 60:
            continue
        lt = light(rows, facs, fred)
        if lt:
            prods.append(dict(id=p["id"], name=p["name"], **lt))
    if not factors or not prods:
        return None
    return {"factors": factors, "products": prods,
            "note": "規則式，不是預測：因子 20 日趨勢 × 近 250 日實測相關方向；相關絕對值低於 0.15 的因子不計分。"}
