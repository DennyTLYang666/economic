# -*- coding: utf-8 -*-
"""更新入口：抓資料 → 併入序列 → 算出網站 JSON。

  python -m pipeline.run --mode snapshot   # 盤中快照：報價、日曆、2 年期殖利率（快）；新聞超過 3 小時才更新
  python -m pipeline.run --mode daily      # 每日更新：再加 FRED 總經、新聞、南山／台壽基金
  python -m pipeline.run --offline         # 不連網，只用現有序列重算（本機改版面、跑測試用）

結束碼：0＝全部成功；2＝網站已更新但部分來源失敗；1＝失敗（網站資料維持原樣）。
新聞與基金是「加值資料」：失敗只會標示、保留上一版，不會讓整體失敗。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import build, events, fetch, funds, news, newsfeed
from .common import ROOT, TZ, SourceError, atomic_write, load_series, merge_bars, save_series, write_pair

MIN_OK_RATIO = 0.5  # 報價成功的標的低於這個比例，視為整體失敗，不更新網站
NEWS_MAX_AGE = timedelta(hours=3)


def log(msg):
    print(msg, flush=True)


class Progress:
    """處理進度：每個大階段一行「[n/總數] 名稱」，階段內逐項顯示「第 k/總數 項（百分比）」。
    在命令列視窗用同一行刷新；導向檔案（例如 GitHub Actions 記錄）時只印階段和每 25% 一行。"""

    def __init__(self, stages):
        self.stages, self.i, self.total, self.name, self.t0, self.shown = stages, 0, 0, "", time.time(), -1
        self.tty = sys.stdout.isatty()

    def stage(self, name, total=0):
        self.done()
        self.i += 1
        self.total, self.name, self.t0, self.shown = total, name, time.time(), -1
        self._line("[%d/%d] %s%s" % (self.i, self.stages, name, "（共 %d 項）" % total if total else ""), end="\n")

    def tick(self, k, label=""):
        if not self.total:
            return
        pct = int(k * 100 / self.total)
        if self.tty:
            self._line("      %3d%%  %d/%d  %s" % (pct, k, self.total, label[:40]), end="\r")
        elif pct // 25 > self.shown // 25 or k == self.total:
            self._line("      %3d%%  %d/%d" % (pct, k, self.total), end="\n")
        self.shown = pct

    def done(self):
        if self.name and self.tty and self.total:
            print(" " * 70, end="\r", flush=True)

    def _line(self, text, end):
        print(text, end=end, flush=True)

    def note(self, text):
        self.done()
        print("      " + text, flush=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stale(doc, max_age, now):
    try:
        return now - datetime.fromisoformat(doc["updated"]) > max_age
    except (TypeError, KeyError, ValueError):
        return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("snapshot", "daily"), default="snapshot")
    ap.add_argument("--root", default=str(ROOT), help="專案根目錄")
    ap.add_argument("--offline", action="store_true", help="不連網，只重算")
    ap.add_argument("--pause", type=float, default=0.25, help="每次請求之間的間隔秒數")
    ap.add_argument("--news", choices=("auto", "yes", "no"), default="auto")
    ap.add_argument("--funds", choices=("auto", "yes", "no"), default="auto")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    root = Path(args.root)
    cfg = json.loads((root / "config" / "symbols.json").read_text(encoding="utf-8"))
    theme_cfg = json.loads((root / "config" / "fund_themes.json").read_text(encoding="utf-8"))
    manual = (read_json(root / "config" / "events.json") or {}).get("events", [])
    ccfg = events.load_cfg(read_json(root / "config" / "calendar.json"))
    intl = read_json(root / "config" / "macro_intl.json") or {}
    t_start = time.time()
    watch = read_json(root / "config" / "fund_watch.json") or {}
    sdir, out = root / "data" / "series", root / "data" / "site"
    daily, fred = load_series(sdir / "daily.csv", "close"), load_series(sdir / "fred.csv", "value")
    navs = load_series(sdir / "fund_nav.csv", "nav")
    now = datetime.now(TZ)
    today = now.date()
    # 先算出這次會做哪些階段，進度才能顯示「第 n／共 m 步」
    old_f, old_n = read_json(out / "funds.json"), read_json(out / "news.json")
    plan_funds = not args.offline and args.funds != "no" and (args.funds == "yes" or args.mode == "daily" or not old_f)
    plan_news = not args.offline and args.news != "no" and (args.news == "yes" or args.mode == "daily" or not old_n or stale(old_n, NEWS_MAX_AGE, now))
    full = not args.offline and (args.mode == "daily" or not fred)
    prog = Progress(2 + (0 if args.offline else 3) + (1 if full else 0) + int(plan_funds) + 2 * int(plan_news))
    status, problems = {}, []
    src = {"yahoo": {"ok": 0, "fail": 0, "errors": []}, "fred": {"ok": 0, "fail": 0, "errors": []},
           "calendar": {"ok": False, "error": None, "note": None}, "news": {"ok": None, "errors": 0},
           "funds": {"ok": None, "errors": []}}

    auto_events = read_json(sdir / "calendar_cache.json") or []
    if not isinstance(auto_events, list):
        auto_events = []

    quotes = [s for s in cfg["symbols"] if s.get("yahoo")]
    from_fred = {s["id"]: s["from_fred"] for s in cfg["symbols"] if s.get("from_fred")}

    if not args.offline:
        prog.stage("報價（Yahoo）", len(quotes))
        for k, s in enumerate(quotes, 1):
            prog.tick(k, s["id"])
            try:
                bars, info, used = fetch.fetch_symbol(s, daily.get(s["id"], []), today)
                daily[s["id"]] = merge_bars(daily.get(s["id"]), bars)
                status[s["id"]] = dict(info, ok=True, symbol=used)
                src["yahoo"]["ok"] += 1
            except SourceError as e:
                status[s["id"]] = {"ok": False, "error": str(e)}
                src["yahoo"]["fail"] += 1
                src["yahoo"]["errors"].append("%s：%s" % (s["id"], e))
            time.sleep(args.pause)
            if src["yahoo"]["fail"] > len(quotes) * (1 - MIN_OK_RATIO):
                break  # 失敗已過半，不必再逐一重試
        if src["yahoo"]["ok"] < len(quotes) * MIN_OK_RATIO:
            log("[失敗] 報價只成功 %d／%d 項，視為整體失敗，網站資料維持原樣。" % (src["yahoo"]["ok"], len(quotes)))
            for m in src["yahoo"]["errors"][:8]:
                log("  - " + m)
            return 1

        everything = args.mode == "daily" or not fred
        todo = [f for f in cfg["fred"] if everything or f["id"] in from_fred.values()]  # 盤中快照只更新「當成報價用」的序列
        prog.stage("總經（FRED）", len(todo))
        for k, f in enumerate(todo, 1):
            prog.tick(k, f["id"])
            try:
                start = (today - timedelta(days=760 if (everything or not fred.get(f["id"])) else 45)).isoformat()
                fred[f["id"]] = merge_bars(fred.get(f["id"]), fetch.fred_series(f["id"], start))
                src["fred"]["ok"] += 1
            except SourceError as e:
                src["fred"]["fail"] += 1
                src["fred"]["errors"].append("%s：%s" % (f["id"], e))
            time.sleep(args.pause)
        if everything:
            prog.stage("台灣總經（國發會）")
            ndc_cols = {i["id"]: i["col"] for i in intl.get("indicators", []) if i.get("src") == "ndc"}
            if ndc_cols:
                try:
                    for iid, pts in fetch.fetch_ndc(ndc_cols, intl.get("ndc_csv")).items():
                        fred["NDC_" + iid] = merge_bars(fred.get("NDC_" + iid), pts)
                    src["fred"]["ok"] += 1
                except Exception as e:  # 台灣總經是加分項，任何錯誤都不能拖垮整個更新
                    src["fred"]["fail"] += 1
                    src["fred"]["errors"].append("國發會景氣指標：%s（台灣總經沿用上次資料）" % str(e)[:140])
        prog.stage("經濟日曆")
        try:
            auto_events, note = fetch.fetch_calendar()
            src["calendar"]["ok"] = True
            src["calendar"]["note"] = note  # 下週資料常常還沒發布，只記錄、不算失敗
            atomic_write(sdir / "calendar_cache.json", json.dumps(auto_events, ensure_ascii=False) + "\n")
        except (SourceError, ValueError, OSError) as e:
            src["calendar"]["error"] = str(e)[:200]
    else:
        src["calendar"]["ok"] = bool(auto_events)

    # 沒有 Yahoo 報價、改用 FRED 序列的標的（例如 2 年期殖利率）
    for sid, fid in from_fred.items():
        if fred.get(fid):
            daily[sid] = merge_bars(daily.get(sid), fred[fid])
            status.setdefault(sid, {"ok": True, "symbol": "FRED:" + fid})

    feed_events, feed_cache = [], sdir / "feeds_cache.json"
    if ccfg["feeds"] and not args.offline:
        feed_events, fwarn = events.fetch_feeds(ccfg["feeds"], today)
        if fwarn and not feed_events:  # 全部訂閱都失敗：沿用上一次成功的內容
            feed_events = read_json(feed_cache) or []
        elif feed_events:
            atomic_write(feed_cache, json.dumps(feed_events, ensure_ascii=False) + "\n")
        problems += ["日曆" + w for w in fwarn]
    elif ccfg["feeds"]:
        feed_events = read_json(feed_cache) or []
    prog.stage("整理成網站資料")
    docs = build.build_all(cfg, daily, fred, auto_events, manual, status, today, ccfg, feed_events, intl)

    # ---------- 基金（加值資料：失敗就保留上一版） ----------
    ids_path = sdir / "fund_ids.json"
    prev_ids = read_json(ids_path) or {}
    old_funds = read_json(out / "funds.json")
    want_funds = not args.offline and args.funds != "no" and (args.funds == "yes" or args.mode == "daily" or not old_funds)
    if want_funds:
        prog.stage("基金（台壽、南山）", 2)
        raw = {}
        for k, (co, fn) in enumerate((("tlife", funds.fetch_tlife), ("nanshan", funds.fetch_nanshan)), 1):
            prog.tick(k - 1, funds.CO[co])
            try:
                raw[co] = fn()
            except (SourceError, ValueError, OSError) as e:
                raw[co] = None
                src["funds"]["errors"].append("基金（%s）：%s" % (funds.CO[co], str(e)[:160]))
        if None in raw.values() and old_funds:
            src["funds"]["ok"] = False  # 只成功一家：統計會失真，整組保留上一版
        else:
            try:
                fdocs, new_ids, _ = funds.build(raw, theme_cfg, daily, cfg["symbols"], prev_ids, watch, today, navs)
                docs.update(fdocs)
                prev_ids.update(new_ids)
                atomic_write(ids_path, json.dumps(prev_ids, ensure_ascii=False, separators=(",", ":")) + "\n")
                src["funds"]["ok"] = True
            except (SourceError, ValueError, KeyError) as e:
                src["funds"]["ok"] = False
                src["funds"]["errors"].append("基金整理失敗：%s" % str(e)[:160])
        problems += src["funds"]["errors"]
    elif old_funds:  # 這次不更新基金，但代理指數走勢要跟著報價更新太貴；沿用上一版即可
        src["funds"]["ok"] = None

    # ---------- 新聞（加值資料） ----------
    old_news = read_json(out / "news.json")
    themes_for_news = [t for t in theme_cfg["themes"]]
    want_news = not args.offline and args.news != "no" and (args.news == "yes" or args.mode == "daily" or not old_news
                                                              or stale(old_news, NEWS_MAX_AGE, now))
    if want_news:
        prog.stage("新聞（Google News）", len(news.wanted(cfg, themes_for_news, theme_cfg.get("groups", []))))
        try:
            ndoc, nerr = news.refresh(cfg, themes_for_news, old_news, pause=min(args.pause, 0.3), groups=theme_cfg.get("groups", []),
                                      on_item=lambda k, key: prog.tick(k, key))
            docs["news"] = ndoc
            src["news"] = {"ok": True, "errors": len(nerr)}
        except SourceError as e:
            src["news"] = {"ok": False, "errors": 1}
            log("[提醒] 新聞更新失敗，沿用上一版：%s" % str(e)[:120])

    # ---------- 「新聞」頁（主題彙整，保存 30 天）；和上面的標的新聞一起更新 ----------
    if want_news:
        old_nf = read_json(out / "newsfeed.json")
        ncfg = newsfeed.load_cfg(root / "config" / "news_topics.json")
        prog.stage("新聞彙整（主題標籤）", newsfeed.total_requests(ncfg))
        try:
            nfdoc, nferr, n_ai = newsfeed.refresh(ncfg, old_nf, now, pause=min(args.pause, 0.3), on_item=lambda k, key: prog.tick(k, key),
                                                  api_key=os.environ.get("ANTHROPIC_API_KEY") or None)
            docs["newsfeed"] = nfdoc
            src["newsfeed"] = {"ok": True, "errors": len(nferr), "items": len(nfdoc["items"]), "ai": n_ai if nfdoc["ai"] else None}
            problems += nferr[:3]
        except SourceError as e:
            src["newsfeed"] = {"ok": False, "errors": 1}
            log("[提醒] 新聞彙整失敗，沿用上一版：%s" % str(e)[:120])

    prog.stage("寫入檔案")
    for name, obj in docs.items():
        write_pair(out / (name + ".json"), obj, "data/site/%s.json" % name)
    for k in ("yahoo", "fred"):
        problems += src[k]["errors"]
    if src["calendar"]["error"]:
        problems.append("日曆：" + src["calendar"]["error"])
    ends = {i: rows[-1][0].isoformat() for i, rows in daily.items() if rows}
    meta = {"schema": 2, "generated_at": now.isoformat(timespec="seconds"), "mode": "offline" if args.offline else args.mode,
            "sources": src, "problems": problems[:20], "latest": {"twii": ends.get("twii"), "spx": ends.get("spx")}}
    write_pair(out / "meta.json", meta, "data/site/meta.json")
    save_series(sdir / "daily.csv", daily, "close")
    save_series(sdir / "fred.csv", fred, "value")
    save_series(sdir / "fund_nav.csv", navs, "nav")

    n_t = sum(len(g["tiles"]) for g in docs["overview"]["groups"])
    extra = ""
    if "funds" in docs:
        extra += "；基金 台壽 %d・南山 %d 檔" % tuple(docs["funds"]["companies"].get(c, {}).get("count", 0) for c in ("tlife", "nanshan"))
    if "news" in docs:
        extra += "；新聞 %d 組" % len(docs["news"]["items"])
    if "newsfeed" in docs:
        extra += "；新聞頁 %d 則（重點 %d）" % (len(docs["newsfeed"]["items"]), sum(1 for i in docs["newsfeed"]["items"] if i.get("hot")))
    prog.done()
    log("完成（%s，%d 秒）：市場方塊 %d 個、趨勢圖 %d 張、總經 %d 項、行事曆 %d 筆；氣氛：%s%s"
        % (meta["mode"], round(time.time() - t_start), n_t, len(docs["trends"]["charts"]), len(docs["macro"]), len(docs["calendar"]), docs["overview"]["mood"]["label"], extra))
    for m in problems[:10]:
        log("[警告] " + m)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
