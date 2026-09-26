#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史蓄势形态扫描 (Coil scan) — M1 研究引擎
============================================
在本地价格库的 5 年日线上, 找"上涨后横盘收敛 + 缩量"的蓄势形态历史样本,
统计随后 10/20 个交易日的真实结果, 并按 基本面质量(点时可得) × 市场温度 ×
形态紧致度 × 缩量强度 分组 —— 回答: 「优质公司横盘蓄势 → 两周内爆发」
到底有多大概率、什么条件下最灵。

诚实约束 (报告里必须带着):
  * 幸存者偏差: 股票池是"今天还上市的", 已退市的不在 —— 统计略偏乐观。
  * 基本面按法定披露截止日滞后 (Q1->4/30, 中报->8/31, Q3->10/31, 年报->次年4/30),
    宁可晚不可早, 无前视。
  * 前复权价: 分红除权会整体平移历史价格, 形态比例不受影响。
  * 无成本口径: 形态研究报毛收益; 实盘往返成本 A股~0.3% / 美股~0.2% 自行扣。

判定规则 (v1, 全部透明可复现):
  蓄势 = 近30bar箱体振幅<=15% 且 收盘比120bar前高>=10% 且 近10bar均量<=箱体
  前20bar均量×0.75 且 收盘仍在箱体内 (未破位未突破) 且 日均成交额达门槛。
  突破 = 信号后10bar内最高价站上箱体上沿×1.005; 入场=max(次bar开盘, 上沿×1.005),
  开盘超上沿5%不追。止损=箱体下沿×0.995。持有20bar: 先碰止损算止损,
  先到+10%/+20%算达标, 完整窗口才进统计 (与信号回测同口径)。
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os

import numpy as np

from .market import current
from . import pricestore as ps

log = logging.getLogger("leftside_core.coilscan")

BOX_W = 30                 # 箱体窗口 (bar)
TREND_W = 120              # 前置上涨窗口
TREND_MIN = 0.10           # 120bar 涨幅下限
TIGHT_MAX = 0.15           # 箱体振幅上限
VC_MAX = 0.75              # 缩量: 近10bar均量 / 箱体前20bar均量 上限
STRIDE = 5                 # 评估步长 (bar)
COOLDOWN = 20              # 同一只票两次信号最小间隔 (bar)
BREAK_WAIT = 10            # 等突破窗口
HOLD = 20                  # 突破后持有窗口
CHASE_MAX = 0.05           # 开盘高于上沿 5% 不追
MIN_TURNOVER = {"ashare": 3e7, "us": 5e6}   # 箱体内日均成交额下限 (本币)


def _regime_map() -> tuple[list, np.ndarray] | None:
    idx = ps.load_index()
    if not idx:
        return None
    closes = idx["ohlcv"][:, 3]
    ma50 = np.convolve(closes, np.ones(50) / 50, mode="valid")
    bull = np.zeros(len(closes), dtype=bool)
    bull[49:] = closes[49:] >= ma50
    return idx["dates"], bull


def _episode_outcome(ohlcv: np.ndarray, t: int, box_hi: float, box_lo: float) -> dict:
    """从 t+1 起: 等突破 -> 入场 -> 20bar 结果。"""
    n = len(ohlcv)
    trigger = box_hi * 1.005
    stop = box_lo * 0.995
    j = t + 1
    while j < min(n, t + 1 + BREAK_WAIT):
        o, h, l, c, v = ohlcv[j]
        if l <= box_lo * 0.99:
            return {"status": "broke_down", "days_to_event": j - t}
        if h >= trigger:
            entry = max(o, trigger)
            if entry > box_hi * (1 + CHASE_MAX):
                return {"status": "gap_break", "days_to_event": j - t}
            return _hold_outcome(ohlcv, j, entry, stop, days_to_break=j - t)
        j += 1
    return {"status": "no_break" if j >= t + 1 + BREAK_WAIT else "data_end"}


def _hold_outcome(ohlcv: np.ndarray, j: int, entry: float, stop: float,
                  days_to_break: int) -> dict:
    n = len(ohlcv)
    end = min(j + HOLD, n - 1)
    complete = (j + HOLD) <= (n - 1)
    hit10 = hit20 = stopped = False
    exit_i = end
    k = j
    while k <= end:
        o, h, l, c, v = ohlcv[k]
        if l <= stop:                      # 同bar双触发按先止损 (保守, 与回测一致)
            stopped, exit_i = True, k
            break
        if not hit10 and h >= entry * 1.10:
            hit10 = True
        if not hit20 and h >= entry * 1.20:
            hit20 = True
        k += 1
    seg = ohlcv[j:exit_i + 1]
    ret_exit = (stop if stopped else ohlcv[exit_i][3]) / entry - 1.0
    out = {
        "status": "stopped" if stopped else "held",
        "days_to_break": days_to_break, "complete": bool(complete),
        "entry": round(float(entry), 3),
        "hit10": bool(hit10), "hit20": bool(hit20),
        "ret20": round(float(ret_exit), 4),
        "max_gain": round(float(np.max(seg[:, 1]) / entry - 1.0), 4),
        "max_dd": round(float(np.min(seg[:, 2]) / entry - 1.0), 4),
        "hold_days": int(exit_i - j),
    }
    if not stopped and complete:
        mid = min(j + 10, n - 1)
        out["ret10"] = round(float(ohlcv[mid][3] / entry - 1.0), 4)
    return out


def scan(codes: list | None = None, quality_at=None) -> list[dict]:
    """全库扫描 -> episode 列表。quality_at(code, date)->int|None 为点时基本面档位。"""
    m = current()
    conn_codes = codes or sorted(ps.last_dates())
    reg = _regime_map()
    min_turn = MIN_TURNOVER.get(m.name, 5e6)
    episodes = []
    data = ps.load(conn_codes)
    log.info("形态扫描: %d 只票入库可用", len(data))
    pit_on = ps.pit_enabled()                         # 点时股票池 (ENGINE-UAT 第 0 步)
    log.info("%s", ps.pit_universe_line())
    pit_c = {"bars_out": 0, "codes_out": 0, "bars_st": 0, "codes_st": 0}
    for ci, (code, ser) in enumerate(data.items(), 1):
        dates, ohlcv = ser["dates"], ser["ohlcv"]
        n = len(dates)
        elig = ps.pit_mask(code, dates) if pit_on else None   # None = 关 / universe 表空 -> 旧行为
        if elig is None and pit_on:
            ps.pit_warn_unavailable('coilscan', log)
        elif elig is not None:
            if elig.n_out:
                pit_c["bars_out"] += elig.n_out
                pit_c["codes_out"] += 1
            if elig.n_st:
                pit_c["bars_st"] += elig.n_st
                pit_c["codes_st"] += 1
        o_, h_, l_, c_, v_ = (ohlcv[:, 0], ohlcv[:, 1], ohlcv[:, 2],
                              ohlcv[:, 3], ohlcv[:, 4])
        next_ok = 0
        t = TREND_W + BOX_W
        while t < n - 1:
            if t < next_ok:
                t += STRIDE
                continue
            if elig is not None and not elig.mask[t]:     # 点时: 当天不在市 / ST -> 不出信号
                t += STRIDE
                continue
            box_hi = float(np.max(h_[t - BOX_W + 1:t + 1]))
            box_lo = float(np.min(l_[t - BOX_W + 1:t + 1]))
            mid = (box_hi + box_lo) / 2.0
            if mid <= 0:
                t += STRIDE
                continue
            tight = (box_hi - box_lo) / mid
            close = c_[t]
            v_recent = float(np.mean(v_[t - 9:t + 1]))
            v_early = float(np.mean(v_[t - BOX_W + 1:t - 9]))
            turn = float(np.mean(v_[t - BOX_W + 1:t + 1] * c_[t - BOX_W + 1:t + 1]))
            cond = (tight <= TIGHT_MAX
                    and c_[t - TREND_W] > 0 and close >= c_[t - TREND_W] * (1 + TREND_MIN)
                    and v_early > 0 and v_recent / v_early <= VC_MAX
                    and box_lo + 0.3 * (box_hi - box_lo) <= close <= box_hi
                    and turn >= min_turn)
            if not cond:
                t += STRIDE
                continue
            ep = {"code": code, "date": dates[t], "tight": round(tight, 4),
                  "vc": round(v_recent / v_early, 3),
                  "trend": round(float(close / c_[t - TREND_W] - 1.0), 3)}
            if reg is not None:
                ri = int(np.searchsorted(np.array(reg[0]), dates[t], side="right")) - 1
                ep["regime"] = "bull" if (0 <= ri < len(reg[1]) and reg[1][ri]) else "bear"
            if quality_at is not None:
                try:
                    ep["q"] = quality_at(code, dates[t])
                except Exception:
                    ep["q"] = None
            ep.update(_episode_outcome(ohlcv, t, box_hi, box_lo))
            episodes.append(ep)
            next_ok = t + COOLDOWN
            t += STRIDE
        if ci % 500 == 0:
            log.info("形态扫描进度 %d/%d (episodes %d)", ci, len(data), len(episodes))
    log.info("形态扫描完成: %d episodes", len(episodes))
    if pit_on:
        log.info("coilscan 点时剔除: 不在市 %d 根 bar / %d 只, ST·退市整理期 %d 根 / %d 只",
                 pit_c["bars_out"], pit_c["codes_out"], pit_c["bars_st"], pit_c["codes_st"])
    return episodes


# ---------------------------------------------------------------------------
#  汇总矩阵
# ---------------------------------------------------------------------------
def _cell(eps: list[dict]) -> dict:
    broke = [e for e in eps if e["status"] in ("stopped", "held")]
    entered = [e for e in broke if e.get("complete")]
    out = {
        "n": len(eps),
        "break_rate": round(len(broke) / len(eps) * 100, 1) if eps else None,
        "broke_down_rate": round(sum(1 for e in eps if e["status"] == "broke_down")
                                 / len(eps) * 100, 1) if eps else None,
        "n_entered": len(entered),
    }
    if entered:
        out.update({
            "hit10": round(sum(1 for e in entered if e["hit10"]) / len(entered) * 100, 1),
            "hit20": round(sum(1 for e in entered if e["hit20"]) / len(entered) * 100, 1),
            "stop_rate": round(sum(1 for e in entered if e["status"] == "stopped")
                               / len(entered) * 100, 1),
            "avg_ret20": round(float(np.mean([e["ret20"] for e in entered])) * 100, 2),
            "med_ret20": round(float(np.median([e["ret20"] for e in entered])) * 100, 2),
            "p10_ret20": round(float(np.percentile([e["ret20"] for e in entered], 10)) * 100, 2),
            "avg_max_gain": round(float(np.mean([e["max_gain"] for e in entered])) * 100, 2),
        })
    return out


def _qbucket(q) -> str:
    if q is None:
        return "na"
    return "q3" if q >= 3 else ("q2" if q == 2 else "q01")


def summarize(episodes: list[dict]) -> dict:
    by = {"ALL": _cell(episodes)}
    for reg in ("bull", "bear"):
        by[f"regime:{reg}"] = _cell([e for e in episodes if e.get("regime") == reg])
    for qb in ("q3", "q2", "q01", "na"):
        by[f"quality:{qb}"] = _cell([e for e in episodes if _qbucket(e.get("q")) == qb])
    for tb, lo, hi in (("tight<=10%", 0, 0.10), ("tight10-15%", 0.10, 1)):
        by[f"box:{tb}"] = _cell([e for e in episodes if lo < e["tight"] <= hi])
    for vb, lo, hi in (("vc<=0.55", 0, 0.55), ("vc0.55-0.75", 0.55, 1)):
        by[f"vol:{vb}"] = _cell([e for e in episodes if lo < e["vc"] <= hi])
    # 用户核心问题: 优质 × 蓄势 (× 市场温度)
    for qb in ("q3", "q2"):
        for reg in ("bull", "bear"):
            by[f"combo:{qb}+{reg}"] = _cell([
                e for e in episodes
                if _qbucket(e.get("q")) == qb and e.get("regime") == reg])
    by["combo:q3+tight<=10%"] = _cell([
        e for e in episodes if _qbucket(e.get("q")) == "q3" and e["tight"] <= 0.10])
    by["combo:q3+vc<=0.55"] = _cell([
        e for e in episodes if _qbucket(e.get("q")) == "q3" and e["vc"] <= 0.55])
    by["combo:q3+bull+tight<=10%"] = _cell([
        e for e in episodes if _qbucket(e.get("q")) == "q3"
        and e.get("regime") == "bull" and e["tight"] <= 0.10])
    return by


def run(codes: list | None = None, quality_at=None, out_name: str = "coil_research.json") -> dict:
    m = current()
    episodes = scan(codes, quality_at=quality_at)
    matrix = summarize(episodes)
    result = {
        "meta": {"market": m.name, "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "params": {"box_w": BOX_W, "trend_w": TREND_W, "trend_min": TREND_MIN,
                            "tight_max": TIGHT_MAX, "vc_max": VC_MAX,
                            "break_wait": BREAK_WAIT, "hold": HOLD},
                 "n_episodes": len(episodes),
                 "caveats": "幸存者偏差(退市股缺席); 基本面按法定披露截止日滞后; 毛收益未扣成本"},
        "matrix": matrix,
    }
    out_path = os.path.join(m.data_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({**result, "episodes": episodes}, f, ensure_ascii=False)
    log.info("形态研究已写出: %s (%d episodes)", out_path, len(episodes))
    for k, v in matrix.items():
        if v.get("n_entered"):
            log.info("  %-26s n=%-5d 突破率%5.1f%% | 入场%4d: +10%%达标 %5.1f%% 止损 %5.1f%% 平均 %6.2f%%",
                     k, v["n"], v["break_rate"], v["n_entered"],
                     v["hit10"], v["stop_rate"], v["avg_ret20"])
    return result
