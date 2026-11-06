# -*- coding: utf-8 -*-
"""新增一筆手動事件到 config/events.json（會檢查格式、自動判斷分類與標籤、依日期排序）。

範例：
  python -m pipeline.add_event --date 2026-12-01 --event "央行房市信用管制檢討" --country 台灣 --importance M
  python -m pipeline.add_event --date 2026-12-05 --event "某電競賽事決賽" --cat esport --main   # 升級成主要事件
可用分類見 config/calendar.json 的 cats。沒指定 --cat 時依關鍵字自動判斷，結果會印出來給你確認。
"""
import argparse
import json
import re
import sys
from datetime import date

from . import events
from .common import ROOT


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD（台灣日期）")
    ap.add_argument("--time", default="", help="HH:MM（台北時間，可省略）")
    ap.add_argument("--event", required=True)
    ap.add_argument("--country", default="台灣")
    ap.add_argument("--importance", choices=("H", "M", "L"), default="M")
    ap.add_argument("--cat", help="分類 id；省略則自動判斷")
    ap.add_argument("--tags", default="", help="額外標籤，逗號分隔")
    ap.add_argument("--why", default="", help="一句話說明為什麼影響經濟")
    ap.add_argument("--main", action="store_true", help="非經濟事件也當主要事件")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    try:
        date.fromisoformat(a.date)
    except ValueError:
        sys.exit("日期格式要像 2026-12-01")
    if a.time and not re.match(r"^\d{2}:\d{2}$", a.time):
        sys.exit("時間格式要像 21:30")
    from pathlib import Path
    root = Path(a.root)
    ccfg = events.load_cfg(json.loads((root / "config" / "calendar.json").read_text(encoding="utf-8")))
    if a.cat and a.cat not in ccfg["_cat"]:
        sys.exit("沒有這個分類：%s。可用：%s" % (a.cat, "、".join(ccfg["_cat"])))
    path = root / "config" / "events.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    ev = {"date": a.date, "event": a.event, "country": a.country, "importance": a.importance}
    if a.time:
        ev["time"] = a.time
    if a.cat:
        ev["cat"] = a.cat
    if a.tags:
        ev["tags"] = [t.strip() for t in a.tags.split(",") if t.strip()]
    if a.why:
        ev["why"] = a.why
    if a.main:
        ev["main"] = True
    probe = events.tag_event(dict(ev), ccfg, "manual")
    if probe["scope"] == "other":
        ev["scope"] = "other"
    for old in doc["events"]:
        if old["date"] == ev["date"] and old["event"] == ev["event"]:
            sys.exit("已經有同一天同名稱的事件了")
    doc["events"].append(ev)
    doc["events"].sort(key=lambda e: (e["date"], e.get("time") or "99:99"))
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已新增：%s %s｜分類 %s｜標籤 %s｜%s" % (ev["date"], ev["event"], probe["catn"], "、".join(probe["tags"]) or "無",
          "經濟事件（預設顯示）" if probe["main"] else "經濟以外（勾選「顯示經濟以外」才看得到）"))


if __name__ == "__main__":
    main()
