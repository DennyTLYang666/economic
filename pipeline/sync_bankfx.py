# -*- coding: utf-8 -*-
"""在你自己的電腦抓臺灣銀行牌告匯率，存成 data/series/bankfx_cache.json 再推上 GitHub。
為什麼要在本機做：臺灣銀行對 GitHub Actions 的機器位址回「Challenge Validation」驗證頁（機器人驗證），Actions 抓不到；
家用網路一般不會被擋。GitHub 上的更新流程（config/bankfx.json 的 remote=false）不再自己抓，只讀這個檔案並發布到網站。
用法（在 eco-data 的 git 工作目錄、或用 --repo 指到那個目錄）：
    python -m pipeline.sync_bankfx --push            # 抓 → 寫檔 → git pull / commit / push
    python -m pipeline.sync_bankfx                   # 只抓與寫檔（不碰 git）
資料沒變（掛牌時間與價格都一樣）就不會 commit，避免洗版。只用標準函式庫與 git。"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import bankfx
from .common import ROOT, TZ, SourceError, atomic_write, load_series


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def same(old, new):
    """價格與掛牌時間都沒變就視為相同（updated 每次都不同，不能拿來比）。"""
    if not old:
        return False
    keys = lambda d: (d.get("quoted_at"), json.dumps(d.get("items"), sort_keys=True, ensure_ascii=False))  # noqa: E731
    return keys(old) == keys(new)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SourceError("git %s 失敗：%s" % (" ".join(args), (r.stderr or r.stdout).strip()[:300]))
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description="在本機抓臺灣銀行牌告匯率並推上 GitHub")
    ap.add_argument("--repo", default=str(ROOT), help="eco-data 的 git 工作目錄（預設：這個專案資料夾）")
    ap.add_argument("--push", action="store_true", help="寫檔後 git pull / commit / push")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    repo = Path(args.repo)
    cache = repo / "data" / "series" / "bankfx_cache.json"
    try:
        if args.push:
            git(repo, "pull", "--rebase", "--autostash")  # 先和 GitHub 對齊（Actions 也會推資料）
        cfg = _read(repo / "config" / "bankfx.json") or bankfx.DEFAULT
        daily = load_series(repo / "data" / "series" / "daily.csv", "close")
        quotes, when = bankfx.fetch()
        doc, warns = bankfx.build(cfg, quotes, daily, datetime.now(TZ), when)
    except SourceError as e:
        print("[失敗]", e)
        return 1
    for w in warns:
        print("[警告]", w)
    if same(_read(cache), doc):
        print("牌價沒變（掛牌時間 %s），不更新。" % (doc.get("quoted_at") or "未知"))
        return 0
    atomic_write(cache, json.dumps(doc, ensure_ascii=False) + "\n")
    print("已寫入 %s：%d 個幣別，掛牌時間 %s" % (cache, len(doc["items"]), doc.get("quoted_at") or "未知"))
    if not args.push:
        return 0
    try:
        git(repo, "add", "data/series/bankfx_cache.json")
        if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
            print("沒有變動。")
            return 0
        git(repo, "commit", "-m", "data: bankfx %s" % datetime.now(TZ).strftime("%Y-%m-%d %H:%M"))
        for i in range(3):
            if git(repo, "push", check=False).returncode == 0:
                print("已推上 GitHub。")
                return 0
            git(repo, "pull", "--rebase", "--autostash")  # 剛好和 Actions 的 commit 撞到：重新對齊再推
        raise SourceError("git push 連續失敗 3 次")
    except SourceError as e:
        print("[失敗]", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
