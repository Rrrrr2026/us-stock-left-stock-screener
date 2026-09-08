#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双周量化组合 (Bi-weekly quant portfolio, M2)
=============================================
每 10 个交易日一期: 从当日榜单选 <=6 只, 次日开盘等额买入, 期内只做止损保护,
期末收盘全部结算 —— 规则刻意简单, 让"选股信号的质量"成为唯一变量。

选股打分 (全部来自系统已有的、每日更新的统计, 阈值可调):
  * 信号战绩 seg_win: 该票 标签×成长质量 组合的历史达标率 (backtest_result.json,
    样本 n>=SEG_N_MIN 才有资格; 与"回测优选"同口径)
  * P20: 该票自身历史上 30bar 内涨 20% 的频率 (prob20)
  * 质量: 当日优质榜在榜 +2 / 错杀候选 +1
  * 蓄势加成: 蓄势待发标签, 且形态研究矩阵 (coil_research.json) 里本市场
    "缩量蓄势"先验达标时加分
  * 市场温度门: 温度计/风险偏好 分数 -> 满仓 / 半仓 / 空仓 (跳过本期)

诚实声明: 纸面推演。每期规则 = 次日开盘买入(A股一字涨停买不进则放弃)、
止损单挂计划止损价 (无计划则 -10%)、期末收盘结算、含交易成本。
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os

import numpy as np

from .market import current
from . import backtest as bt

log = logging.getLogger("leftside_core.biweekly")

CYCLE_BARS = 10             # 一期 = 10 个交易日
MAX_PICKS = 6
IND_CAP = 2                 # 每行业最多 2 只
SEG_N_MIN = 12              # 信号战绩最小样本
SEG_WIN_MIN = 45.0          # 信号历史达标率下限 (%)
STOP_FALLBACK = 0.10        # 无计划止损时 -10%
TEMP_FULL, TEMP_HALF = 55.0, 40.0   # 温度门: >=55 满仓, 40-55 半仓, <40 空仓


def _paths() -> tuple[str, str]:
    m = current()
    return (os.path.join(m.data_dir, "biweekly_state.json"),
            os.path.join(m.dashboard_dir, "biweekly_data.js"))


def _budget() -> tuple[float, int, str]:
    if current().name == "ashare":
        return 70000.0, 100, "¥"
    return 10000.0, 1, "$"


def _load_state(p: str) -> dict:
    try:
        st = json.load(open(p, encoding="utf-8"))
        if isinstance(st.get("cycles"), list):
            return st
    except Exception:
        pass
    return {"cycles": []}


def _load_json(path: str):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def _seg_stats() -> dict:
    """backtest_result.json -> {(tag|growth): {"n":…, "win":…%}} (完整窗口已了结口径)。"""
    m = current()
    j = _load_json(os.path.join(m.data_dir, "backtest_result.json")) or {}
    out = {}
    agg = j.get("agg") or {}
    pool = ((agg.get("pool") or {}).get("win10"))
    out["__pool__"] = float(pool) * 100.0 if pool is not None else None
    for key, seg in (agg.get("by_combo") or {}).items():
        if isinstance(seg, dict) and seg.get("n_resolved") and seg.get("win10") is not None:
            out[key] = {"n": seg["n_resolved"], "win": float(seg["win10"]) * 100.0,
                        "avg_ret": seg.get("avg_ret")}
    return out


def _coil_prior() -> dict | None:
    """本市场形态研究先验: 缩量蓄势 cell (vc<=0.55)。"""
    m = current()
    j = _load_json(os.path.join(m.data_dir, "coil_research.json"))
    if not j:
        return None
    return (j.get("matrix") or {}).get("vol:vc<=0.55")


def _temperature(payload: dict) -> float | None:
    """市场温度: A股 = 机会温度计 score; 美股 = 风险偏好 score。"""
    m = current()
    if m.name == "ashare":
        opp = ((payload.get("meta") or {}).get("opp") or {})
        return opp.get("score")
    s = _load_json(os.path.join(m.data_dir, "sentiment_result.json")) or {}
    return s.get("score")


def _trading_bars_since(d0: str) -> int | None:
    from . import pricestore as ps
    idx = ps.load_index()
    if not idx:
        return None
    dates = idx["dates"]
    import bisect
    i0 = bisect.bisect_left(dates, d0)
    return max(0, len(dates) - 1 - i0)


def select(payload: dict, quality_picks: list, seg: dict, coil_prior: dict | None,
           temp: float | None) -> tuple[list[dict], float, str]:
    """-> (picks, budget_scale, gate_note)。纯函数, 方便回放与测试。"""
    m = current()
    if temp is None:
        scale, note = 0.5, "温度未知->半仓"
    elif temp >= TEMP_FULL:
        scale, note = 1.0, f"温度{temp:.0f}>= {TEMP_FULL:.0f} 满仓"
    elif temp >= TEMP_HALF:
        scale, note = 0.5, f"温度{temp:.0f} 半仓"
    else:
        return [], 0.0, f"温度{temp:.0f}<{TEMP_HALF:.0f} 本期空仓"

    ql_codes = {p.get("code") for p in quality_picks}
    coil_ok = bool(coil_prior and coil_prior.get("n_entered", 0) >= 30
                   and (coil_prior.get("hit10") or 0) >= 25.0)
    pool = seg.get("__pool__")
    win_bar = max(SEG_WIN_MIN, pool + 3.0) if pool else SEG_WIN_MIN   # 必须显著好于全池
    scored = []
    for c in payload.get("candidates") or []:
        code, tag = c.get("code"), (c.get("tag") or "").strip()
        gt = m.growth_tier.get(c.get("growth_quality"), "NA")
        s = seg.get(f"{tag}|{gt}")
        if not s or s["n"] < SEG_N_MIN or (s["win"] or 0) < win_bar:
            continue
        px = c.get("price")
        if m.name == "us" and (not isinstance(px, (int, float)) or px < 3.0):
            continue                                  # 美股仙股不进组合
        if c.get("earn_days") is not None and 0 <= c["earn_days"] <= 10:
            continue                                  # 期内出财报的不进组合
        score = float(s["win"])
        p20 = c.get("cuosha_p20") if c.get("cuosha_p20") is not None else c.get("p20")
        if isinstance(p20, (int, float)):
            score += min(15.0, p20 * 0.3)
        if code in ql_codes:
            score += 8.0
        if c.get("cuosha_score"):
            score += 4.0
        if "蓄势待发" in tag and coil_ok:
            score += 6.0
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    picks, per_ind = [], {}
    for score, c in scored:
        ind = c.get("industry") or "?"
        if per_ind.get(ind, 0) >= IND_CAP:
            continue
        plan = c.get("plan") if isinstance(c.get("plan"), dict) else {}
        stop = plan.get("stop_price")
        picks.append({
            "code": c["code"], "name": c.get("name"), "industry": ind,
            "tag": (c.get("tag") or "").strip(), "score": round(score, 1),
            "sig_price": c.get("price"), "stop_ref": stop,
            # 写快照那天该票除权 (生成侧只打标记, 不改价) -> 锚定要换 "raw×因子比" 序列
            # (见 backtest.anchor_closes); 绝大多数候选没有这个键, 存 None 不占地方。
            "xd": True if c.get("xd") else None,
        })
        per_ind[ind] = per_ind.get(ind, 0) + 1
        if len(picks) >= MAX_PICKS:
            break
    return picks, scale, note


def _sim_cycle(ser: dict, start_date: str, sig_px: float, stop_ref, budget: float,
               lot: int, xd: bool = False) -> dict:
    """次日开盘买入 -> 止损保护 -> 第 CYCLE_BARS 根bar收盘结算。价格按锚定缩放。"""
    m = current()
    dates, ohlcv = ser["dates"], np.asarray(ser["ohlcv"], dtype=float)
    idx0 = int(np.searchsorted(np.array(dates), start_date, side="right")) - 1
    if idx0 < 0 or idx0 + 1 >= len(dates):
        return {"status": "pending"}
    if not sig_px or sig_px <= 0:
        return {"status": "bad_anchor"}
    # 锚定用原始价 (与快照价同口径), scale/模拟仍用同索引的 qfq —— 见 backtest.anchor_closes
    # `xd` = 入选那天该票除权 (生成侧只打标记, 不改价) -> 换 "raw×因子比" 序列比 (同回测/模拟盘)。
    anchor = bt.find_anchor(bt.anchor_closes(ser, xd=bool(xd)), idx0, float(sig_px))
    if anchor is None or anchor + 1 >= len(dates) or ohlcv[anchor][3] <= 0:
        return {"status": "pending"}
    scale = float(ohlcv[anchor][3]) / float(sig_px)
    if not (0.2 < scale < 5.0):
        return {"status": "bad_anchor"}
    j = anchor + 1
    o, h, l, c, v = ohlcv[j]
    prev_c = ohlcv[j - 1][3]
    if m.limit_boards and m.limit_up_oneline and m.limit_up_oneline(o, h, l, c, prev_c):
        return {"status": "no_fill"}                  # 一字涨停买不进, 放弃
    entry = float(o)
    stop = float(stop_ref) * scale if stop_ref and 0 < stop_ref * scale < entry \
        else entry * (1.0 - STOP_FALLBACK)
    end = min(j + CYCLE_BARS - 1, len(dates) - 1)
    complete = (j + CYCLE_BARS - 1) <= (len(dates) - 1)
    exit_i, exit_px, status = end, float(ohlcv[end][3]), "cycle_end"
    k = j if not m.t_plus_one else j + 1              # A股 T+1 当日不可卖
    while k <= end:
        o2, h2, l2, c2, v2 = ohlcv[k]
        if l2 <= stop:
            status, exit_i, exit_px = "stopped", k, float(min(o2, stop))
            break
        k += 1
    if status == "cycle_end" and not complete:
        status = "open"
    shares = int(budget / (entry / scale * lot)) * lot if lot > 1 \
        else int(budget / (entry / scale))
    if shares <= 0:
        return {"status": "too_expensive"}
    used = shares * entry / scale
    ret = exit_px / entry - 1.0
    ret -= current().cost_rt if status != "open" else current().cost_rt / 2.0
    return {"status": status, "fill_date": dates[j], "entry": round(entry / scale, 3),
            "exit_date": dates[exit_i], "exit_px": round(exit_px / scale, 3),
            "ret": round(ret, 5), "shares": shares, "used": round(used, 2),
            "pnl": round(used * ret, 2), "complete": bool(complete)}


def update() -> dict | None:
    """每日调用: 需要开新期就选股开期; 对所有期的持仓用最新行情重算。幂等。"""
    m = current()
    state_path, js_path = _paths()
    budget, lot, cur_sym = _budget()
    state = _load_state(state_path)

    snaps = bt.load_snapshots()
    if not snaps:
        return None
    payload = {"candidates": snaps[-1]["cands"], "meta": {"opp": {"score": snaps[-1].get("opp_score")}}}
    as_of = snaps[-1]["as_of"]

    # 开新期?
    last = state["cycles"][-1] if state["cycles"] else None
    need_new = last is None
    if last is not None:
        bars = _trading_bars_since(last["start_date"])
        gap_ok = (bars is not None and bars >= CYCLE_BARS) or \
            (bars is None and (dt.date.fromisoformat(as_of)
                               - dt.date.fromisoformat(last["start_date"])).days >= 14)
        need_new = gap_ok and as_of > last["start_date"]
    if need_new:
        qs = sorted(__import__("glob").glob(os.path.join(m.dashboard_dir, "history", "quality_*.json")))
        qpicks = (_load_json(qs[-1]) or {}).get("picks") or [] if qs else []
        picks, scale, note = select(payload, qpicks, _seg_stats(), _coil_prior(),
                                    _temperature(payload))
        state["cycles"].append({
            "start_date": as_of, "budget_scale": scale, "gate_note": note,
            "picks": picks, "n": len(picks),
        })
        log.info("双周组合 新一期 %s: %d 只, %s", as_of, len(picks), note)

    # 只重算未了结的期 (一期10bar, 更早的期结果已封存不会再变);
    # 行情走生产日线取数 (与回测/模拟组合同源), 不依赖研究价格库的新鲜度。
    active = [cy for cy in state["cycles"]
              if not cy.get("picks") or "summary" not in cy
              or cy["summary"].get("n_open") or any(
                  (p.get("result") or {}).get("status") in (None, "pending", "open", "no_data")
                  for p in cy["picks"])]
    prices = {}
    codes = sorted({p["code"] for cy in active for p in cy["picks"]})
    if codes:
        start = min(cy["start_date"] for cy in active)
        start = (dt.date.fromisoformat(start) - dt.timedelta(days=40)).isoformat()
        fetched = bt.fetch_price_series(codes, start, need_date=as_of)
        for code, ser in fetched.items():
            arr = np.asarray(ser["ohlc"], dtype=float)
            row = {"dates": ser["dates"],
                   "ohlcv": np.column_stack([arr, np.zeros(len(arr))])}
            if ser.get("raw_close") is not None:
                row["raw_close"] = ser["raw_close"]   # 锚定要用它, 重排列时别弄丢
            prices[code] = row
    for cy in active:
        eff_budget = budget * (cy.get("budget_scale") or 1.0)
        for p in cy["picks"]:
            ser = prices.get(p["code"])
            r = {"status": "no_data"} if ser is None else _sim_cycle(
                ser, cy["start_date"], p.get("sig_price"), p.get("stop_ref"),
                eff_budget, lot, xd=bool(p.get("xd")))
            p["result"] = r
        done = [p["result"] for p in cy["picks"]
                if p["result"].get("status") in ("stopped", "cycle_end")]
        opn = [p["result"] for p in cy["picks"] if p["result"].get("status") == "open"]
        cy["summary"] = {
            "n_filled": len(done) + len(opn), "n_done": len(done), "n_open": len(opn),
            "pnl_done": round(sum(r["pnl"] for r in done), 2),
            "pnl_open": round(sum(r["pnl"] for r in opn), 2),
            "avg_ret": round(float(np.mean([r["ret"] for r in done])) * 100, 2) if done else None,
        }

    closed = [cy for cy in state["cycles"] if cy["summary"]["n_open"] == 0 and cy["summary"]["n_filled"]]
    total_pnl = round(sum(cy["summary"]["pnl_done"] + cy["summary"]["pnl_open"]
                          for cy in state["cycles"]), 2)
    win_cycles = [cy for cy in closed if cy["summary"]["pnl_done"] > 0]
    payload_out = {
        "meta": {"updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "as_of": as_of, "currency": cur_sym, "budget": budget,
                 "cycle_bars": CYCLE_BARS, "max_picks": MAX_PICKS,
                 "params": {"seg_n_min": SEG_N_MIN, "seg_win_min": SEG_WIN_MIN,
                            "temp_full": TEMP_FULL, "temp_half": TEMP_HALF}},
        "total": {"n_cycles": len(state["cycles"]), "n_closed": len(closed),
                  "n_win_cycles": len(win_cycles), "pnl": total_pnl},
        "cycles": state["cycles"][-8:],
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.__BW__ = " + json.dumps(payload_out, ensure_ascii=False) + ";\n")
    log.info("双周组合: %d 期, 合计盈亏 %s%.0f -> %s",
             len(state["cycles"]), cur_sym, total_pnl, os.path.basename(js_path))
    return payload_out
