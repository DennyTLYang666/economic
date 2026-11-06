# -*- coding: utf-8 -*-
"""新聞標題：用 Google News RSS 依關鍵字抓最新幾則（標題＋連結＋來源），點進去是原文網站。

只存標題與連結，不存內文。每個關鍵字獨立失敗；失敗時保留上次的標題。
"""
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

from .common import SourceError, TZ, http_get, url

PER_KEY = 6
MARKET_QUERIES = {"mkt_tw": "台股 盤勢 加權指數", "mkt_us": "美股 收盤 道瓊 那斯達克 費半"}


def feed_url(q):
    return "%s?q=%s&hl=zh-TW&gl=TW&ceid=TW:zh-Hant" % (url("NEWS"), urllib.parse.quote(q + " when:7d"))


def parse_feed(body):
    root = ET.fromstring(body)
    items, seen = [], set()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].rstrip()
        if not title or not link.startswith("http") or title in seen:
            continue
        seen.add(title)
        d = ""
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(TZ).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            pass
        items.append({"t": title, "u": link, "s": src, "d": d})
        if len(items) >= PER_KEY:
            break
    return items


def fetch_query(q):
    try:
        return parse_feed(http_get(feed_url(q), retries=1, timeout=20))
    except ET.ParseError as e:
        raise SourceError("新聞格式無法解析：%s" % e)


def wanted(cfg, themes, groups=()):
    """{key: 關鍵字}：每個標的、每個基金主題、加上兩個大盤。"""
    out = dict(MARKET_QUERIES)
    for s in cfg["symbols"]:
        if s.get("q"):
            out[s["id"]] = s["q"]
    for d in cfg.get("derived", []):
        if d.get("q"):
            out[d["id"]] = d["q"]
    for g in groups:
        out["g:" + g["id"]] = g["name"].replace("｜", " ") + " 基金 市場"
    for t in themes:
        out["t:" + t["id"]] = t["news"]
    return out


def refresh(cfg, themes, old, pause=0.2, groups=(), on_item=None):
    """回傳 (新聞文件, 錯誤清單)。old＝上次的文件（可為 None）。全部失敗才丟 SourceError。"""
    q = wanted(cfg, themes, groups)
    prev = (old or {}).get("items", {})
    items, errors, ok = {}, [], 0
    for n, (key, query) in enumerate(q.items(), 1):
        if on_item:
            on_item(n, key)
        try:
            got = fetch_query(query)
            items[key] = got
            ok += 1
        except (SourceError, ValueError) as e:
            errors.append("新聞 %s：%s" % (key, e))
            if prev.get(key):
                items[key] = prev[key]
        time.sleep(pause)
        if not ok and len(errors) >= 6:
            break  # 一開始就連續失敗，多半是來源擋住，不要再硬試
    if not ok:
        raise SourceError("新聞來源全部失敗（%s）" % (errors[0] if errors else "無資料"))
    return {"updated": datetime.now(TZ).isoformat(timespec="seconds"), "q": q, "items": items}, errors
