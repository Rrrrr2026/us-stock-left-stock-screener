#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runmeta — 跑批当时才知道、要原样带进看板 meta 的东西 (2026-10-04 卡 A-HOLIDAY)
==============================================================================
看板原来拿 data_date 与浏览器当天比自然日, >4 天就亮红条「定时任务可能失败」, 不认交易日历: 美股周一休市的
周三凌晨会误亮 (周二跑批拿到的仍是上周五的收盘)。与 A 股看板同名的三个字段让看板分清「休市」与「没跑」:

  last_closed_day  已收盘的最后一个交易日 (纽约 16:00 后当天算)
  next_open_day    last_closed_day 之后的第一个开市日 = 下一个会出新收盘的交易日
  market_status    跑批那一刻纽约日历那天: 'open_day' | 'holiday' | 'weekend'

**日历 = 静态 NYSE 休市表 `NYSE_CLOSED`** (2025-2028, 含 Good Friday 与周末顺延), 是 stock-core
`research/data_catalog.py` 里那张手抄表的**逐条副本** —— 不引新依赖 (不上 pandas_market_calendars), 也不让筛选器仓
去 import 研究脚本; 两处同表由 tests/test_export_meta.py 在旁边有 stock-core 检出时逐条比对, 续表 (那边文件顶部的
TODO(2028-06)) 时两处一起改。

**只写表给得出的精确值, 给不出一律 None**: 超出覆盖年 (2029+) 三个日子全是 None (周末除外 —— 周六日不靠表也确定),
看板自动回退到「跑批新鲜度 + 数据日是否落后于跑批那天应到的工作日」那条不需要日历的判断 (dashboard/index.html 的
FV-FRESH 块)。绝不按工作日近似去填: 看板拿 `data_date < last_closed_day` 判红条「数据落后于应到交易日」, 近似会把
感恩节 / MLK 这类工作日休市当成开市日, 正好把要消掉的误报写回去。

已知风险 (与数据总览同一条, 那边的说明更全): 一次没登记进表的**临时休市** (风暴 / 国葬) 当天, 表说开市 -> 次日跑批
写出的 last_closed_day 是那天, 而行情里没有那天的 bar -> 看板亮一天红条「数据落后于应到交易日」, 再下一轮自动消失。

`run_sources`: 这一轮用的数据源留痕 -> meta.sources (页头「数据源」标签从这里读)。两个函数都**绝不抛**。
"""
from __future__ import annotations
import datetime as dt
import logging

from .config import CONFIG

log = logging.getLogger("screener.runmeta")

CALENDAR_KEYS = ("last_closed_day", "next_open_day", "market_status")
NY_CLOSE_HOUR = 16                       # NYSE 16:00 收盘 (半日市 13:00 收, 仍是交易日, 不影响这里)
#: 往前找最后收盘日 / 往后找下一个开市日, 最多走多少个自然日。NYSE 连续休市最长也就周末接一两个假日 (≤ 5 天),
#: 12 与 data_catalog.us_prev_open_day 的回看步数相同。
LOOK_DAYS = 12

# 静态 NYSE 休市表 —— stock-core research/data_catalog.py::NYSE_CLOSED 的逐条副本 (同步由用例守着, 见模块说明)。
NYSE_CLOSED = {
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04",
    "2028-09-04", "2028-11-23", "2028-12-25",
}
NYSE_YEARS = (2025, 2028)

#: all_us 名单 (NASDAQ 官方筛选器) 正常 4000+ 只; 低于这个数说明当天退回了标普500 / 内置兜底名单
#: (与 datasource.get_universe 里「只缓存看起来完整的名单」用的是同一条线)
ALL_US_MIN_ROWS = 1500


def ny_now(now: dt.datetime | None = None) -> dt.datetime:
    """这一刻的纽约墙钟 (带时区)。zoneinfo 不可用 (Windows 没装 tzdata) 时退到固定 UTC−5: 夏令时差一小时只在
    纽约零点 / 16 点前后一小时内才会判错, 而跑批在柏林早上 = 纽约凌晨 2-3 点, 碰不到。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()                                  # 朴素时间按本机时区解释
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:                                           # noqa: BLE001
        return now.astimezone(dt.timezone(dt.timedelta(hours=-5)))


def ny_date(now: dt.datetime | None = None) -> dt.date:
    return ny_now(now).date()


def is_open_day(d: dt.date) -> bool | None:
    """NYSE 那天开不开市。None = 超出静态表覆盖年, 判不了 (调用方写 None, 不近似)。"""
    if not (NYSE_YEARS[0] <= d.year <= NYSE_YEARS[1]):
        return None
    return d.weekday() < 5 and d.isoformat() not in NYSE_CLOSED


def calendar_fields(now: dt.datetime | None = None) -> dict:
    """-> {last_closed_day, next_open_day, market_status}, 定义见模块说明。纯函数 (时钟注入), 绝不抛。"""
    out = {k: None for k in CALENDAR_KEYS}
    try:
        ny = ny_now(now)
        today = ny.date()
        if today.weekday() >= 5:
            out["market_status"] = "weekend"                    # 周六日不靠表也确定
        cand = today if ny.hour >= NY_CLOSE_HOUR else today - dt.timedelta(days=1)
        last_closed, d = None, cand
        for _ in range(LOOK_DAYS):
            o = is_open_day(d)
            if o is None:                                       # 走出了覆盖年: 判不了
                break
            if o:
                last_closed = d
                break
            d -= dt.timedelta(days=1)
        if last_closed is not None:
            out["last_closed_day"] = last_closed.isoformat()
            d = last_closed + dt.timedelta(days=1)
            for _ in range(LOOK_DAYS):
                o = is_open_day(d)
                if o is None:
                    break
                if o:
                    out["next_open_day"] = d.isoformat()
                    break
                d += dt.timedelta(days=1)
        o = is_open_day(today)
        if o is True:
            out["market_status"] = "open_day"
        elif o is False and today.weekday() < 5:
            out["market_status"] = "holiday"
    except Exception as e:                                      # noqa: BLE001
        log.warning("交易日历字段没算出来 (三个字段写 null, 看板回退): %s", str(e)[:120])
        out = {k: None for k in CALENDAR_KEYS}
    log.info("交易日历字段: last_closed_day=%s next_open_day=%s market_status=%s (静态 NYSE 休市表 %d-%d)",
             out["last_closed_day"], out["next_open_day"], out["market_status"], NYSE_YEARS[0], NYSE_YEARS[1])
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
