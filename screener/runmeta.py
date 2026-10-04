#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runmeta — 跑批当时才知道、要原样带进看板 meta 的东西 (2026-10-04 卡 A-HOLIDAY)
==============================================================================
看板原来拿 data_date 与浏览器当天比自然日, >4 天就亮红条「定时任务可能失败」, 不认交易日历: 美股周一休市的
周三凌晨会误亮 (周二跑批拿到的仍是上周五的收盘)。A 股那边流水线写 last_closed_day / next_open_day /
market_status 三个字段给看板分清「休市」与「没跑」; **美股没有交易日历** —— 代码里没有 NYSE 静态表, 这张卡
明确不引新依赖 (不上 pandas_market_calendars) —— 所以这里只写**不靠日历也确定**的那一个状态:

  market_status    跑批那一刻纽约日历是周六日 -> 'weekend'; 工作日 -> None (开市还是节假日, 没日历判不了)
  last_closed_day  None
  next_open_day    None

**为什么不按工作日近似去填前两个**: 看板拿 `data_date < last_closed_day` 判红条「数据落后于应到交易日」,
近似会把感恩节 / MLK 这类工作日休市当成开市日, 正好把要消掉的误报写回去。字段是 None 时看板走回退判断
(跑批新鲜度 + 「数据日是否落后于跑批那天应到的工作日」), 那条路不需要日历, 见 dashboard/index.html 的 FV-FRESH 块。

`run_sources`: 这一轮用的数据源留痕 -> meta.sources (页头「数据源」标签从这里读)。两个函数都**绝不抛**。
"""
from __future__ import annotations
import datetime as dt
import logging

from .config import CONFIG

log = logging.getLogger("screener.runmeta")

CALENDAR_KEYS = ("last_closed_day", "next_open_day", "market_status")
#: all_us 名单 (NASDAQ 官方筛选器) 正常 4000+ 只; 低于这个数说明当天退回了标普500 / 内置兜底名单
#: (与 datasource.get_universe 里「只缓存看起来完整的名单」用的是同一条线)
ALL_US_MIN_ROWS = 1500


def ny_date(now: dt.datetime | None = None) -> dt.date:
    """这一刻的纽约日历日。zoneinfo 不可用 (Windows 没装 tzdata) 时退到固定 UTC−5: 夏令时差一小时只在
    纽约零点前后一小时内才会差一天, 而跑批在柏林早上 = 纽约凌晨 2-3 点, 碰不到。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()                                  # 朴素时间按本机时区解释
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:                                           # noqa: BLE001
        return now.astimezone(dt.timezone(dt.timedelta(hours=-5))).date()


def calendar_fields(now: dt.datetime | None = None) -> dict:
    """-> {last_closed_day: None, next_open_day: None, market_status: 'weekend' | None}。绝不抛。"""
    out = {k: None for k in CALENDAR_KEYS}
    try:
        if ny_date(now).weekday() >= 5:
            out["market_status"] = "weekend"
    except Exception as e:                                      # noqa: BLE001
        log.warning("market_status 没算出来 (写 null, 看板回退): %s", str(e)[:120])
    log.info("交易日历字段: last_closed_day=None next_open_day=None market_status=%s (美股无日历, 只判周末)",
             out["market_status"])
    return out


def run_sources(n_universe: int | None = None) -> dict:
    """这一轮的数据源留痕 -> meta.sources。日线 / 基本面 / 板块都来自 yfinance; 股票池看模式与实际只数:
    all_us 且名单完整 = NASDAQ 筛选器, 否则 = 标普500 成分 (筛选器失败时 get_universe 会静默退回)。绝不抛。"""
    out = {"bars": "yfinance", "valuation": "yfinance", "industry_basis": "GICS", "universe": None, "benchmark": None}
    try:
        src = CONFIG["source"]
        mode = str(src.get("universe_mode") or "")
        full = n_universe is None or int(n_universe) > ALL_US_MIN_ROWS
        out["universe"] = "nasdaq_screener" if (mode == "all_us" and full) else "sp500"
        out["benchmark"] = src.get("benchmark")
    except Exception as e:                                      # noqa: BLE001
        log.debug("数据源留痕失败 (不影响出榜): %s", e)
    return out
