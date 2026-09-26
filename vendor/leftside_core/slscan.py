#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""slscan — 「强左侧」策略类的九年重放 (M3 研究)
==============================================
问题: 两个月线上回测显示强左侧 reach5=74% 但 win10 仅 40%——alpha 集中在头 +5%。
短验证(信号重放)表明: +4~5% 目标胜率 65-79%, 每笔期望与 +10% 打平但持有时间减半。
本引擎在九年价格库上验证该结论是否长期成立。

形态代理 (线上 tech_score>=2.5 不可逐日复算, 用其本质特征代替):
  优质股(点时基本面档) 从上方回踩到 MA60 支撑带, 非崩落状态, RSI 显示回调而非强势。
入场/止损与生产 tradeplan 同公式; 出场一次遍历同时记录 {+4%,+5%,+10%} x {15,20bar}。
输出: data/sl_research.json (总矩阵 / 按牛熊 / 按基本面档 / 按年份)。
"""
from __future__ import annotations

import json
import logging
import os

import numpy as np

from . import pricestore as ps
from .market import current

log = logging.getLogger("leftside_core.slscan")

# ---- 形态参数 ----
MA_W = 60                  # 支撑代理: 60日均线
NEAR_LO, NEAR_HI = 0.99, 1.03   # 收盘落在 MA60 的 [-1%, +3%] 支撑带
ABOVE_MIN = 1.05           # 此前10bar内明确在支撑上方 (>=+5%) -> 确为"回踩"
RSI_MAX = 45.0             # 回调而非强势 (与深跌 rsi<=32 区分)
DD_MAX = 0.45              # 距250日高回撤 <=45%: 排除崩落股 (那是深跌抄底的地盘)
STRIDE = 2                 # 隔2bar检一次
COOLDOWN = 20              # 事件结束后冷却bar数
ENTRY_WAIT = 10            # 回踩限价等待窗口 (与生产 ENTRY_VALID_BARS 一致)
HOLD = 20                  # 最大窗口
TARGETS = (0.04, 0.05, 0.10)
WINDOWS = (15, 20)
MIN_TURNOVER = {"ashare": 30e6, "us": 5e6}


def _wilder(x: np.ndarray, w: int) -> np.ndarray:
    out = np.empty_like(x)
    out[:w] = np.nan
    if len(x) <= w:
        return out
    prev = float(np.mean(x[1:w + 1]))
    out[w] = prev
    a = 1.0 / w
    for i in range(w + 1, len(x)):
        prev = prev + a * (x[i] - prev)
        out[i] = prev
    return out


def _indicators(o, h, l, c):
    n = len(c)
    ma = np.full(n, np.nan)
    if n >= MA_W:
        cs = np.cumsum(np.insert(c, 0, 0.0))
        ma[MA_W - 1:] = (cs[MA_W:] - cs[:-MA_W]) / MA_W
    d = np.diff(c, prepend=c[0])
    gain = _wilder(np.where(d > 0, d, 0.0), 14)
    loss = _wilder(np.where(d < 0, -d, 0.0), 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100.0 - 100.0 / (1.0 + gain / np.where(loss == 0, np.nan, loss))
    rsi = np.where((loss == 0) & (gain > 0), 100.0, rsi)
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr = _wilder(tr, 14)
    return ma, rsi, atr


def _regime_map():
    """与 coilscan 相同口径: 指数收盘 vs 50日均线 (数据来自 pricestore.idx_bars)。"""
    idx = ps.load_index()
    if not idx:
        return None
    closes = idx["ohlcv"][:, 3]
    ma50 = np.convolve(closes, np.ones(50) / 50, mode="valid")
    bull = np.zeros(len(closes), dtype=bool)
    bull[49:] = closes[49:] >= ma50
    return idx["dates"], bull


def _episode(o, h, l, c, t, ma_t, atr_t, cost):
    """从信号bar t 起: 限价回踩入场 -> 一次遍历记录全部出场网格的结果。"""
    a = float(np.clip(atr_t / c[t], 0.008, 0.08))
    ref = float(ma_t)
    entry_high = ref * (1.0 + 0.5 * a)
    sret = float(np.clip(1.8 * a, 0.05, 0.15))
    stop = ref * (1.0 - sret)
    stop = min(stop, ref * 0.97 * 0.995)          # 支撑破位加深 (生产 breakdown 同型)
    stop = max(stop, ref * (1.0 - 0.15))
    n = len(c)

    fill_i = fill_px = None
    i = t + 1
    while i < n and i <= t + ENTRY_WAIT:
        if o[i] <= stop:
            return None                            # 开盘破止损: 不接飞刀
        if l[i] <= entry_high:
            fill_i, fill_px = i, min(float(o[i]), entry_high)
            break
        i += 1
    if fill_i is None or fill_i + HOLD > n - 1:
        return None                                # 未成交 / 窗口不完整

    first_stop = None
    first_tgt = {g: None for g in TARGETS}
    end20 = fill_i + HOLD
    j = fill_i + 1
    while j <= end20:
        if l[j] <= stop and first_stop is None:
            first_stop = j                          # 同bar双触发按先止损 (保守)
            break
        for g in TARGETS:
            if first_tgt[g] is None and h[j] >= fill_px * (1.0 + g):
                first_tgt[g] = j
        j += 1

    out = {}
    for g in TARGETS:
        for w in WINDOWS:
            endw = fill_i + w
            it, si = first_tgt[g], first_stop
            if it is not None and it <= endw and (si is None or it < si):
                ret, days, st = g, it - fill_i, "won"
            elif si is not None and si <= endw:
                ret, days, st = stop / fill_px - 1.0, si - fill_i, "stopped"
            else:
                ret, days, st = float(c[endw]) / fill_px - 1.0, w, "expired"
            out[f"{int(g*100)}_{w}"] = (round(ret - cost, 4), days, st)
    return {"fill_i": fill_i, "grid": out}


def scan(codes=None, quality_at=None):
    m = current()
    reg = _regime_map()
    min_turn = MIN_TURNOVER.get(m.name, 5e6)
    cost = m.cost_rt
    data = ps.load(codes or sorted(ps.last_dates()))
    log.info("slscan: %d 只票入库可用", len(data))
    pit_on = ps.pit_enabled()                         # 点时股票池 (ENGINE-UAT 第 0 步)
    log.info("%s", ps.pit_universe_line())
    pit_c = {"bars_out": 0, "codes_out": 0, "bars_st": 0, "codes_st": 0}
    episodes = []
    for ci, (code, ser) in enumerate(data.items(), 1):
        dates, ohlcv = ser["dates"], ser["ohlcv"]
        n = len(dates)
        if n < 300:
            continue
        elig = ps.pit_mask(code, dates) if pit_on else None   # None = 关 / universe 表空 -> 旧行为
        if elig is None and pit_on:
            ps.pit_warn_unavailable('slscan', log)
        elif elig is not None:
            if elig.n_out:
                pit_c["bars_out"] += elig.n_out
                pit_c["codes_out"] += 1
            if elig.n_st:
                pit_c["bars_st"] += elig.n_st
                pit_c["codes_st"] += 1
        o, h, l, c, v = (ohlcv[:, 0], ohlcv[:, 1], ohlcv[:, 2], ohlcv[:, 3], ohlcv[:, 4])
        ma, rsi, atr = _indicators(o, h, l, c)
        roll_hi = np.full(n, np.nan)
        from numpy.lib.stride_tricks import sliding_window_view
        if n >= 250:
            roll_hi[249:] = sliding_window_view(h, 250).max(axis=1)
        next_ok = 300
        t = 300
        while t < n - 1:
            if t < next_ok or np.isnan(ma[t]) or np.isnan(rsi[t]) or np.isnan(atr[t]):
                t += STRIDE
                continue
            if elig is not None and not elig.mask[t]:     # 点时: 当天不在市 / ST -> 不出信号
                t += STRIDE
                continue
            ma_t = ma[t]
            close = c[t]
            cond = (ma_t > 0
                    and NEAR_LO * ma_t <= close <= NEAR_HI * ma_t
                    and float(np.max(c[t - 10:t])) >= ma_t * ABOVE_MIN
                    and rsi[t] <= RSI_MAX
                    and not np.isnan(roll_hi[t]) and close >= roll_hi[t] * (1.0 - DD_MAX)
                    and float(np.mean(v[t - 19:t + 1] * c[t - 19:t + 1])) >= min_turn)
            if not cond:
                t += STRIDE
                continue
            r = _episode(o, h, l, c, t, ma_t, atr[t], cost)
            if r is None:
                next_ok = t + 5
                t += STRIDE
                continue
            ep = {"code": code, "date": dates[t], "year": dates[t][:4]}
            if reg is not None:
                ri = int(np.searchsorted(np.array(reg[0]), dates[t], side="right")) - 1
                ep["regime"] = "bull" if (0 <= ri < len(reg[1]) and reg[1][ri]) else "bear"
            if quality_at is not None:
                try:
                    ep["q"] = quality_at(code, dates[t])
                except Exception:
                    ep["q"] = None
            ep["grid"] = r["grid"]
            episodes.append(ep)
            next_ok = r["fill_i"] + HOLD + COOLDOWN
            t += STRIDE
        if ci % 500 == 0:
            log.info("slscan 进度 %d/%d (episodes %d)", ci, len(data), len(episodes))
    log.info("slscan 完成: %d episodes", len(episodes))
    if pit_on:
        log.info("slscan 点时剔除: 不在市 %d 根 bar / %d 只, ST·退市整理期 %d 根 / %d 只",
                 pit_c["bars_out"], pit_c["codes_out"], pit_c["bars_st"], pit_c["codes_st"])
    return episodes


def _cell(eps, key):
    rows = [e["grid"][key] for e in eps if key in e.get("grid", {})]
    if not rows:
        return None
    rets = [r[0] for r in rows]
    return {"n": len(rows), "win": round(sum(1 for r in rows if r[2] == "won") / len(rows), 4),
            "avg_ret": round(float(np.mean(rets)), 4),
            "med_days": int(np.median([r[1] for r in rows])),
            "stop_rate": round(sum(1 for r in rows if r[2] == "stopped") / len(rows), 4)}


def summarize(episodes):
    keys = [f"{int(g*100)}_{w}" for g in TARGETS for w in WINDOWS]
    out = {"n_episodes": len(episodes), "grid": {}, "by_regime": {}, "by_q": {}, "by_year": {}}
    for k in keys:
        out["grid"][k] = _cell(episodes, k)
    for dim, fn in (("by_regime", lambda e: e.get("regime", "na")),
                    ("by_q", lambda e: str(e.get("q"))),
                    ("by_year", lambda e: e.get("year"))):
        groups = {}
        for e in episodes:
            groups.setdefault(fn(e), []).append(e)
        for gname, eps in sorted(groups.items()):
            out[dim][gname] = {k: _cell(eps, k) for k in ("4_15", "5_20", "10_20")}
    return out


def run(codes=None, quality_at=None, out_name="sl_research.json"):
    episodes = scan(codes, quality_at=quality_at)
    res = summarize(episodes)
    path = os.path.join(current().data_dir, out_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"summary": res, "episodes": episodes}, f, ensure_ascii=False)
    log.info("slscan 结果 -> %s", path)
    return res
