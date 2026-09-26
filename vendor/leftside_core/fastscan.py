#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fastscan — ⚡快弹 信号九年验证 (M3.5)
两月挖掘 (2026-08-30) 发现: 深跌(回撤深+超卖) & 高波动 的信号, 成交后3个交易日内
先摸+5%的概率 ~50% (最优格62%), 两市方向一致。本引擎在长历史上验证:
  ① 该概率是否跨年份/牛熊稳定  ② "摸+5%即卖"整套策略(与止损耦合)的真实期望。
入场口径与生产深跌一致: 信号bar次日开盘市价入场, 止损=clip(1.8×ATR,5%,15%)。
输出: data/fast_research.json
"""
from __future__ import annotations

import json
import logging
import os

import numpy as np

from . import pricestore as ps
from .market import current

log = logging.getLogger("leftside_core.fastscan")

DD_MIN = 0.35          # 距250日高回撤下限
RSI_MAX = 28.0
ATR_MIN = 0.05         # ATR/价 ≥5%
FAST_BARS = 3          # "快"的定义: 成交后3个交易日内
EV_HOLD = 5            # 快弹策略的最长持有 (bar)
STRIDE = 1
COOLDOWN = 10
MIN_TURNOVER = {"ashare": 30e6, "us": 5e6}


def _wilder(x, w):
    out = np.full_like(x, np.nan)
    if len(x) <= w:
        return out
    prev = float(np.mean(x[1:w + 1]))
    out[w] = prev
    a = 1.0 / w
    for i in range(w + 1, len(x)):
        prev = prev + a * (x[i] - prev)
        out[i] = prev
    return out


def _ind(o, h, l, c):
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
    return rsi, atr


def _regime_map():
    idx = ps.load_index()
    if not idx:
        return None
    closes = idx["ohlcv"][:, 3]
    ma50 = np.convolve(closes, np.ones(50) / 50, mode="valid")
    bull = np.zeros(len(closes), dtype=bool)
    bull[49:] = closes[49:] >= ma50
    return idx["dates"], bull


def scan(codes=None):
    m = current()
    reg = _regime_map()
    min_turn = MIN_TURNOVER.get(m.name, 5e6)
    cost = m.cost_rt
    data = ps.load(codes or sorted(ps.last_dates()))
    log.info("fastscan: %d 只票", len(data))
    pit_on = ps.pit_enabled()                         # 点时股票池 (ENGINE-UAT 第 0 步)
    log.info("%s", ps.pit_universe_line())
    pit_c = {"bars_out": 0, "codes_out": 0, "bars_st": 0, "codes_st": 0}
    episodes = []
    from numpy.lib.stride_tricks import sliding_window_view
    for ci, (code, ser) in enumerate(data.items(), 1):
        dates, ohlcv = ser["dates"], ser["ohlcv"]
        n = len(dates)
        if n < 300:
            continue
        elig = ps.pit_mask(code, dates) if pit_on else None   # None = 关 / universe 表空 -> 旧行为
        if elig is None and pit_on:
            ps.pit_warn_unavailable('fastscan', log)
        elif elig is not None:
            if elig.n_out:
                pit_c["bars_out"] += elig.n_out
                pit_c["codes_out"] += 1
            if elig.n_st:
                pit_c["bars_st"] += elig.n_st
                pit_c["codes_st"] += 1
        o, h, l, c, v = (ohlcv[:, 0], ohlcv[:, 1], ohlcv[:, 2], ohlcv[:, 3], ohlcv[:, 4])
        rsi, atr = _ind(o, h, l, c)
        roll_hi = np.full(n, np.nan)
        if n >= 250:
            roll_hi[249:] = sliding_window_view(h, 250).max(axis=1)
        next_ok = 300
        t = 300
        while t < n - EV_HOLD - 2:
            if (t < next_ok or np.isnan(rsi[t]) or np.isnan(atr[t])
                    or np.isnan(roll_hi[t]) or c[t] <= 0):
                t += STRIDE
                continue
            if elig is not None and not elig.mask[t]:     # 点时: 当天不在市 / ST -> 不出信号
                t += STRIDE
                continue
            cond = (c[t] <= roll_hi[t] * (1.0 - DD_MIN)
                    and rsi[t] <= RSI_MAX
                    and atr[t] / c[t] >= ATR_MIN
                    and float(np.mean(v[t - 19:t + 1] * c[t - 19:t + 1])) >= min_turn)
            if not cond:
                t += STRIDE
                continue
            e = t + 1                                  # 次日开盘市价入场
            fill = float(o[e])
            if fill <= 0:
                t += STRIDE
                continue
            a = float(np.clip(atr[t] / c[t], 0.008, 0.08))
            stop = fill * (1.0 - float(np.clip(1.8 * a, 0.05, 0.15)))
            if fill <= stop:
                t += STRIDE
                continue
            t5 = ts = None
            end = min(e + EV_HOLD, n - 1)
            for j in range(e + 1, end + 1):
                if l[j] <= stop and ts is None:
                    ts = j
                    break                              # 同bar双触发按先止损 (保守)
                if t5 is None and h[j] >= fill * 1.05:
                    t5 = j
                    break                              # 摸到+5%即卖
            fast = bool(t5 is not None and (t5 - e) <= FAST_BARS)
            if t5 is not None:
                ret, dys = 0.05 - cost, t5 - e
            elif ts is not None:
                ret, dys = stop / fill - 1.0 - cost, ts - e
            else:
                ret, dys = float(c[end]) / fill - 1.0 - cost, end - e
            ep = {"code": code, "date": dates[t], "year": dates[t][:4],
                  "fast": fast, "won5": bool(t5 is not None),
                  "stopped": bool(ts is not None), "ret": round(ret, 4), "days": int(dys)}
            if reg is not None:
                ri = int(np.searchsorted(np.array(reg[0]), dates[t], side="right")) - 1
                ep["regime"] = "bull" if (0 <= ri < len(reg[1]) and reg[1][ri]) else "bear"
            episodes.append(ep)
            next_ok = end + COOLDOWN
            t += STRIDE
        if ci % 800 == 0:
            log.info("fastscan %d/%d (eps %d)", ci, len(data), len(episodes))
    log.info("fastscan 完成: %d episodes", len(episodes))
    if pit_on:
        log.info("fastscan 点时剔除: 不在市 %d 根 bar / %d 只, ST·退市整理期 %d 根 / %d 只",
                 pit_c["bars_out"], pit_c["codes_out"], pit_c["bars_st"], pit_c["codes_st"])
    return episodes


def _cell(eps):
    n = len(eps)
    if not n:
        return None
    return {"n": n,
            "fast5_3": round(sum(1 for e in eps if e["fast"]) / n, 4),
            "won5": round(sum(1 for e in eps if e["won5"]) / n, 4),
            "stop_rate": round(sum(1 for e in eps if e["stopped"]) / n, 4),
            "avg_ret": round(float(np.mean([e["ret"] for e in eps])), 4),
            "med_days": int(np.median([e["days"] for e in eps]))}


def run(codes=None, out_name="fast_research.json"):
    eps = scan(codes)
    out = {"pool": _cell(eps), "by_year": {}, "by_regime": {}}
    for dim, fn in (("by_year", lambda e: e["year"]),
                    ("by_regime", lambda e: e.get("regime", "na"))):
        g = {}
        for e in eps:
            g.setdefault(fn(e), []).append(e)
        for k, sub in sorted(g.items()):
            out[dim][k] = _cell(sub)
    path = os.path.join(current().data_dir, out_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"summary": out, "episodes": eps}, f, ensure_ascii=False)
    log.info("fastscan -> %s", path)
    return out
