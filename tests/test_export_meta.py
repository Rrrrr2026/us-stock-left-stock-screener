#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板 meta 的 market_status / 数据源留痕 / 历史清单 data_dates (2026-10-04 卡 A-HOLIDAY) —— 离线, 零联网
=======================================================================================================
美股看板与 A 股看板共用一套「数据新鲜度」判定 (dashboard/index.html 的 FV-FRESH 块, 用例在 test_fresh_banner.py)。
流水线这一侧, 美股**没有交易日历** (不引新依赖), 所以:

  ① `screener.runmeta.calendar_fields`: last_closed_day / next_open_day 恒为 None, market_status 只在纽约周六日写
     'weekend' —— 绝不按工作日近似去填 (近似会把感恩节 / MLK 当开市日, 看板就会误报「数据落后于应到交易日」)。
  ② `screener.runmeta.run_sources`: 数据源留痕 (页头「数据源」标签从 meta 读)。
  ③ `export_data.history_data_dates`: history/index.json 的 data_dates {跑批日: 数据日} —— 美股每天早上跑的是
     前一个交易日的收盘, 周六 / 周日 / 周一三天跑的都是周五; 日期选择器据此显示数据日。
外加: db 迁移 (老库补 extra_json 列)、老 run_log 没有 extra 时键照样写出 (None)、run_pipeline 接线契约。

时钟全部注入 (now=...), 不读真实的今天。
运行: python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_export_meta.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
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
NONE3 = {"last_closed_day": None, "next_open_day": None, "market_status": None}


# ---------------------------------------------------------------- ① calendar_fields

@pytest.mark.parametrize("now,status", [
    (dt.datetime(2026, 10, 4, 8, 30, tzinfo=CEST), "weekend"),      # 周日早上的跑批 (纽约周日 02:30)
    (dt.datetime(2026, 10, 3, 8, 30, tzinfo=CEST), "weekend"),      # 周六
    (dt.datetime(2026, 10, 5, 8, 30, tzinfo=CEST), None),           # 周一: 开市还是节假日, 没日历判不了
    (dt.datetime(2026, 11, 26, 8, 30, tzinfo=CEST), None),          # 感恩节 (周四): 同样不猜
    (dt.datetime(2026, 10, 5, 3, 30, tzinfo=UTC), "weekend"),       # 柏林已是周一 05:30, 纽约仍是周日 23:30
    (dt.datetime(2026, 10, 3, 3, 30, tzinfo=UTC), None),            # 柏林周六 05:30, 纽约仍是周五 23:30
])
def test_calendar_fields_only_knows_weekends(now, status):
    got = runmeta.calendar_fields(now)
    assert got == dict(NONE3, market_status=status), got
    assert set(got) == set(runmeta.CALENDAR_KEYS)


def test_calendar_fields_never_writes_day_fields_and_never_raises(monkeypatch, caplog):
    """last_closed_day / next_open_day 恒为 None (没有日历就不近似); 内部炸了也只是三个 None。"""
    for d in range(1, 15):
        got = runmeta.calendar_fields(dt.datetime(2026, 10, d, 8, 30, tzinfo=CEST))
        assert got["last_closed_day"] is None and got["next_open_day"] is None
    monkeypatch.setattr(runmeta, "ny_date", lambda now=None: (_ for _ in ()).throw(RuntimeError("tz")))
    with caplog.at_level(logging.WARNING, logger="screener.runmeta"):
        assert runmeta.calendar_fields(dt.datetime(2026, 10, 4, 8, 30, tzinfo=CEST)) == NONE3
    assert any("market_status 没算出来" in r.getMessage() for r in caplog.records)


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


# ---------------------------------------------------------------- ② run_sources

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
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_pipeline.py"),
               encoding="utf-8").read()
    j = src.index('run_extra = {"calendar": runmeta.calendar_fields(), "sources": runmeta.run_sources(len(universe))}')
    k = src.index("data_date=data_date, extra=run_extra)")
    m = src.index("ex.write_dashboard_js(run_date)")
    assert j < k < m and "from screener import runmeta" in src


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


EXTRA = {"calendar": {"last_closed_day": None, "next_open_day": None, "market_status": "weekend"},
         "sources": {"bars": "yfinance", "valuation": "yfinance", "industry_basis": "GICS",
                     "universe": "nasdaq_screener", "benchmark": "SPY"}}


def _log(run, data, extra=None):
    db.log_run(run, run + " 08:30:05", run + " 08:49:09", 4090, 0, ["Information Technology"], "ok",
               data_date=data, extra=extra)


def test_export_meta_carries_market_status_and_sources():
    with _TmpDb():
        db.init_db()
        _log("2026-10-04", "2026-10-02", EXTRA)
        meta = ex.build_payload("2026-10-04")["meta"]
        assert (meta["run_date"], meta["data_date"], meta["updated_at"]) == ("2026-10-04", "2026-10-02", "2026-10-04 08:49:09")
        assert (meta["last_closed_day"], meta["next_open_day"], meta["market_status"]) == (None, None, "weekend")
        assert meta["sources"] == EXTRA["sources"]
        snap = json.load(open(ex.write_history_snapshot("2026-10-04"), encoding="utf-8"))
        assert snap["meta"]["market_status"] == "weekend" and snap["meta"]["sources"]["bars"] == "yfinance"


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


# ---------------------------------------------------------------- ③ history/index.json 的 data_dates

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
