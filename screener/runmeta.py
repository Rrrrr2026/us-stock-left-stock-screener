#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runmeta — 跑批当时才知道、要原样带进看板 meta 的东西 (2026-10-04 卡 A-HOLIDAY; 2026-10-12 卡 US-CAL-TRIPLE 填上美股日历)
=======================================================================================================================
看板原来拿 data_date 与浏览器当天比自然日, >4 天就亮红条「定时任务可能失败」, 不认交易日历: 美股周一休市的
周三凌晨会误亮 (周二跑批拿到的仍是上周五的收盘)。与 A 股看板同名的三个字段让看板分清「休市」与「没跑」
(dashboard/index.html 的 FV-FRESH 块, 日历模式):

  last_closed_day  已收盘的最后一个交易日 (纽约 16:00 后当天算; 跑批在柏林 08:30 = 纽约 02:30, 所以是昨天或更早)
  next_open_day    last_closed_day 之后的第一个开市日 = 下一个会出新收盘的交易日 (工作日早上跑 = 当天)
  market_status    跑批那一刻纽约日历那天: 'open_day' | 'holiday' | 'weekend' (与 A 股 ashare/datadate.py 同一套枚举)

卡 A-HOLIDAY (10-04) 时美股没有交易日历, 三个字段只写周末; 10-12 起**内置 NYSE 日历** (服务器 venv 没装
pandas_market_calendars / exchange_calendars, 10-12 ssh 只读核过; 不引新依赖):

  · `NYSE_CLOSED`  2025–2028 固定休市表, 是 stock-core research/data_catalog.py::NYSE_CLOSED 的**逐条副本**
    (含 Good Friday、observed 挪日 与 2025-01-09 国葬临时休市); 覆盖年内以这张表为准。
  · `nyse_holidays_by_rule(year)`  NYSE 十条规则 (元旦 / MLK / 总统日 / 耶稣受难日 / 阵亡将士日 / 六月节 /
    独立日 / 劳动节 / 感恩节 / 圣诞, 周六提前到周五、周日顺延到周一、元旦落周六不补休) + `NYSE_SPECIAL_CLOSED`
    (规则推不出的临时休市); 覆盖年之外 (2029+) 用它, 字段照写、日志标「规则推算」。
  · 周末不靠表也确定。

**维护点 (每年年末一次)**: NYSE 公布下一年假日表后, 核 `nyse_holidays_by_rule(年)` 与官网一致 → 把那一年并进
`NYSE_CLOSED` 并把 `NYSE_TABLE_YEARS` 上限 +1; 临时休市 (国葬 / 风暴) 公告当天加进 `NYSE_SPECIAL_CLOSED` 与
`NYSE_CLOSED` (两处同加), stock-core 那张表同步。tests/test_export_meta.py 守着三件事: 规则 ∪ 临时休市 == 固定表
(2025–2028 逐年), 固定表 == stock-core 的表 (旁边有 stock-core 检出时), 覆盖年之外三个字段仍非 None。

已知风险 (与数据总览同一条): 一次没登记进表的临时休市当天, 表说开市 → 次日跑批写出的 last_closed_day 是那天,
而行情里没有那天的 bar → `data_date_check` 打一行 WARNING、看板亮一天红条「数据落后于应到交易日」, 再下一轮
自动消失 (last_closed_day 前移到真有 bar 的那天)。

`run_sources`: 这一轮用的数据源留痕 -> meta.sources (页头「数据源」标签从这里读)。本模块的入口函数都**绝不抛**。
"""
from __future__ import annotations
import datetime as dt
import logging

from .config import CONFIG

log = logging.getLogger("screener.runmeta")

CALENDAR_KEYS = ("last_closed_day", "next_open_day", "market_status")
NY_CLOSE_HOUR = 16                       # NYSE 16:00 收盘 (半日市 13:00 收, 仍是交易日, 不影响这里)
#: 往前找最后收盘日 / 往后找下一个开市日, 最多走多少个自然日。NYSE 连续休市最长也就周末接一两个假日 (≤ 5 天);
#: 12 与 stock-core data_catalog.us_prev_open_day 的回看步数相同。
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
NYSE_TABLE_YEARS = (2025, 2028)
#: 规则推不出的临时休市 (已核对的年份这里与 NYSE_CLOSED 两处同加): 2025-01-09 卡特总统国葬日。
NYSE_SPECIAL_CLOSED = {"2025-01-09"}

#: all_us 名单 (NASDAQ 官方筛选器) 正常 4000+ 只; 低于这个数说明当天退回了标普500 / 内置兜底名单
#: (与 datasource.get_universe 里「只缓存看起来完整的名单」用的是同一条线)
ALL_US_MIN_ROWS = 1500


# ---------------------------------------------------------------- NYSE 日历 (纯函数, 不碰 IO)
def _easter(year: int) -> dt.date:
    """西方复活节 (Anonymous Gregorian algorithm)。耶稣受难日 = 复活节前两天。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """某月第 n 个星期 weekday (0 = 周一)。"""
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: dt.date, new_year: bool = False) -> dt.date | None:
    """固定日期假日的 NYSE 挪日: 周六 -> 前一个周五, 周日 -> 下一个周一; 元旦落周六不补休 (上一年 12-31 照常开市)。"""
    if d.weekday() == 5:
        return None if new_year else d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def nyse_holidays_by_rule(year: int) -> set[str]:
    """按 NYSE 规则推算的某年休市日 (不含临时休市)。六月节 2022 年起才有。"""
    days = [
        _observed(dt.date(year, 1, 1), new_year=True),           # New Year's Day
        _nth_weekday(year, 1, 0, 3),                             # Martin Luther King Jr. Day: 1 月第 3 个周一
        _nth_weekday(year, 2, 0, 3),                             # Washington's Birthday: 2 月第 3 个周一
        _easter(year) - dt.timedelta(days=2),                    # Good Friday
        _last_weekday(year, 5, 0),                               # Memorial Day: 5 月最后一个周一
        _observed(dt.date(year, 6, 19)) if year >= 2022 else None,   # Juneteenth
        _observed(dt.date(year, 7, 4)),                          # Independence Day
        _nth_weekday(year, 9, 0, 1),                             # Labor Day: 9 月第 1 个周一
        _nth_weekday(year, 11, 3, 4),                            # Thanksgiving: 11 月第 4 个周四
        _observed(dt.date(year, 12, 25)),                        # Christmas
    ]
    return {d.isoformat() for d in days if d is not None}


def nyse_closed_days(year: int) -> set[str]:
    """某年的 NYSE 休市日: 固定表覆盖年用表 (已核对), 之外用规则 + 临时休市表。"""
    if NYSE_TABLE_YEARS[0] <= year <= NYSE_TABLE_YEARS[1]:
        return {d for d in NYSE_CLOSED if d.startswith("%04d-" % year)}
    return nyse_holidays_by_rule(year) | {d for d in NYSE_SPECIAL_CLOSED if d.startswith("%04d-" % year)}


def is_open_day(d: dt.date) -> bool:
    """NYSE 那天开不开市 (周一至周五且不在休市日里)。"""
    return d.weekday() < 5 and d.isoformat() not in nyse_closed_days(d.year)


def by_table(d: dt.date) -> bool:
    return NYSE_TABLE_YEARS[0] <= d.year <= NYSE_TABLE_YEARS[1]


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
    """这一刻的纽约日历日。"""
    return ny_now(now).date()


def _walk(d: dt.date, step: int) -> dt.date | None:
    """从 d 起按 step (±1 天) 走, 返回第一个开市日; LOOK_DAYS 天内没有 -> None (连续休市超过两周, 不该发生)。"""
    for _ in range(LOOK_DAYS):
        if is_open_day(d):
            return d
        d += dt.timedelta(days=step)
    return None


def calendar_fields(now: dt.datetime | None = None) -> dict:
    """-> {last_closed_day, next_open_day, market_status}, 定义见模块说明。纯函数 (时钟注入), 绝不抛。"""
    out = {k: None for k in CALENDAR_KEYS}
    basis = "?"
    try:
        ny = ny_now(now)
        today = ny.date()
        cand = today if ny.hour >= NY_CLOSE_HOUR else today - dt.timedelta(days=1)
        last_closed = _walk(cand, -1)
        next_open = _walk(last_closed + dt.timedelta(days=1), +1) if last_closed else None
        if today.weekday() >= 5:
            status = "weekend"
        elif is_open_day(today):
            status = "open_day"
        else:
            status = "holiday"
        out["last_closed_day"] = last_closed.isoformat() if last_closed else None
        out["next_open_day"] = next_open.isoformat() if next_open else None
        out["market_status"] = status
        if all(by_table(x) for x in (today, last_closed or today, next_open or today)):
            basis = "固定表 %d-%d" % NYSE_TABLE_YEARS
        else:
            basis = "规则推算, 超出固定表 %d-%d —— 年末按模块说明的维护点核对" % NYSE_TABLE_YEARS
        if last_closed is None or next_open is None:
            log.warning("交易日历字段: %d 天内找不到开市日 (last_closed=%s next_open=%s), 缺的写 null, 看板回退",
                        LOOK_DAYS, last_closed, next_open)
    except Exception as e:                                      # noqa: BLE001
        log.warning("交易日历字段没算出来 (三个字段写 null, 看板回退): %s", str(e)[:120])
        out = {k: None for k in CALENDAR_KEYS}
    log.info("交易日历字段: last_closed_day=%s next_open_day=%s market_status=%s (内置 NYSE 日历: %s)",
             out["last_closed_day"], out["next_open_day"], out["market_status"], basis)
    return out


def data_date_check(data_date, cal: dict | None) -> str | None:
    """跑批算出的行情日 (基准指数末日) 与日历的最后收盘日交叉核对 -> None (一致 / 判不了) 或一句 WARNING 文案。
    不改 data_date (它是实情, 日历只是期望); 绝不抛。"""
    try:
        dd = str(data_date)[:10] if data_date else None
        lc = (cal or {}).get("last_closed_day")
        if not dd or not isinstance(lc, str) or len(dd) != 10:
            return None
        if dd < lc:
            return ("data_date %s 早于 NYSE 日历的最后收盘日 %s —— 行情源还没出 %s 的收盘 (yfinance 延迟), 或那天是"
                    "表里没登记的临时休市; 看板会亮「数据落后于应到交易日」直到下一轮" % (dd, lc, lc))
        if dd > lc:
            return ("data_date %s 晚于 NYSE 日历的最后收盘日 %s —— 休市表把开市日记成休市 (核 NYSE_CLOSED), 或跑批"
                    "时钟不对" % (dd, lc))
    except Exception as e:                                      # noqa: BLE001
        log.debug("data_date 交叉核对失败 (忽略): %s", e)
    return None


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
