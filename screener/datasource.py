#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据源访问层 (美股, yfinance)
=============================
封装 Yahoo Finance (yfinance):
  * 股票池 + GICS 板块: 标普500 成分 (GitHub CSV / 维基百科 备份 / 内置兜底)
  * 个股日线: yf.Ticker(t).history(period, auto_adjust)
  * 个股基本面/板块/52周: yf.Ticker(t).info
  * 板块指数代理: SPDR 行业 ETF; 基准: SPY
带缓存 + 重试; 单只失败只跳过并记录, 不打断整轮。
"""
from __future__ import annotations
import os
import io
import re
import time
import pickle
import threading
import hashlib
import datetime as dt
import logging

import numpy as np
import pandas as pd

from .config import CONFIG, DATA_DIR

log = logging.getLogger("screener.datasource")

_CACHE_DIR = CONFIG["source"]["cache_dir"]
os.makedirs(_CACHE_DIR, exist_ok=True)

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

# 兜底最小股票池 (CSV/维基都拿不到时, 至少能跑)
_FALLBACK_UNIVERSE = [
    ("AAPL", "Apple", "Information Technology"), ("MSFT", "Microsoft", "Information Technology"),
    ("NVDA", "NVIDIA", "Information Technology"), ("AMZN", "Amazon", "Consumer Discretionary"),
    ("GOOGL", "Alphabet", "Communication Services"), ("META", "Meta Platforms", "Communication Services"),
    ("TSLA", "Tesla", "Consumer Discretionary"), ("JPM", "JPMorgan Chase", "Financials"),
    ("JNJ", "Johnson & Johnson", "Health Care"), ("XOM", "Exxon Mobil", "Energy"),
    ("UNH", "UnitedHealth", "Health Care"), ("V", "Visa", "Financials"),
    ("PG", "Procter & Gamble", "Consumer Staples"), ("HD", "Home Depot", "Consumer Discretionary"),
    ("CAT", "Caterpillar", "Industrials"), ("NEE", "NextEra Energy", "Utilities"),
    ("LIN", "Linde", "Materials"), ("AMT", "American Tower", "Real Estate"),
]


def _yf():
    import yfinance as yf
    return yf


def _yf_symbol(code: str) -> str:
    # yfinance 的多类股用 '-' (如 BRK.B -> BRK-B)
    return str(code).replace(".", "-").strip().upper()


# ===========================================================================
#  缓存
# ===========================================================================
def _cache_key(name: str, *args) -> str:
    raw = name + "|" + "|".join(str(a) for a in args)
    return f"{name}_{hashlib.md5(raw.encode()).hexdigest()[:16]}"


def _cache_load(key):
    if not CONFIG["source"]["use_cache"]:
        return None
    path = os.path.join(_CACHE_DIR, key + ".pkl")
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) / 3600.0 > CONFIG["source"]["cache_ttl_hours"]:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _cache_save(key, obj):
    if not CONFIG["source"]["use_cache"]:
        return
    path = os.path.join(_CACHE_DIR, key + ".pkl")
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            pickle.dump(obj, f)
        os.replace(tmp, path)
    except Exception as e:
        log.debug("cache save failed %s: %s", key, e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _retry(fn, *a, **k):
    f = CONFIG["fetch"]
    last = None
    for i in range(f["max_retries"]):
        try:
            time.sleep(f["sleep_sec"])
            return fn(*a, **k)
        except Exception as e:  # noqa
            last = e
            time.sleep(f["retry_backoff_sec"] * (2 ** i))
    raise last


# ===========================================================================
#  1) 股票池 + 板块 (标普500)
# ===========================================================================
# NASDAQ 板块名 -> GICS/SPDR 口径 (统一以便板块景气 ETF 匹配)
# 注意: NASDAQ 的分类质量一般 (MO/BTI 标成 Health Care 之类), 仅作初值;
# 候选股在基本面阶段会用 Yahoo 的 sector 覆盖 (见 YAHOO_TO_GICS + module3)。
_NASDAQ_TO_GICS = {
    "Technology": "Information Technology",
    "Finance": "Financials",
    "Health Care": "Health Care",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Staples": "Consumer Staples",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
    "Basic Materials": "Materials",
    "Telecommunications": "Communication Services",
}

# Yahoo Finance (yf.info["sector"]) -> GICS 板块名 (权威覆盖源)
YAHOO_TO_GICS = {
    "Technology": "Information Technology",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Materials",
}

# 名称形如债券/优先股/权证/SPAC单位的非普通股, 从股票池剔除 (NASDAQ 会给它们填发行人市值)
# 注意 "Preferred Bank" 是真公司名, 用负向断言放行
_NON_COMMON_RE = re.compile(
    r"(?i)\b(notes? due|debentures?|preferred\b(?!\s+bank)|"
    r"warrants?\b|rights?$|\bunits?$|units?,? (each )?consisting)")


def get_universe() -> pd.DataFrame | None:
    """返回 code, name, sector 的 DataFrame。
    模式 all_us: 市值>=下限的全美股 (NASDAQ 官方筛选器); sp500: 仅标普500。"""
    mode = CONFIG["source"]["universe_mode"]
    # 市值下限+schema版本进缓存键: 改配置/改列结构立即生效, 不被当日旧名单缓存挡住
    key = _cache_key("universe", mode, CONFIG["source"]["min_market_cap"], "v3",
                     dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = None
    if mode == "all_us":
        df = _universe_all_us()
    if df is None or df.empty:      # sp500 模式, 或 all_us 失败 -> 退回标普500
        df = _universe_from_csv()
        if df is None or df.empty:
            df = _universe_from_wiki()
    if df is None or df.empty:
        log.warning("在线名单获取失败, 使用内置兜底名单(%d只)", len(_FALLBACK_UNIVERSE))
        df = pd.DataFrame(_FALLBACK_UNIVERSE, columns=["code", "name", "sector"])
    df["code"] = df["code"].astype(str).str.strip()
    df = df.dropna(subset=["code"]).drop_duplicates(subset=["code"]).reset_index(drop=True)
    # all_us 模式只缓存"看起来完整"的名单: 避免 NASDAQ 一次抖动导致
    # 降级的 S&P500/兜底名单被缓存一整天, 白天重跑也扫不全
    if mode != "all_us" or len(df) > 1500:
        _cache_save(key, df)
    return df


def _clean_mcap(x) -> float:
    s = str(x).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except Exception:
        return 0.0


def _valid_ticker(sym: str) -> bool:
    sym = str(sym).strip().upper()
    return bool(sym) and "^" not in sym and " " not in sym and all(
        ch.isalnum() or ch in ".-" for ch in sym)


def _universe_all_us() -> pd.DataFrame | None:
    import requests
    hdr = {**_UA, "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9",
           "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
    try:
        r = _retry(requests.get, CONFIG["source"]["nasdaq_screener"], headers=hdr,
                   timeout=CONFIG["fetch"]["timeout_sec"])
        data = r.json().get("data", {})
        rows = data.get("rows") or (data.get("table") or {}).get("rows") or []
    except Exception as e:
        log.warning("NASDAQ 全美股名单失败: %s", e)
        return None
    if not rows:
        return None
    minmc = CONFIG["source"]["min_market_cap"]
    out = []
    for row in rows:
        sym = str(row.get("symbol", "")).strip()
        if not _valid_ticker(sym):
            continue
        mcap = _clean_mcap(row.get("marketCap"))
        if mcap < minmc:
            continue
        name = str(row.get("name", "")).strip()
        if _NON_COMMON_RE.search(name):     # 剔除挂牌债券/优先股/权证/SPAC单位
            continue
        sec_raw = str(row.get("sector", "")).strip()
        if sec_raw == "Miscellaneous":       # NASDAQ 的杂项与空板块统一记 "" (未知, 待Yahoo覆盖)
            sec_raw = ""
        # 细分行业 + 市值: 用于"市场地位"(行业内市值排名/份额, 作垄断力代理指标)
        ind_raw = str(row.get("industry", "")).strip()
        if ind_raw in ("Miscellaneous", "Blank Checks"):
            ind_raw = ""
        out.append((sym, name, _NASDAQ_TO_GICS.get(sec_raw, sec_raw), ind_raw, mcap))
    if not out:
        return None
    log.info("全美股(市值>=$%.0fM): %d 只", minmc / 1e6, len(out))
    return pd.DataFrame(out, columns=["code", "name", "sector", "nasdaq_industry", "mcap"])


def _universe_from_csv():
    import requests
    for url in CONFIG["source"]["sp500_csv"]:
        try:
            r = _retry(requests.get, url, headers=_UA, timeout=CONFIG["fetch"]["timeout_sec"])
            raw = pd.read_csv(io.StringIO(r.text))
            cols = {c.lower(): c for c in raw.columns}
            sym = cols.get("symbol")
            name = cols.get("security") or cols.get("name")
            sec = cols.get("gics sector") or cols.get("sector")
            if not sym:
                continue
            df = pd.DataFrame({
                "code": raw[sym],
                "name": raw[name] if name else raw[sym],
                "sector": raw[sec] if sec else "",
            })
            return df
        except Exception as e:
            log.debug("S&P500 CSV %s 失败: %s", url, e)
    return None


def _universe_from_wiki():
    import requests
    try:
        r = _retry(requests.get, CONFIG["source"]["sp500_wiki"], headers=_UA,
                   timeout=CONFIG["fetch"]["timeout_sec"])
        tabs = pd.read_html(io.StringIO(r.text))
        raw = tabs[0]
        return pd.DataFrame({
            "code": raw["Symbol"],
            "name": raw.get("Security", raw["Symbol"]),
            "sector": raw.get("GICS Sector", ""),
        })
    except Exception as e:
        log.debug("维基 S&P500 失败: %s", e)
        return None


# ===========================================================================
#  2) 个股日线 (yfinance, 复权)
# ===========================================================================
def _normalize_hist(raw: pd.DataFrame) -> pd.DataFrame | None:
    if raw is None or len(raw) == 0:
        return None
    df = raw.reset_index()
    ren = {}
    for c in df.columns:
        cl = str(c).lower()
        if cl in ("date", "datetime"):
            ren[c] = "date"
        elif cl == "open":
            ren[c] = "open"
        elif cl == "high":
            ren[c] = "high"
        elif cl == "low":
            ren[c] = "low"
        elif cl == "close":
            ren[c] = "close"
        elif cl == "volume":
            ren[c] = "volume"
    df = df.rename(columns=ren)
    need = {"date", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        return None
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "volume" in df.columns:
        df["amount"] = df["close"] * df["volume"]   # 成交额(美元) 用 收盘*成交量 近似
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df


def fetch_hist(code: str) -> pd.DataFrame | None:
    f = CONFIG["fetch"]
    key = _cache_key("hist", code, f["period"], dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        raw = _retry(tk.history, period=f["period"], auto_adjust=f["auto_adjust"],
                     timeout=f["timeout_sec"])
    except Exception as e:
        log.debug("fetch_hist %s 失败: %s", code, e)
        return None
    df = _normalize_hist(raw)
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


# ===========================================================================
#  3) 个股基本面 / 板块 / 52周  (yf.info)
# ===========================================================================
def fetch_info(code: str) -> dict | None:
    key = _cache_key("info", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    def _get():   # 每次重试都用全新 Ticker, 促使 yfinance 重新取 crumb/cookie
        return _yf().Ticker(_yf_symbol(code)).info
    try:
        info = _retry(_get)
    except Exception as e:
        log.debug("fetch_info %s 失败: %s", code, e)
        return None
    if not isinstance(info, dict) or not info:
        return None
    # 只缓存"像样"的 info: 限频时 Yahoo 会返回只有零星键的空壳 dict,
    # 缓存它会让该股当天所有重试都拿到空基本面 (全横杠行的根因之一)
    looks_ok = len(info) >= 5 and any(
        info.get(k) is not None for k in ("regularMarketPrice", "currentPrice", "marketCap"))
    if looks_ok:
        _cache_save(key, info)
    return info


_NI_ROWS = ("Net Income", "Net Income Common Stockholders",
            "Net Income Including Noncontrolling Interests")
_EQ_ROWS = ("Stockholders Equity", "Total Stockholders Equity",
            "Common Stock Equity", "Total Equity Gross Minority Interest")


def fetch_roe_trend(code: str) -> list:
    """年度 ROE 趋势 = 净利润 / 股东权益 * 100 (yfinance 年报, 通常近4个财年)。
    取不到(接口失败/行名缺失/权益<=0)返回 [], 前端显示"暂无"。"""
    key = _cache_key("roetrend", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, list) else []

    def _row(df, names):
        if df is None or getattr(df, "empty", True):
            return None
        for n in names:
            if n in df.index:
                return df.loc[n]
        return None

    out = []
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        inc = _retry(lambda: tk.income_stmt)
        bal = _retry(lambda: tk.balance_sheet)
        ni = _row(inc, _NI_ROWS)
        eq = _row(bal, _EQ_ROWS)
        if ni is not None and eq is not None:
            for col in ni.index:
                if col not in eq.index:
                    continue
                n_v, e_v = ni[col], eq[col]
                try:
                    n_v, e_v = float(n_v), float(e_v)
                except Exception:
                    continue
                if not (pd.notna(n_v) and pd.notna(e_v)) or e_v <= 0:
                    continue
                out.append({"date": pd.Timestamp(col).strftime("%Y-%m"),
                            "value": round(n_v / e_v * 100.0, 1)})
        out.sort(key=lambda d: d["date"])
        out = out[-5:]
    except Exception as e:
        log.debug("fetch_roe_trend %s 失败: %s", code, e)
        out = []
    if out:                    # 失败/空结果不缓存, 避免一次网络抖动污染当日所有重跑
        _cache_save(key, out)
    return out


def fetch_roe_trend_q(code: str) -> list:
    """单季 ROE 趋势 (TTM 口径): 最近4季净利润之和 / 当季股东权益 *100, 逐季滚动, 近8季。
    用于详情页 ROE 图的"季度"细化选项。"""
    key = _cache_key("roetrendq", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, list) else []

    def _row(df, names):
        if df is None or getattr(df, "empty", True):
            return None
        for n in names:
            if n in df.index:
                return df.loc[n]
        return None

    out = []
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        inc = _retry(lambda: tk.quarterly_income_stmt)
        bal = _retry(lambda: tk.quarterly_balance_sheet)
        ni, eq = _row(inc, _NI_ROWS), _row(bal, _EQ_ROWS)
        if ni is not None and eq is not None:
            ni_by, eq_by = {}, {}
            for col, v in ni.items():
                try:
                    v = float(v)
                except Exception:
                    continue
                if pd.notna(v):
                    ni_by[pd.Timestamp(col)] = v
            for col, v in eq.items():
                try:
                    v = float(v)
                except Exception:
                    continue
                if pd.notna(v) and v > 0:
                    eq_by[pd.Timestamp(col)] = v
            dates = sorted(set(ni_by) & set(eq_by))
            for i in range(3, len(dates)):     # 需4季凑TTM, 且4季必须连续(相邻间隔<=120天)
                span = [dates[j] for j in range(i - 3, i + 1)]
                if any((span[j + 1] - span[j]).days > 120 for j in range(3)):
                    continue                   # 有缺季, TTM 不成立, 跳过
                ttm = sum(ni_by[d] for d in span)
                out.append({"date": dates[i].strftime("%Y-%m"),
                            "value": round(ttm / eq_by[dates[i]] * 100.0, 1)})
            out = out[-8:]
    except Exception as e:
        log.debug("fetch_roe_trend_q %s 失败: %s", code, e)
        out = []
    if out:
        _cache_save(key, out)
    return out


def fetch_eps_history(code: str) -> dict:
    """财报日历的"实际公布EPS"序列 (get_earnings_dates, 约8-12个季度) —
    这是 Yahoo 免费口径里唯一够深的"单季盈利"数据, 用于 近四季EPS同比×4。
    返回 {"dates": [...升序 'YYYY-MM-DD'], "eps": [...]}; 失败 {}。"""
    key = _cache_key("epsh", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    out = {}
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        ed = _retry(lambda: tk.get_earnings_dates(limit=16))
        if ed is not None and len(ed):
            rows = []
            col = "Reported EPS"
            today_s = dt.date.today().isoformat()
            future = []
            for idx, r in ed.iterrows():
                v = r.get(col)
                d_s = pd.Timestamp(idx).strftime("%Y-%m-%d")
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    if d_s >= today_s:
                        future.append(d_s)          # 尚未公布的 = 下一次财报日
                    continue
                rows.append((d_s, float(v)))
            rows.sort(key=lambda t: t[0])
            if rows or future:
                out = {"dates": [d for (d, _) in rows], "eps": [v for (_, v) in rows],
                       "next": (min(future) if future else None)}
    except Exception as e:
        log.debug("fetch_eps_history %s 失败: %s", code, e)
        out = {}
    if out:
        _cache_save(key, out)
    return out


def fetch_quarterly_ni(code: str) -> dict:
    """季度+年度 净利润 与 归母净利润 (百万美元) — 用于近四季增速与增长持续性。
    返回 {"q_dates":[...], "ni":[...], "ni_parent":[...],
          "fy_dates":[...], "fy_ni":[...], "fy_ni_parent":[...]} (时间升序, 缺值为None)。
    美股口径: Net Income Common Stockholders ≈ 归母净利润。"""
    key = _cache_key("qni", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    out = {}
    _NI = ("Net Income",)
    _NIP = ("Net Income Common Stockholders", "Net Income Attributable To Parent Company")
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        qinc = _retry(lambda: tk.quarterly_income_stmt)
        inc = _retry(lambda: tk.income_stmt)

        def _pairs(df, names):
            row = _stmt_row(df, names)
            if row is None:
                return []
            pts = []
            for col, v in row.items():
                try:
                    v = float(v)
                except Exception:
                    v = None
                if v is not None and pd.isna(v):
                    v = None
                pts.append((pd.Timestamp(col), None if v is None else round(v / 1e6, 1)))
            pts.sort(key=lambda t: t[0])
            return pts

        q_ni = _pairs(qinc, _NI)
        q_nip = _pairs(qinc, _NIP)
        q_rev = _pairs(qinc, ("Total Revenue", "Operating Revenue"))
        if q_ni:
            dates = [d for (d, _) in q_ni]
            nip_by = {d: v for (d, v) in q_nip}
            rev_by = {d: v for (d, v) in q_rev}
            out["q_dates"] = [d.strftime("%Y-%m") for d in dates]
            out["ni"] = [v for (_, v) in q_ni]
            out["ni_parent"] = [nip_by.get(d) for d in dates]
            out["rev"] = [rev_by.get(d) for d in dates]
        fy_ni = _pairs(inc, _NI)
        fy_nip = _pairs(inc, _NIP)
        if fy_ni:
            fdates = [d for (d, _) in fy_ni]
            fnip_by = {d: v for (d, v) in fy_nip}
            out["fy_dates"] = [d.strftime("%Y-%m") for d in fdates]
            out["fy_ni"] = [v for (_, v) in fy_ni]
            out["fy_ni_parent"] = [fnip_by.get(d) for d in fdates]
    except Exception as e:
        log.debug("fetch_quarterly_ni %s 失败: %s", code, e)
        out = {}
    if out:
        _cache_save(key, out)
    return out


# ===========================================================================
#  3b) 深度档案: 现金流 / 营收 / 新闻 / 期权 / FINRA 场外空头占比
# ===========================================================================
def _stmt_row(df, names):
    """从财报 DataFrame 里按候选行名取一行 (Series, 索引=财报期)。"""
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def _row_by_year(series):
    """财报行 -> {年份str: 百万美元float}; NaN 跳过。"""
    out = {}
    if series is None:
        return out
    for col, v in series.items():
        try:
            v = float(v)
        except Exception:
            continue
        if pd.isna(v):
            continue
        out[pd.Timestamp(col).strftime("%Y")] = round(v / 1e6, 1)
    return out


def _row_by_qtr(series):
    """财报行 -> {Timestamp: 百万美元float}; 用于季度序列。"""
    out = {}
    if series is None:
        return out
    for col, v in series.items():
        try:
            v = float(v)
        except Exception:
            continue
        if pd.isna(v):
            continue
        out[pd.Timestamp(col)] = round(v / 1e6, 1)
    return out


def fetch_cashflow(code: str) -> dict:
    """年度现金流关键科目 (百万美元, 近4财年)。用于"钱流向哪了"分析:
    经营现金流 / 资本开支 / 自由现金流 / 收购 / 回购 / 分红 / 净发债 / 净利润。"""
    key = _cache_key("cashflow2", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    out = {}
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        cf = _retry(lambda: tk.cash_flow)
        rows = {
            "ocf":      ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
            "capex":    ("Capital Expenditure",),
            "fcf":      ("Free Cash Flow",),
            "acq":      ("Purchase Of Business", "Net Business Purchase And Sale"),
            "buyback":  ("Repurchase Of Capital Stock",),
            "dividend": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
            "debt_net": ("Net Issuance Payments Of Debt",),
            "debt_iss": ("Issuance Of Debt", "Long Term Debt Issuance"),
            "debt_rep": ("Repayment Of Debt", "Long Term Debt Payments"),
            "net_income": ("Net Income From Continuing Operations", "Net Income"),
            "invest_cf": ("Investing Cash Flow",),
            "fin_cf":   ("Financing Cash Flow",),
            "end_cash": ("End Cash Position",),
        }
        data = {k: _row_by_year(_stmt_row(cf, names)) for k, names in rows.items()}
        years = sorted({y for d in data.values() for y in d})[-4:]
        if years:
            out = {"years": years}
            for k, d in data.items():
                out[k] = [d.get(y) for y in years]
        # 季度现金流(近8季) — 用于图表的"季度"细化选项
        try:
            qcf = _retry(lambda: tk.quarterly_cash_flow)
            qdata = {k: _row_by_qtr(_stmt_row(qcf, names)) for k, names in rows.items()}
            qdates = sorted({d for dd in qdata.values() for d in dd})[-8:]
            if qdates and out:
                out["q_years"] = [d.strftime("%y") + "Q" + str((d.month - 1) // 3 + 1) for d in qdates]
                for k, dd in qdata.items():
                    out["q_" + k] = [dd.get(d) for d in qdates]
        except Exception:
            pass
    except Exception as e:
        log.debug("fetch_cashflow %s 失败: %s", code, e)
        out = {}
    if out:
        _cache_save(key, out)
    return out


def fetch_revenue_trend(code: str) -> dict:
    """营收增速(年度+季度, YoY) + 最近财年"营收流向"成本结构拆解
    (营业成本/研发/销售管理/税/净利, 各项含同比 — 用于饼图与增速标注)。
    注: 免费数据源无分部(business segment)营收, 以成本结构拆解替代。"""
    key = _cache_key("revtrend3", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    out = {}
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        inc = _retry(lambda: tk.income_stmt)
        qinc = _retry(lambda: tk.quarterly_income_stmt)
        rev = _row_by_year(_stmt_row(inc, ("Total Revenue", "Operating Revenue")))
        years = sorted(rev)[-5:]
        yoy = []
        for i, y in enumerate(years):
            prev = rev.get(str(int(y) - 1))
            yoy.append(round((rev[y] / prev - 1) * 100.0, 1) if (prev and prev > 0) else None)
        out["years"] = years
        out["revenue"] = [rev.get(y) for y in years]
        out["rev_yoy"] = yoy

        # 季度营收 (近8季, YoY 需同比4季前)
        qrow = _stmt_row(qinc, ("Total Revenue", "Operating Revenue"))
        if qrow is not None:
            q = []
            for col, v in qrow.items():
                try:
                    v = float(v)
                except Exception:
                    continue
                if pd.isna(v):
                    continue
                q.append((pd.Timestamp(col), round(v / 1e6, 1)))
            q.sort(key=lambda t: t[0])
            out["quarters"] = [t[0].strftime("%Y-%m") for t in q][-8:]
            out["q_revenue"] = [t[1] for t in q][-8:]

        # 成本结构拆解 (最近财年 vs 上一财年 -> 各项 YoY)
        items = {
            "营业成本": ("Cost Of Revenue",),
            "研发投入": ("Research And Development",),
            "销售与管理": ("Selling General And Administration",
                       "Selling General And Administrative"),
            "所得税": ("Tax Provision",),
            "净利润": ("Net Income", "Net Income Common Stockholders"),
        }
        if years and rev.get(years[-1]):
            y_now, y_prev = years[-1], str(int(years[-1]) - 1)
            total_now = rev[y_now]
            comps, used_signed = [], 0.0
            for lab, names in items.items():
                d = _row_by_year(_stmt_row(inc, names))
                v_now, v_prev = d.get(y_now), d.get(y_prev)
                if v_now is None:
                    continue
                item_yoy = (round((v_now / v_prev - 1) * 100.0, 1)
                            if (v_prev and v_prev > 0 and v_now > 0) else None)
                # 保留符号! 亏损/税收抵免为负值; 饼图只画正值项, 负值由前端注记
                comps.append({"name": lab, "value": round(v_now, 1), "yoy": item_yoy})
                if lab != "净利润":
                    used_signed += v_now
            ni_signed = next((x["value"] for x in comps if x["name"] == "净利润"), 0.0)
            # 带符号恒等式: 其他 = 营收 - Σ成本(带符号) - 净利润 (亏损为负 => 自动加回)
            other = round(total_now - used_signed - ni_signed, 1)
            if other > total_now * 0.01:
                comps.append({"name": "其他费用/摊销", "value": other, "yoy": None})
            out["cost_year"] = y_now
            out["cost_total"] = round(total_now, 1)
            out["cost_items"] = comps
    except Exception as e:
        log.debug("fetch_revenue_trend %s 失败: %s", code, e)
        out = {}
    # 只缓存拿到"年度"数据的结果; 仅季度(年报被限频)视为不完整, 不缓存 -> 下次重试年报
    if out.get("years"):
        _cache_save(key, out)
    return out


_POS_KW = ("beat", "beats", "tops", "top estimate", "upgrade", "raised", "raises",
           "buyback", "record", "surge", "soars", "soar", "jumps", "jump", "rally",
           "wins", "win ", "approval", "approves", "partnership", "outperform",
           "strong", "expands", "expansion", "better-than-expected", "bullish",
           "dividend increase", "hikes dividend", "all-time high", "breakthrough")
_NEG_KW = ("miss", "misses", "downgrade", "cut", "cuts", "lawsuit", "sues", "probe",
           "investigation", "recall", "layoff", "warns", "warning", "plunge",
           "sinks", "slump", "falls", "drops", "tumbles", "bearish", "fraud",
           "bankruptcy", "underperform", "weak", "sec charges", "delist",
           "short seller", "guidance cut", "halts", "fined", "penalty")


# 词边界匹配, 避免 'cut' 命中 'executive'、'miss' 命中 'commission' 之类的误判
_POS_RE = [re.compile(r"\b" + re.escape(k.strip()) + r"\b") for k in _POS_KW]
_NEG_RE = [re.compile(r"\b" + re.escape(k.strip()) + r"\b") for k in _NEG_KW]


def _news_tone(title: str) -> str:
    t = (title or "").lower()
    pos = sum(1 for r in _POS_RE if r.search(t))
    neg = sum(1 for r in _NEG_RE if r.search(t))
    if pos > neg:
        return "利好"
    if neg > pos:
        return "利空"
    return "中性"


# ---------------------------------------------------------------------------
#  新闻: 多源兜底 (2026-10-04 卡 NEWS-OVERDUE)
# ---------------------------------------------------------------------------
# 雅虎 xhr/ncp 新闻端点 2026-09-29 起 404, yfinance (1.2.1 与最新 1.7.0 同一实现) 吞错返回空列表; 这里原来写的是
# `tk.news or []` —— 全市场 0 条也不吭声, 看板 10-02 起「利好 0 / 利空 0」「近30天无相关新闻标题」, 读起来像没有利空。
# 现在每只票按序试四家, 第一家非空即用 (档案新闻 fetch_news 与错杀红旗 market.news_titles 共用这一条链):
#   ① yfinance Ticker.news            (雅虎 xhr/ncp; 现在是坏的, 留着等它恢复)
#   ② yfinance Search(...).news       (query2 搜索接口 = 0.2.x 时代 Ticker.news 的实现; 10-04 服务器生产 venv 实测可用)
#   ③ Yahoo RSS headline feed         (10-04 实测: 本机 200, 服务器 429 —— 数据中心 IP 被限)
#   ④ Google News RSS search          (两边都 200)
# 纪律: 每次调用有墙钟硬期限 (线程守护, 不靠 socket 超时); ②③④ 串行 + 最小间隔 (档案阶段是多线程, 不并发敲兜底端点);
#       某一家连续 _NEWS_BREAK_AFTER 只票没取到 → 本轮熔断, 不再问它; 同一进程内每只票只取一次 (导出要调三遍 build_payload)。
_NEWS_PROVIDERS = ("yfinance", "yahoo_search", "yahoo_rss", "google_rss")
_NEWS_DEADLINE_S = 20
_NEWS_BREAK_AFTER = 8
_NEWS_GAP_S = 0.25
_NEWS_FETCH_N = 20
_NEWS_RSS_MAX_BYTES = 2 * 1024 * 1024
_YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
_GOOGLE_RSS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
_NEWS_LOCK = threading.Lock()          # 保护下面三个状态
_NEWS_GATE = threading.Lock()          # 兜底源串行闸
_NEWS_MEMO: dict = {}                  # yfinance 代码 -> 本轮取到的条目 (空表也记: 四家都问过了)
_NEWS_FAILS = {p: 0 for p in _NEWS_PROVIDERS}    # 各家连续没取到的票数
_NEWS_LAST_CALL = [0.0]


def _hard_deadline(fn, seconds: float):
    """在守护线程里跑 fn, 超过 seconds 就放弃 (线程被遗弃)。socket 超时管不住慢慢滴数据的服务器, 硬期限只能靠墙钟。"""
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:      # noqa: BLE001 —— 原样转交调用方
            box["e"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(max(0.1, seconds))
    if th.is_alive():
        raise TimeoutError(f"超过 {seconds:.0f}s 硬期限")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _news_item(title, publisher, when, url, source) -> dict | None:
    """统一结构: title / publisher / time ('YYYY-MM-DD HH:MM' UTC, 没有就 '—') / date / url / source。"""
    title = str(title or "").strip()
    if not title:
        return None
    ts = when.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if isinstance(when, dt.datetime) else None
    return {"title": title[:300], "publisher": str(publisher or "").strip()[:80] or "—",
            "time": ts or "—", "date": ts[:10] if ts else "", "url": str(url or "").strip() or "#", "source": source}


def parse_yf_news(raw, source: str) -> list:
    """yfinance 新旧两种新闻结构 ({content:{...}} 与平铺; Search(...).news 是平铺那种) → 统一结构。"""
    out = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        c = it.get("content") if isinstance(it.get("content"), dict) else it
        prov = c.get("provider")
        publisher = (prov or {}).get("displayName") if isinstance(prov, dict) else c.get("publisher")
        cu = c.get("canonicalUrl")
        url = (cu.get("url") if isinstance(cu, dict) else None) or c.get("link")
        when = None
        ts = c.get("pubDate") or c.get("displayTime")
        try:
            if isinstance(ts, str) and ts:
                when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
            elif c.get("providerPublishTime"):
                when = dt.datetime.fromtimestamp(int(c["providerPublishTime"]), dt.timezone.utc)
        except (TypeError, ValueError, OSError):
            when = None
        item = _news_item(c.get("title"), publisher, when, url, source)
        if item:
            out.append(item)
    return out[:_NEWS_FETCH_N]


def parse_rss(xml, source: str) -> list:
    """RSS 2.0 → 统一结构。Yahoo headline feed 没有出版方字段 (用链接域名), 链接带 `.tsrc=rss` 追踪参数 (去掉);
    Google News search feed 用 <source> 给出版方, 标题尾巴带「 - 出版方」(去掉)。时间取 pubDate 换成 UTC。
    带 DTD / 实体声明的文档不解析 (正常 RSS 没有; 挡实体炸弹), 超过 _NEWS_RSS_MAX_BYTES 的也不解析。"""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    raw = xml.encode("utf-8") if isinstance(xml, str) else bytes(xml or b"")
    if len(raw) > _NEWS_RSS_MAX_BYTES:
        raise ValueError(f"RSS {len(raw)} 字节超过上限")
    low = raw.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise ValueError("RSS 含 DTD/实体声明, 拒绝解析")
    root = ET.fromstring(raw)
    out, seen = [], set()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        try:
            p = urlsplit(link)
            q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.startswith(".tsrc")]
            link, host = urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment)), p.netloc.lower()
        except ValueError:
            continue
        if not re.match(r"^https?://", link) or link in seen:
            continue
        src = it.find("source")
        publisher = (src.text or "").strip() if src is not None else ""
        if publisher and title.endswith(" - " + publisher):
            title = title[:-len(" - " + publisher)].rstrip()
        try:
            when = parsedate_to_datetime((it.findtext("pubDate") or "").strip())
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError, IndexError):
            continue
        item = _news_item(title, publisher or (host[4:] if host.startswith("www.") else host), when, link, source)
        if item:
            seen.add(link)
            out.append(item)
    out.sort(key=lambda x: x["time"], reverse=True)
    return out[:_NEWS_FETCH_N]


def _news_from_ticker(sym: str) -> list:
    tk = _yf().Ticker(sym)
    return parse_yf_news(_retry(lambda: tk.news) or [], "yfinance")


def _news_from_search(sym: str) -> list:
    return parse_yf_news(_yf().Search(sym, max_results=1, news_count=_NEWS_FETCH_N).news or [], "yahoo_search")


def _news_from_rss(url: str, source: str) -> list:
    import requests
    r = requests.get(url, headers={**_UA, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"},
                     timeout=(5, 10))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return parse_rss(r.content, source)


def _news_chain(sym: str) -> list:
    """(来源名, 调用, 是否走串行闸)。顺序 = 取用优先级。"""
    from urllib.parse import quote, quote_plus
    y_url = _YAHOO_RSS_URL.format(sym=quote(sym))
    g_url = _GOOGLE_RSS_URL.format(q=quote_plus(f"{sym} stock when:7d"))
    return [("yfinance", lambda: _news_from_ticker(sym), False),
            ("yahoo_search", lambda: _news_from_search(sym), True),
            ("yahoo_rss", lambda: _news_from_rss(y_url, "yahoo_rss"), True),
            ("google_rss", lambda: _news_from_rss(g_url, "google_rss"), True)]


def _news_fetch(sym: str) -> list:
    for name, fn, serial in _news_chain(sym):
        with _NEWS_LOCK:
            if _NEWS_FAILS[name] >= _NEWS_BREAK_AFTER:
                continue                               # 这一家本轮已熔断
        items = []
        try:
            if serial:
                with _NEWS_GATE:
                    wait = _NEWS_GAP_S - (time.monotonic() - _NEWS_LAST_CALL[0])
                    if wait > 0:
                        time.sleep(wait)
                    try:
                        items = _hard_deadline(fn, _NEWS_DEADLINE_S) or []
                    finally:
                        _NEWS_LAST_CALL[0] = time.monotonic()
            else:
                items = _hard_deadline(fn, _NEWS_DEADLINE_S) or []
        except Exception as e:                        # noqa: BLE001 —— 新闻永远不许打断流水线
            log.debug("news %s via %s 失败: %s", sym, name, e)
            items = []
        with _NEWS_LOCK:
            if items:
                _NEWS_FAILS[name] = 0
            else:
                _NEWS_FAILS[name] += 1
                if _NEWS_FAILS[name] == _NEWS_BREAK_AFTER:
                    log.warning("新闻源 %s 连续 %d 只票没取到, 本轮不再问它 (改走后面的兜底源)", name, _NEWS_BREAK_AFTER)
        if items:
            return items
    return []


def news_items(code: str) -> list:
    """这只票的新闻 (统一结构, 多源兜底)。同一进程内每只票只取一次 (空结果也记住: 四家都问过了);
    非空结果另有当日文件缓存 (键 news3; 旧键 news2 是带 tone 的旧结构, 不混用)。永不抛异常。"""
    sym = _yf_symbol(code)
    with _NEWS_LOCK:
        if sym in _NEWS_MEMO:
            return _NEWS_MEMO[sym]
    items = None
    try:
        key = _cache_key("news3", sym, dt.date.today().isoformat())
        c = _cache_load(key)
        if isinstance(c, list) and c:
            items = c
        else:
            items = _news_fetch(sym)
            if items:
                _cache_save(key, items)
    except Exception as e:                            # noqa: BLE001
        log.debug("news_items %s 失败: %s", code, e)
        items = items or []
    with _NEWS_LOCK:
        _NEWS_MEMO[sym] = items
    return items


def news_round_stats() -> dict:
    """本进程到目前为止的新闻取数统计 (写进 meta.news_stats, 出事时看是哪一家在供数、哪一家熔断了)。"""
    with _NEWS_LOCK:
        memo = dict(_NEWS_MEMO)
        tripped = [p for p in _NEWS_PROVIDERS if _NEWS_FAILS[p] >= _NEWS_BREAK_AFTER]
    by: dict = {}
    for items in memo.values():
        if items:
            s = items[0].get("source") or "?"
            by[s] = by.get(s, 0) + 1
    return {"fetched": len(memo), "with_news": sum(1 for v in memo.values() if v),
            "items": sum(len(v) for v in memo.values()), "by_source": by, "tripped": tripped}


def reset_news_round() -> None:
    """清掉本进程的新闻记忆与熔断计数 (用例用; 流水线每天是新进程, 不需要调)。"""
    with _NEWS_LOCK:
        _NEWS_MEMO.clear()
        for p in _NEWS_PROVIDERS:
            _NEWS_FAILS[p] = 0
    _NEWS_LAST_CALL[0] = 0.0


def fetch_news(code: str, limit: int = 12) -> list:
    """个股新闻 (标题/来源/时间/链接), 关键词法粗分 利好/利空/中性。取数见 news_items (多源兜底); source 标明走的哪一家。"""
    out = []
    try:
        for it in news_items(code)[:limit]:
            out.append({"title": it["title"], "publisher": it.get("publisher") or "—",
                        "time": it.get("time") or "—", "url": it.get("url") or "#",
                        "tone": _news_tone(it["title"]), "source": it.get("source")})
    except Exception as e:
        log.debug("fetch_news %s 失败: %s", code, e)
        out = []
    return out


def fetch_options_summary(code: str) -> dict:
    """最近到期日的期权链概览: Put/Call 持仓比与成交比 / 最大痛点价 / 总持仓。"""
    key = _cache_key("opts", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    out = {}
    try:
        tk = _yf().Ticker(_yf_symbol(code))
        exps = _retry(lambda: tk.options)
        if exps:
            exp = exps[0]
            ch = _retry(tk.option_chain, exp)
            calls, puts = ch.calls, ch.puts
            c_oi = float(calls["openInterest"].fillna(0).sum())
            p_oi = float(puts["openInterest"].fillna(0).sum())
            c_vol = float(calls["volume"].fillna(0).sum())
            p_vol = float(puts["volume"].fillna(0).sum())
            # 最大痛点: 令期权买方总收益最小的到期价
            strikes = sorted(set(calls["strike"]) | set(puts["strike"]))
            best_p, best_pain = None, None
            cs = calls[["strike", "openInterest"]].fillna(0).values
            ps = puts[["strike", "openInterest"]].fillna(0).values
            for s in strikes:
                pain = float(sum(oi * max(0.0, s - k) for k, oi in cs)
                             + sum(oi * max(0.0, k - s) for k, oi in ps))
                if best_pain is None or pain < best_pain:
                    best_pain, best_p = pain, s
            out = {"expiry": str(exp),
                   "pc_oi": round(p_oi / c_oi, 2) if c_oi > 0 else None,
                   "pc_vol": round(p_vol / c_vol, 2) if c_vol > 0 else None,
                   "call_oi": int(c_oi), "put_oi": int(p_oi),
                   "max_pain": best_p}
    except Exception as e:
        log.debug("fetch_options_summary %s 失败: %s", code, e)
        out = {}
    if out:
        _cache_save(key, out)
    return out


def fetch_finra_short_volume() -> dict:
    """FINRA RegSHO 日度场外(含暗池)成交数据 — 全市场一个文件。
    返回 {ticker: {"short_pct": 空头成交占比%, "date": 数据日}}。
    这是公开数据里最接近"暗池情绪"的代理指标 (逐笔暗池数据无免费源)。"""
    key = _cache_key("finrasv", dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    import requests
    out = {}
    for back in range(1, 8):          # 从昨天起往回找最近一个交易日文件
        d = dt.date.today() - dt.timedelta(days=back)
        url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
        try:
            r = _retry(requests.get, url, headers=_UA,
                       timeout=CONFIG["fetch"]["timeout_sec"])
            if r.status_code != 200 or "|" not in r.text[:200]:
                continue
            for line in r.text.splitlines()[1:]:
                p = line.split("|")
                if len(p) < 5:
                    continue
                try:
                    sv, tv = float(p[2]), float(p[4])
                except Exception:
                    continue
                if tv > 0:
                    out[p[1]] = {"short_pct": round(sv / tv * 100.0, 1),
                                 "date": f"{d:%Y-%m-%d}"}
            break
        except Exception as e:
            log.debug("FINRA %s 失败: %s", url, e)
            continue
    if out:
        _cache_save(key, out)
    return out


# ===========================================================================
#  4) 板块指数 (SPDR 行业 ETF) / 基准 (SPY)
# ===========================================================================
def fetch_sector_hist(sector: str) -> pd.DataFrame | None:
    etf = CONFIG["source"]["sector_etf"].get(sector)
    if not etf:
        return None
    return fetch_hist(etf)


def fetch_benchmark() -> pd.DataFrame | None:
    return fetch_hist(CONFIG["source"]["benchmark"])
