#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键运行 (美股左侧支撑位筛选)
    python run_pipeline.py
流程: 板块景气(GICS/ETF) -> 技术左侧扫描(标普500) -> 基本面 -> 交叉打分 -> 入库 -> 导出看板
"""
from __future__ import annotations
import os
import sys
import time
import socket
import argparse
import logging
import statistics
import datetime as dt
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# 防卡死: 给所有网络请求设默认超时, 避免某个卡住的连接让流程无限期挂起。
socket.setdefaulttimeout(30)

from screener.config import CONFIG
from screener.config import DATA_DIR
from screener import db
from screener import datasource as ds
from screener import module1_industry as m1
from screener import module2_tech as m2
from screener import module3_fundamentals as m3
from screener import module4_crossscore as m4
from screener import module6_profile as m6
from screener import tradeplan as tp
from screener import export_data as ex
from screener import runmeta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("screener.run")


def _tqdm():
    try:
        from tqdm import tqdm
        return tqdm
    except Exception:
        return lambda x, **k: x


def run(use_cache=True):
    # 全局socket兜底超时: 没设超时的阻塞读60秒后抛异常走重试, 不许挂死整条流水线
    # (A股版 2026-08-13~19 连续被无超时网络读卡死, 两边都加同样的保险)
    socket.setdefaulttimeout(60)
    # 心跳 (watchdog.py 据此判断是否卡死): 只有"有进展"才更新 —— 进展 = 各阶段循环每完成
    # 一只 (下方 _prog) 或 任意一条日志; 纯存活不算进展, 否则主线程卡在网络读上时心跳照样跳。
    # 心跳带 watchdog 下发的令牌, 孤儿/手动进程写的心跳不会冒充被监控的子进程。
    import threading as _th
    _hb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "heartbeat.txt")
    _hb_token = os.environ.get("LS_HB_TOKEN", "manual")
    _prog = {"n": 0}

    class _ProgressHandler(logging.Handler):
        def emit(self, record):
            _prog["n"] += 1
    logging.getLogger().addHandler(_ProgressHandler())

    def _beat():
        last = -1
        while True:
            if _prog["n"] != last:
                last = _prog["n"]
                try:
                    with open(_hb, "w", encoding="utf-8") as _f:
                        _f.write(f"{_hb_token}|{dt.datetime.now().isoformat()}|{last}")
                except Exception:
                    pass
            time.sleep(60)
    _th.Thread(target=_beat, daemon=True).start()
    tqdm = _tqdm()
    run_date = dt.date.today().isoformat()
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    CONFIG["source"]["use_cache"] = use_cache

    db.init_db()
    db.clear_run(run_date)

    log.info("拉取股票池 (全美股, 市值>=$%.0fM) ...", CONFIG["source"]["min_market_cap"] / 1e6)
    universe = ds.get_universe()
    if universe is None or universe.empty:
        log.error("股票池获取失败, 退出")
        return
    log.info("股票池: %d 只", len(universe))

    log.info("模块1: 计算板块景气度 ...")
    sec_df = m1.compute_sector_scores(
        universe, progress_cb=lambda i, n, s: log.info("  板块 %d/%d %s", i, n, s))
    if sec_df is not None and not sec_df.empty:
        db.save_industry_scores(run_date, sec_df)
    prosperity_map = dict(zip(sec_df["industry"], sec_df["prosperity_score"])) if (sec_df is not None and not sec_df.empty) else {}
    selected = list(sec_df[sec_df["selected"]]["industry"]) if (sec_df is not None and not sec_df.empty) else []
    log.info("板块景气榜前3: %s", list(sec_df["industry"][:3]) if (sec_df is not None and not sec_df.empty) else [])

    # 市场地位 (垄断力代理): 细分行业内市值排名与份额。免费源无产品级市占率,
    # 以 NASDAQ 细分行业(153组)的市值份额近似; 行业缺失回退到 GICS 板块分组。
    dom_map = {}
    if "mcap" in universe.columns:
        _u = universe[universe["mcap"] > 0].copy()
        _u["_grp"] = _u["nasdaq_industry"].where(
            _u["nasdaq_industry"].astype(bool), _u["sector"])
        for gname, g in _u.groupby("_grp"):
            if not gname:
                continue
            g = g.sort_values("mcap", ascending=False).reset_index(drop=True)
            total = float(g["mcap"].sum())
            for i, r in g.iterrows():
                share = round(r["mcap"] / total * 100.0, 1) if total > 0 else None
                dom_map[r["code"]] = {"rank": int(i) + 1, "n": int(len(g)), "share": share}
        log.info("市场地位分组: %d 个行业组, 覆盖 %d 只", _u["_grp"].nunique(), len(dom_map))

    stocks = [(r["code"], r["name"], r["sector"]) for _, r in universe.iterrows()]
    ds.register_news_names({c: n for c, n, _ in stocks})   # 新闻相关性判据 (非本公司新闻过滤) 与 Google News 查询词用公司名
    workers = CONFIG["fetch"]["max_workers"] or min(12, (os.cpu_count() or 4) * 2)

    _bench = ds.fetch_benchmark()
    if _bench is not None and not _bench.empty:
        # 日期作索引 -> beta() 按日期交集对齐
        bench_close = _bench.set_index(_bench["date"].astype(str))["close"]
        data_date = str(_bench["date"].iloc[-1])   # 真实数据日期 = 最新交易日收盘
    else:
        bench_close = None
        data_date = run_date

    # ---- 阶段A: 技术扫描 ----
    def _scan(code, name, sector):
        h = ds.fetch_hist(code)
        if h is None:
            return None
        rec, detail = m2.scan_one(code, name, h, None, bench_close=bench_close)
        if rec is None:
            return None
        # 支撑分达标 OR 深跌抄底桶 OR 蓄势待发桶, 三者其一即保留
        if (rec["tech_score"] < CONFIG["tech"]["min_tech_score"]
                and not rec.get("dip") and not rec.get("coil")):
            return None
        rec["industry"] = sector
        return (rec, detail)

    log.info("阶段A 技术扫描: %d 只, 并发 %d 线程 ...", len(stocks), workers)
    hits, n_scanned = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_scan, c, n, s) for (c, n, s) in stocks]
        for fut in tqdm(as_completed(futs), total=len(futs)):
            _prog["n"] += 1
            n_scanned += 1
            try:
                r = fut.result()
            except Exception as e:
                log.debug("扫描失败: %s", e)
                continue
            if r:
                hits.append(r)
    log.info("技术命中 %d 只", len(hits))

    # ---- 阶段B: 仅对技术分最高的前N只拉基本面 ----
    hits.sort(key=lambda rd: (-rd[0]["tech_score"], rd[0]["code"]))
    top_hits = hits[:CONFIG["output"]["fund_top_n"]]
    # 并入"深跌抄底"桶: 支撑分排不进 top_hits、但深跌达标的, 按 dip_score 取前 dip_top_n 只补进来。
    # 这样 BABA 这类 falling knife 也能拿到基本面 + 进 final_rank(带 🪸 标签), 又不挤占支撑型名额。
    _seen = {rd[0]["code"] for rd in top_hits}
    dip_pool = sorted([rd for rd in hits if rd[0].get("dip")],
                      key=lambda rd: -rd[0].get("dip_score", 0.0))
    # 先剔除已在 top_hits 的(它们本就会拿到基本面), 再取前 dip_top_n 只 —— 与 export 的过滤顺序一致,
    # 避免"已入选的 dip 白占名额、排名靠后的真 dip 反而进不了 final_rank"。
    dip_new = [rd for rd in dip_pool if rd[0]["code"] not in _seen][:CONFIG["output"].get("dip_top_n", 40)]
    for rd in dip_new:
        top_hits.append(rd)
        _seen.add(rd[0]["code"])
    n_dip_added = len(dip_new)
    log.info("深跌抄底桶: 命中 %d 只, 并入候选 %d 只", len(dip_pool), n_dip_added)
    # 并入"蓄势待发"桶 (与 dip 同构): 按 coil_score 取前 coil_top_n 只。
    # 排除 dip 重叠, 与 coil_tail/coil_extra 的展示过滤同口径, 避免配额被展示不了的股占掉
    coil_pool = sorted([rd for rd in hits if rd[0].get("coil") and not rd[0].get("dip")],
                       key=lambda rd: -rd[0].get("coil_score", 0.0))
    coil_new = [rd for rd in coil_pool if rd[0]["code"] not in _seen][:CONFIG["output"].get("coil_top_n", 40)]
    for rd in coil_new:
        top_hits.append(rd)
        _seen.add(rd[0]["code"])
    log.info("蓄势待发桶: 命中 %d 只, 并入候选 %d 只", len(coil_pool), len(coil_new))
    log.info("阶段B 基本面+交叉打分: 取技术分最高 %d 只(含深跌抄底) ...", len(top_hits))

    def _fund(rd):
        rec, detail = rd
        f = m3.pull_fundamentals(rec["code"], sector=rec.get("industry"))
        return (rec, detail, f)

    # 预热 Yahoo crumb/cookie(顺序拉2只), 基本面用更低并发, 显著减少 401 Invalid Crumb
    for rd in top_hits[:2]:
        try:
            ds.fetch_info(rd[0]["code"])
        except Exception:
            pass
    fund_workers = CONFIG["fetch"].get("fund_workers") or workers
    results = []
    with ThreadPoolExecutor(max_workers=fund_workers) as pool:
        futs = [pool.submit(_fund, rd) for rd in top_hits]
        for fut in tqdm(as_completed(futs), total=len(futs)):
            _prog["n"] += 1
            try:
                results.append(fut.result())
            except Exception as e:
                log.debug("基本面失败: %s", e)

    # ---- 加固: Yahoo 限频会让整批基本面空白, 此时重新预热 crumb 并对空白项重试一轮 ----
    def _fund_empty(f):
        return not (f.get("pe_ttm") or f.get("target_price") or f.get("roe"))
    n_empty = sum(1 for (_, _, f) in results if _fund_empty(f))
    if results and n_empty >= max(5, int(0.4 * len(results))):
        log.warning("基本面覆盖偏低(空白 %d/%d), 重新预热并重试空白项 ...", n_empty, len(results))
        for rd in top_hits[:3]:
            try:
                ds.fetch_info(rd[0]["code"])   # 重新预热 crumb/cookie
            except Exception:
                pass
        idx_empty = [i for i, (_, _, f) in enumerate(results) if _fund_empty(f)]
        with ThreadPoolExecutor(max_workers=max(2, fund_workers // 2)) as pool:
            futs = {pool.submit(m3.pull_fundamentals, results[i][0]["code"],
                                sector=results[i][0].get("industry")): i for i in idx_empty}
            for fut in as_completed(futs):
                _prog["n"] += 1
                i = futs[fut]
                try:
                    nf = fut.result()
                    if nf and not _fund_empty(nf):
                        rec, detail, _ = results[i]
                        results[i] = (rec, detail, nf)
                except Exception:
                    pass
        log.info("基本面重试后覆盖: %d/%d",
                 sum(1 for (_, _, f) in results if not _fund_empty(f)), len(results))

    # ---- 加固2: 零散空白(不到40%阈值)也无条件低并发重试一轮 ----
    # 限频往往打在批次的一段区间上; 阈值式重试漏掉的散点会以"全横杠行"上榜(蓄势股尤甚)。
    idx_scatter = [i for i, (_, _, f) in enumerate(results) if _fund_empty(f)]
    if idx_scatter:
        log.info("零散空白基本面 %d 只, 低并发重试 ...", len(idx_scatter))
        with ThreadPoolExecutor(max_workers=2) as pool:
            def _slow_pull(i):
                time.sleep(0.4)
                return i, m3.pull_fundamentals(results[i][0]["code"],
                                               sector=results[i][0].get("industry"))
            futs = [pool.submit(_slow_pull, i) for i in idx_scatter]
            for fut in as_completed(futs):
                _prog["n"] += 1
                try:
                    i, nf = fut.result()
                    if nf and not _fund_empty(nf):
                        rec, detail, _ = results[i]
                        results[i] = (rec, detail, nf)
                except Exception:
                    pass
    # ---- 加固3: 仍空白的回落到最近一天的基本面 (基本面日变化很小, 好过全横杠) ----
    n_fb = 0
    for i, (rec, detail, f) in enumerate(results):
        if not _fund_empty(f):
            continue
        old = db.fetch_latest_fundamental(rec["code"], run_date)
        if not old:
            continue
        of = {k: old.get(k) for k in f.keys() if k in old}
        import json as _json
        for jk, k in (("roe_trend_json", "roe_trend"), ("roe_trend_q_json", "roe_trend_q"),
                      ("fund_flags_json", "fund_flags"), ("ni_qoq_json", "ni_qoq"),
                      ("ni_parent_qoq_json", "ni_parent_qoq"), ("ni_q_labels_json", "ni_q_labels")):
            try:
                of[k] = _json.loads(old.get(jk) or "[]")
            except Exception:
                of[k] = []
        nf = dict(f)
        nf.update({k: v for k, v in of.items() if v is not None and v != []})
        if not _fund_empty(nf):
            results[i] = (rec, detail, nf)
            n_fb += 1
    if n_fb:
        log.info("基本面回落补齐(用最近一天数据): %d 只", n_fb)
    log.info("基本面最终覆盖: %d/%d",
             sum(1 for (_, _, f) in results if not _fund_empty(f)), len(results))

    # 板块覆盖: NASDAQ 名单板块质量差(MO被标Health Care等)且有缺失,
    # 候选股以 Yahoo info 的 GICS 口径板块为准, NASDAQ 仅兜底。
    # 必须在 板块PE中位 分组之前做, 否则中位数按错误板块分组 (审查发现的排序缺陷)
    n_sec_fix = 0
    for (rec, _, f) in results:
        syf = f.get("sector_yf")
        if syf and syf != rec.get("industry"):
            rec["industry"] = syf
            n_sec_fix += 1
        # 市场地位字段并入基本面记录 (行业内市值 排名/份额, 👑=行业市值第一且份额>=15%)
        d = dom_map.get(rec["code"])
        if d:
            crown = "👑" if (d["rank"] == 1 and (d["share"] or 0) >= 15) else ""
            share_txt = f" · {d['share']}%" if d["share"] is not None else ""
            f["dominance_disp"] = f"{crown}#{d['rank']}/{d['n']}{share_txt}"
            f["dom_rank"], f["dom_n"], f["dom_share"] = d["rank"], d["n"], d["share"]
    log.info("板块修正(Yahoo GICS 覆盖 NASDAQ): %d 只", n_sec_fix)

    # 板块PE中位 + 全体PE/PB横截面分位 (使 便宜加分与"分位"列有意义)
    all_pe = sorted([f["pe_ttm"] for (_, _, f) in results if f.get("pe_ttm") and f["pe_ttm"] > 0])
    all_pb = sorted([f["pb"] for (_, _, f) in results if f.get("pb") and f["pb"] > 0])
    sec_pe = defaultdict(list)
    for (rec, _, f) in results:
        if f.get("pe_ttm") and f["pe_ttm"] > 0:
            sec_pe[rec.get("industry")].append(f["pe_ttm"])
    sec_pe_med = {s: statistics.median(v) for s, v in sec_pe.items() if v}

    def _pe_pct(pe):
        if not pe or pe <= 0 or not all_pe:
            return None
        return round(sum(1 for x in all_pe if x <= pe) / len(all_pe) * 100.0, 1)

    def _pb_pct(pb):
        if not pb or pb <= 0 or not all_pb:
            return None
        return round(sum(1 for x in all_pb if x <= pb) / len(all_pb) * 100.0, 1)

    scored = []
    for (rec, detail, f) in results:
        sec = rec.get("industry")
        if f.get("pe_ttm") and f["pe_ttm"] > 0 and sec in sec_pe_med:
            f["pe_industry_median"] = round(sec_pe_med[sec], 2)
            f["pe_vs_industry"] = round(f["pe_ttm"] / sec_pe_med[sec], 2)
        f["pe_pct"] = _pe_pct(f.get("pe_ttm"))
        f["pb_pct"] = _pb_pct(f.get("pb"))
        fr = m4.cross_score(rec, f, prosperity_map.get(sec))
        scored.append((rec, detail, f, fr))

    scored.sort(key=lambda x: (-(x[3]["final_score"] if x[3].get("final_score") is not None else -1),
                               x[0]["code"]))
    detail_n = CONFIG["output"]["dashboard_detail_top_n"]
    show_n = CONFIG["output"]["final_top_n"]
    final_records = [x[3] for x in scored]
    # export 浮现集合 = 前 show_n 名 + (排名>show_n 的 dip 按 dip_score 取前 dip_top_n)。
    # detail 与 profile 都对齐这个集合: 既保证浮现的 dip 股点开有 K线/档案, 又不为不展示的股白存(控 JS 体积)。
    dip_tail = sorted([fr for fr in final_records[show_n:] if fr.get("dip")],
                      key=lambda fr: -(fr.get("dip_score") or 0.0))[:CONFIG["output"].get("dip_top_n", 40)]
    coil_tail = sorted([fr for fr in final_records[show_n:]
                        if fr.get("coil") and not fr.get("dip")],
                       key=lambda fr: -(fr.get("coil_score") or 0.0))[:CONFIG["output"].get("coil_top_n", 40)]
    # 深跌全池留痕: 恐慌日命中远超top40, 公布的桶胜率实为前40名 — 全量落盘供分位验证
    try:
        import json as _json
        _dp_dir = os.path.join(DATA_DIR, "dip_pool")
        os.makedirs(_dp_dir, exist_ok=True)
        _dp = [{"code": x.get("code"), "dip_score": x.get("dip_score"),
                "drawdown_pct": x.get("drawdown_pct"), "tag": x.get("tag"), "rank": _i + 1}
               for _i, x in enumerate(sorted([x for x in final_records if x.get("dip")],
                                             key=lambda x: -(x.get("dip_score") or 0.0)))]
        with open(os.path.join(_dp_dir, f"{run_date}.json"), "w", encoding="utf-8") as _f:
            _json.dump(_dp, _f, ensure_ascii=False)
    except Exception:
        pass

    # 所有"会展示"的 dip/coil 股(主榜内的 + 补进来的)都存 K线: 点开有图不空;
    # 非展示的不存, 控 JS 体积。(前 detail_n 名照常存, 与支撑股一致)
    shown_extra = ({fr["code"] for fr in final_records[:show_n] if fr.get("dip") or fr.get("coil")}
                   | {fr["code"] for fr in dip_tail} | {fr["code"] for fr in coil_tail})
    for idx, (rec, detail, f, fr) in enumerate(scored):
        db.save_tech(run_date, [rec])
        db.save_fundamental(run_date, rec["code"], f)
        db.save_final(run_date, [fr])
        if (idx < detail_n or rec["code"] in shown_extra) and detail:
            db.save_detail(run_date, rec["code"], detail)

    # ---- 阶段C1: 买卖点建议 (Trade Plan) — 对将展示的候选做支撑回踩事件回测 ----
    # 历史数据走当日缓存(fetch_hist 命中即秒回), 先收集各股事件统计, 再算全池先验, 最后收缩出胜率。
    # ⚠️ 技术好但基本面弱: 两市完整窗口与M1九年皆为负期望 -> 停发买点 (标签与展示
    # 保留; 回测影子事件照常构建)。复活门(数据化, 杜绝手工翻案): 影子样本
    # n_resolved>=12 且 win10_post>=max(0.55, 全池+3pp) 且 avg_ret>0 时自动恢复发放。
    def _weak_tag_allowed() -> bool:
        try:
            import json as _json
            with open(os.path.join(DATA_DIR, "backtest_result.json"), encoding="utf-8") as _f:
                _agg = (_json.load(_f) or {}).get("agg") or {}
            _s = (_agg.get("by_tag") or {}).get("⚠️ 技术好但基本面弱") or {}
            _p0 = _agg.get("p0") or 0.0
            return ((_s.get("n_resolved") or 0) >= 12
                    and (_s.get("win10_post") or 0.0) >= max(0.55, _p0 + 0.03)
                    and (_s.get("avg_ret") or 0.0) > 0)
        except Exception:
            return False
    _weak_ok = _weak_tag_allowed()
    if not _weak_ok:
        log.info("⚠️标签复活门未达标: 本轮不为该标签生成买卖点 (影子统计继续)")
    plan_targets = [fr for fr in (final_records[:show_n] + dip_tail + coil_tail)
                    if _weak_ok or "基本面弱" not in (fr.get("tag") or "")]
    tech_by_code = {rec["code"]: rec for (rec, _, _, _) in scored}
    log.info("阶段C1 买卖点回测: %d 只 ...", len(plan_targets))
    plan_stats = {}
    for fr in tqdm(plan_targets):
        try:
            h = ds.fetch_hist(fr["code"])
            plan_stats[fr["code"]] = tp.compute_event_stats(h) if h is not None else None
        except Exception as e:
            log.debug("买卖点回测 %s 失败: %s", fr["code"], e)
            plan_stats[fr["code"]] = None
    prior = tp.pool_prior([s for s in plan_stats.values() if s])
    log.info("  事件池: 全池 %d 次事件 (先验)", prior.get("n", 0))
    n_plans = 0
    for fr in plan_targets:
        rec = tech_by_code.get(fr["code"])
        if not rec:
            continue
        try:
            plan = tp.build_trade_plan(rec, plan_stats.get(fr["code"]), prior)
            if plan:
                db.save_trade_plan(run_date, fr["code"], plan)
                n_plans += 1
        except Exception as e:
            log.debug("买卖点生成 %s 失败: %s", fr["code"], e)
    log.info("  买卖点建议: %d 只已生成", n_plans)

    # ---- 阶段C: 深度档案 (现金流/营收/新闻/期权/暗池) — 最终候选 + 浮现的 dip/coil 股 ----
    # 深度档案只为可操作标签生成 (用户指定: 仅 强左侧 + 蓄势待发) —
    # 观察/基本面弱 占榜单大头但很少被点开, 砍掉后阶段C耗时降 ~2/3, 限频压力大减
    _prof_pool = final_records[:show_n] + dip_tail + coil_tail
    prof_targets = [fr for fr in _prof_pool
                    if ("强左侧" in (fr.get("tag") or "")) or ("蓄势待发" in (fr.get("tag") or ""))]
    log.info("深度档案范围: 强左侧+蓄势待发 %d 只 (榜单共 %d)", len(prof_targets), len(_prof_pool))
    log.info("阶段C 深度档案: %d 只 (现金流/营收/新闻/期权/FINRA) ...", len(prof_targets))
    finra_map = ds.fetch_finra_short_volume()
    log.info("  FINRA 场外空头数据: %d 只", len(finra_map))

    _profiles = {}   # code -> 档案 (判断哪些需要重试)

    def _prof(fr):
        p = m6.pull_profile(fr["code"], sector=fr.get("industry"), short_map=finra_map)
        db.save_profile(run_date, fr["code"], p)
        _profiles[fr["code"]] = p

    def _rev_ok(p):
        return bool((p.get("revenue") or {}).get("years"))

    def _prof_empty(p):
        # info 整体被限频: 简介/管理层/营收全空 -> 弹窗四个页签全是"暂无"
        return not p.get("summary") and not p.get("officers") and not _rev_ok(p)

    with ThreadPoolExecutor(max_workers=fund_workers) as pool:
        futs = [pool.submit(_prof, fr) for fr in prof_targets]
        for fut in tqdm(as_completed(futs), total=len(futs)):
            _prog["n"] += 1
            try:
                fut.result()
            except Exception as e:
                log.debug("深度档案失败: %s", e)

    # ---- 加固: 全空/拉取即崩(不在_profiles里)的档案无条件重试; 缺年度营收批量偏高时也重试 ----
    empty = [fr for fr in prof_targets
             if fr["code"] not in _profiles or _prof_empty(_profiles[fr["code"]])]
    miss_rev = [fr for fr in prof_targets
                if fr["code"] in _profiles and not _rev_ok(_profiles[fr["code"]])]
    retry = list(empty)
    if len(miss_rev) >= max(5, int(0.3 * len(prof_targets))):
        seen_r = {fr["code"] for fr in retry}
        retry += [fr for fr in miss_rev if fr["code"] not in seen_r]
    if retry:
        log.warning("深度档案重试: 全空 %d 只, 缺年度营收 %d/%d ...",
                    len(empty), len(miss_rev), len(prof_targets))
        with ThreadPoolExecutor(max_workers=max(2, fund_workers // 2)) as pool:
            futs = [pool.submit(_prof, fr) for fr in retry]
            for fut in as_completed(futs):
                _prog["n"] += 1
                try:
                    fut.result()
                except Exception:
                    pass
        n_empty_after = sum(1 for p in _profiles.values() if _prof_empty(p))
        log.info("深度档案重试后: 全空 %d, 有年度营收 %d/%d", n_empty_after,
                 sum(1 for p in _profiles.values() if _rev_ok(p)), len(prof_targets))

    # market_status (美股无日历, 只判周末) + 数据源留痕 -> run_log.extra_json -> 看板 meta (2026-10-04 卡 A-HOLIDAY)。
    # 两个函数都不抛; 看板据此 + 跑批新鲜度分清「休市」与「没跑」, 周一休市的周三凌晨不再误报「定时任务可能失败」。
    run_extra = {"calendar": runmeta.calendar_fields(), "sources": runmeta.run_sources(len(universe))}
    finished = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.log_run(run_date, started, finished, n_scanned, len(final_records), selected, "ok",
               data_date=data_date, extra=run_extra)
    log.info("扫描完成: 扫描 %d, 命中 %d", n_scanned, len(final_records))
    ex.write_dashboard_js(run_date)
    ex.write_csv(run_date)
    ex.write_history_snapshot(run_date)
    try:
        ex.write_watch_js()
    except Exception as e:
        log.warning("watch_data 导出失败: %s", e)
    try:
        from screener import backtest as bt
        bt.run_backtest()
    except Exception as e:
        log.warning("信号回测失败(不影响榜单与发布): %s", e)
    try:
        from screener import sentiment
        sentiment.build()
    except Exception as e:
        log.warning("市场风险偏好指数失败(不影响榜单与发布): %s", e)
    try:
        from screener import qfund
        qfund.update_shard()
    except Exception as e:
        log.warning("qfund 轮转抓取失败(优质榜仅用候选基本面): %s", e)
    try:
        from screener import quality as ql
        ql.build_quality()
    except Exception as e:
        log.warning("优质榜构建失败(不影响榜单与发布): %s", e)
    try:
        from screener import paper
        paper.update_portfolio()
    except Exception as e:
        log.warning("自动模拟组合更新失败(不影响榜单与发布): %s", e)
    try:
        from screener import biweekly
        biweekly.update()
    except Exception as e:
        log.warning("双周组合更新失败(不影响榜单与发布): %s", e)
    log.info("✅ 全部完成。请双击打开 dashboard/index.html")


def main():
    ap = argparse.ArgumentParser(description="美股左侧支撑位筛选")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    try:
        run(use_cache=not args.no_cache)
    except KeyboardInterrupt:
        log.warning("用户中断")
        sys.exit(1)
    log.info("耗时 %.1f 秒", time.time() - t0)


if __name__ == "__main__":
    main()
