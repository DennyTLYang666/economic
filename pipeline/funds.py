# -*- coding: utf-8 -*-
"""基金：抓台灣人壽、南山人壽的投資標的清單 → 統一欄位 → 規則式分類 → 每個風險階層挑 2–3 檔 → 各類別概況。

兩家官網用同一套後端（JSON、Big5 或 UTF-8 編碼）：
  台壽：一個請求就有全部欄位（V1…V43）。
  南山：用 m=0（淨值）、m=1（績效）、m=2（風險）三個請求，依 V40 合併。
欄位對照（兩家相同）：V3 名稱、V5 官網分類、V7 投信、V10 近1月、V11 近3月、V12 近6月、V13 近1年、V14 近2年、
V15 近3年、V16 近5年、V17 今年以來、V18 淨值、V20 日漲跌%、V21 夏普、V22 β、V23 年化標準差、V26 幣別、
V28 風險等級 RR1–RR5、V29 代碼、V35 成立日、V36 成立以來、V39 規模、V40 唯一鍵、V41 類型（A 國內、B 境外、C ETF、D 委託帳戶…）。
本檔的推薦與統計都是依公開數字做的規則式整理，不是投資建議。
"""
import html
import json
import re
import statistics
import time
from datetime import date, datetime, timedelta

from .common import SourceError, TZ, http_get, merge_bars, num, url

CO = {"tlife": "台壽", "nanshan": "南山"}
BAD_NAME = re.compile(r"暫停|停止|不再接受|清算|終止|合併")
STRIP_PAREN = re.compile(r"[（(][^）)]*[）)]")
STRIP_TAIL = re.compile(r"[（(][^）)]*$")
STRIP_CO = re.compile(r"中國信託|中華開發|中信")
RETS = ("ytd", "m1", "m3", "m6", "y1", "y2", "y3", "y5", "inc")


# ---------- 抓取與解碼 ----------
def decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp950", errors="replace")


def load_json(raw):
    obj = json.loads(decode(raw))
    rows = ((obj.get("ResultSet") or {}).get("Result"))
    if not isinstance(rows, list) or not rows:
        raise SourceError("基金清單格式不符或是空的")
    return rows


def fetch_tlife():
    return load_json(http_get(url("FUND_TLIFE"), retries=1, timeout=60))


def fetch_nanshan():
    base = url("FUND_NANSHAN")
    sep = "&" if "?" in base else "?"
    parts = [load_json(http_get("%s%sm=%d" % (base, sep, m), retries=1, timeout=60)) for m in (0, 1, 2)]
    merged = {}
    for rows in parts:
        for r in rows:
            k = r.get("V40")
            if k:
                merged.setdefault(k, {}).update(r)
    if not merged:
        raise SourceError("南山基金清單沒有可用資料")
    return list(merged.values())


# ---------- 統一欄位 ----------
def fnum(x):
    v = num(x if isinstance(x, str) else ("" if x is None else str(x)))
    return v


def parse_size(s):
    """'54.19億' → 54.19（億，幣別依該基金計價）；看不懂回 None。"""
    m = re.match(r"\s*([\d,.]+)\s*億", s or "")
    return num(m.group(1)) if m else None


def parse_date(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y/%m/%d").date()
    except ValueError:
        return None


STRIP_TAG = re.compile(r"<[^>]*>")


def clean_name(n):
    n = html.unescape(STRIP_TAG.sub("", n or ""))  # 南山的名稱尾端帶 <span class="fundname-warning"></span>
    n = STRIP_PAREN.sub("", n)
    n = STRIP_TAIL.sub("", n)
    return re.sub(r"\s+", " ", n).strip(" ※-")


def normalize(r, co):
    key = r.get("V40") or ""
    rr = re.search(r"\d", r.get("V28") or "")
    f = {"k": key, "b": key.split("-")[0], "co": co, "c": r.get("V29") or "", "raw": html.unescape(STRIP_TAG.sub("", r.get("V3") or "")),
         "n": clean_name(r.get("V3")), "inv": r.get("V7") if r.get("V7") not in (None, "-") else "",
         "cat": r.get("V5") or "", "ty": r.get("V41") or "", "kind": r.get("V31") or "", "cc": r.get("V26") or "",
         "rr": int(rr.group()) if rr else None, "nav": fnum(r.get("V18")), "dt": r.get("V1") or "", "d1": fnum(r.get("V20")),
         "sd": fnum(r.get("V23")), "sh": fnum(r.get("V21")), "be": fnum(r.get("V22")), "sz": parse_size(r.get("V39")),
         "fd": r.get("V35") or "", "dv": (r.get("V42") or "").replace("-", "")}
    for name, col in (("m1", "V10"), ("m3", "V11"), ("m6", "V12"), ("y1", "V13"), ("y2", "V14"), ("y3", "V15"),
                      ("y5", "V16"), ("ytd", "V17"), ("inc", "V36")):
        f[name] = fnum(r.get(col))
    return f


# ---------- 分類 ----------
class Classifier:
    def __init__(self, cfg):
        self.themes = []
        for t in cfg["themes"]:
            self.themes.append((t, re.compile(t["any"], re.I), re.compile(t["not"], re.I) if t.get("not") else None))
        self.managed, self.fallback = cfg["managed"], cfg["fallback"]
        self.by_id = {t["id"]: t for t in cfg["themes"]}
        self.by_id[self.managed["id"]] = self.managed
        self.by_id[self.fallback["id"]] = self.fallback

    def theme(self, f):
        if f["ty"] == "D":  # 委託投資帳戶：名稱常帶「台灣人壽」之類，不能拿來判斷標的
            return self.managed
        text = STRIP_CO.sub("", f["n"]) + " " + f["cat"]
        for t, yes, no in self.themes:
            if yes.search(text) and not (no and no.search(text)):
                return t
        return self.fallback


# ---------- 挑選 ----------
def pct_rank(values):
    """回傳 {index: 0~1 的百分位}；None 排最後（0）。"""
    pairs = sorted((v, i) for i, v in enumerate(values) if v is not None)
    n = len(pairs)
    out = {i: 0.0 for i in range(len(values))}
    for r, (_, i) in enumerate(pairs):
        out[i] = r / (n - 1) if n > 1 else 1.0
    return out


def eligible(f, tier, today):
    if f["ty"] not in ("A", "B") and not (tier == 6 and f["ty"] == "E"):
        return False
    if BAD_NAME.search(f["raw"]):
        return False
    if tier != 6:
        if f["y1"] is None or f["sd"] is None:
            return False
        fd = parse_date(f["fd"])
        if fd is None or fd > today - timedelta(days=365 * 3):
            return False
    if f["sz"] is not None and f["sz"] < 3:
        return False
    return True


def one_per_base(funds):
    """同一檔基金有多個計價幣別／配息級別：每個 base 只留一個（優先台幣、再美元，同幣別取規模大的）。"""
    order = {"台幣": 0, "新臺幣": 0, "美元": 1}
    best = {}
    for f in funds:
        cur = best.get(f["b"])
        rank = (order.get(f["cc"], 5), -(f["sz"] or 0))
        if cur is None or rank < cur[0]:
            best[f["b"]] = (rank, f)
    return [v[1] for v in best.values()]


def score(cands, tier):
    sh, sd = pct_rank([f["sh"] for f in cands]), pct_rank([f["sd"] for f in cands])
    r3 = pct_rank([f["y3"] if f["y3"] is not None else f["y1"] for f in cands])
    r1 = pct_rank([f["y1"] for f in cands])
    sz = pct_rank([f["sz"] for f in cands])
    out = []
    for i, f in enumerate(cands):
        low_vol = 1 - sd[i] if f["sd"] is not None else 0.0
        if tier in (1, 2):
            s = 0.4 * sh[i] + 0.4 * r3[i] + 0.2 * low_vol
        elif tier == 6:
            s = 0.5 * low_vol + 0.3 * r1[i] + 0.2 * sz[i]
        else:
            s = 0.4 * sh[i] + 0.3 * r1[i] + 0.3 * low_vol
        out.append((s, f))
    out.sort(key=lambda x: -x[0])
    return out


def why(f, tier):
    bits = []
    if tier != 6:
        if f["y3"] is not None:
            bits.append("近 3 年 %s%%" % format(f["y3"], "+.1f"))
        elif f["y1"] is not None:
            bits.append("近 1 年 %s%%" % format(f["y1"], "+.1f"))
        if f["sh"] is not None:
            bits.append("夏普 %.2f" % f["sh"])
        if f["sd"] is not None:
            bits.append("年化波動 %.1f%%" % f["sd"])
    else:
        if f["y1"] is not None:
            bits.append("近 1 年 %s%%" % format(f["y1"], "+.1f"))
        if f["sd"] is not None:
            bits.append("年化波動 %.2f%%" % f["sd"])
    if f["sz"]:
        bits.append("規模約 %s 億" % format(f["sz"], ",.0f"))
    return "・".join(bits)


def pick_tier(funds, tier, cls, today, want=3):
    cands = one_per_base([f for f in funds if cls.theme(f)["tier"] == tier and eligible(f, tier, today)])
    ranked = score(cands, tier)
    picked, themes = [], set()
    for s, f in ranked:
        t = cls.theme(f)["id"]
        if t in themes:
            continue
        picked.append((s, f))
        themes.add(t)
        if len(picked) >= want:
            break
    for s, f in ranked:  # 不同主題不夠，再用最高分補滿
        if len(picked) >= min(want, len(ranked)):
            break
        if all(f is not p[1] for p in picked):
            picked.append((s, f))
    return [f for _, f in picked], len(cands)


# ---------- 輸出格式 ----------
def card(f, cls):
    t = cls.theme(f)
    keep = ("k", "n", "co", "cat", "cc", "rr", "nav", "dt", "d1", "ytd", "m1", "m3", "m6", "y1", "y3", "y5", "sd", "sh", "sz", "inv", "dv", "ty")
    d = {k: f[k] for k in keep if f.get(k) not in (None, "")}
    d.update(theme=t["id"], themeLabel=t["label"], tier=t["tier"], grp=t.get("grp", "dev"))
    return d


UCOLS = ["k", "n", "inv", "grp", "theme", "tier", "rr", "cc", "nav", "d1", "m1", "m3", "y1", "y3", "sd", "sh", "sz", "ty", "dv"]


def universe(funds, cls):
    rows = []
    for f in sorted(funds, key=lambda x: (cls.theme(x)["tier"], x["n"])):
        t = cls.theme(f)
        rows.append([f["k"], f["n"][:48], f["inv"], t.get("grp", "dev"), t["id"], t["tier"], f["rr"], f["cc"], f["nav"], f["d1"], f["m1"], f["m3"],
                     f["y1"], f["y3"], f["sd"], f["sh"], f["sz"], f["ty"], f["dv"]])
    return {"cols": UCOLS, "rows": rows}


def med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 2) if len(v) >= 3 else None


def theme_stats(all_funds, cls):
    """依主題統計（兩家合併、同一檔基金只算一次）。"""
    seen, rows = {}, []
    for f in all_funds:
        cur = seen.get(f["b"])
        if cur is None or (cur["y1"] is None and f["y1"] is not None):
            seen[f["b"]] = f
    by = {}
    for f in seen.values():
        by.setdefault(cls.theme(f)["id"], []).append(f)
    for t in list(cls.by_id.values()):
        fs = by.get(t["id"], [])
        if not fs:
            continue
        have = {c: len({f["b"] for f in all_funds if f["co"] == c and cls.theme(f)["id"] == t["id"]}) for c in CO}
        cos = {f["b"]: sorted({x["co"] for x in all_funds if x["b"] == f["b"]}) for f in fs}
        top = sorted([f for f in fs if f["y1"] is not None and f["ty"] in ("A", "B")], key=lambda f: -f["y1"])[:3]
        rows.append({"id": t["id"], "label": t["label"], "tier": t["tier"], "grp": t.get("grp", "dev"), "proxy": (t.get("proxy") or "").lower(),
                     "count": {"total": len(fs), **have}, "med": {"m1": med(f["m1"] for f in fs), "m3": med(f["m3"] for f in fs),
                                                                  "y1": med(f["y1"] for f in fs), "y3": med(f["y3"] for f in fs)},
                     "top": [{"k": f["k"], "n": f["n"][:36], "y1": f["y1"], "rr": f["rr"], "co": cos[f["b"]]} for f in top]})
    rows.sort(key=lambda r: (r["tier"], -r["count"]["total"]))
    return rows


def proxy_idx(daily, cfg_symbols, chart_days=260):
    """各主題的代理指數：最新漲跌、1／3 個月、1 年，加上走勢（供圖表用）。"""
    names = {s["id"]: s for s in cfg_symbols}
    series, tiles = {}, {}
    ids = sorted({s["id"] for s in cfg_symbols if s["group"].startswith("f_")})
    dates = sorted({d for i in ids for d, _ in (daily.get(i) or [])})[-chart_days:]
    for i in ids:
        rows = daily.get(i) or []
        if len(rows) < 22:
            continue
        m = dict(rows)
        cur = [v for d, v in rows if d < dates[0]]
        cur = cur[-1] if cur else None
        vals = []
        for d in dates:
            cur = m.get(d, cur)
            vals.append(None if cur is None else round(cur, 3))
        series[i] = vals
        last, prev = rows[-1][1], rows[-2][1]
        pc = lambda n: round((last / rows[-1 - n][1] - 1) * 100, 2) if len(rows) > n else None
        tiles[i] = {"id": i, "name": names[i]["name"], "ticker": names[i]["yahoo"][0], "date": rows[-1][0].isoformat(),
                    "chg": round((last / prev - 1) * 100, 2), "m1": pc(21), "m3": pc(63), "y1": pc(251)}
    return {"dates": [d.isoformat() for d in dates], "series": series, "tiles": tiles}


def group_stats(theme_cfg, themes, idx):
    """大分類：由各主題彙總（數量、報酬中位數）＋把該類代理 ETF 等權重組成的合成指數。"""
    out, series = [], {}
    by = {}
    for t in themes:
        by.setdefault(t["grp"], []).append(t)
    for g in theme_cfg["groups"]:
        ts = by.get(g["id"], [])
        if not ts:
            continue
        prox = sorted({t["proxy"] for t in ts if t["proxy"] in idx["series"]})
        comp = None
        if prox:
            n = len(idx["dates"])
            cols = []
            for pid in prox:
                v = idx["series"][pid]
                base = next((x for x in v if x is not None), None)
                if base:
                    cols.append([None if x is None else x / base * 100 for x in v])
            if cols:
                comp = []
                for i in range(n):
                    xs = [c[i] for c in cols if c[i] is not None]
                    comp.append(round(sum(xs) / len(xs), 3) if xs else None)
        tot = sum(t["count"]["total"] for t in ts)
        have = {c: sum(t["count"].get(c, 0) for t in ts) for c in CO}
        wm = lambda k: (round(sum((t["med"][k] or 0) * t["count"]["total"] for t in ts if t["med"][k] is not None) /
                              max(1, sum(t["count"]["total"] for t in ts if t["med"][k] is not None)), 2)
                        if any(t["med"][k] is not None for t in ts) else None)
        row = {"id": g["id"], "name": g["name"], "sub": g["sub"], "themes": [t["id"] for t in ts], "proxies": prox,
               "count": {"total": tot, **have}, "med": {k: wm(k) for k in ("m1", "m3", "y1", "y3")}, "idx": None}
        if comp:
            vals = [x for x in comp if x is not None]
            pc = lambda k: round((vals[-1] / vals[-1 - k] - 1) * 100, 2) if len(vals) > k else None
            row["idx"] = {"id": "g_" + g["id"], "name": g["name"] + "（" + "、".join(p.upper() for p in prox) + " 等權重）",
                          "chg": pc(1), "m1": pc(21), "m3": pc(63), "y1": pc(251)}
            series["g_" + g["id"]] = comp
        out.append(row)
    return out, series


# ---------- 變動追蹤 ----------
def diff_ids(prev, funds, today):
    """和上一次的清單比，列出新增／下架。prev＝{"ids": {key: 名稱}, "date": …} 或 None。"""
    now = {f["k"]: f["n"] for f in funds}
    if not prev or not prev.get("ids"):
        return {"ids": now, "date": today.isoformat()}, []
    old = prev["ids"]
    changes = [{"type": "新增", "k": k, "n": now[k], "date": today.isoformat()} for k in now if k not in old]
    changes += [{"type": "下架", "k": k, "n": old[k], "date": today.isoformat()} for k in old if k not in now]
    hist = [c for c in prev.get("changes", []) if c.get("date", "") >= (today - timedelta(days=60)).isoformat()]
    return {"ids": now, "date": today.isoformat(), "changes": (changes + hist)[:80]}, (changes + hist)[:40]


# ---------- 總組裝 ----------
def track_nav(c, navs, co, keep):
    """把這檔的最新淨值記進序列（每次更新累積一筆），並在卡片上附近 60 筆走勢。"""
    sid = "%s:%s" % (co, c["k"])
    d = parse_date(c.get("dt"))
    if d and c.get("nav"):
        navs[sid] = merge_bars(navs.get(sid), [(d, c["nav"])])
    rows = navs.get(sid) or []
    if len(rows) >= 2:
        c["hist"] = [round(v, 4) for _, v in rows[-60:]]
        c["hist_from"] = rows[0][0].isoformat()
    keep.add(sid)
    return c


def build(rows_by_co, theme_cfg, daily, cfg_symbols, prev_ids, watch, today, navs=None):
    """rows_by_co：{'tlife': 原始 rows 或 None, 'nanshan': … }（None＝這家這次沒抓到）。
    回傳 (docs, new_ids, notes)。docs 的鍵：funds、funds_tlife、funds_nanshan。"""
    cls = Classifier(theme_cfg)
    navs = {} if navs is None else navs
    keep = set()
    allf, comp, docs, new_ids, tiers = [], {}, {}, {}, theme_cfg["tiers"]
    for co, rows in rows_by_co.items():
        if rows is None:
            continue
        funds = [normalize(r, co) for r in rows if r.get("V40")]
        allf += funds
        picks, counts = {}, {}
        for tier in tiers:
            got, n = pick_tier(funds, tier["id"], cls, today)
            picks[str(tier["id"])] = [track_nav(dict(card(f, cls), why=why(f, tier["id"])), navs, co, keep) for f in got]
            counts[str(tier["id"])] = len([f for f in funds if cls.theme(f)["tier"] == tier["id"]])
        dts = [parse_date(f["dt"]) for f in funds if parse_date(f["dt"])]
        new_ids[co], changes = diff_ids((prev_ids or {}).get(co), funds, today)
        watched = []
        for w in (watch or {}).get("watch", []):
            hit = next((f for f in funds if f["k"] == w.get("k")), None)
            if w.get("co") == co and hit:
                watched.append(track_nav(dict(card(hit, cls), note=w.get("note", "")), navs, co, keep))
        gc, tc = {}, {}
        for f in funds:
            t = cls.theme(f)
            gc[t.get("grp", "dev")] = gc.get(t.get("grp", "dev"), 0) + 1
            tc[t["id"]] = tc.get(t["id"], 0) + 1
        comp[co] = {"gcounts": gc, "tcounts": tc, "name": {"tlife": "台灣人壽", "nanshan": "南山人壽"}[co], "count": len(funds),
                    "asof": max(dts).isoformat() if dts else None, "picks": picks, "counts": counts,
                    "changes": changes, "watch": watched}
        docs["funds_" + co] = universe(funds, cls)
    if not comp:
        raise SourceError("兩家基金清單都沒有資料")
    cutoff = today - timedelta(days=45)
    for sid in [k for k, rows in navs.items() if k not in keep and (not rows or rows[-1][0] < cutoff)]:
        del navs[sid]  # 不再被推薦、也不在追蹤清單的基金，45 天後清掉，避免檔案越長越大
    idx = proxy_idx(daily, cfg_symbols)
    themes = theme_stats(allf, cls)
    for t in themes:
        t["idx"] = idx["tiles"].get(t["proxy"])
    groups, gseries = group_stats(theme_cfg, themes, idx)
    docs["funds"] = {"updated": datetime.now(TZ).isoformat(timespec="seconds"), "tiers": tiers, "companies": comp, "themes": themes,
                     "groups": groups, "idx": {"dates": idx["dates"], "series": dict(idx["series"], **gseries)},
                     "note": "規則式整理：依官網公布的績效、風險、規模數字排序，不是投資建議，也不代表保險公司推薦。基金有風險，投資前請詳閱公開說明書。"}
    return docs, new_ids, []
