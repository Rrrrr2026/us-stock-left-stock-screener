#!/usr/bin/env python3
"""
抓取 TradingView 筛选接口，产出「分析师目标价 Min/Avg/Max 全部高于现价」的美股清单。

输出: data/screen.json（供 build_page.py / make_excel.py 使用）

原理: min 目标价 > 现价 ⟹ min/avg/max 全部在现价之上，所以只需 price_target_low > close。
接口: POST https://scanner.tradingview.com/america/scan —— TradingView 个股 Forecasts
页面同源的公开筛选接口（无需登录）。请保持低频（本脚本每页间隔 0.6s，每天跑一次足够）。

依赖: requests   (pip install requests)
"""
import json
import time
import datetime
import pathlib
import sys

import requests

# ------------------------- 可调参数 -------------------------
MIN_ANALYSTS = 5            # 至少几位分析师给出评级
EXCHANGES = ["AMEX", "NASDAQ", "NYSE"]
PAGE_SIZE = 500
PAUSE_SEC = 0.6             # 分页间隔，礼貌性限速
OUT = pathlib.Path(__file__).parent / "data" / "screen.json"
# -----------------------------------------------------------

SCAN_URL = "https://scanner.tradingview.com/america/scan"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}

COLS = ["name", "description", "close", "currency", "exchange", "type", "typespecs",
        "is_primary", "market_cap_basic", "sector", "industry",
        "price_target_low", "price_target_average", "price_target_high", "price_target_median",
        "recommendation_mark", "recommendation_total",
        "recommendation_buy", "recommendation_over", "recommendation_hold",
        "recommendation_under", "recommendation_sell"]

FILTER = [
    {"left": "exchange", "operation": "in_range", "right": EXCHANGES},
    {"left": "is_primary", "operation": "equal", "right": True},
    {"left": "recommendation_total", "operation": "egreater", "right": MIN_ANALYSTS},
    {"left": "price_target_low", "operation": "greater", "right": 0},
]


def fetch_page(session: requests.Session, start: int) -> dict:
    body = {
        "filter": FILTER,
        "options": {"lang": "en"},
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": COLS,
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [start, start + PAGE_SIZE],
    }
    r = session.post(SCAN_URL, json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _pick(*vals):
    """第一个非 None 的值 (TradingView 个别行目标价均值为 None)."""
    for v in vals:
        if v is not None:
            return v
    return 0.0


def _mid(lo, hi):
    return (lo + hi) / 2 if (lo is not None and hi is not None) else None


def main() -> None:
    session = requests.Session()
    all_rows, total = [], None
    for start in range(0, 20000, PAGE_SIZE):
        j = fetch_page(session, start)
        total = j["totalCount"]
        batch = j.get("data") or []
        all_rows.extend(batch)
        print(f"  page {start//PAGE_SIZE + 1}: +{len(batch)} (累计 {len(all_rows)}/{total})")
        if len(all_rows) >= total or not batch:
            break
        time.sleep(PAUSE_SEC)

    idx = {c: i for i, c in enumerate(COLS)}

    def keep(row) -> bool:
        d = row["d"]
        typ, ts, cur = d[idx["type"]], d[idx["typespecs"]] or [], d[idx["currency"]]
        ok_type = (typ == "stock" and "common" in ts and "preferred" not in ts) or typ == "dr"
        return (ok_type and cur == "USD"
                and d[idx["price_target_low"]] is not None and d[idx["close"]] is not None
                and d[idx["price_target_low"]] > d[idx["close"]])

    qual = [r for r in all_rows if keep(r)]
    sectors = sorted({(r["d"][idx["sector"]] or "(未分类)") for r in qual})
    sidx = {s: i for i, s in enumerate(sectors)}

    compact = []
    n_none = 0
    for r in qual:
        d = r["d"]
        compact.append([
            r["s"],                                        # NASDAQ:NBIX
            (d[idx["description"]] or "").replace("|", "/"),
            sidx[d[idx["sector"]] or "(未分类)"],
            d[idx["close"]],
            d[idx["price_target_low"]],
            # 2026-10-06 修: TradingView 个别行 price_target_average / recommendation_mark / market_cap_basic 为 None,
            # 10-06 00:15 在 market_cap_basic 上 TypeError 整跑崩掉 (方糖已报)。None 一律降级而不是崩: 均值缺 → 用中位/(低+高)/2,
            # 评级分缺 → 0.0, 市值缺 → 0.0 (排到末尾), 并计数打印。
            round(float(_pick(d[idx["price_target_average"]], d[idx["price_target_median"]],
                              _mid(d[idx["price_target_low"]], d[idx["price_target_high"]]))), 2),
            d[idx["price_target_high"]],
            d[idx["price_target_median"]],
            round(float(d[idx["recommendation_mark"]] if d[idx["recommendation_mark"]] is not None else 0.0), 2),
            d[idx["recommendation_total"]],
            d[idx["recommendation_buy"]] or 0,             # 强烈买入人数
            d[idx["recommendation_over"]] or 0,            # 买入
            d[idx["recommendation_hold"]] or 0,            # 持有
            d[idx["recommendation_under"]] or 0,           # 卖出
            d[idx["recommendation_sell"]] or 0,            # 强烈卖出
            round((d[idx["market_cap_basic"]] or 0.0) / 1e9, 3),    # None → 0.0;    # 市值（十亿美元）
        ])
        if d[idx["market_cap_basic"]] is None or d[idx["price_target_average"]] is None or d[idx["recommendation_mark"]] is None:
            n_none += 1
    if n_none:
        print(f"注意: {n_none} 行含 None 字段 (市值/目标均值/评级分), 已降级处理而非崩溃")
    compact.sort(key=lambda x: -(x[15] or 0.0))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(
        {"fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
         "totalCovered": total, "sectors": sectors, "rows": compact},
        open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"✓ 覆盖 {total} 只，符合条件 {len(compact)} 只 → {OUT}")
    if not compact:
        sys.exit("结果为空，多半是接口或字段变化，请检查响应。")


if __name__ == "__main__":
    main()
