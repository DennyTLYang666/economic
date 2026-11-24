# -*- coding: utf-8 -*-
"""驗證新資料來源（證交所三大法人、期交所夜盤）的真實格式。在有網路的地方執行：
    python -m pipeline.probe            # 或 GitHub Actions →「驗證資料來源」→ Run workflow
會印出每個來源的回應開頭（前 600 字）與解析結果；解析失敗就代表格式和程式預期不同，把輸出貼給維護者調整 chips.py。
不會寫入任何檔案。"""
import sys
from datetime import datetime, timedelta

from . import bankfx, chips
from .common import TZ, SourceError, http_get, url


def show(title, fn):
    print("=" * 8, title)
    try:
        fn()
    except SourceError as e:
        print("[失敗]", e)
    except Exception as e:  # noqa: BLE001
        print("[例外]", type(e).__name__, e)


def twse():
    d = datetime.now(TZ).date()
    for _ in range(10):  # 往回找最近一個有資料的交易日
        if d.weekday() < 5:
            u = "%s?type=day&dayDate=%s&response=json" % (url("TWSE_INST"), d.strftime("%Y%m%d"))
            raw = http_get(u)
            print("GET", u, "→", len(raw), "bytes")
            got = chips.parse_inst(raw)
            if got:
                print(raw[:600].decode("utf-8", "replace"))
                print("解析結果：", got[0], got[1])
                return
            print(d, "沒有資料（休市或尚未公布）")
        d -= timedelta(days=1)
    print("近 10 天都沒有解析出資料")


def taifex():
    raw = http_get(url("TAIFEX_FUT"))
    print("GET", url("TAIFEX_FUT"), "→", len(raw), "bytes")
    print(raw[:600].decode("utf-8", "replace"))
    n = chips.parse_night(raw)
    print("夜盤解析結果：", n if n else "沒有找到台指期盤後時段（開放資料可能不含盤後，或欄位名稱不同）")


def bot():
    raw = http_get(url("BOT_FX"))
    print("GET", url("BOT_FX"), "→", len(raw), "bytes")
    print(raw[:500].decode("utf-8-sig", "replace"))
    got = bankfx.parse(raw)
    for code in ("USD", "JPY", "EUR"):
        print(code, got.get(code, "沒有解析到"))
    print("共解析到 %d 個幣別" % len(got))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    show("證交所：三大法人買賣金額統計表（BFI82U）", twse)
    show("期交所：期貨每日交易行情（夜盤）", taifex)
    show("臺灣銀行：牌告匯率（現金／即期 買入賣出）", bot)
