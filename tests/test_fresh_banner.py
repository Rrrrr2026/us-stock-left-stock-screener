#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板「数据新鲜度提示条」+ 日期显示统一数据日 (2026-10-04 卡 A-HOLIDAY) —— 离线, 零联网
=====================================================================================
病灶: dashboard/index.html 原来拿 data_date 与浏览器当天比自然日, >4 天就亮红条「定时任务可能失败, 请检查
data/update.log」。不认交易日历, 还吃浏览器时区: 国庆休市 10-01..10-07 流水线每个工作日照跑、data_date 停在
09-30, 红条从 10-05 00:00 起会一直误亮到 10-08 中午 (美股周一休市的周三凌晨同样)。

这里锁四件事:
  ① 判定是页面里的纯函数 `FVF.freshness(meta, nowMs, cfg)` (FV-FRESH 块)。同一张用例表, Python 镜像实现
     (`py_freshness`, 永远跑) 与页面里的 JS (无头 Chromium; 没有 playwright / 起不来就 skip) 各跑一遍, 结论逐字段
     相同; JS 那一遍在 北京 / 柏林 / 纽约 三个浏览器时区下各跑一次 —— 结论不许随浏览器时区变。
  ② FV-FRESH 块的 sha256 钉死: a-share / us 两仓 dashboard/index.html 里这一块逐字相同 (两仓的本文件各钉同一个
     值)。改块 = 两仓一起改 + 两个常量一起改 + 下面的 Python 镜像一起改。
  ③ 整页渲染 (小夹具站点 + 固定时钟, 外网全掐): 长假中是中性条不是红条; 跑批停 4 天是红条; 回看历史快照不叠
     提示条; 手机页头并排「数据日 / 跑批」; 日期选择器 / 优质榜 / 回测标题显示数据日 (跑批日不同时加「跑批 MM-DD」),
     共用脚本重渲染 (点排序) 之后仍是数据日; 页头「数据源」从 meta 读。
  ④ 旧逻辑复刻 (`old_stale_age`) 在用例表里那几个时刻确实会亮 / 确实随浏览器时区变 —— 证明表里考的正是当初
     会误报的时刻, 不是凑出来的绿。

用例里的时钟全部是**注入的** (纯函数的 nowMs / 页面的固定时钟), 不读真实的今天, 所以日期可以写死。

**本文件「③ 整页渲染」之前的部分 (用例表 / Python 镜像 / 纯函数与 JS 对表) 与 a-share 仓 tests/test_fresh_banner.py
逐字相同** (只差 MARKET 一行) —— FV-FRESH 块两仓同一份, 判定也就只有一张表; 改表两仓一起改。本仓多出来的是
美股文案的整页用例: 周末 / 周一休市的周三凌晨 / 跑批停 4 天 / 数据源标签 / 日期显示。
运行: python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_fresh_banner.py -q
"""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import http.server
import json
import math
import os
import re
import shutil
import threading

import pytest

try:
    from zoneinfo import ZoneInfo
    ZoneInfo("Europe/Berlin"), ZoneInfo("Asia/Shanghai"), ZoneInfo("America/New_York")
except Exception:                                              # noqa: BLE001 —— Windows 没装 tzdata
    pytest.skip("zoneinfo / tzdata 不可用 (pip install tzdata)", allow_module_level=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "dashboard", "index.html")
SHARED_JS = os.path.join(ROOT, "dashboard", "leftside_shared.js")

#: FV-FRESH 块 (LF 归一后) 的 sha256。**两仓 (a-share / us) 的 tests/test_fresh_banner.py 里是同一个值。**
FRESH_SHA256 = "2656fbcac2a2a04bc2f94d6703bf58d802304cdeebf374c76308c20ec4b79535"

MARKET = "us"                                                  # 本仓页面: 美股
CFG = {
    "a": {"tzRun": "Europe/Berlin", "tzMkt": "Asia/Shanghai", "staleRunDays": 3, "lagDays": 0},
    "us": {"tzRun": "Europe/Berlin", "tzMkt": "America/New_York", "staleRunDays": 3, "lagDays": 1},
}
BROWSER_TZS = ("Asia/Shanghai", "Europe/Berlin", "America/New_York")


def page_src() -> str:
    return open(PAGE, encoding="utf-8", newline="").read().replace("\r\n", "\n")


def fresh_block() -> str:
    m = re.search(r"^  // ===== FV-FRESH BEGIN =====\n.*?^  // ===== FV-FRESH END =====\n", page_src(), re.S | re.M)
    assert m, "dashboard/index.html 里找不到 FV-FRESH 块"
    return m.group(0)


# =========================================================================== Python 镜像 (与 FV-FRESH 块逐条对应)
def _ymd(s):
    return s[:10] if isinstance(s, str) and re.match(r"^\d{4}-\d{2}-\d{2}", s) else None


def _add(d: str, n: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def _diff(a: str, b: str) -> int:
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def _weekend(d: str) -> bool:
    return dt.date.fromisoformat(d).weekday() >= 5


def _today(now: dt.datetime, tz: str) -> str:
    return now.astimezone(ZoneInfo(tz)).date().isoformat()


def py_freshness(meta, now: dt.datetime, cfg: dict) -> dict:
    meta, cfg = meta or {}, cfg or {}
    tz_run = cfg.get("tzRun") or "Europe/Berlin"
    tz_mkt = cfg.get("tzMkt") or tz_run
    stale = cfg["staleRunDays"] if isinstance(cfg.get("staleRunDays"), (int, float)) else 3
    lag = cfg["lagDays"] if isinstance(cfg.get("lagDays"), (int, float)) else 0
    ua = meta.get("updated_at") if isinstance(meta.get("updated_at"), str) else ""
    data_date = _ymd(meta.get("data_date")) or _ymd(meta.get("run_date"))
    run_day = _ymd(ua) or _ymd(meta.get("run_date"))
    out = {"level": "none", "code": "ok", "why": None, "mode": "fallback", "dataDate": data_date, "runDay": run_day,
           "runAt": ((ua[5:16] if (_ymd(ua) and len(ua) >= 16) else run_day[5:]) if run_day else None),
           "ageDays": None, "lastClosed": None, "nextOpen": None, "expectDay": None}
    if not data_date or not run_day:
        out["code"] = "no_meta"
        return out

    def ret(level, code, why=None):
        out.update(level=level, code=code, why=why)
        return out
    today_run, today_mkt = _today(now, tz_run), _today(now, tz_mkt)
    out["ageDays"] = _diff(today_run, run_day)
    if out["ageDays"] > stale:
        return ret("alert", "run_stale")
    lc, no = _ymd(meta.get("last_closed_day")), _ymd(meta.get("next_open_day"))
    if lc:
        out["mode"], out["lastClosed"] = "calendar", lc
        out["nextOpen"] = no if (no and no > lc) else None
        if data_date < lc:
            out["expectDay"] = lc
            return ret("alert", "data_behind")
        if out["nextOpen"]:
            if data_date < no and _diff(today_run, no) > lag:
                out["expectDay"] = no
                return ret("alert", "data_overdue")
            if lc < today_mkt < no:
                d, hol = _add(lc, 1), False
                while d < no:
                    if not _weekend(d):
                        hol = True
                        break
                    d = _add(d, 1)
                return ret("info", "closed", "holiday" if hol else "weekend")
            if today_mkt >= no and data_date < no and _diff(no, data_date) > 1:
                return ret("info", "await")
            return out
        if today_mkt > lc and _weekend(today_mkt):
            return ret("info", "closed", "weekend")
        return out
    expect = _add(run_day, -lag)
    while _weekend(expect):
        expect = _add(expect, -1)
    if data_date < expect:
        return ret("info", "closed", "no_new_day")
    if _weekend(today_mkt):
        return ret("info", "closed", "weekend")
    return out


def py_run_label(run_date, mp, live, lag) -> dict:
    rd = _ymd(run_date)
    if not rd:
        return {"data": "" if run_date is None else str(run_date), "note": ""}
    lag = lag if isinstance(lag, (int, float)) else 0
    has_map = isinstance(mp, dict) and len(mp) > 0
    dd = _ymd(mp.get(rd)) if has_map else None
    if not has_map and lag == 0 and live:
        ld, lr = _ymd(live.get("data_date")), _ymd(live.get("run_date"))
        if ld and lr and ld < rd <= lr:
            dd = ld
    if not dd or dd > rd:
        return {"data": rd, "note": ""}
    return {"data": dd, "note": "" if rd == _add(dd, lag) else rd[5:]}


def old_stale_age(data_date: str, now: dt.datetime, browser_tz: str) -> int:
    """旧页面那一行: floor((Date.now() − new Date(dd+"T00:00:00")) / 1 天) —— `new Date` 按**浏览器本地**零点解析; >4 亮红条。"""
    midnight = dt.datetime.fromisoformat(data_date + "T00:00:00").replace(tzinfo=ZoneInfo(browser_tz))
    return math.floor((now - midnight).total_seconds() / 86400)


# =========================================================================== 用例表
def T(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso)


# 今晚服务器上的真数据 (10-02 周五跑的, 没有交易日历字段): 数据日 09-30
A_1002 = {"run_date": "2026-10-02", "data_date": "2026-09-30", "updated_at": "2026-10-02 12:09:05"}


def a_cal(run, data, lc, no, status, hm="12:50:11"):
    return {"run_date": run, "data_date": data, "updated_at": "%s %s" % (run, hm),
            "last_closed_day": lc, "next_open_day": no, "market_status": status}


A_1005_HOL = a_cal("2026-10-05", "2026-09-30", "2026-09-30", "2026-10-08", "holiday")
A_1007_HOL = a_cal("2026-10-07", "2026-09-30", "2026-09-30", "2026-10-08", "holiday")
A_1008_OK = a_cal("2026-10-08", "2026-10-08", "2026-10-08", "2026-10-09", "open_day")
A_1009_FRI = a_cal("2026-10-09", "2026-10-09", "2026-10-09", "2026-10-12", "open_day")
A_1012_MON = a_cal("2026-10-12", "2026-10-12", "2026-10-12", "2026-10-13", "open_day")
A_FRI_NOCAL = {"run_date": "2026-10-09", "data_date": "2026-10-09", "updated_at": "2026-10-09 12:50:00"}
US_SUN = {"run_date": "2026-10-04", "data_date": "2026-10-02", "updated_at": "2026-10-04 08:49:09"}
US_MON = {"run_date": "2026-10-05", "data_date": "2026-10-02", "updated_at": "2026-10-05 08:49:00"}

# (名字, 市场, meta, now, 期望 {level, code, why, mode, ...可选字段})
CASES = [
    # ---- 长假中, 跑批新鲜 / 数据旧 -> 中性条 (回退模式 = 今晚只发页面时的形态) ----
    ("a_fb_holiday_sunday_now", "a", A_1002, T("2026-10-04T16:30:00+02:00"),
     dict(level="info", code="closed", why="no_new_day", mode="fallback", ageDays=2, dataDate="2026-09-30", runAt="10-02 12:09")),
    ("a_fb_holiday_monday_0000_old_logic_turns_red_here", "a", A_1002, T("2026-10-05T00:00:30+02:00"),
     dict(level="info", code="closed", why="no_new_day", mode="fallback", ageDays=3)),
    ("a_fb_holiday_monday_2359", "a", A_1002, T("2026-10-05T23:59:00+02:00"),
     dict(level="info", code="closed", why="no_new_day", ageDays=3)),
    # ---- 跑批停 4 天 -> 红条 ----
    ("a_fb_run_stopped_4d", "a", A_1002, T("2026-10-06T00:00:30+02:00"),
     dict(level="alert", code="run_stale", why=None, ageDays=4)),
    ("a_fb_run_stopped_6d", "a", A_1002, T("2026-10-08T09:00:00+02:00"), dict(level="alert", code="run_stale", ageDays=6)),
    # 同一时刻三种写法 (UTC / 北京 / 纽约 偏移): 柏林日历 10-06 00:30 -> 红; 柏林 10-05 23:30 -> 中性
    ("a_fb_instant_berlin_1006_0030_as_utc", "a", A_1002, T("2026-10-05T22:30:00+00:00"), dict(level="alert", code="run_stale", ageDays=4)),
    ("a_fb_instant_berlin_1005_2330_as_bj", "a", A_1002, T("2026-10-06T05:30:00+08:00"), dict(level="info", code="closed", why="no_new_day", ageDays=3)),
    ("a_fb_instant_berlin_1005_2330_as_ny", "a", A_1002, T("2026-10-05T17:30:00-04:00"), dict(level="info", code="closed", why="no_new_day", ageDays=3)),
    # ---- 有交易日历字段 (流水线下一轮起写) ----
    ("a_cal_holiday_tuesday", "a", A_1005_HOL, T("2026-10-06T10:00:00+02:00"),
     dict(level="info", code="closed", why="holiday", mode="calendar", lastClosed="2026-09-30", nextOpen="2026-10-08")),
    ("a_cal_holiday_spans_weekend_still_holiday", "a",
     a_cal("2026-10-02", "2026-09-30", "2026-09-30", "2026-10-08", "holiday"), T("2026-10-03T12:00:00+02:00"),
     dict(level="info", code="closed", why="holiday", nextOpen="2026-10-08")),
    ("a_cal_holiday_last_minute_bj_1007_2359", "a", A_1007_HOL, T("2026-10-07T17:59:00+02:00"),
     dict(level="info", code="closed", why="holiday")),
    ("a_cal_reopen_bj_1008_0000_await", "a", A_1007_HOL, T("2026-10-07T18:00:30+02:00"),
     dict(level="info", code="await", why=None, nextOpen="2026-10-08")),
    ("a_cal_reopen_morning_await", "a", A_1007_HOL, T("2026-10-08T08:00:00+02:00"), dict(level="info", code="await")),
    ("a_cal_reopen_batch_late_same_day_still_await", "a", A_1007_HOL, T("2026-10-08T23:59:00+02:00"), dict(level="info", code="await")),
    ("a_cal_reopen_batch_missed_overdue", "a", A_1007_HOL, T("2026-10-09T00:00:30+02:00"),
     dict(level="alert", code="data_overdue", expectDay="2026-10-08", ageDays=2)),
    ("a_cal_after_batch_ok", "a", A_1008_OK, T("2026-10-08T14:00:00+02:00"), dict(level="none", code="ok", mode="calendar")),
    ("a_cal_data_behind_at_batch_time", "a", a_cal("2026-10-08", "2026-09-30", "2026-10-08", "2026-10-09", "open_day"),
     T("2026-10-08T14:00:00+02:00"), dict(level="alert", code="data_behind", expectDay="2026-10-08")),
    ("a_cal_weekend_saturday", "a", A_1009_FRI, T("2026-10-10T12:00:00+02:00"),
     dict(level="info", code="closed", why="weekend", nextOpen="2026-10-12")),
    ("a_cal_friday_evening_berlin_is_saturday_in_beijing", "a", A_1009_FRI, T("2026-10-09T18:30:00+02:00"),
     dict(level="info", code="closed", why="weekend")),
    ("a_cal_friday_afternoon_ok", "a", A_1009_FRI, T("2026-10-09T15:00:00+02:00"), dict(level="none", code="ok")),
    ("a_cal_monday_morning_await", "a", A_1009_FRI, T("2026-10-12T08:00:00+02:00"), dict(level="info", code="await", nextOpen="2026-10-12")),
    ("a_cal_monday_batch_missed_tuesday", "a", A_1009_FRI, T("2026-10-13T00:00:30+02:00"), dict(level="alert", code="run_stale", ageDays=4)),
    ("a_cal_tuesday_morning_ok", "a", A_1012_MON, T("2026-10-13T08:00:00+02:00"), dict(level="none", code="ok", mode="calendar")),
    ("a_cal_midweek_batch_missed_next_midnight", "a", A_1012_MON, T("2026-10-14T00:00:30+02:00"),
     dict(level="alert", code="data_overdue", expectDay="2026-10-13", ageDays=2)),
    # ---- 周末, 没有日历字段 ----
    ("a_fb_weekend_saturday", "a", A_FRI_NOCAL, T("2026-10-10T12:00:00+02:00"), dict(level="info", code="closed", why="weekend", mode="fallback")),
    ("a_fb_weekend_sunday", "a", A_FRI_NOCAL, T("2026-10-11T12:00:00+02:00"), dict(level="info", code="closed", why="weekend")),
    ("a_fb_monday_morning_ok", "a", A_FRI_NOCAL, T("2026-10-12T08:00:00+02:00"), dict(level="none", code="ok", ageDays=3)),
    ("a_fb_monday_2359_still_ok", "a", A_FRI_NOCAL, T("2026-10-12T23:59:00+02:00"), dict(level="none", code="ok", ageDays=3)),
    ("a_fb_tuesday_0000_stale", "a", A_FRI_NOCAL, T("2026-10-13T00:00:30+02:00"), dict(level="alert", code="run_stale", ageDays=4)),
    # ---- 字段缺失回退 ----
    ("a_missing_updated_at_uses_run_date", "a", {"run_date": "2026-10-02", "data_date": "2026-09-30"}, T("2026-10-04T16:30:00+02:00"),
     dict(level="info", code="closed", why="no_new_day", runDay="2026-10-02", runAt="10-02")),
    ("a_updated_at_is_bare_date", "a", {"run_date": "2026-10-02", "data_date": "2026-09-30", "updated_at": "2026-10-02"},
     T("2026-10-04T16:30:00+02:00"), dict(level="info", code="closed", why="no_new_day", runAt="10-02")),
    ("a_missing_data_date_uses_run_date", "a", {"run_date": "2026-10-06", "updated_at": "2026-10-06 12:50:00"},
     T("2026-10-06T14:00:00+02:00"), dict(level="none", code="ok", dataDate="2026-10-06")),
    ("a_empty_meta", "a", {}, T("2026-10-06T14:00:00+02:00"), dict(level="none", code="no_meta")),
    ("a_null_meta", "a", None, T("2026-10-06T14:00:00+02:00"), dict(level="none", code="no_meta")),
    ("a_calendar_fields_null_fall_back", "a", dict(A_1002, last_closed_day=None, next_open_day=None, market_status=None),
     T("2026-10-05T10:00:00+02:00"), dict(level="info", code="closed", why="no_new_day", mode="fallback")),
    ("a_next_open_null_weekend_by_clock", "a", dict(A_1009_FRI, next_open_day=None), T("2026-10-10T12:00:00+02:00"),
     dict(level="info", code="closed", why="weekend", mode="calendar", nextOpen=None)),
    ("a_next_open_null_monday_ok", "a", dict(A_1009_FRI, next_open_day=None), T("2026-10-12T08:00:00+02:00"), dict(level="none", code="ok", mode="calendar")),
    ("a_next_open_not_after_last_closed_is_ignored", "a", dict(A_1009_FRI, next_open_day="2026-10-09"), T("2026-10-10T12:00:00+02:00"),
     dict(level="info", code="closed", why="weekend", nextOpen=None)),
    ("a_garbage_calendar_types_fall_back", "a", dict(A_1002, last_closed_day=20260930, next_open_day="soon"),
     T("2026-10-04T16:30:00+02:00"), dict(level="info", code="closed", why="no_new_day", mode="fallback")),
    # ---- 美股 (次日早上跑批, 每天跑; 无日历 -> 只有回退模式) ----
    ("us_weekend_sunday", "us", US_SUN, T("2026-10-04T16:30:00+02:00"), dict(level="info", code="closed", why="weekend", mode="fallback", ageDays=0)),
    ("us_saturday_run", "us", {"run_date": "2026-10-03", "data_date": "2026-10-02", "updated_at": "2026-10-03 08:49:00"},
     T("2026-10-03T12:00:00+02:00"), dict(level="info", code="closed", why="weekend")),
    ("us_ny_still_sunday_2300", "us", US_SUN, T("2026-10-05T05:00:00+02:00"), dict(level="info", code="closed", why="weekend")),
    ("us_ny_monday_0030_ok", "us", US_SUN, T("2026-10-05T06:30:00+02:00"), dict(level="none", code="ok")),
    ("us_monday_after_batch_ok", "us", US_MON, T("2026-10-05T12:00:00+02:00"), dict(level="none", code="ok")),
    ("us_tuesday_before_batch_ok", "us", US_MON, T("2026-10-06T01:00:00+02:00"), dict(level="none", code="ok", ageDays=1)),
    # 周一休市 (MLK 2027-01-18): 周二跑批拿到的仍是上周五 -> 周三凌晨旧逻辑误亮, 现在是中性条
    ("us_monday_holiday_wednesday_dawn", "us", {"run_date": "2027-01-19", "data_date": "2027-01-15", "updated_at": "2027-01-19 08:49:00"},
     T("2027-01-20T01:00:00+01:00"), dict(level="info", code="closed", why="no_new_day", ageDays=1)),
    ("us_run_stopped_4d", "us", US_SUN, T("2026-10-08T09:00:00+02:00"), dict(level="alert", code="run_stale", ageDays=4)),
]

LIVE_A = {"run_date": "2026-10-02", "data_date": "2026-09-30"}
MAP_A = {"2026-10-02": "2026-09-30", "2026-10-01": "2026-09-30", "2026-09-30": "2026-09-30", "2026-09-29": "2026-09-29"}
MAP_US = {"2026-10-06": "2026-10-05", "2026-10-05": "2026-10-02", "2026-10-04": "2026-10-02", "2026-10-03": "2026-10-02"}
# (名字, 跑批日, map, live, lagDays, 期望 data, 期望 note)
LABEL_CASES = [
    ("map_holiday_rerun", "2026-10-02", MAP_A, LIVE_A, 0, "2026-09-30", "10-02"),
    ("map_holiday_rerun_2", "2026-10-01", MAP_A, LIVE_A, 0, "2026-09-30", "10-01"),
    ("map_same_day", "2026-09-30", MAP_A, LIVE_A, 0, "2026-09-30", ""),
    ("map_missing_entry_shows_run_date", "2026-09-25", MAP_A, LIVE_A, 0, "2026-09-25", ""),
    ("nomap_infer_between_live_data_and_run", "2026-10-01", None, LIVE_A, 0, "2026-09-30", "10-01"),
    ("nomap_infer_live_run_itself", "2026-10-02", None, LIVE_A, 0, "2026-09-30", "10-02"),
    ("nomap_live_data_day_itself", "2026-09-30", None, LIVE_A, 0, "2026-09-30", ""),
    ("nomap_older_not_guessed", "2026-09-29", None, LIVE_A, 0, "2026-09-29", ""),
    ("emptymap_treated_as_nomap", "2026-10-01", {}, LIVE_A, 0, "2026-09-30", "10-01"),
    ("nomap_lag1_never_infers", "2026-10-04", None, {"run_date": "2026-10-04", "data_date": "2026-10-02"}, 1, "2026-10-04", ""),
    ("us_map_normal_next_morning", "2026-10-06", MAP_US, None, 1, "2026-10-05", ""),
    ("us_map_saturday_is_normal_lag", "2026-10-03", MAP_US, None, 1, "2026-10-02", ""),
    ("us_map_sunday_rerun", "2026-10-04", MAP_US, None, 1, "2026-10-02", "10-04"),
    ("us_map_monday_rerun", "2026-10-05", MAP_US, None, 1, "2026-10-02", "10-05"),
    ("map_value_later_than_run_is_ignored", "2026-10-02", {"2026-10-02": "2026-10-03"}, LIVE_A, 0, "2026-10-02", ""),
    ("not_a_date", "latest", MAP_A, LIVE_A, 0, "latest", ""),
]


def _ms(now: dt.datetime) -> int:
    return int(round(now.timestamp() * 1000))


# =========================================================================== ① Python 镜像对用例表
@pytest.mark.parametrize("name,mkt,meta,now,exp", CASES, ids=[c[0] for c in CASES])
def test_python_mirror_matches_table(name, mkt, meta, now, exp):
    got = py_freshness(meta, now, CFG[mkt])
    assert {k: got[k] for k in exp} == exp, got


@pytest.mark.parametrize("name,rd,mp,live,lag,data,note", LABEL_CASES, ids=[c[0] for c in LABEL_CASES])
def test_python_run_label_matches_table(name, rd, mp, live, lag, data, note):
    assert py_run_label(rd, mp, live, lag) == {"data": data, "note": note}


def test_cases_cover_the_card_checklist():
    """卡上点名的五类: 长假中中性条 / 跑批停 4 天红条 / 周末 / 三种浏览器时区 / 字段缺失回退 —— 表里都得有。"""
    by = {c[0]: c for c in CASES}
    assert by["a_fb_holiday_sunday_now"][4]["level"] == "info"
    assert by["a_fb_run_stopped_4d"][4] == dict(level="alert", code="run_stale", why=None, ageDays=4)
    assert any(c[4].get("why") == "weekend" and c[4].get("mode") == "fallback" for c in CASES)
    assert any(c[4].get("why") == "weekend" and c[4].get("mode") == "calendar" for c in CASES)
    assert sum(1 for c in CASES if c[0].startswith(("a_missing", "a_empty", "a_null", "a_calendar_fields_null", "a_next_open", "a_garbage"))) >= 7
    assert {c[1] for c in CASES} == {"a", "us"}
    assert {c[4]["code"] for c in CASES} == {"closed", "run_stale", "data_behind", "data_overdue", "await", "ok", "no_meta"}


# =========================================================================== ④ 旧逻辑在这些时刻确实误报
def test_old_logic_alarmed_exactly_where_the_table_says_info():
    """旧判断 = data_date 自然日 > 4 (浏览器本地)。10-05 00:00 柏林起它对 09-30 的数据亮红条; 新判断同一时刻是中性条。"""
    now = T("2026-10-05T00:00:30+02:00")
    assert old_stale_age("2026-09-30", now, "Europe/Berlin") == 5                 # > 4 -> 旧页面亮红条
    assert old_stale_age("2026-09-30", T("2026-10-04T23:59:00+02:00"), "Europe/Berlin") == 4
    assert py_freshness(A_1002, now, CFG["a"])["level"] == "info"
    # 旧判断还随浏览器时区变: 同一时刻 (柏林 10-04 20:00) 柏林浏览器不亮, 北京浏览器已经亮了
    t = T("2026-10-04T20:00:00+02:00")
    assert old_stale_age("2026-09-30", t, "Europe/Berlin") == 4 and old_stale_age("2026-09-30", t, "Asia/Shanghai") == 5
    # 美股周一休市的周三凌晨: 旧判断 5 天 -> 红; 新判断中性
    t = T("2027-01-20T01:00:00+01:00")
    assert old_stale_age("2027-01-15", t, "Europe/Berlin") == 5
    us = {"run_date": "2027-01-19", "data_date": "2027-01-15", "updated_at": "2027-01-19 08:49:00"}
    assert py_freshness(us, t, CFG["us"])["level"] == "info"


# =========================================================================== ② 块指纹 + 页面契约 (不需要浏览器)
def test_fresh_block_sha_is_pinned_and_identical_across_repos():
    got = hashlib.sha256(fresh_block().encode("utf-8")).hexdigest()
    assert got == FRESH_SHA256, (
        "FV-FRESH 块变了 (sha256 %s)。它在 a-share / us 两仓的 dashboard/index.html 里必须逐字相同: 两仓一起改, "
        "两仓 tests/test_fresh_banner.py 的 FRESH_SHA256 与 Python 镜像一起改。" % got)


def test_page_contract_old_logic_gone_and_config_matches():
    src = page_src()
    # 旧判断与旧文案不许回来
    assert 'new Date(dd+"T00:00:00")' not in src and "age>4" not in src
    assert "data/update.log" not in src and 't("stale_a")' not in src
    assert "请查看 /data/ 数据总览" in src
    # 本页参数与本文件的 CFG 一致 (改常量要两边一起改)
    c = CFG[MARKET]
    assert ('const FRESH_CFG = {tzRun:"%s", tzMkt:"%s", staleRunDays:%d, lagDays:%d};'
            % (c["tzRun"], c["tzMkt"], c["staleRunDays"], c["lagDays"])) in src
    # 判定永远吃最新一份数据 + 注入的时钟只有 Date.now() 一处
    assert "FVF.freshness(liveMeta(), Date.now(), FRESH_CFG)" in src
    # 数据源标签不再写死
    assert 'data-i18n="datasource"' not in src and 'id="dsrcLabel"' in src
    assert "akshare（东财行业" not in src and "akshare (EM industries" not in src
    # 块内 (去掉注释后的代码) 不许出现依赖浏览器本地时区 / 真实时钟的写法
    code = "\n".join(ln.split("//")[0] for ln in fresh_block().splitlines())
    for bad in ("getDate()", "getDay()", "getHours()", "getTimezoneOffset", "toLocaleDateString", "Date.now()", "new Date()"):
        assert bad not in code, bad


# =========================================================================== 浏览器夹具
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):                                 # noqa: D401 —— 用例输出里不要访问日志
        pass


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch()
    except Exception as e:                                     # noqa: BLE001 —— 装了库没装浏览器
        pw.stop()
        pytest.skip(f"Chromium 起不来: {e}")
    yield b
    b.close()
    pw.stop()


# --------------------------------------------------------------------------- ① JS 对用例表, 三个浏览器时区
JS_HARNESS = "<!doctype html><meta charset='utf-8'><script>\n%s\n</script>"


@pytest.mark.parametrize("tz", BROWSER_TZS)
def test_js_block_matches_table_and_python_mirror_in_three_browser_timezones(browser, tz):
    ctx = browser.new_context(timezone_id=tz)
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.set_content(JS_HARNESS % fresh_block())
    assert page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone") == tz
    args = [{"meta": c[2], "nowMs": _ms(c[3]), "cfg": CFG[c[1]]} for c in CASES]
    got = page.evaluate("(cs) => cs.map(c => FVF.freshness(c.meta, c.nowMs, c.cfg))", args)
    labels = page.evaluate("(cs) => cs.map(c => FVF.runLabel(c[0], c[1], c[2], c[3]))",
                           [[c[1], c[2], c[3], c[4]] for c in LABEL_CASES])
    ctx.close()
    assert not errs, errs
    for c, g in zip(CASES, got):
        name, mkt, meta, now, exp = c
        assert {k: g[k] for k in exp} == exp, (tz, name, g)
        assert g == py_freshness(meta, now, CFG[mkt]), (tz, name)            # 逐字段与 Python 镜像相同
    for c, g in zip(LABEL_CASES, labels):
        assert g == {"data": c[5], "note": c[6]}, (tz, c[0], g)


def test_js_survives_missing_intl_timezone_support(browser):
    """Intl 不认时区 (极老的浏览器) -> 退到固定偏移 (标准时), 不抛、不显示成红条。"""
    ctx = browser.new_context(timezone_id="America/New_York")
    page = ctx.new_page()
    page.set_content(JS_HARNESS % ("Intl.DateTimeFormat = function(){ throw new RangeError('no tz'); };\n" + fresh_block()))
    g = page.evaluate("(c) => FVF.freshness(c.meta, c.nowMs, c.cfg)",
                      {"meta": A_1002, "nowMs": _ms(T("2026-10-04T16:30:00+02:00")), "cfg": CFG["a"]})
    ctx.close()
    assert (g["level"], g["code"], g["why"], g["ageDays"]) == ("info", "closed", "no_new_day", 2)


# --------------------------------------------------------------------------- ③ 整页渲染
HIST_DATES = ["2026-10-04", "2026-10-03", "2026-10-02", "2026-10-01", "2026-09-30"]
#: 夹具里每份历史快照文件自己的数据日: 美股每天早上跑的是前一个交易日的收盘, 周六 / 周日跑的仍是周五
SNAP_DATA = {"2026-10-04": "2026-10-02", "2026-10-03": "2026-10-02", "2026-10-02": "2026-10-01",
             "2026-10-01": "2026-09-30", "2026-09-30": "2026-09-29"}


def _data_js(meta: dict) -> str:
    cols = [{"key": "code", "label": "代码"}, {"key": "name", "label": "名称"}, {"key": "tag", "label": "结论标签"},
            {"key": "final_score", "label": "综合分"}, {"key": "price", "label": "现价"}]
    return "window.__ASHARE__ = " + json.dumps(
        {"meta": meta, "industries": [], "candidates": [], "details": {}, "profiles": {}, "columns": cols},
        ensure_ascii=False) + ";\n"


def _write_site(root, meta, dates=HIST_DATES, data_dates=None, ql_date="2026-10-04", bt_last="2026-10-04"):
    """data_dates = 写进 history/index.json 的映射 (None = 老清单, 没有这个键); 快照文件自己的 meta 另按 SNAP_DATA 写。"""
    os.makedirs(os.path.join(root, "history"), exist_ok=True)
    shutil.copy(PAGE, os.path.join(root, "index.html"))
    shutil.copy(SHARED_JS, os.path.join(root, "leftside_shared.js"))
    w = lambda name, text: open(os.path.join(root, name), "w", encoding="utf-8").write(text)   # noqa: E731
    w("dashboard_data.js", _data_js(meta))
    gates = {k: True for k in ("q4", "y4", "beat", "roe", "pe", "dom", "cap", "up")}
    picks = [{"code": "AAA", "name": "Alpha Inc.", "industry": "Information Technology", "score": 88.0, "gates": gates,
              "upside": 25.0, "pe": 20.1, "roe": 30.2, "ni_q4": [3, 4, 5, 6], "ni_y4": [2, 3, 4, 5], "dom_rank": 1,
              "dom_share": 12.5, "mcap_b": 3000.0, "p20": 8.0, "p20_n": 40},
             {"code": "BBB", "name": "Beta Corp.", "industry": "Health Care", "score": 71.0, "gates": dict(gates, pe=False),
              "upside": 30.0, "pe": 32.0, "roe": 15.5, "ni_q4": [1, 2, 3, 4], "ni_y4": [1, 2, 3, 4], "dom_rank": 2,
              "dom_share": 9.0, "mcap_b": 2000.0, "p20": 6.0, "p20_n": 40}]
    w("quality_data.js", "window.__QL__ = " + json.dumps(
        {"meta": {"date": ql_date, "n_screened": 4111, "n_pool": 482, "n_crown": 1}, "picks": picks},
        ensure_ascii=False) + ";\n")
    pool = {"n_signals": 40, "n_filled": 38, "n_resolved": 30, "n_open": 8, "fill_rate": 0.95, "win10": 0.5,
            "win10_post": 0.48, "reach5": 0.7, "avg_ret": 0.012, "med_days": 7}
    w("backtest_data.js", "window.__BT__ = " + json.dumps(
        {"meta": {"generated": "x", "n_days": 74, "first_day": "2026-07-05", "last_day": bt_last, "horizon": 20},
         "agg": {"pool": pool, "p0": 0.4, "by_tag": {"✅ 强左侧": pool}}, "recos": [], "recent": [], "open": [],
         "picks_bt": {}}, ensure_ascii=False) + ";\n")
    for name, var in (("paper_data.js", "__PP__"), ("biweekly_data.js", "__BW__"), ("watch_data.js", "__ALL__"),
                      ("sentiment_data.js", "__SENT__")):
        w(name, "window.%s = null;\n" % var)
    idx = {"dates": list(dates), "hits": {d: 10 for d in dates}}
    if data_dates is not None:
        idx["data_dates"] = data_dates
    w(os.path.join("history", "index.json"), json.dumps(idx))
    for d in dates:
        snap = {"meta": {"run_date": d, "data_date": SNAP_DATA.get(d, d), "updated_at": d + " 08:49:00"},
                "industries": [], "candidates": [], "columns": []}
        w(os.path.join("history", "day_%s.json" % d), json.dumps(snap))


@pytest.fixture()
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"root": str(root), "base": "http://127.0.0.1:%d" % srv.server_address[1]}
    srv.shutdown()
    srv.server_close()


READ = """() => {
  const q = s => document.querySelector(s), sb = q('#staleBanner'), sel = q('#dateSel');
  const disp = el => el ? getComputedStyle(el).display : null;
  return {
    hidden: sb.classList.contains('hidden'), level: sb.dataset.level, code: sb.dataset.code, mode: sb.dataset.mode,
    text: sb.textContent.replace(/\\s+/g, ' ').trim(), link: (sb.querySelector('a') || {}).href || null,
    ovFull: q('#ovMini .ovFull').textContent, ovShort: q('#ovMini .ovShort').textContent,
    ovFullDisp: disp(q('#ovMini .ovFull')), ovShortDisp: disp(q('#ovMini .ovShort')), hdrMetaDisp: disp(q('#hdrMeta')),
    dsrc: q('#dsrcLabel').textContent, updated: q('#updatedAt').textContent,
    opts: Array.from(sel.options).map(o => o.value + '|' + o.textContent), sel: sel.value,
    dsLabel: q('#dsLabel').textContent,
    ql: q('#qlMeta').textContent.replace(/\\s+/g, ' ').trim(), bt: q('#btMeta').textContent.replace(/\\s+/g, ' ').trim(),
    hist: q('#histBanner').classList.contains('hidden') ? null : q('#histBanner').textContent,
  };
}"""
READY = ("() => { const s = document.querySelector('#dateSel'), q = document.querySelector('#qlMeta'); "
         "return !!s && s.options.length > 0 && !!q && q.textContent.length > 0 && "
         "document.querySelector('#ovMini').textContent.length > 0; }")


def _open(browser, site, now, tz="Europe/Berlin", w=1280, h=800, lang="zh"):
    mobile = w < 960
    ctx = browser.new_context(viewport={"width": w, "height": h}, timezone_id=tz, is_mobile=mobile, has_touch=mobile,
                              locale="zh-CN")
    ctx.add_init_script("try{localStorage.setItem('ashare_lang_v1','%s');localStorage.setItem('us_lang_v1','%s');}catch(e){}"
                        % (lang, lang))
    base = site["base"]
    ctx.route(lambda url: not url.startswith(base), lambda route: route.abort())      # 零联网: CDN / 行情挂件全掐
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.clock.set_fixed_time(now)
    page.goto(base + "/index.html")
    page.wait_for_function(READY, timeout=20000)
    page.wait_for_timeout(120)
    return ctx, page, errs


@pytest.mark.parametrize("tz", BROWSER_TZS)
def test_page_weekend_shows_neutral_bar_in_any_browser_timezone(browser, site, tz):
    """今晚只发页面时的形态 (周日早上跑的数据, 数据日是周五, 没有日历字段): 中性条「周末休市」, 三个浏览器时区文案逐字相同。"""
    _write_site(site["root"], dict(US_SUN))
    ctx, page, errs = _open(browser, site, T("2026-10-04T16:30:00+02:00"), tz)
    r = page.evaluate(READ)
    ctx.close()
    assert not errs, errs
    assert (r["hidden"], r["level"], r["code"], r["mode"]) == (False, "info", "closed:weekend", "fallback"), r
    assert r["text"] == "⏸️ 美股周末休市· 当前为 2026-10-02 收盘· 最近一次跑批 10-04 08:49", (tz, r["text"])
    assert r["link"] is None


def test_page_monday_holiday_wednesday_dawn_is_neutral_not_red(browser, site):
    """卡上点名的美股误报: 周一休市 (MLK 2027-01-18), 周二跑批拿到的仍是上周五 -> 旧逻辑周三凌晨 5 天亮红条。"""
    meta = {"run_date": "2027-01-19", "data_date": "2027-01-15", "updated_at": "2027-01-19 08:49:00"}
    _write_site(site["root"], meta, dates=["2027-01-19", "2027-01-18", "2027-01-17"])
    ctx, page, errs = _open(browser, site, T("2027-01-20T01:00:00+01:00"))
    r = page.evaluate(READ)
    ctx.close()
    assert not errs, errs
    assert old_stale_age("2027-01-15", T("2027-01-20T01:00:00+01:00"), "Europe/Berlin") == 5
    assert (r["level"], r["code"]) == ("info", "closed:no_new_day"), r
    assert r["text"] == "⏸️ 美股休市中 / 无新交易日· 当前为 2027-01-15 收盘· 最近一次跑批 01-19 08:49", r["text"]


def test_page_normal_weekday_shows_nothing_and_run_stopped_is_red(browser, site):
    _write_site(site["root"], dict(US_MON), dates=["2026-10-05"] + HIST_DATES)
    ctx, page, errs = _open(browser, site, T("2026-10-06T01:00:00+02:00"))               # 周二凌晨, 当天的批还没跑
    r = page.evaluate(READ)
    ctx.close()
    assert not errs and (r["hidden"], r["level"], r["code"]) == (True, "none", "ok"), r
    _write_site(site["root"], dict(US_SUN))
    ctx, page, errs = _open(browser, site, T("2026-10-08T09:00:00+02:00"), "Asia/Shanghai", lang="en")
    r = page.evaluate(READ)
    ctx.close()
    assert not errs, errs
    assert (r["hidden"], r["level"], r["code"]) == (False, "alert", "run_stale"), r
    assert r["text"] == ("⚠️ Last batch run 10-04 08:49 was 4 days ago — the scheduled job may have failed; "
                         "see the /data/ overview (showing 2026-10-02 close)"), r["text"]
    assert r["link"] == "https://fairvalpha.com/data/"


def test_page_header_and_data_source_label(browser, site):
    """手机页头并排「数据日 / 跑批」; 数据源: 老数据没有 meta.sources -> 静态文案 (Yahoo Finance, 属实); 有则从 meta 拼。"""
    _write_site(site["root"], dict(US_SUN))
    ctx, page, errs = _open(browser, site, T("2026-10-04T16:30:00+02:00"))
    d = page.evaluate(READ)
    ctx.close()
    ctx, page, errs2 = _open(browser, site, T("2026-10-04T16:30:00+02:00"), w=375, h=812)
    m = page.evaluate(READ)
    ctx.close()
    assert not errs and not errs2, (errs, errs2)
    assert d["ovFull"] == "数据日期 2026-10-02" and d["ovFullDisp"] != "none" and d["ovShortDisp"] == "none"
    assert d["hdrMetaDisp"] != "none" and d["updated"] == "2026-10-04 08:49:09"
    assert m["ovShort"] == "数据日 10-02 · 跑批 10-04 08:49" and m["ovShortDisp"] != "none" and m["ovFullDisp"] == "none"
    assert m["hdrMetaDisp"] == "none"
    assert d["dsrc"] == "数据源：Yahoo Finance（全美股 · GICS 板块）· 价格单位 USD", d["dsrc"]
    meta = dict(US_SUN, last_closed_day=None, next_open_day=None, market_status="weekend",
                sources={"bars": "yfinance", "valuation": "yfinance", "industry_basis": "GICS",
                         "universe": "nasdaq_screener", "benchmark": "SPY"})
    _write_site(site["root"], meta)
    ctx, page, errs = _open(browser, site, T("2026-10-04T16:30:00+02:00"))
    r = page.evaluate(READ)
    ctx.close()
    assert not errs, errs
    assert r["dsrc"] == "数据源：Yahoo Finance 日线 · 股票池 NASDAQ 筛选器 · 行业 GICS · 估值 Yahoo Finance · 价格单位 USD", r["dsrc"]
    assert (r["level"], r["code"], r["mode"]) == ("info", "closed:weekend", "fallback"), r      # 日历键是 null -> 回退模式


def test_page_dates_stay_run_dates_until_index_has_data_dates(browser, site):
    """老清单没有 data_dates: 美股 (lagDays 1) 不做推断, 选项 / 标题全部仍按跑批日显示 —— 一张清单里不混两种口径。"""
    _write_site(site["root"], dict(US_SUN))
    ctx, page, errs = _open(browser, site, T("2026-10-04T16:30:00+02:00"))
    r = page.evaluate(READ)
    ctx.close()
    assert not errs, errs
    assert r["opts"] == ["2026-10-04|📅 2026-10-04", "2026-10-03|2026-10-03", "2026-10-02|2026-10-02",
                         "2026-10-01|2026-10-01", "2026-09-30|2026-09-30"], r["opts"]
    assert r["ql"] == "覆盖 4111 只 · 入池 482 · 👑门槛全过 1 只 · 2026-10-04 · 成分随季报更新", r["ql"]
    assert r["bt"] == "2026-07-05 → 2026-10-04 · 74 天快照 · 持有窗口 20 交易日", r["bt"]


def test_page_dates_show_data_day_once_index_has_data_dates(browser, site):
    """流水线写了 data_dates 之后: 选项显示数据日; 跑批日 = 数据日 + 1 天 (正常的次日早上跑) 不加注, 周日 / 周一重跑加「跑批 MM-DD」。"""
    _write_site(site["root"], dict(US_SUN), data_dates=dict(SNAP_DATA))
    ctx, page, errs = _open(browser, site, T("2026-10-04T16:30:00+02:00"))
    r = page.evaluate(READ)
    assert r["opts"] == ["2026-10-04|📅 2026-10-02（跑批 10-04）", "2026-10-03|2026-10-02", "2026-10-02|2026-10-01",
                         "2026-10-01|2026-09-30", "2026-09-30|2026-09-29"], r["opts"]
    assert r["sel"] == "2026-10-04" and r["dsLabel"] == "2026-10-02（跑批 10-04）"
    assert r["ql"] == "覆盖 4111 只 · 入池 482 · 👑门槛全过 1 只 · 2026-10-02 (跑批 10-04) · 成分随季报更新", r["ql"]
    assert r["bt"] == "2026-07-05 → 2026-10-02 (跑批 10-04) · 74 天快照 · 持有窗口 20 交易日", r["bt"]
    page.click("#qlTbl th[onclick]")                             # 共用脚本自己重渲染之后仍是数据日
    page.wait_for_timeout(120)
    assert page.evaluate(READ)["ql"] == r["ql"]
    # 回看历史快照: 只留 #histBanner, 提示条不叠
    page.select_option("#dateSel", "2026-10-03")
    page.wait_for_function("() => !document.querySelector('#histBanner').classList.contains('hidden')", timeout=10000)
    h = page.evaluate(READ)
    ctx.close()
    assert not errs, errs
    assert h["hidden"] is True and h["level"] == "none" and h["hist"].startswith("📅 正在查看历史快照 2026-10-02 "), h
