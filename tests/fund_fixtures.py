# -*- coding: utf-8 -*-
"""合成的基金清單（欄位與官網相同：V1…V43），涵蓋每種主題與風險階層。測試用，數字是隨機的。"""
import random

# (名稱, V5 官網分類, V41 類型, 預期主題)
SEEDS = [
    ("安聯台灣科技基金", "國內股票開放型科技類", "A", "tech"),
    ("貝萊德世界黃金基金A2美元", "貴重金屬", "B", "precious"),
    ("摩根拉丁美洲基金", "股票型", "B", "latam"),
    ("施羅德環球基金系列-新興市場A1", "股票型", "B", "em"),
    ("富蘭克林坦伯頓全球投資系列-印度基金美元A(累計)", "股票型", "B", "india"),
    ("貝萊德美國增長基金A2美元", "股票型", "B", "us"),
    ("富達台灣成長基金A股累積型新臺幣計價級別", "國內股票開放型一般股票型", "A", "taiwan"),
    ("中國信託台灣活力基金", "國內股票開放型一般股票型", "A", "taiwan"),
    ("宏利環球基金-日本股票基金AA股", "股票型", "B", "japan"),
    ("貝萊德歐洲基金A2歐元", "股票型", "B", "europe"),
    ("富達基金-全球科技基金", "資訊科技股", "B", "tech"),
    ("聯博美國成長入息基金-AI類型(新台幣)", "跨國投資股票型單一國家", "B", "us"),
    ("野村全球高股息基金累積型", "跨國投資股票型全球市場", "B", "global"),
    ("摩根全球平衡基金", "股票債券平衡型", "B", "balanced"),
    ("聯博債券收益組合基金-AA", "全球組合型債券型", "B", "ig_bond"),
    ("貝萊德環球非投資等級債券基金A6美元(本基金有相當比重投資於非投資等級之高風險債券)", "非投資等級債券", "B", "hy_bond"),
    ("富蘭克林坦伯頓全球債券總報酬基金(本基金有相當比重投資於非投資等級之高風險債券)", "固定收益", "B", "ig_bond"),
    ("摩根新興市場債券基金", "債券型", "B", "em_bond"),
    ("PIMCO全球投資級別債券基金-M級類別", "公司債券", "B", "ig_bond"),
    ("貝萊德2028目標到期債券基金", "債券型", "B", "target"),
    ("復華貨幣市場基金", "國內貨幣市場型", "A", "money"),
    ("瑞銀(盧森堡)美元基金", "貨幣型", "B", "money"),
    ("景順健康護理創新基金A-年配息股美元", "健康護理股", "B", "health"),
    ("元大全球不動產證券化基金", "不動產證券化型", "A", "reit"),
]
EXTRA = [  # 只出現一次的特殊案例
    ("暫停測試基金(暫停新申購)", "股票型", "B", "equity"),
    ("元大台灣50ETF", "股票型", "C", "taiwan"),
    ("台灣人壽台幣代操帳戶（成長型）", "-", "D", "managed"),
]


def _stats(r, theme):
    hi = theme in ("tech", "precious", "latam", "em", "india", "health")
    sd = r.uniform(18, 30) if hi else r.uniform(10, 18) if theme in ("us", "taiwan", "japan", "europe", "global", "reit") else \
        r.uniform(7, 12) if theme == "balanced" else r.uniform(3, 8) if theme not in ("money",) else r.uniform(0.1, 0.5)
    y1 = r.uniform(-12, 38) if hi else r.uniform(-5, 20) if sd > 9 else r.uniform(-2, 8)
    return {"sd": sd, "y1": y1, "y3": y1 * r.uniform(1.2, 2.6), "y5": y1 * r.uniform(2, 4), "m1": r.uniform(-9, 4),
            "m3": r.uniform(-12, 8), "m6": r.uniform(-15, 14), "ytd": y1 * 0.8, "inc": y1 * r.uniform(3, 9),
            "sh": r.uniform(-0.3, 1.8), "be": r.uniform(0.3, 1.3)}


def rows(seed=1, per_seed=3, date="2026/11/06", with_extra=True, co="tlife"):
    r = random.Random(seed)
    out, n = [], 0
    items = [(s, per_seed) for s in SEEDS] + ([(e, 1) for e in EXTRA] if with_extra else [])
    for (name, cat, ty, theme), k in items:
        for j in range(k):
            n += 1
            st = _stats(r, theme)
            base = "%s%03d" % ("TL" if co == "tlife" else "NS", n)
            rr = 5 if st["sd"] > 18 else 4 if st["sd"] > 10 else 3 if st["sd"] > 5 else 2 if st["sd"] > 1 else 1
            nm = name if j == 0 else "%s-%s" % (name, "ABCDEF"[j])
            nav = round(r.uniform(8, 300), 2)
            out.append({
                "V1": date, "V2": base, "V3": nm, "V4": "ET001", "V5": cat, "V6": "B1", "V7": r.choice(["摩根投信", "安聯投信", "貝萊德投信"]),
                "V8": "0.1", "V9": "0.2", "V10": "%.2f" % st["m1"], "V11": "%.2f" % st["m3"], "V12": "%.2f" % st["m6"],
                "V13": "%.2f" % st["y1"], "V14": "%.2f" % (st["y1"] * 1.5), "V15": "%.2f" % st["y3"], "V16": "%.2f" % st["y5"],
                "V17": "%.2f" % st["ytd"], "V18": str(nav), "V19": "0.5", "V20": "%.2f" % r.uniform(-3, 3),
                "V21": "%.2f" % st["sh"], "V22": "%.2f" % st["be"], "V23": "%.2f" % st["sd"], "V24": "10", "V25": "AX1",
                "V26": r.choice(["台幣", "美元"]) if ty != "A" else "台幣", "V27": "4", "V28": "RR%d" % rr, "V29": "F%d" % n,
                "V30": "-", "V31": {"C": "ETF", "D": "DA"}.get(ty, "FUND"), "V32": "-", "V33": "1", "V34": "-",
                "V35": "2015/05/20" if "2028" not in nm else "2024/01/05", "V36": "%.2f" % st["inc"],
                "V37": "47", "V38": "全球", "V39": ("%.1f億" % r.uniform(3.5, 400)) if "暫停" not in nm else "50.0億",
                "V40": "%s-F%d" % (base, n), "V41": ty, "V42": "月配" if j == 1 else "-", "V43": "-"})
    return out


def split_nanshan(all_rows):
    """南山：m=0 淨值、m=1 績效、m=2 風險，三份各只帶部分欄位。"""
    keep = {0: ("V1", "V3", "V5", "V18", "V19", "V20", "V26", "V29", "V40", "V41"),
            1: ("V3", "V11", "V12", "V13", "V14", "V15", "V16", "V17", "V26", "V29", "V40", "V41"),
            2: ("V3", "V21", "V22", "V23", "V26", "V27", "V28", "V29", "V35", "V39", "V40", "V41")}
    return {m: [{k: r[k] for k in ks if k in r} for r in all_rows] for m, ks in keep.items()}
