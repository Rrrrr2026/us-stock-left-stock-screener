#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股 Market 适配器 — leftside_core 共用核心的全部市场差异都在这里
==================================================================
回测交易规则 (当日可卖 / 无涨跌停 / 0.2% 往返成本)、成长质量标签、价格序列
(yfinance 批量, 自动复权)、基准指数、个股新闻标题与风险关键词。
"""
from __future__ import annotations
import logging

from .config import DASHBOARD_DIR, DATA_DIR, DB_PATH
from leftside_core.market import Market, set_market

log = logging.getLogger("screener.market")

GROWTH_TIER = {"持续增长": "G", "拐点向上": "G", "增速回落": "M",
               "增长下滑": "W", "单季脉冲": "W"}
TIER_LABEL = {"G": "🟢 持续/拐点", "M": "🟡 增速回落", "W": "🔴 下滑/脉冲", "NA": "⚪ 无数据"}

NEWS_KEYWORDS = [
    ("downgrade", "downgrade"), ("cuts guidance", "guidance cut"), ("lowers guidance", "guidance cut"),
    ("guidance", "guidance"), ("misses", "miss"), ("miss ", "miss"), ("lawsuit", "lawsuit"),
    ("class action", "lawsuit"), ("investigation", "investigation"), ("probe", "investigation"),
    ("sec ", "SEC"), ("offering", "offering"), ("dilut", "dilution"), ("resign", "executive change"),
    ("steps down", "executive change"), ("ceo", "executive change"), ("layoff", "layoffs"),
    ("recall", "recall"), ("fda", "FDA"), ("delist", "delisting"), ("bankrupt", "bankruptcy"),
    ("fraud", "fraud"), ("short seller", "short report"), ("short report", "short report"),
    ("activist", "activist"), ("tariff", "tariffs"), ("warning", "warning"), ("plunge", "selloff"),
    ("tumble", "selloff"), ("sinks", "selloff"),
]


def fetch_price_series(codes: list, start: str) -> dict:
    """code -> {"dates":[...], "ohlc": ndarray[N,4] (o,h,l,c)}; yfinance 自动复权, 100只一批。"""
    res = {}
    try:
        import yfinance as yf
        import pandas as pd
        for i in range(0, len(codes), 100):
            batch = codes[i:i + 100]
            try:
                df = yf.download(batch, start=start, auto_adjust=True,
                                 progress=False, group_by="ticker", threads=True)
            except Exception as e:
                log.warning("yf batch %d 失败: %s", i // 100, e)
                continue
            for c in batch:
                try:
                    sub = df[c] if isinstance(df.columns, pd.MultiIndex) else df
                    sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
                    if len(sub) < 5:
                        continue
                    res[c] = {
                        "dates": [d.strftime("%Y-%m-%d") for d in sub.index],
                        "ohlc": sub[["Open", "High", "Low", "Close"]].to_numpy(float),
                    }
                except Exception:
                    continue
            log.info("价格进度 %d/%d (拿到 %d)", min(i + 100, len(codes)), len(codes), len(res))
    except Exception as e:
        log.warning("yfinance 不可用: %s", e)
    # Tiingo 兜底 yfinance 漏掉的 (限量, 免费档有独立代码配额)
    missing = [c for c in codes if c not in res]
    if missing and len(missing) <= 250:
        import numpy as np
        for c, rows in fetch_bars_bulk_tiingo(missing, start).items():
            res[c] = {"dates": [r[0] for r in rows],
                      "ohlc": np.array([r[1:5] for r in rows], dtype=float)}
    return res


def fetch_benchmark():
    from . import datasource as ds
    return ds.fetch_benchmark()


def news_titles(code: str) -> list:
    """错杀候选「为什么跌」线索用的标题: [(日期, 标题, 链接)]。取数走 datasource.news_items —— 与档案新闻同一条多源兜底链
    (yfinance Ticker.news → Search → Yahoo RSS → Google News RSS), 同一只票本轮只取一次。
    2026-10-04 前这里直接 `yf.Ticker(code).news or []`: 雅虎端点 404 时全市场静默为空, 🚩 一个都不亮, 读起来像没有利空。"""
    try:
        from . import datasource as ds
        return [(it["date"], it["title"], "" if it.get("url") in (None, "#") else it["url"])
                for it in ds.news_items(code) if it.get("date") and it.get("title")]
    except Exception as e:
        log.debug("news %s failed: %s", code, e)
        return []


def _skip_us_today() -> str | None:
    """美股收盘(≈UTC 20:00/21:00)前丢当日未走完bar; 统一按 UTC 21:10 保守判断。"""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    if now.hour < 21 or (now.hour == 21 and now.minute < 10):
        return now.date().isoformat()
    return None


def fetch_bars_bulk(codes: list, start: str) -> dict:
    """长历史日线(含成交量), yfinance 批量下载 -> {code: [(d,o,h,l,c,v), ...]}。"""
    import time
    res = {}
    skip_day = _skip_us_today()
    try:
        import pandas as pd
        import yfinance as yf
        for i in range(0, len(codes), 50):
            batch = codes[i:i + 50]
            df = None
            for attempt in (1, 2):
                try:
                    df = yf.download(batch, start=start, auto_adjust=True,
                                     progress=False, group_by="ticker", threads=True)
                    break
                except Exception as e:
                    log.warning("yf 长历史批 %d 第%d次失败: %s", i // 50, attempt, e)
                    time.sleep(5 * attempt)
            if df is None or len(df) == 0:
                continue
            for c in batch:
                try:
                    sub = df[c] if isinstance(df.columns, pd.MultiIndex) else df
                    sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
                    if len(sub) < 60:
                        continue
                    rows = []
                    for d, r in sub.iterrows():
                        d1 = d.strftime("%Y-%m-%d")
                        if skip_day and d1 >= skip_day:
                            continue
                        o, h, l, cl = (float(r["Open"]), float(r["High"]),
                                       float(r["Low"]), float(r["Close"]))
                        v = float(r.get("Volume") or 0)
                        if h < l or min(o, h, l, cl) <= 0:
                            continue
                        rows.append((d1, o, h, l, cl, v))
                    if len(rows) >= 60:
                        res[c] = rows
                except Exception:
                    continue
            log.info("长历史进度 %d/%d (拿到 %d)", min(i + 50, len(codes)), len(codes), len(res))
            time.sleep(1.0)
    except Exception as e:
        log.warning("yfinance 不可用: %s", e)
    return res


def _tiingo_token() -> str | None:
    import json
    import os
    tok = os.environ.get("TIINGO_TOKEN")
    if tok:
        return tok
    try:
        from .config import DATA_DIR
        return json.load(open(os.path.join(DATA_DIR, "secrets.json")))["tiingo_token"]
    except Exception:
        return None


def fetch_bars_bulk_tiingo(codes: list, start: str) -> dict:
    """Tiingo 备源 (yfinance 限流时用): 复权OHLCV, 一只票一请求。
    免费档有请求/独立代码配额 —— 只作兜底, 不做全池日常抓取。"""
    from concurrent.futures import ThreadPoolExecutor
    import requests
    tok = _tiingo_token()
    if not tok:
        return {}
    skip_day = _skip_us_today()

    def one(code):
        try:
            r = requests.get(f"https://api.tiingo.com/tiingo/daily/{code}/prices",
                             params={"startDate": start, "token": tok},
                             headers={"Content-Type": "application/json"}, timeout=30)
            if r.status_code != 200:
                return code, None
            rows = []
            for b in r.json():
                d1 = str(b.get("date") or "")[:10]
                if not d1 or (skip_day and d1 >= skip_day):
                    continue
                o, h, l, c = (b.get("adjOpen"), b.get("adjHigh"),
                              b.get("adjLow"), b.get("adjClose"))
                v = b.get("adjVolume") or b.get("volume") or 0
                if not all(isinstance(x, (int, float)) and x > 0 for x in (o, h, l, c)):
                    continue
                rows.append((d1, float(o), float(h), float(l), float(c), float(v)))
            rows.sort()
            return code, (rows if len(rows) >= 5 else None)
        except Exception:
            return code, None

    res = {}
    with ThreadPoolExecutor(max_workers=4) as exe:
        for code, rows in exe.map(one, codes):
            if rows:
                res[code] = rows
    if res:
        log.info("Tiingo 兜底: %d/%d 只", len(res), len(codes))
    return res


def fetch_index_bars(start: str) -> list:
    """SPY 长历史日线 -> [(d,o,h,l,c,v), ...]。"""
    r = fetch_bars_bulk(["SPY"], start)
    if not r.get("SPY"):
        r = fetch_bars_bulk_tiingo(["SPY"], start)
    return r.get("SPY") or []


def universe_codes() -> list:
    from . import datasource as ds
    uni = ds.get_universe()
    if uni is None or uni.empty:
        return []
    return sorted({str(c) for c in uni["code"] if c})


MARKET = set_market(Market(
    name="us",
    dashboard_dir=DASHBOARD_DIR, data_dir=DATA_DIR, db_path=DB_PATH,
    t_plus_one=False, limit_boards=False, cost_rt=0.002,
    growth_tier=GROWTH_TIER, tier_label=TIER_LABEL,
    fetch_price_series=fetch_price_series, fetch_benchmark=fetch_benchmark,
    limit_up_oneline=None, limit_down_oneline=None,
    news_titles=news_titles, news_keywords=NEWS_KEYWORDS,
    fetch_bars_bulk=fetch_bars_bulk, fetch_index_bars=fetch_index_bars,
    universe_codes=universe_codes,
    log_prefix="screener",
))
