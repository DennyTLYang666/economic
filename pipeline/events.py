# -*- coding: utf-8 -*-
"""行事曆事件的分類（分類＋標籤＋是否主要事件）與 ICS 訂閱。

流程：自動抓到的經濟數據、config/events.json 手動事件、config/calendar.json 的 ICS 訂閱，
統一在這裡補上 cat／tags／scope／main 後再合併。
scope＝econ 的預設顯示；scope＝other（電競、體育、娛樂…）預設隱藏，除非 main＝true（升級成主要事件）。
"""
import re
from datetime import date, datetime, timedelta, timezone

from .common import SourceError, TZ, http_get

DEFAULT_CFG = {"default_auto": "growth", "default_manual": "misc", "cats": [], "rules": [], "feeds": []}


def load_cfg(raw):
    cfg = dict(DEFAULT_CFG)
    cfg.update(raw or {})
    cfg["_cat"] = {c["id"]: c for c in cfg["cats"]}
    pats = []
    for r in cfg["rules"]:
        parts = []
        for kw in r.get("kw", []):
            k = kw.lower()
            # 英文字詞比對字首（employment 不會誤中 unemployment 裡面），中文直接比對
            parts.append((r"\b" + re.escape(k)) if k.isascii() else re.escape(k))
        if parts:
            pats.append((r["cat"], list(r.get("tags", [])), re.compile("|".join(parts))))
    cfg["_rules"] = pats
    return cfg


def classify(text, cfg):
    """回傳 (cat, tags)；沒有規則符合時回傳 (None, [])。"""
    t = (text or "").lower()
    for cat, tags, pat in cfg["_rules"]:
        if pat.search(t):
            return cat, list(tags)
    return None, []


def tag_event(e, cfg, source):
    """補上 cat、catn（分類名稱）、tags、scope、main。事件自己寫的欄位優先。"""
    cat, tags = e.get("cat"), list(e.get("tags") or [])
    if not cat or cat not in cfg["_cat"]:
        c2, t2 = classify((e.get("title") or "") + " " + (e.get("event") or "") + " " + (e.get("note") or ""), cfg)
        cat = c2 or (cfg["default_auto"] if source == "auto" else cfg["default_manual"])
        if not e.get("tags"):
            tags = t2
    meta = cfg["_cat"].get(cat) or {"label": "其他經濟", "scope": "econ"}
    scope = e.get("scope") if e.get("scope") in ("econ", "other") else meta["scope"]
    label = meta["label"]
    seen, tg = {label}, []
    for x in tags:
        if x and x not in seen:
            seen.add(x)
            tg.append(x)
    e["cat"], e["catn"], e["tags"], e["scope"] = cat, label, tg[:3], scope
    # 非經濟事件只有被標成主要事件（main）、或重要度 H 才會預設顯示
    e["main"] = bool(e.get("main")) or scope == "econ" or e.get("importance") == "H"
    return e


# ---------- ICS 訂閱 ----------
def _unfold(text):
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unesc(s):
    return s.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _dt(value, params):
    """ICS 時間 → (台北日期字串, 台北時間字串或空字串)。"""
    v = value.strip()
    if len(v) == 8 and v.isdigit():
        return "%s-%s-%s" % (v[:4], v[4:6], v[6:8]), ""
    m = re.match(r"^(\d{8})T(\d{6})(Z?)$", v)
    if not m:
        return None, None
    d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3):
        d = d.replace(tzinfo=timezone.utc)
    else:
        tz = None
        tzid = (params.get("TZID") or "").strip('"')
        if tzid:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tzid)
            except Exception:
                tz = None
        d = d.replace(tzinfo=tz or TZ)
    d = d.astimezone(TZ)
    return d.strftime("%Y-%m-%d"), d.strftime("%H:%M")


def parse_ics(text):
    """極簡 ICS 解析：只取 VEVENT 的日期時間、標題、說明；重複規則（RRULE）與取消的事件略過。"""
    evs, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur and cur.get("date") and cur.get("event") and not cur.get("skip"):
                evs.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, _, val = line.partition(":")
            name, *ps = head.split(";")
            params = dict(p.split("=", 1) for p in ps if "=" in p)
            name = name.upper()
            if name == "DTSTART":
                cur["date"], cur["time"] = _dt(val, params)
            elif name == "SUMMARY":
                cur["event"] = _unesc(val)
            elif name == "DESCRIPTION":
                cur["note"] = _unesc(val)[:80]
            elif name == "LOCATION":
                cur["place"] = _unesc(val)
            elif name == "RRULE" or (name == "STATUS" and val.strip().upper() == "CANCELLED"):
                cur["skip"] = True
    return evs


def fetch_feeds(feeds, today, fetch=http_get):
    """回傳 (events, warnings)。單一訂閱失敗只記警告。只取今天前 1 天到後 90 天內的事件。"""
    out, warns = [], []
    lo, hi = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=90)).isoformat()
    for f in feeds or []:
        u, name = f.get("url"), f.get("name") or f.get("url")
        if not u:
            continue
        try:
            evs = parse_ics(fetch(u))
        except (SourceError, OSError, ValueError) as ex:
            warns.append("訂閱「%s」失敗：%s" % (name, str(ex)[:120]))
            continue
        pat = re.compile(f["match"], re.I) if f.get("match") else None
        n = 0
        for e in evs:
            if not (lo <= e["date"] <= hi) or (pat and not pat.search(e["event"])):
                continue
            ev = {"date": e["date"], "time": e.get("time") or "", "event": e["event"], "country": f.get("country", ""),
                  "importance": f.get("importance", "L"), "note": e.get("note", "") or e.get("place", ""),
                  "cat": f.get("cat"), "tags": f.get("tags"), "scope": f.get("scope"), "main": f.get("main", False), "source": "feed:" + name}
            out.append(ev)
            n += 1
            if n >= 150:
                break
    return out, warns
