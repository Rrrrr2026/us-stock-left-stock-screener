#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块5 — 持久化 (SQLite)
=======================
表: industry_score / tech_scan / fundamental / final_rank / stock_detail / run_log
每张表带 run_date, 保留每日历史 (PRD §7)。
"""
from __future__ import annotations
import json
import sqlite3
import datetime as dt
import logging

from .config import DB_PATH

log = logging.getLogger("ashare.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS industry_score(
    run_date TEXT, industry TEXT, prosperity_score REAL,
    trend REAL, momentum REAL, breadth REAL, capital REAL, fundamental REAL,
    idx_close REAL, ma120 REAL, above_ma120 INTEGER,
    eligible INTEGER, selected INTEGER,
    PRIMARY KEY(run_date, industry)
);
CREATE TABLE IF NOT EXISTS tech_scan(
    run_date TEXT, code TEXT, name TEXT, industry TEXT,
    price REAL, tech_score REAL, n_hit INTEGER,
    sig_channel TEXT, sig_pivot TEXT, sig_ma TEXT, sig_osc TEXT,
    dist_lower REAL, dist_pivot REAL, dist_ma REAL, drawdown_pct REAL, rsi REAL,
    support_label TEXT, support_price REAL, dist_support_pct REAL, breakdown_price REAL,
    high_52w REAL, low_52w REAL, pos_52w_pct REAL, ret_half_year_pct REAL, ret_1m_pct REAL,
    turnover REAL, volume_ratio REAL, amount_today REAL, avg_amt20_yi REAL,
    kdj_k REAL, kdj_d REAL, kdj_j REAL, kdj_tag TEXT,
    spark_json TEXT, atr_pct REAL, max_dd_pct REAL, beta REAL, vol_ratio_calc REAL,
    sig_vol TEXT, boll_low REAL, fib_382 REAL, fib_500 REAL, fib_618 REAL,
    dip INTEGER, dip_score REAL, dip_confirm TEXT,
    PRIMARY KEY(run_date, code)
);
CREATE TABLE IF NOT EXISTS fundamental(
    run_date TEXT, code TEXT,
    pe_ttm REAL, pe_pct REAL, pe_industry_median REAL, pe_vs_industry REAL,
    pb REAL, pb_pct REAL, dividend_yield REAL,
    eps REAL, eps_yoy REAL, roe REAL,
    revenue_yoy REAL, netprofit_yoy REAL, gross_margin REAL, debt_ratio REAL,
    target_price REAL, analyst_rating TEXT, analyst_count REAL, upside_pct REAL,
    roe_trend_json TEXT, roe_trend_q_json TEXT, fund_flags_json TEXT,
    PRIMARY KEY(run_date, code)
);
CREATE TABLE IF NOT EXISTS final_rank(
    run_date TEXT, code TEXT, name TEXT, industry TEXT, tag TEXT,
    final_score REAL, tech_score REAL, tech_norm REAL, fund_score REAL,
    prosperity_score REAL, conclusion TEXT, conclusion_en TEXT,
    dip INTEGER, dip_score REAL, dip_confirm TEXT,
    PRIMARY KEY(run_date, code)
);
CREATE TABLE IF NOT EXISTS stock_detail(
    run_date TEXT, code TEXT, detail_json TEXT,
    PRIMARY KEY(run_date, code)
);
CREATE TABLE IF NOT EXISTS profile(
    run_date TEXT, code TEXT, profile_json TEXT,
    PRIMARY KEY(run_date, code)
);
CREATE TABLE IF NOT EXISTS trade_plan(
    run_date TEXT, code TEXT, plan_json TEXT,
    PRIMARY KEY(run_date, code)
);
CREATE TABLE IF NOT EXISTS run_log(
    run_date TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
    n_scanned INTEGER, n_hit INTEGER, selected_industries TEXT,
    status TEXT, message TEXT, data_date TEXT,
    -- 2026-10-04 卡 A-HOLIDAY: 跑批当时的 market_status + 数据源留痕, 一列 JSON (看板 meta 原样带出)
    extra_json TEXT
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
    log.info("DB 初始化: %s", DB_PATH)


def _migrate(conn):
    """给老库补新列(不丢历史)。"""
    want = {
        "tech_scan": [("support_label", "TEXT"), ("support_price", "REAL"),
                      ("dist_support_pct", "REAL"), ("breakdown_price", "REAL"),
                      ("high_52w", "REAL"), ("low_52w", "REAL"), ("pos_52w_pct", "REAL"),
                      ("ret_half_year_pct", "REAL"),
                      ("kdj_k", "REAL"), ("kdj_d", "REAL"), ("kdj_j", "REAL"), ("kdj_tag", "TEXT"),
                      ("ret_1m_pct", "REAL"), ("spark_json", "TEXT"), ("atr_pct", "REAL"),
                      ("max_dd_pct", "REAL"), ("beta", "REAL"), ("vol_ratio_calc", "REAL"),
                      ("sig_vol", "TEXT"), ("boll_low", "REAL"), ("fib_382", "REAL"),
                      ("fib_500", "REAL"), ("fib_618", "REAL"),
                      ("dip", "INTEGER"), ("dip_score", "REAL"), ("dip_confirm", "TEXT"),
                      ("supp_touches", "INTEGER"), ("trend_ok", "INTEGER"), ("rs_60", "REAL"),
                      ("coil", "INTEGER"), ("coil_score", "REAL"), ("coil_confirm", "TEXT"),
                      ("box_hi", "REAL"), ("box_lo", "REAL")],
        "fundamental": [("target_price", "REAL"), ("analyst_rating", "TEXT"),
                        ("analyst_count", "REAL"), ("upside_pct", "REAL"),
                        ("roe_trend_q_json", "TEXT"),
                        ("fcf_yield", "REAL"), ("sector_yf", "TEXT"),
                        ("ni_ttm_yoy", "REAL"), ("ni_parent_ttm_yoy", "REAL"),
                        ("ni_basis", "TEXT"), ("ni_parent_basis", "TEXT"),
                        ("growth_quality", "TEXT"),
                        ("dominance_disp", "TEXT"), ("dom_rank", "INTEGER"),
                        ("dom_n", "INTEGER"), ("dom_share", "REAL"),
                        ("ni_qoq_json", "TEXT"), ("ni_parent_qoq_json", "TEXT"),
                        ("ni_q_labels_json", "TEXT"),
                        ("rev_ttm_yoy", "REAL"), ("rev_basis", "TEXT"), ("rev_qoq_json", "TEXT")],
        "final_rank": [("conclusion_en", "TEXT"),
                       ("dip", "INTEGER"), ("dip_score", "REAL"), ("dip_confirm", "TEXT"),
                       ("coil", "INTEGER"), ("coil_score", "REAL"), ("coil_confirm", "TEXT")],
        "run_log": [("data_date", "TEXT"), ("extra_json", "TEXT")],
    }
    for table, cols in want.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, typ in cols:
            if name not in have:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                except Exception as e:
                    log.debug("migrate %s.%s: %s", table, name, e)


_RUN_TABLES = ("industry_score", "tech_scan", "fundamental",
               "final_rank", "stock_detail", "profile", "trade_plan", "run_log")


def clear_run(run_date: str):
    """清空某个 run_date 的全部结果, 保证每次运行是干净的快照
    (避免演示数据与实盘数据在同一天混在一起)。"""
    with get_conn() as conn:
        for t in _RUN_TABLES:
            conn.execute(f"DELETE FROM {t} WHERE run_date=?", (run_date,))


def _cols(table):
    with get_conn() as conn:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return [r["name"] for r in cur.fetchall()]


def _upsert(table, rows: list[dict]):
    if not rows:
        return
    cols = _cols(table)
    rows = [{k: r.get(k) for k in cols} for r in rows]
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({placeholders})"
    with get_conn() as conn:
        conn.executemany(sql, [[_coerce(r[c]) for c in cols] for r in rows])


def _coerce(v):
    if isinstance(v, bool):
        return 1 if v else 0
    return v


# ---------------------------------------------------------------------------
def save_industry_scores(run_date: str, df):
    if df is None or df.empty:
        return
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "run_date": run_date, "industry": r["industry"],
            "prosperity_score": r.get("prosperity_score"),
            "trend": r.get("trend"), "momentum": r.get("momentum"),
            "breadth": r.get("breadth"), "capital": r.get("capital"),
            "fundamental": r.get("fundamental"),
            "idx_close": r.get("idx_close"), "ma120": r.get("ma120"),
            "above_ma120": bool(r.get("above_ma120")),
            "eligible": bool(r.get("eligible")), "selected": bool(r.get("selected")),
        })
    _upsert("industry_score", rows)


def save_tech(run_date: str, records: list[dict]):
    rows = []
    for r in records:
        row = {**r, "run_date": run_date}
        if isinstance(row.get("spark"), list):
            row["spark_json"] = json.dumps(row["spark"])   # 列表转JSON存
        rows.append(row)
    _upsert("tech_scan", rows)


def save_fundamental(run_date: str, code: str, f: dict):
    row = {k: f.get(k) for k in (
        "pe_ttm", "pe_pct", "pe_industry_median", "pe_vs_industry", "pb", "pb_pct",
        "dividend_yield", "eps", "eps_yoy", "roe", "revenue_yoy", "netprofit_yoy",
        "gross_margin", "debt_ratio",
        "target_price", "analyst_rating", "analyst_count", "upside_pct",
        "fcf_yield", "sector_yf",
        "ni_ttm_yoy", "ni_parent_ttm_yoy", "ni_basis", "ni_parent_basis", "growth_quality",
        "rev_ttm_yoy", "rev_basis",
        "dominance_disp", "dom_rank", "dom_n", "dom_share")}
    row.update({
        "run_date": run_date, "code": code,
        "roe_trend_json": json.dumps(f.get("roe_trend", []), ensure_ascii=False),
        "roe_trend_q_json": json.dumps(f.get("roe_trend_q", []), ensure_ascii=False),
        "fund_flags_json": json.dumps(f.get("fund_flags", []), ensure_ascii=False),
        "ni_qoq_json": json.dumps(f.get("ni_qoq", []), ensure_ascii=False),
        "ni_parent_qoq_json": json.dumps(f.get("ni_parent_qoq", []), ensure_ascii=False),
        "ni_q_labels_json": json.dumps(f.get("ni_q_labels", []), ensure_ascii=False),
        "rev_qoq_json": json.dumps(f.get("rev_qoq", []), ensure_ascii=False),
    })
    _upsert("fundamental", [row])


def fetch_latest_fundamental(code: str, before_run_date: str) -> dict | None:
    """取该股最近一次(早于指定日)的基本面行 — Yahoo限频兜底用, 基本面日变化很小。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fundamental WHERE code=? AND run_date<? "
            "ORDER BY run_date DESC LIMIT 1", (code, before_run_date)).fetchone()
        return dict(row) if row else None


def save_final(run_date: str, records: list[dict]):
    _upsert("final_rank", [{**r, "run_date": run_date} for r in records])


def save_detail(run_date: str, code: str, detail: dict):
    _upsert("stock_detail", [{
        "run_date": run_date, "code": code,
        "detail_json": json.dumps(detail, ensure_ascii=False),
    }])


def save_profile(run_date: str, code: str, profile: dict):
    _upsert("profile", [{
        "run_date": run_date, "code": code,
        "profile_json": json.dumps(profile, ensure_ascii=False),
    }])


def save_trade_plan(run_date: str, code: str, plan: dict):
    _upsert("trade_plan", [{
        "run_date": run_date, "code": code,
        "plan_json": json.dumps(plan, ensure_ascii=False),
    }])


def log_run(run_date, started_at, finished_at, n_scanned, n_hit,
            selected_industries, status, message="", data_date=None, extra=None):
    """extra = 跑批当时才知道、导出时要原样带进 meta 的东西 (dict, 存成一列 JSON):
    {"calendar": {last_closed_day, next_open_day, market_status}, "sources": {...}} —— 见 screener/runmeta.py。"""
    _upsert("run_log", [{
        "run_date": run_date, "started_at": started_at, "finished_at": finished_at,
        "n_scanned": n_scanned, "n_hit": n_hit,
        "selected_industries": json.dumps(selected_industries, ensure_ascii=False),
        "status": status, "message": message, "data_date": data_date or run_date,
        "extra_json": json.dumps(extra, ensure_ascii=False) if extra else None,
    }])


# ---------------------------------------------------------------------------
def latest_run_date() -> str | None:
    # 只认 status='ok' 的完整运行 —— 演示/中断的部分扫描不能成为导出锚点
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT run_date FROM run_log WHERE status='ok' ORDER BY run_date DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            return row["run_date"]
        cur = conn.execute("SELECT run_date FROM run_log ORDER BY run_date DESC LIMIT 1")
        row = cur.fetchone()
        return row["run_date"] if row else None


def fetch_table(table: str, run_date: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(f"SELECT * FROM {table} WHERE run_date=?", (run_date,))
        return [dict(r) for r in cur.fetchall()]


def recent_run_dates(limit: int = 5) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT run_date FROM run_log ORDER BY run_date DESC LIMIT ?", (limit,)).fetchall()
    return [r["run_date"] for r in rows]


def recent_appearance_counts(run_dates: list[str]) -> dict:
    """给定若干 run_date, 返回 code -> 在这些日子里作为候选出现的次数 (连续上榜信号)。"""
    if not run_dates:
        return {}
    qs = ",".join("?" * len(run_dates))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT code, COUNT(DISTINCT run_date) c FROM final_rank "
            f"WHERE run_date IN ({qs}) GROUP BY code", run_dates).fetchall()
    return {r["code"]: r["c"] for r in rows}


def fetch_run_log(run_date: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM run_log WHERE run_date=?", (run_date,))
        row = cur.fetchone()
        return dict(row) if row else None


def fetch_industry_history(industry: str, limit: int = 60) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT run_date, prosperity_score FROM industry_score "
            "WHERE industry=? ORDER BY run_date DESC LIMIT ?", (industry, limit))
        return [dict(r) for r in cur.fetchall()][::-1]
