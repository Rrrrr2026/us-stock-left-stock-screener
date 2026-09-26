#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exitgrid — 出场三参数联合网格 · 九年重放 (M4 前置, 二档最高优先)
裁决对象: 止损放宽(exits1) / 目标随止损缩放(exits2) / 窗口缩短 / tp1分批(strong-left1)。
协议 (对抗审核版): 两入场族分开跑; 同一bar序列上对每个止损策略一次遍历同时判定
全部目标×窗口格 + tp1对照臂; 2017-2022 选参(IS) vs 2023起样本外(OOS); 牛熊分列;
同bar双触发先止损(与生产一致); 扣往返成本。
幸存者声明: 价格库仅含现存股票, 深跌族绝对期望系统性偏乐观 — 本研究只做格间相对比较,
严禁引用绝对EV。
输出: data/exitgrid_research.json
"""
from __future__ import annotations

import json
import logging
import os

import numpy as np

from . import pricestore as ps
from .market import current

log = logging.getLogger("leftside_core.exitgrid")

STOPS = (("s18f5", 1.8, 0.05), ("s25f7", 2.5, 0.07), ("s30f8", 3.0, 0.08))
TGT_FIXED = (0.07, 0.10)
TGT_RMULT = 1.4                 # 目标 = 1.4×实际止损距
WINDOWS = (10, 15, 20, 30)
HOLD = 30
ENTRY_WAIT = 10
STRIDE = 2
COOLDOWN = 20
OOS_FROM = "2023-01-01"
MIN_TURNOVER = {"ashare": 30e6, "us": 5e6}

# 回踩族 (与 slscan 同口径)
MA_W, NEAR_LO, NEAR_HI, ABOVE_MIN = 60, 0.99, 1.03, 1.05
PB_RSI_MAX, PB_DD_MAX = 45.0, 0.45
# 深跌族 (生产 dip 代理: 回撤>=35% + RSI<=32 + 52周底部20%内)
DIP_DD_MIN, DIP_RSI_MAX, DIP_POS_MAX = 0.35, 32.0, 0.20


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
    idx = ps.load_index()
    if not idx:
        return None
    closes = idx["ohlcv"][:, 3]
    ma50 = np.convolve(closes, np.ones(50) / 50, mode="valid")
    bull = np.zeros(len(closes), dtype=bool)
    bull[49:] = closes[49:] >= ma50
    return idx["dates"], bull


def _sim(o, h, l, c, fill_i, fill, ref, a, deepen_px, cost):
    """一次成交, 对全部 止损策略×目标×窗口 + tp1 求结果。返回 {cellkey: (ret,days,st)}"""
    n = len(c)
    out = {}
    for sk, k, floor in STOPS:
        sret = float(np.clip(k * a, floor, 0.15))
        stop = fill * (1.0 - sret)
        if deepen_px is not None:
            stop = max(min(stop, deepen_px), fill * (1.0 - 0.15))
            sret = 1.0 - stop / fill
        tgts = {f"t{int(g*100)}": fill * (1.0 + g) for g in TGT_FIXED}
        tgts["tR"] = fill * (1.0 + TGT_RMULT * sret)
        istop = None
        itgt = {tk: None for tk in tgts}
        i5 = None
        end = min(fill_i + HOLD, n - 1)
        j = fill_i + 1
        while j <= end:
            if l[j] <= stop:
                istop = j
                break                              # 同bar先止损 (保守, 与生产一致)
            for tk, tp in tgts.items():
                if itgt[tk] is None and h[j] >= tp:
                    itgt[tk] = j
            if i5 is None and h[j] >= fill * 1.05:
                i5 = j
            j += 1
        for tk, tp in tgts.items():
            g = tp / fill - 1.0
            for w in WINDOWS:
                if fill_i + w > n - 1:
                    continue
                endw = fill_i + w
                it = itgt[tk]
                if it is not None and it <= endw and (istop is None or it < istop):
                    out[f"{sk}|{tk}|w{w}"] = (g - cost, it - fill_i, "won")
                elif istop is not None and istop <= endw:
                    out[f"{sk}|{tk}|w{w}"] = (stop / fill - 1.0 - cost, istop - fill_i, "stopped")
                else:
                    out[f"{sk}|{tk}|w{w}"] = (float(c[endw]) / fill - 1.0 - cost, w, "expired")
        # tp1 对照臂: +5%落袋一半->保本, 余仓搏+10%, 窗口20
        if fill_i + 20 <= n - 1:
            end20 = fill_i + 20
            if i5 is not None and i5 <= end20 and (istop is None or i5 < istop):
                r1 = 0.05
                be_i = t10_i = None
                jj = i5
                while jj <= end20:
                    if jj > i5 and l[jj] <= fill:
                        be_i = jj
                        break
                    if h[jj] >= fill * 1.10 and jj > i5:
                        t10_i = jj
                        break
                    jj += 1
                if t10_i is not None:
                    out[f"{sk}|tp1|w20"] = (0.5 * r1 + 0.5 * 0.10 - cost, t10_i - fill_i, "won")
                elif be_i is not None:
                    out[f"{sk}|tp1|w20"] = (0.5 * r1 + 0.0 - cost, be_i - fill_i, "expired")
                else:
                    out[f"{sk}|tp1|w20"] = (0.5 * r1 + 0.5 * (float(c[end20]) / fill - 1.0) - cost,
                                            20, "expired")
            elif istop is not None and istop <= end20:
                out[f"{sk}|tp1|w20"] = (stop / fill - 1.0 - cost, istop - fill_i, "stopped")
            elif fill_i + 20 <= n - 1:
                out[f"{sk}|tp1|w20"] = (float(c[end20]) / fill - 1.0 - cost, 20, "expired")
    return out


def scan():
    m = current()
    reg = _regime_map()
    min_turn = MIN_TURNOVER.get(m.name, 5e6)
    cost = m.cost_rt
    data = ps.load(sorted(ps.last_dates()))
    log.info("exitgrid: %d 只票", len(data))
    pit_on = ps.pit_enabled()                         # 点时股票池 (ENGINE-UAT 第 0 步)
    log.info("%s", ps.pit_universe_line())
    pit_c = {"bars_out": 0, "codes_out": 0, "bars_st": 0, "codes_st": 0}
    from numpy.lib.stride_tricks import sliding_window_view
    episodes = []
    for ci, (code, ser) in enumerate(data.items(), 1):
        dates, ohlcv = ser["dates"], ser["ohlcv"]
        n = len(dates)
        if n < 320:
            continue
        elig = ps.pit_mask(code, dates) if pit_on else None   # None = 关 / universe 表空 -> 旧行为
        if elig is None and pit_on:
            ps.pit_warn_unavailable('exitgrid', log)
        elif elig is not None:
            if elig.n_out:
                pit_c["bars_out"] += elig.n_out
                pit_c["codes_out"] += 1
            if elig.n_st:
                pit_c["bars_st"] += elig.n_st
                pit_c["codes_st"] += 1
        o, h, l, c, v = (ohlcv[:, 0], ohlcv[:, 1], ohlcv[:, 2], ohlcv[:, 3], ohlcv[:, 4])
        ma, rsi, atr = _ind(o, h, l, c)
        roll_hi = np.full(n, np.nan)
        roll_lo = np.full(n, np.nan)
        if n >= 250:
            roll_hi[249:] = sliding_window_view(h, 250).max(axis=1)
            roll_lo[249:] = sliding_window_view(l, 250).min(axis=1)
        next_ok = {"pb": 300, "dip": 300}
        t = 300
        while t < n - 2:
            if np.isnan(rsi[t]) or np.isnan(atr[t]) or np.isnan(roll_hi[t]) or c[t] <= 0:
                t += STRIDE
                continue
            if elig is not None and not elig.mask[t]:     # 点时: 当天不在市 / ST -> 不出信号
                t += STRIDE
                continue
            turn_ok = float(np.mean(v[t - 19:t + 1] * c[t - 19:t + 1])) >= min_turn
            if not turn_ok:
                t += STRIDE
                continue
            a = float(np.clip(atr[t] / c[t], 0.008, 0.08))
            fam = None
            if (t >= next_ok["dip"]
                    and c[t] <= roll_hi[t] * (1.0 - DIP_DD_MIN) and rsi[t] <= DIP_RSI_MAX
                    and not np.isnan(roll_lo[t]) and roll_hi[t] > roll_lo[t]
                    and (c[t] - roll_lo[t]) / (roll_hi[t] - roll_lo[t]) <= DIP_POS_MAX):
                fam = "dip"
                e = t + 1
                fill_i, fill = e, float(o[e])
                ref, deepen = fill, None
            elif (t >= next_ok["pb"] and not np.isnan(ma[t]) and ma[t] > 0
                    and NEAR_LO * ma[t] <= c[t] <= NEAR_HI * ma[t]
                    and float(np.max(c[t - 10:t])) >= ma[t] * ABOVE_MIN
                    and rsi[t] <= PB_RSI_MAX
                    and c[t] >= roll_hi[t] * (1.0 - PB_DD_MAX)):
                fam = "pb"
                ref = float(ma[t])
                ehigh = ref * (1.0 + 0.5 * a)
                worst_stop = ref * (1.0 - 0.15)
                fill_i = fill = None
                i = t + 1
                while i < n and i <= t + ENTRY_WAIT:
                    if o[i] <= worst_stop:
                        break
                    if l[i] <= ehigh:
                        fill_i, fill = i, min(float(o[i]), ehigh)
                        break
                    i += 1
                deepen = ref * 0.97 * 0.995
            if fam is None or fill_i is None or fill is None or fill <= 0:
                t += STRIDE
                continue
            if fill_i + WINDOWS[0] > n - 1:
                t += STRIDE
                continue
            cells = _sim(o, h, l, c, fill_i, fill, ref, a, deepen, cost)
            if cells:
                ep = {"fam": fam, "date": dates[t], "code": code, "cells": cells,
                      # 生产 ⚡快弹 子标签口径: dip 且 RSI<=28 且 ATR%>=5
                      "fast": bool(fam == "dip" and rsi[t] <= 28.0
                                   and atr[t] / c[t] >= 0.05)}
                if reg is not None:
                    ri = int(np.searchsorted(np.array(reg[0]), dates[t], side="right")) - 1
                    ep["regime"] = "bull" if (0 <= ri < len(reg[1]) and reg[1][ri]) else "bear"
                episodes.append(ep)
                next_ok[fam] = fill_i + HOLD + COOLDOWN
            t += STRIDE
        if ci % 800 == 0:
            log.info("exitgrid %d/%d (eps %d)", ci, len(data), len(episodes))
    log.info("exitgrid 完成: %d episodes", len(episodes))
    if pit_on:
        log.info("exitgrid 点时剔除: 不在市 %d 根 bar / %d 只, ST·退市整理期 %d 根 / %d 只",
                 pit_c["bars_out"], pit_c["codes_out"], pit_c["bars_st"], pit_c["codes_st"])
    return episodes


def _agg(rows):
    if len(rows) < 30:
        return None
    rets = [r[0] for r in rows]
    return {"n": len(rows),
            "win": round(sum(1 for r in rows if r[2] == "won") / len(rows), 4),
            "stop_rate": round(sum(1 for r in rows if r[2] == "stopped") / len(rows), 4),
            "avg_ret": round(float(np.mean(rets)), 4),
            "med_days": int(np.median([r[1] for r in rows]))}


def summarize(episodes):
    out = {}
    for fam in ("dip", "pb"):
        eps = [e for e in episodes if e["fam"] == fam]
        cellkeys = set()
        for e in eps[:200]:
            cellkeys.update(e["cells"].keys())
        fam_out = {}
        for ck in sorted(cellkeys):
            groups = {"all": [], "is": [], "oos": [], "bull": [], "bear": []}
            for e in eps:
                r = e["cells"].get(ck)
                if r is None:
                    continue
                groups["all"].append(r)
                groups["is" if e["date"] < OOS_FROM else "oos"].append(r)
                groups[e.get("regime", "bear")].append(r)
            fam_out[ck] = {g: _agg(rows) for g, rows in groups.items()}
        out[fam] = {"n_episodes": len(eps), "cells": fam_out}
    out["_meta"] = {"note": "幸存者偏差: 仅现存股票, 深跌族绝对EV偏乐观 — 只做格间相对比较",
                    "oos_from": OOS_FROM, "cost_rt": current().cost_rt}
    return out


def run(out_name="exitgrid_research.json"):
    eps = scan()
    res = summarize(eps)
    path = os.path.join(current().data_dir, out_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    log.info("exitgrid -> %s", path)
    return res
