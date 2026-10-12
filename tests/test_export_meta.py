#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板 meta 的交易日历三件套 / 数据源留痕 / 历史清单 data_dates (2026-10-04 卡 A-HOLIDAY; 2026-10-12 卡 US-CAL-TRIPLE) —— 离线, 零联网
===========================================================================================================================
美股看板与 A 股看板共用一套「数据新鲜度」判定 (dashboard/index.html 的 FV-FRESH 块, 用例在 test_fresh_banner.py)。
流水线这一侧:

  ① `screener.runmeta.calendar_fields`: last_closed_day / next_open_day / market_status —— 10-12 起由**内置 NYSE 日历**给出
     (固定休市表 2025–2028 = stock-core research/data_catalog.py::NYSE_CLOSED 的逐条副本; 之外按 NYSE 十条规则推算 +
     临时休市表)。市场状态枚举与 A 股一致: open_day | holiday | weekend。纯函数 (时钟注入), 绝不抛。
     守三件事: 规则 ∪ 临时休市 == 固定表 (逐年); 固定表 == stock-core 那张表 (旁边有检出时); 覆盖年之外三键仍非 None。
  ② `screener.runmeta.data_date_check`: 跑批算出的 data_date (基准末日) 与日历最后收盘日交叉核对, 只给 WARNING 文案不改值。
  ③ `screener.runmeta.run_sources`: 数据源留痕 (页头「数据源」标签从 meta 读)。
  ④ `export_data.history_data_dates`: history/index.json 的 data_dates {跑批日: 数据日} —— 美股每天早上跑的是
     前一个交易日的收盘, 周六 / 周日 / 周一三天跑的都是周五; 日期选择器据此显示数据日。
外加: db 迁移 (老库补 extra_json 列)、老 run_log 没有 extra 时键照样写出 (None)、run_pipeline 接线契约。

时钟全部注入 (now=...), 不读真实的今天。
运行: python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_export_meta.py -q
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import datasource as ds                           # noqa: E402
from screener import db                                         # noqa: E402
from screener import export_data as ex                          # noqa: E402
from screener import runmeta                                    # noqa: E402
from screener.config import CONFIG                              # noqa: E402

UTC = dt.timezone.utc
CEST = dt.timezone(dt.timedelta(hours=2))
CET = dt.timezone(dt.timedelta(hours=1))
NONE3 = {"last_closed_day": None, "next_open_day": None, "market_status": None}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- ① calendar_fields (内置 NYSE 日历)

@pytest.mark.parametrize("now,exp", [
    # 每天柏林 08:30 跑批 = 纽约凌晨 02:30 (夏令时) / 03:30 (冬令时): 最后收盘日是前一个开市日
    (dt.datetime(2026, 10, 4, 8, 30, tzinfo=CEST), ("2026-10-02", "2026-10-05", "weekend")),     # 周日
    (dt.datetime(2026, 10, 3, 8, 30, tzinfo=CEST), ("2026-10-02", "2026-10-05", "weekend")),     # 周六
    (dt.datetime(2026, 10, 5, 8, 30, tzinfo=CEST), ("2026-10-02", "2026-10-05", "open_day")),    # 周一: 下一个出新收盘的交易日 = 当天
    (dt.datetime(2026, 10, 6, 8, 30, tzinfo=CEST), ("2026-10-05", "2026-10-06", "open_day")),    # 周二
    (dt.datetime(2026, 10, 9, 8, 30, tzinfo=CEST), ("2026-10-08", "2026-10-09", "open_day")),    # 周五
    (dt.datetime(2026, 10, 12, 8, 30, tzinfo=CEST), ("2026-10-09", "2026-10-12", "open_day")),   # 10-12 周一 (线上此前写的是三个 null)
    # 纽约 16:00 分界: 15:30 还没收盘 (最后收盘日仍是上周五), 16:30 当天已收盘
    (dt.datetime(2026, 10, 5, 21, 30, tzinfo=CEST), ("2026-10-02", "2026-10-05", "open_day")),
    (dt.datetime(2026, 10, 5, 22, 30, tzinfo=CEST), ("2026-10-05", "2026-10-06", "open_day")),
    # 感恩节 (周四休市) 与次日 (半日市, 仍是交易日)
    (dt.datetime(2026, 11, 26, 8, 30, tzinfo=CET), ("2026-11-25", "2026-11-27", "holiday")),
    (dt.datetime(2026, 11, 27, 8, 30, tzinfo=CET), ("2026-11-25", "2026-11-27", "open_day")),
    # MLK 周一休市 (2027-01-18): 周二跑批拿到的仍是上周五 —— 卡上点名的误报场景
    (dt.datetime(2027, 1, 18, 8, 30, tzinfo=CET), ("2027-01-15", "2027-01-19", "holiday")),
    (dt.datetime(2027, 1, 19, 8, 30, tzinfo=CET), ("2027-01-15", "2027-01-19", "open_day")),
    (dt.datetime(2027, 1, 20, 8, 30, tzinfo=CET), ("2027-01-19", "2027-01-20", "open_day")),
    # 耶稣受难日 (2026-04-03) 接周末; 独立日落周六 -> 周五 07-03 补休; 圣诞落周六 -> 12-24 补休
    (dt.datetime(2026, 4, 3, 8, 30, tzinfo=CEST), ("2026-04-02", "2026-04-06", "holiday")),
    (dt.datetime(2026, 7, 3, 8, 30, tzinfo=CEST), ("2026-07-02", "2026-07-06", "holiday")),
    (dt.datetime(2027, 12, 24, 8, 30, tzinfo=CET), ("2027-12-23", "2027-12-27", "holiday")),
    # 2028 元旦落周六不补休: 2027-12-31 (周五) 照常开市
    (dt.datetime(2027, 12, 31, 8, 30, tzinfo=CET), ("2027-12-30", "2027-12-31", "open_day")),
    (dt.datetime(2028, 1, 3, 8, 30, tzinfo=CET), ("2027-12-31", "2028-01-03", "open_day")),
    # 跨日: 柏林已是周一 05:30, 纽约仍是周日 23:30; 柏林周六 05:30, 纽约仍是周五 23:30 (已收盘)
    (dt.datetime(2026, 10, 5, 3, 30, tzinfo=UTC), ("2026-10-02", "2026-10-05", "weekend")),
    (dt.datetime(2026, 10, 3, 3, 30, tzinfo=UTC), ("2026-10-02", "2026-10-05", "open_day")),
])
def test_calendar_fields_from_builtin_nyse_calendar(now, exp):
    got = runmeta.calendar_fields(now)
    assert (got["last_closed_day"], got["next_open_day"], got["market_status"]) == exp, got
    assert set(got) == set(runmeta.CALENDAR_KEYS)
    assert got["market_status"] in ("open_day", "holiday", "weekend")       # 与 A 股 ashare/datadate.py 同一套枚举


def test_calendar_fields_never_raises(monkeypatch, caplog):
    """内部炸了 (时区 / 日历) 只是三个 None + 一行 WARNING, 不能拖垮出榜。"""
    monkeypatch.setattr(runmeta, "ny_now", lambda now=None: (_ for _ in ()).throw(RuntimeError("tz")))
    with caplog.at_level(logging.WARNING, logger="screener.runmeta"):
        assert runmeta.calendar_fields(dt.datetime(2026, 10, 4, 8, 30, tzinfo=CEST)) == NONE3
    assert any("交易日历字段没算出来" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    monkeypatch.setattr(runmeta, "is_open_day", lambda d: (_ for _ in ()).throw(KeyError("cal")))
    assert runmeta.calendar_fields(dt.datetime(2026, 10, 5, 8, 30, tzinfo=CEST)) == NONE3


def test_calendar_fields_writes_null_when_no_open_day_within_look_window(monkeypatch, caplog):
    """LOOK_DAYS 内找不到开市日 (不该发生): 两个日子写 None + WARNING, 看板回退; 周末 / 工作日状态照写。"""
    monkeypatch.setattr(runmeta, "is_open_day", lambda d: False)
    with caplog.at_level(logging.WARNING, logger="screener.runmeta"):
        got = runmeta.calendar_fields(dt.datetime(2026, 10, 5, 8, 30, tzinfo=CEST))
    assert got == dict(NONE3, market_status="holiday"), got
    assert any("找不到开市日" in r.getMessage() for r in caplog.records)
    assert runmeta.calendar_fields(dt.datetime(2026, 10, 4, 8, 30, tzinfo=CEST))["market_status"] == "weekend"


def test_nyse_rules_reproduce_the_fixed_table_year_by_year():
    """规则 (十条 + observed 挪日) ∪ 临时休市 == 固定表, 2025–2028 逐年 —— 续表 / 改规则时这条先响。"""
    for y in range(runmeta.NYSE_TABLE_YEARS[0], runmeta.NYSE_TABLE_YEARS[1] + 1):
        table = {d for d in runmeta.NYSE_CLOSED if d.startswith("%d-" % y)}
        rules = runmeta.nyse_holidays_by_rule(y) | {d for d in runmeta.NYSE_SPECIAL_CLOSED if d.startswith("%d-" % y)}
        assert rules == table, (y, sorted(rules ^ table))
        assert runmeta.nyse_closed_days(y) == table
        assert 9 <= len(runmeta.nyse_holidays_by_rule(y)) <= 10                 # 元旦落周六那年少一天
    # 几个有讲究的日子
    assert "2026-04-03" in runmeta.NYSE_CLOSED and "2028-04-14" in runmeta.NYSE_CLOSED      # Good Friday
    assert "2026-07-03" in runmeta.NYSE_CLOSED and "2026-07-04" not in runmeta.NYSE_CLOSED  # 独立日落周六 -> 周五
    assert "2027-07-05" in runmeta.NYSE_CLOSED                                             # 独立日落周日 -> 周一
    assert "2027-12-24" in runmeta.NYSE_CLOSED and "2027-12-31" not in runmeta.NYSE_CLOSED  # 圣诞挪周五; 元旦落周六不补
    assert "2025-01-09" in runmeta.NYSE_SPECIAL_CLOSED                                     # 卡特国葬 (规则推不出)
    assert all(dt.date.fromisoformat(d).weekday() < 5 for d in runmeta.NYSE_CLOSED)       # 表里只登记工作日


def test_nyse_rules_keep_working_beyond_the_fixed_table(caplog):
    """2029+ 没有固定表: 规则照算, 三个字段仍非 None, 日志标「规则推算」提醒年末按维护点核对。"""
    r29 = runmeta.nyse_holidays_by_rule(2029)
    assert {"2029-01-01", "2029-01-15", "2029-02-19", "2029-03-30", "2029-05-28", "2029-06-19", "2029-07-04",
            "2029-09-03", "2029-11-22", "2029-12-25"} == r29, sorted(r29)
    assert runmeta.nyse_closed_days(2029) == r29
    assert runmeta.is_open_day(dt.date(2029, 7, 5)) and not runmeta.is_open_day(dt.date(2029, 7, 4))
    with caplog.at_level(logging.INFO, logger="screener.runmeta"):
        got = runmeta.calendar_fields(dt.datetime(2029, 7, 4, 8, 30, tzinfo=CEST))
    assert got == {"last_closed_day": "2029-07-03", "next_open_day": "2029-07-05", "market_status": "holiday"}, got
    assert any("规则推算" in r.getMessage() for r in caplog.records)
    assert "2021-06-18" not in runmeta.nyse_holidays_by_rule(2021) and "2022-06-20" in runmeta.nyse_holidays_by_rule(2022)


def test_nyse_table_is_a_copy_of_stock_core_table():
    """两处同表: 旁边有 stock-core 检出 (STOCK_CORE_DIR 或 ../stock-core) 时逐条比对; 没有就 skip。"""
    core = os.environ.get("STOCK_CORE_DIR") or os.path.join(os.path.dirname(ROOT), "stock-core")
    path = os.path.join(core, "research", "data_catalog.py")
    if not os.path.isfile(path):
        pytest.skip("旁边没有 stock-core 检出: %s" % path)
    src = open(path, encoding="utf-8").read()
    m = re.search(r"^NYSE_CLOSED\s*=\s*(\{.*?\})\s*$", src, re.S | re.M)
    y = re.search(r"^NYSE_YEARS\s*=\s*(\(.*?\))\s*$", src, re.M)
    assert m and y, "stock-core research/data_catalog.py 里找不到 NYSE_CLOSED / NYSE_YEARS"
    assert ast.literal_eval(m.group(1)) == runmeta.NYSE_CLOSED
    assert tuple(ast.literal_eval(y.group(1))) == runmeta.NYSE_TABLE_YEARS


def test_ny_date_falls_back_to_fixed_offset_without_tzdata(monkeypatch):
    """Windows 没装 tzdata 时 zoneinfo 取不到纽约: 退固定 UTC−5。跑批在纽约凌晨 2-3 点, 差一小时不跨日。"""
    import builtins
    real_import = builtins.__import__

    def no_zoneinfo(name, *a, **k):
        if name == "zoneinfo":
            raise ImportError("no tzdata")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_zoneinfo)
    assert runmeta.ny_date(dt.datetime(2026, 10, 4, 8, 30, tzinfo=CEST)) == dt.date(2026, 10, 4)
    assert runmeta.ny_date(dt.datetime(2026, 10, 5, 3, 30, tzinfo=UTC)) == dt.date(2026, 10, 4)
    assert runmeta.calendar_fields(dt.datetime(2026, 10, 5, 8, 30, tzinfo=CEST))["last_closed_day"] == "2026-10-02"


# ---------------------------------------------------------------- ② data_date 交叉核对

def test_data_date_check_only_warns_and_never_changes_anything():
    cal = {"last_closed_day": "2026-10-09", "next_open_day": "2026-10-12", "market_status": "open_day"}
    assert runmeta.data_date_check("2026-10-09", cal) is None
    behind = runmeta.data_date_check("2026-10-08", cal)
    assert behind and "2026-10-08 早于" in behind and "2026-10-09" in behind
    ahead = runmeta.data_date_check("2026-10-12", cal)
    assert ahead and "晚于" in ahead
    for dd, c in [(None, cal), ("", cal), ("2026-10-09", None), ("2026-10-09", {}), ("2026-10-09", {"last_closed_day": None}),
                  ("bad", cal), ("2026-10-09", {"last_closed_day": 20261009})]:
        assert runmeta.data_date_check(dd, c) is None, (dd, c)


# ---------------------------------------------------------------- ③ run_sources

def test_run_sources_reflects_universe_mode_and_size():
    saved = dict(CONFIG["source"])
    try:
        CONFIG["source"]["universe_mode"], CONFIG["source"]["benchmark"] = "all_us", "SPY"
        full = runmeta.run_sources(4111)
        assert full == {"bars": "yfinance", "valuation": "yfinance", "industry_basis": "GICS",
                        "universe": "nasdaq_screener", "benchmark": "SPY"}
        assert runmeta.run_sources(503)["universe"] == "sp500"          # 筛选器失败, get_universe 静默退回标普500
        assert runmeta.run_sources(None)["universe"] == "nasdaq_screener"
        CONFIG["source"]["universe_mode"] = "sp500"
        assert runmeta.run_sources(4111)["universe"] == "sp500"
    finally:
        CONFIG["source"].clear()
        CONFIG["source"].update(saved)
    assert runmeta.run_sources("not-a-number")["bars"] == "yfinance"    # 不抛


def test_run_pipeline_wires_runmeta_into_run_log():
    src = open(os.path.join(ROOT, "run_pipeline.py"), encoding="utf-8").read()
    j = src.index('run_extra = {"calendar": runmeta.calendar_fields(), "sources": runmeta.run_sources(len(universe))}')
    c = src.index('runmeta.data_date_check(data_date, run_extra["calendar"])')
    k = src.index("data_date=data_date, extra=run_extra)")
    m = src.index("ex.write_dashboard_js(run_date)")
    assert j < c < k < m and "from screener import runmeta" in src


# ---------------------------------------------------------------- run_log.extra_json -> export meta

class _TmpDb:
    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="usx_")
        self.hist = os.path.join(self.tmp, "history")
        os.makedirs(self.hist)
        self._saved = (db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark, ds.fetch_eps_history)
        db.DB_PATH = os.path.join(self.tmp, "us.db")
        ex.HISTORY_DIR = self.hist
        ds.fetch_benchmark = lambda *a, **k: None               # build_payload 顺带联网的两处, 与本文件无关, 掐掉
        ds.fetch_eps_history = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark, ds.fetch_eps_history = self._saved
        return False


EXTRA = {"calendar": {"last_closed_day": "2026-10-02", "next_open_day": "2026-10-05", "market_status": "weekend"},
         "sources": {"bars": "yfinance", "valuation": "yfinance", "industry_basis": "GICS",
                     "universe": "nasdaq_screener", "benchmark": "SPY"}}


def _log(run, data, extra=None):
    db.log_run(run, run + " 08:30:05", run + " 08:49:09", 4090, 0, ["Information Technology"], "ok",
               data_date=data, extra=extra)


def test_export_meta_carries_calendar_triple_and_sources():
    with _TmpDb():
        db.init_db()
        _log("2026-10-04", "2026-10-02", EXTRA)
        meta = ex.build_payload("2026-10-04")["meta"]
        assert (meta["run_date"], meta["data_date"], meta["updated_at"]) == ("2026-10-04", "2026-10-02", "2026-10-04 08:49:09")
        assert (meta["last_closed_day"], meta["next_open_day"], meta["market_status"]) == ("2026-10-02", "2026-10-05", "weekend")
        assert meta["sources"] == EXTRA["sources"]
        snap = json.load(open(ex.write_history_snapshot("2026-10-04"), encoding="utf-8"))
        assert snap["meta"]["market_status"] == "weekend" and snap["meta"]["last_closed_day"] == "2026-10-02"
        assert snap["meta"]["sources"]["bars"] == "yfinance"
        # 老行 (10-04..10-11 的形态): 日历里只有周末那一键, 另两键 None 照样带出
        _log("2026-10-11", "2026-10-09", dict(EXTRA, calendar={"last_closed_day": None, "next_open_day": None, "market_status": "weekend"}))
        meta = ex.build_payload("2026-10-11")["meta"]
        assert (meta["last_closed_day"], meta["next_open_day"], meta["market_status"]) == (None, None, "weekend")


def test_export_meta_keys_present_but_null_without_extra():
    with _TmpDb():
        db.init_db()
        _log("2026-10-05", "2026-10-02", None)
        meta = ex.build_payload("2026-10-05")["meta"]
        for k in ("last_closed_day", "next_open_day", "market_status", "sources"):
            assert k in meta and meta[k] is None, (k, meta.get(k))
        with db.get_conn() as conn:                              # extra_json 被写坏也不抛
            conn.execute("UPDATE run_log SET extra_json=? WHERE run_date=?", ("{oops", "2026-10-05"))
        assert ex.build_payload("2026-10-05")["meta"]["market_status"] is None


def test_db_migration_adds_extra_json_to_old_run_log():
    with _TmpDb():
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("CREATE TABLE run_log(run_date TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, n_scanned INTEGER, "
                     "n_hit INTEGER, selected_industries TEXT, status TEXT, message TEXT, data_date TEXT)")
        conn.execute("INSERT INTO run_log VALUES('2026-10-03','a','2026-10-03 08:49:00',4090,300,'[]','ok','','2026-10-02')")
        conn.commit()
        conn.close()
        _log("2026-10-04", "2026-10-02", EXTRA)                  # 迁移前: 不抛, 那一键被丢弃
        assert "extra_json" not in (db.fetch_run_log("2026-10-04") or {})
        db.init_db()
        old = db.fetch_run_log("2026-10-03")
        assert old["data_date"] == "2026-10-02" and old["extra_json"] is None
        _log("2026-10-05", "2026-10-02", EXTRA)
        assert json.loads(db.fetch_run_log("2026-10-05")["extra_json"]) == EXTRA


# ---------------------------------------------------------------- ④ history/index.json 的 data_dates

def _snap(hist, run, data):
    meta = {"run_date": run}
    if data is not None:
        meta["data_date"] = data
    meta["updated_at"] = run + " 08:49:00"
    with open(os.path.join(hist, "day_%s.json" % run), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "industries": [], "candidates": [], "columns": []}, f, ensure_ascii=False)


def test_history_data_dates_from_known_prev_and_file_heads():
    hist = tempfile.mkdtemp(prefix="ush_")
    _snap(hist, "2026-10-03", "2026-10-02")                 # 周六跑的是周五
    _snap(hist, "2026-10-02", "2026-10-01")
    _snap(hist, "2026-10-01", None)                         # 极老的快照: meta 没有 data_date -> 不进表
    _snap(hist, "2026-09-30", "2026-10-01")                 # 数据日晚于跑批日 = 文件被动过 -> 不采信
    dates = ["2026-10-04", "2026-10-03", "2026-10-02", "2026-10-01", "2026-09-30", "2026-09-29"]
    got = ex.history_data_dates(hist, dates, prev={"2026-09-29": "2026-09-28", "2026-10-03": "2026-10-01"},
                                known={"2026-10-04": "2026-10-02"})
    assert got == {"2026-10-04": "2026-10-02",              # known: 本轮刚写的那份
                   "2026-10-03": "2026-10-01",              # prev 优先于读文件
                   "2026-10-02": "2026-10-01",              # 读文件开头
                   "2026-09-29": "2026-09-28"}, got         # 文件不在盘上, prev 里有 -> 沿用


def test_write_history_snapshot_writes_data_dates_into_index():
    with _TmpDb() as t:
        db.init_db()
        _snap(t.hist, "2026-10-03", "2026-10-02")
        _snap(t.hist, "2026-10-02", "2026-10-01")
        json.dump({"dates": ["2026-10-03", "2026-10-02"], "hits": {"2026-10-03": 300, "2026-10-02": 298}},
                  open(os.path.join(t.hist, "index.json"), "w"))                   # 老清单: 没有 data_dates
        _log("2026-10-04", "2026-10-02", EXTRA)
        ex.write_history_snapshot("2026-10-04")
        idx = json.load(open(os.path.join(t.hist, "index.json"), encoding="utf-8"))
        assert idx["dates"] == ["2026-10-04", "2026-10-03", "2026-10-02"]
        assert idx["hits"] == {"2026-10-04": 0, "2026-10-03": 300, "2026-10-02": 298}      # 老键原样
        assert idx["data_dates"] == {"2026-10-04": "2026-10-02", "2026-10-03": "2026-10-02", "2026-10-02": "2026-10-01"}
        # data_dates 算崩了也不许拖垮快照 / 清单
        saved = ex.history_data_dates
        ex.history_data_dates = lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
        try:
            _log("2026-10-05", "2026-10-02", EXTRA)
            assert ex.write_history_snapshot("2026-10-05")
        finally:
            ex.history_data_dates = saved
        idx = json.load(open(os.path.join(t.hist, "index.json"), encoding="utf-8"))
        assert idx["dates"][0] == "2026-10-05" and "2026-10-05" not in idx["data_dates"]
        assert idx["data_dates"]["2026-10-04"] == "2026-10-02"
