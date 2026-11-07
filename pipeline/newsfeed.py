# -*- coding: utf-8 -*-
"""「新聞」頁的資料。

流程：依主題抓多組 Google 新聞搜尋詞（另可加媒體 RSS）→ 清理標題 → 同事件合併（多家報導只留一則，其餘放「另有報導」）
→ 依關鍵字貼標籤 → 規則式判斷「重點」→ 與上次合併、只保留 retention_days 天。
選用：有 ANTHROPIC_API_KEY 時，用 Claude 為重點新聞寫一句精簡摘要（只依標題，不臆測內文）。
只存標題、摘要與連結，不存內文。每個來源獨立失敗。
"""
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from .common import SourceError, TZ, UA, http_get, url

OVERLAP = 0.42       # 兩則標題的二連詞重疊比例（交集÷較短者）達此值，且共用 ≥ MIN_SHARED 個，視為同一事件
MIN_SHARED = 3
WINDOW = timedelta(hours=48)
ALSO_MAX = 3         # 每則最多保留幾則「另有報導」
SUM_MAX = 70


def load_cfg(path):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("retention_days", 30)
    cfg.setdefault("max_items", 700)
    cfg.setdefault("per_query", 12)
    return cfg


def total_requests(cfg):
    return sum(len(t["queries"]) for t in cfg["topics"]) + len(cfg.get("feeds", []))


# ---------- 文字處理 ----------
def norm(t):
    t = re.sub(r"[\s\W_]+", "", (t or "").lower())
    return t


def bigrams(n):
    return {n[i:i + 2] for i in range(len(n) - 1)} or {n}


def sim(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def nums(t):
    return set(re.findall(r"\d+(?:\.\d+)?", t or ""))


def same_event(b1, t1, b2, t2):
    """標題是否在講同一件事：二連詞重疊夠多；若兩邊都有數字（如「上漲50點」vs「下跌30點」），數字要有交集。"""
    shared = len(b1 & b2)
    if shared < MIN_SHARED or shared / float(min(len(b1), len(b2))) < OVERLAP:
        return False
    n1, n2 = nums(t1), nums(t2)
    return not (n1 and n2 and not (n1 & n2))


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def clean_title(t, src=""):
    t = html.unescape((t or "").strip())
    if src and t.endswith(" - " + src):
        t = t[: -len(src) - 3].rstrip()
    t = re.sub(r"\s+[-｜|]\s+[^-｜|]{1,16}$", "", t) if not src else t
    return t


def trim(s, n=SUM_MAX):
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    m = max(cut.rfind("。"), cut.rfind("；"), cut.rfind("！"), cut.rfind("？"))
    return cut[: m + 1] if m >= n // 2 else cut.rstrip("，、 ") + "…"


def good_summary(desc, title):
    d = strip_html(desc)
    if len(d) < 20:
        return ""
    if sim(bigrams(norm(d[: len(title) + 10])), bigrams(norm(title))) > 0.6 or norm(d).startswith(norm(title)[:12]):
        return ""  # 摘要只是在重複標題（Google 新聞的 description 常是這樣）
    return trim(d)


# ---------- 抓取 ----------
def feed_url(q, days):
    return "%s?q=%s&hl=zh-TW&gl=TW&ceid=TW:zh-Hant" % (url("NEWS"), urllib.parse.quote("%s when:%dd" % (q, days)))


def parse_feed(body, per=12, default_src=""):
    root = ET.fromstring(body)
    out, seen = [], set()
    for it in root.iter("item"):
        src = (it.findtext("source") or default_src or "").strip()
        title = clean_title(it.findtext("title"), src)
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("http") or norm(title) in seen:
            continue
        seen.add(norm(title))
        d = ""
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(TZ).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            pass
        out.append({"t": title, "u": link, "s": src, "d": d, "sum": good_summary(it.findtext("description"), title)})
        if len(out) >= per:
            break
    return out


def collect(cfg, days, pause=0.2, on_item=None):
    """回傳 ([(原始新聞, 搜尋主題)], 錯誤清單)。"""
    raw, errors, ok, n = [], [], 0, 0
    jobs = [(t["id"], q, feed_url(q, days), "") for t in cfg["topics"] for q in t["queries"]]
    jobs += [((f.get("topics") or [""])[0], f["url"], f["url"], f.get("name", "")) for f in cfg.get("feeds", [])]
    for topic, label, u, name in jobs:
        n += 1
        if on_item:
            on_item(n, label[:24])
        try:
            for it in parse_feed(http_get(u, retries=1, timeout=20), cfg["per_query"], name):
                raw.append((it, topic))
            ok += 1
        except (SourceError, ET.ParseError) as e:
            errors.append("新聞彙整 %s：%s" % (label[:20], e))
        time.sleep(pause)
        if not ok and len(errors) >= 6:
            break
    if not ok:
        raise SourceError("新聞來源全部失敗（%s）" % (errors[0] if errors else "無資料"))
    return raw, errors


# ---------- 標籤與重點 ----------
def tag(title, summary, base, cfg):
    text = (title + " " + (summary or "")).lower()
    hits = []
    for t in cfg["topics"]:
        n = sum(1 for k in t["keywords"] if k.lower() in text)
        if n:
            hits.append((t["id"], n))
    hits.sort(key=lambda x: (x[0] != base, -x[1]))
    ids = [h[0] for h in hits]
    if base and base not in ids:
        ids.append(base)
    order = [t["id"] for t in cfg["topics"]]
    return sorted(ids[:3], key=order.index)


def word_score(text, hot):
    return min(4.0, sum(w for k, w in hot["words"].items() if k in text))


def score(c, cfg):
    hot = cfg["hot"]
    srcs = {c.get("s", "")} | {a.get("s", "") for a in c.get("also", [])}
    base = hot["sources"].get(c.get("s", ""), 0)
    return round(base + min(len(srcs) - 1, 5) * 1.0 + word_score(c["t"] + " " + (c.get("sum") or ""), hot), 1)


def mark_hot(items, cfg):
    hot = cfg["hot"]
    for c in items:
        c["imp"] = score(c, cfg)
        c["hot"] = False
    per, bucket = hot.get("per_topic_day", 2), {}
    for c in sorted(items, key=lambda x: (-x["imp"], x.get("d", ""))):
        if c["imp"] < hot["threshold"]:
            continue
        k = ((c.get("d") or "")[:10], (c.get("tg") or [""])[0])
        bucket[k] = bucket.get(k, 0) + 1
        c["hot"] = bucket[k] <= per


# ---------- 合併 ----------
def _also(c):
    return {"s": c.get("s", ""), "t": c["t"][:60], "u": c["u"]}


def merge(old_items, raw, now, cfg):
    """old_items：上次存的（每則＝一個事件）。raw：這次抓到的 [(item, topic)]。回傳新的事件清單。"""
    weights = cfg["hot"]["sources"]
    keep_from = now - timedelta(days=cfg["retention_days"])
    clusters = []
    for o in old_items:
        o = dict(o)
        o["_m"] = [(bigrams(norm(x)), x) for x in [o["t"]] + [a["t"] for a in o.get("also", [])]]
        o["_t"] = _dt(o.get("d")) or now
        clusters.append(o)
    seen_u = {c["u"] for c in clusters} | {a["u"] for c in clusters for a in c.get("also", [])}
    for it, topic in sorted(raw, key=lambda x: x[0].get("d") or ""):
        if it["u"] in seen_u:
            continue
        seen_u.add(it["u"])
        t = _dt(it["d"]) or now
        if t < keep_from:
            continue
        b = bigrams(norm(it["t"]))
        hit = None
        for c in clusters:
            if abs(c["_t"] - t) <= WINDOW and any(same_event(b, it["t"], mb, mt) for mb, mt in c["_m"]):
                hit = c
                break
        if hit is None:
            c = {"id": hashlib.sha1(norm(it["t"]).encode("utf-8")).hexdigest()[:10], "t": it["t"], "u": it["u"], "s": it["s"], "d": it["d"] or now.isoformat(timespec="minutes"),
                 "sum": it.get("sum", ""), "tg": tag(it["t"], it.get("sum", ""), topic, cfg), "also": [], "_m": [(b, it["t"])], "_t": t}
            clusters.append(c)
            continue
        hit["_m"].append((b, it["t"]))
        if it["s"] and it["s"] != hit.get("s") and it["s"] not in {a.get("s") for a in hit.get("also", [])}:
            hit.setdefault("also", []).append(_also(it))
        if not hit.get("sum") and it.get("sum"):
            hit["sum"] = it["sum"]
        merged_tags = set(hit.get("tg", [])) | set(tag(it["t"], it.get("sum", ""), topic, cfg))
        order = [x["id"] for x in cfg["topics"]]
        hit["tg"] = sorted(merged_tags, key=order.index)[:3]
        # 代表來源：來源權重較高者；同權重保留較早的
        if weights.get(it["s"], 0) > weights.get(hit.get("s", ""), 0):
            swap = _also(hit)
            hit["t"], hit["u"], hit["s"] = it["t"], it["u"], it["s"]
            hit["also"] = [a for a in hit["also"] if a["u"] != it["u"]] + [swap]
    out = []
    for c in clusters:
        if c["_t"] < keep_from:
            continue
        c["also"] = c.get("also", [])[:ALSO_MAX]
        c["n"] = 1 + len({a["s"] for a in c["also"] if a["s"] and a["s"] != c.get("s")})  # n＝幾家媒體報導
        d = {k: v for k, v in c.items() if not k.startswith("_") and v not in ("", None)}
        if not d.get("also"):
            d.pop("also", None)
        out.append(d)
    out.sort(key=lambda x: x.get("d", ""), reverse=True)
    out = out[: cfg["max_items"]]
    mark_hot(out, cfg)
    return out


def _dt(s):
    try:
        return datetime.fromisoformat(s).astimezone(TZ) if s else None
    except ValueError:
        return None


# ---------- 選用：Claude 摘要 ----------
def ai_enrich(items, cfg, key, now):
    """為「重點」且還沒有摘要的新聞補一句精簡摘要。失敗拋 SourceError。回傳補了幾則。"""
    ai = cfg.get("ai", {})
    cand = [c for c in items if not c.get("sum") and not c.get("ai") and (c.get("hot") or c.get("n", 1) >= 2)]
    cand.sort(key=lambda c: c.get("d", ""), reverse=True)
    cand.sort(key=lambda c: -c.get("imp", 0))  # 穩定排序：重要度高的在前，同分取較新的
    cand = cand[: ai.get("max_items_per_run", 40)]
    if not cand:
        return 0
    names = {t["id"]: t["name"] for t in cfg["topics"]}
    lines = []
    for i, c in enumerate(cand):
        others = "；".join(a["t"] for a in c.get("also", []))
        lines.append("%d. [%s] %s（%s）%s" % (i, "/".join(names.get(x, x) for x in c.get("tg", [])), c["t"], c.get("s", ""), ("　其他媒體標題：" + others) if others else ""))
    chars = ai.get("summary_chars", 60)
    prompt = ("你是財經新聞編輯，讀者是台灣的保險業務員，每天快速掌握外匯、投資、保險、銀行商品、不動產與賽事動態。\n"
              "下面每則新聞只有標題（有時附其他媒體對同一事件的標題）。請為每則寫一句繁體中文摘要，最多 %d 字，只寫重點事實與數字，"
              "不要重複標題的字句，**只能根據提供的標題，不可臆測標題沒有的資訊**；標題已足夠清楚時就用更短的白話濃縮。\n"
              "只輸出 JSON 陣列，格式：[{\"i\":0,\"s\":\"摘要\"}]，不要其他文字。\n\n%s") % (chars, "\n".join(lines))
    body = json.dumps({"model": os.environ.get("ECO_AI_MODEL") or ai.get("model", "claude-haiku-5-5"), "max_tokens": 4000,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(url("AI"), data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        arr = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
    except urllib.error.HTTPError as e:
        raise SourceError("AI 摘要 HTTP %s（金鑰或模型名稱有誤？）" % e.code)
    except (urllib.error.URLError, OSError, ValueError, AttributeError, KeyError) as e:
        raise SourceError("AI 摘要失敗：%s" % str(e)[:80])
    n = 0
    for o in arr:
        try:
            c, s = cand[int(o["i"])], trim(str(o["s"]).strip(), chars + 10)
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if s:
            c["sum"], c["ai"] = s, 1
            n += 1
    return n


# ---------- 對外 ----------
def refresh(cfg, old_doc, now, pause=0.2, on_item=None, api_key=None):
    """回傳 (文件, 錯誤清單, AI 補摘要數)。old_doc 可為 None。全部來源失敗才丟 SourceError。"""
    old = [x for x in (old_doc or {}).get("items", [])]
    days = 2 if old else 7
    raw, errors = collect(cfg, days, pause, on_item)
    items = merge(old, raw, now, cfg)
    n_ai = 0
    if api_key:
        try:
            n_ai = ai_enrich(items, cfg, api_key, now)
        except SourceError as e:
            errors.append(str(e))
    doc = {"updated": now.isoformat(timespec="seconds"), "retention_days": cfg["retention_days"],
           "topics": [{"id": t["id"], "name": t["name"]} for t in cfg["topics"]], "ai": bool(api_key), "items": items}
    return doc, errors, n_ai
