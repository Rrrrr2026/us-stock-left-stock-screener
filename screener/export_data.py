#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出层 (Export)
===============
把某个 run_date 的库表汇成仪表盘数据对象, 写成:
  dashboard/dashboard_data.js  ->  window.__ASHARE__ = {...};
  data/candidates_<date>.csv   ->  主表中文表头, 一键导出 (utf-8-sig, Excel可读)
仪表盘 index.html 用 <script src="dashboard_data.js"> 直接读取, 双击即可打开。
"""
from __future__ import annotations
import os
import csv
import json
import datetime as dt
import logging

from . import db
from . import opportunity as opp
from .config import DASHBOARD_DATA_JS, DATA_DIR, CONFIG

log = logging.getLogger("ashare.export")

DISCLAIMER = ("本系统仅对美股(全美股, 市值≥$100M)做技术/基本面数据的自动化整理与形态筛选, 不构成任何投资建议。"
              "“左侧买入”是在下跌中、支撑确认前进场, 风险天然更高(可能继续下跌或破位)。"
              "买卖点建议与胜率为历史回测统计, 不构成对未来的保证。"
              "价格单位为美元(USD)。所有标的需人工复核, 使用者自负盈亏与风控。")

# 主表中文表头 (顺序; 换手率/量比是A股概念, 美股换成 日均额$/股息率)
# 市场地位 = 细分行业内市值排名/份额 (垄断力代理); 近四季增速为分层口径(TTM/单季/年度)
MAIN_COLUMNS = [
    ("code", "代码"), ("name", "名称"), ("industry", "所属板块"),
    ("dominance_disp", "市场地位"), ("ni_ttm_yoy", "近四季EPS同比%"),
    ("rev_ttm_yoy", "近四季营收增速%"), ("growth_quality", "增长持续性"),
    ("pe_disp", "市盈率TTM(分位)"),
    ("tag", "结论标签"), ("streak", "连续上榜"), ("final_score", "综合分"), ("tech_score", "技术分"),
    ("fund_score", "基本面分"), ("price", "现价$"), ("spark", "近期走势"), ("dist_support_pct", "距支撑%"),
    ("support_disp", "关键支撑位"), ("breakdown_price", "破位位"),
    ("pos_52w_pct", "52周位置%"), ("ret_1m_pct", "近一月涨%"), ("ret_half_year_pct", "近半年涨跌%"),
    ("avg_amt20_yi", "日均额$M"), ("dividend_yield", "股息率%"), ("kdj_tag", "KDJ"),
    ("pb", "市净率"), ("eps", "EPS"), ("roe", "ROE"),
    ("cuosha_score", "错杀分"), ("cuosha_upside", "修复空间%"), ("cuosha_p20", "30日涨20%概率"),
    ("upside_pct", "距目标价%"),
]


def _index_by_code(rows):
    return {r["code"]: r for r in rows}


NEWS_MIN_RATE = 0.20      # 有标题的票占比低于这个数 = 新闻源整体不可用 (不是个别票安静)


def news_health(profiles: dict, candidates: list) -> dict:
    """本轮新闻源体检 —— 只看导出物本身 (看板展示什么就按什么判, 重导某个历史日期同样成立)。
    需要新闻的票 = 有深度档案的 (档案里的 news, 流水线阶段 C 取) ∪ 错杀候选 (newsflag 导出时现取的 news)。
      ok = False : 一条标题都没有 (总数 0), 或有标题的票占比 < NEWS_MIN_RATE
      ok = True  : 其余
      ok = None  : 本轮没有任何票需要新闻 (无从判断, 看板照旧显示)
    2026-10-02..10-04 雅虎新闻端点 404 时就是「总数 0」而流水线一声不吭, 看板的「利空 0」「🚩 无」被读成没有利空。"""
    need: dict = {}
    for code, p in (profiles or {}).items():
        if isinstance(p, dict) and "news" in p:
            need[code] = len(p.get("news") or [])
    for c in candidates or []:
        if c.get("cuosha_score"):
            need[c["code"]] = max(need.get(c["code"], 0), len(c.get("news") or []))
    n = len(need)
    with_news = sum(1 for v in need.values() if v)
    items = sum(need.values())
    if n == 0:
        return {"ok": None, "n": 0, "with_news": 0, "items": 0, "rate": None}
    rate = with_news / n
    return {"ok": bool(items > 0 and rate >= NEWS_MIN_RATE), "n": n, "with_news": with_news,
            "items": items, "rate": round(rate, 3)}


def build_payload(run_date: str | None = None) -> dict:
    if run_date is None:
        run_date = db.latest_run_date()
    if run_date is None:
        return {"meta": {"run_date": None, "candidates": []}, "industries": [],
                "candidates": [], "details": {}}

    runlog = db.fetch_run_log(run_date) or {}
    industries = db.fetch_table("industry_score", run_date)
    tech = _index_by_code(db.fetch_table("tech_scan", run_date))
    fund = _index_by_code(db.fetch_table("fundamental", run_date))
    finals = db.fetch_table("final_rank", run_date)
    details_rows = db.fetch_table("stock_detail", run_date)
    plans = {r["code"]: _loads(r.get("plan_json"), default=None)
             for r in db.fetch_table("trade_plan", run_date)}

    # 行业榜 (按景气分降序)
    industries_sorted = sorted(industries, key=lambda r: (r.get("prosperity_score") or -1),
                               reverse=True)
    selected_inds = [r["industry"] for r in industries_sorted if r.get("selected")]

    appear = db.recent_appearance_counts(db.recent_run_dates(5))   # 连续上榜次数
    candidates = []
    for fr in finals:
        code = fr["code"]
        t = tech.get(code, {})
        f = fund.get(code, {})
        support_disp = None
        if t.get("support_price") is not None:
            support_disp = f"{t.get('support_label') or '支撑'} {round(float(t['support_price']), 2)}"
        pe_disp = None
        if f.get("pe_ttm") is not None:
            pe_disp = f"{round(f['pe_ttm'],1)}"
            if f.get("pe_pct") is not None:
                pe_disp += f" ({round(f['pe_pct'])}%分位)"
        row = {
            **fr,
            # 技术/行情字段
            "price": t.get("price"),
            "dist_support_pct": t.get("dist_support_pct"),
            "support_label": t.get("support_label"),
            "support_price": t.get("support_price"),
            "support_disp": support_disp,
            "breakdown_price": t.get("breakdown_price"),
            "pos_52w_pct": t.get("pos_52w_pct"),
            "high_52w": t.get("high_52w"), "low_52w": t.get("low_52w"),
            "ret_half_year_pct": t.get("ret_half_year_pct"),
            "ret_1m_pct": t.get("ret_1m_pct"),
            "turnover": t.get("turnover"), "volume_ratio": t.get("volume_ratio"),
            "amount_today": t.get("amount_today"), "avg_amt20_yi": t.get("avg_amt20_yi"),
            "kdj_tag": t.get("kdj_tag"),
            "kdj_k": t.get("kdj_k"), "kdj_d": t.get("kdj_d"), "kdj_j": t.get("kdj_j"),
            "rsi": t.get("rsi"),
            "sig_channel": t.get("sig_channel"), "sig_pivot": t.get("sig_pivot"),
            "sig_ma": t.get("sig_ma"), "sig_osc": t.get("sig_osc"),
            "n_hit": t.get("n_hit"),
            # 基本面字段
            "pe_ttm": f.get("pe_ttm"), "pe_pct": f.get("pe_pct"),
            "pe_industry_median": f.get("pe_industry_median"),
            "pe_vs_industry": f.get("pe_vs_industry"), "pe_disp": pe_disp,
            "pb": f.get("pb"), "pb_pct": f.get("pb_pct"),
            "dividend_yield": f.get("dividend_yield"),
            "eps": f.get("eps"), "eps_yoy": f.get("eps_yoy"), "roe": f.get("roe"),
            "revenue_yoy": f.get("revenue_yoy"), "netprofit_yoy": f.get("netprofit_yoy"),
            "gross_margin": f.get("gross_margin"), "debt_ratio": f.get("debt_ratio"),
            "roe_trend": _loads(f.get("roe_trend_json"), default=[]),
            "roe_trend_q": _loads(f.get("roe_trend_q_json"), default=[]),
            "fund_flags": _loads(f.get("fund_flags_json"), default=[]),
            # 新增: sparkline / 风控 / 量能 / 斐波那契 / 分析师 / 连续上榜
            "spark": _loads(t.get("spark_json"), default=[]),
            "atr_pct": t.get("atr_pct"), "max_dd_pct": t.get("max_dd_pct"),
            "beta": t.get("beta"), "vol_ratio_calc": t.get("vol_ratio_calc"),
            "sig_vol": t.get("sig_vol"), "boll_low": t.get("boll_low"),
            "supp_touches": t.get("supp_touches"), "trend_ok": t.get("trend_ok"),
            "rs_60": t.get("rs_60"), "fcf_yield": f.get("fcf_yield"),
            "box_hi": t.get("box_hi"), "box_lo": t.get("box_lo"),
            # 市场地位 / 近四季增速 / 增长持续性
            "dominance_disp": f.get("dominance_disp"), "dom_rank": f.get("dom_rank"),
            "dom_n": f.get("dom_n"), "dom_share": f.get("dom_share"),
            "ni_ttm_yoy": f.get("ni_ttm_yoy"), "ni_parent_ttm_yoy": f.get("ni_parent_ttm_yoy"),
            "ni_basis": f.get("ni_basis"), "ni_parent_basis": f.get("ni_parent_basis"),
            "rev_ttm_yoy": f.get("rev_ttm_yoy"), "rev_basis": f.get("rev_basis"),
            "growth_quality": f.get("growth_quality"),
            "ni_qoq": _loads(f.get("ni_qoq_json"), default=[]),
            "ni_parent_qoq": _loads(f.get("ni_parent_qoq_json"), default=[]),
            "rev_qoq": _loads(f.get("rev_qoq_json"), default=[]),
            "ni_q_labels": _loads(f.get("ni_q_labels_json"), default=[]),
            "fib_382": t.get("fib_382"), "fib_500": t.get("fib_500"), "fib_618": t.get("fib_618"),
            "target_price": f.get("target_price"), "analyst_rating": f.get("analyst_rating"),
            "analyst_count": f.get("analyst_count"), "upside_pct": f.get("upside_pct"),
            "streak": appear.get(code, 1),
            # 买卖点建议 (入场区/止损/目标梯子+胜率), 详情弹窗渲染
            "plan": plans.get(code),
        }
        candidates.append(row)

    candidates.sort(key=lambda r: (-(r["final_score"] if r.get("final_score") is not None else -1),
                                   r.get("code") or ""))
    top_n = CONFIG["output"]["final_top_n"]
    head = candidates[:top_n]                                   # 支撑型主榜(展示上限)
    # 深跌抄底桶: 支撑分低会被 final_top_n 截掉, 这里把落榜的 dip 候选按 dip_score 补回来
    # (上限 dip_top_n), 保证 BABA 这类作为独立标签组浮现, 不挤占支撑型名额。
    seen = {r.get("code") for r in head}
    dip_extra = sorted((r for r in candidates[top_n:] if r.get("dip")),
                       key=lambda r: -(r.get("dip_score") or 0.0))[:CONFIG["output"].get("dip_top_n", 40)]
    coil_extra = sorted((r for r in candidates[top_n:] if r.get("coil") and not r.get("dip")),
                        key=lambda r: -(r.get("coil_score") or 0.0))[:CONFIG["output"].get("coil_top_n", 40)]
    extras = [r for r in dip_extra + coil_extra if r.get("code") not in seen]
    candidates = head + extras

    details = {}
    for dr in details_rows:
        details[dr["code"]] = _loads(dr["detail_json"], default={})

    profiles = {}
    for pr in db.fetch_table("profile", run_date):
        profiles[pr["code"]] = _loads(pr["profile_json"], default={})

    # 错杀检测: 高质量+情绪性下跌 打分 (字段随快照沉淀, 回测可分段验证)
    try:
        from . import cuosha
        n_cs = cuosha.annotate(candidates)
        log.info("错杀候选: %d 只", n_cs)
        # 30日内涨20%的历史概率 (条件: 该股历史上同样深跌的日子); 长历史不可得时退回存档K线
        from . import prob20
        _cs_items = [c for c in candidates if c.get("cuosha_score")]
        _details = locals().get("details") or {}

        def _hist(code):
            try:
                import yfinance as _yf
                df = _yf.Ticker(code).history(period="5y", auto_adjust=True)
                if df is not None and len(df) >= 120:
                    return (df["High"].to_numpy(float), df["Close"].to_numpy(float))
            except Exception:
                pass
            d = _details.get(code) or {}
            oh = d.get("ohlc") or []
            if len(oh) >= 120:
                return ([r[3] for r in oh], [r[1] for r in oh])      # echarts [o,c,l,h]
            return None
        n_p = prob20.annotate(_cs_items, _hist, conditional=True, key="cuosha_p20")
        log.info("错杀候选 30日涨20%%概率: %d/%d 只有数", n_p, len(_cs_items))
        # 下一财报日 (来自财报日历缓存的未公布条目): 7天内亮 📅, 买卖点提示"财报前不建仓"
        from . import datasource as _dsx
        _base = dt.date.fromisoformat(str(runlog.get("data_date") or run_date)[:10])
        n_e = 0
        for c in candidates:
            try:
                nxt = (_dsx.fetch_eps_history(c["code"]) or {}).get("next")
                if not nxt:
                    continue
                days = (dt.date.fromisoformat(nxt) - _base).days
            except Exception:
                continue
            if days >= 0:
                c["earn_date"] = nxt
                c["earn_days"] = days
                n_e += 1
        log.info("财报日标注: %d 只", n_e)
        # "为什么跌"线索: 错杀候选的近期新闻标题关键词 🚩 (只拉错杀股, 数量小)
        from . import newsflag
        n_f = newsflag.annotate(candidates, as_of=str(runlog.get("data_date") or run_date)[:10])
        log.info("错杀候选新闻标记: %d 只有🚩", n_f)
    except Exception as e:
        log.warning("错杀检测失败: %s", e)

    # 新闻源体检 (2026-10-04 卡 NEWS-OVERDUE; 紧跟在错杀红旗取数之后): 整轮一条标题都没有 / 有标题的票不到两成 → WARNING + meta.news_source_ok=false,
    # 看板据此把「利空 0」「近30天无相关新闻标题」换成「新闻源不可用」; 数据总览「美股 新闻源」那一行据此判红推方糖。
    nh = {"ok": None, "n": 0, "with_news": 0, "items": 0, "rate": None}
    try:
        nh = news_health(profiles, candidates)
        try:
            from . import datasource as _dsn
            nh["fetch"] = _dsn.news_round_stats()       # 本进程实际问了哪几家、哪家熔断 (重导历史时可能为空)
        except Exception:
            pass
        if nh["ok"] is False:
            log.warning("新闻源不可用: 本轮 %d 只票需要新闻, 只有 %d 只取到标题 (共 %d 条) → meta.news_source_ok=false "
                        "(看板不再显示「利空 0」; 取数统计 %s)", nh["n"], nh["with_news"], nh["items"], nh.get("fetch"))
        else:
            log.info("新闻源体检: %d 只票需要新闻, %d 只有标题, 共 %d 条 (%s)", nh["n"], nh["with_news"], nh["items"],
                     nh.get("fetch"))
    except Exception as e:
        log.warning("新闻源体检失败 (不影响榜单与发布): %s", e)

    # 机会温度计: 当日榜单质量 vs 自身历史的分位 (指导"今天该不该重仓")
    opp_result = None
    try:
        from . import datasource as _ds
        _bench = _ds.fetch_benchmark()
        if _bench is not None and not _bench.empty and run_date:
            # as-of 截断: 为历史日期重算快照时, 指数回撤必须只用该日之前的数据
            _bench = _bench[_bench["date"].astype(str) <= run_date]
        _bc = _bench["close"] if (_bench is not None and not _bench.empty) else None
        comps = opp.compute_components(candidates, _bc)
        hist = opp.load_history_components(HISTORY_DIR, exclude_date=run_date)
        opp_result = opp.temperature(comps, hist)
    except Exception as e:
        log.warning("机会温度计计算失败: %s", e)

    payload = {
        "meta": {
            "run_date": run_date,
            # 新闻源体检结果。放在 meta 最前: 数据总览只读文件头 64 KB 抠 news_source_ok (后面的 opp / disclaimer 可能很长)
            "news_source_ok": nh["ok"],
            "news_stats": {k: nh.get(k) for k in ("n", "with_news", "items", "rate", "fetch")},
            "data_date": runlog.get("data_date") or run_date,   # 真实行情数据日期(最新收盘)
            "updated_at": runlog.get("finished_at") or run_date,
            "n_scanned": runlog.get("n_scanned"),
            "n_hit": len(candidates),   # 与主表展示条数一致
            "selected_industries": selected_inds,
            "disclaimer": DISCLAIMER,
            "opp": opp_result,
        },
        "industries": industries_sorted,
        "candidates": candidates,
        "details": details,
        "profiles": profiles,
        "columns": [{"key": k, "label": lab} for k, lab in MAIN_COLUMNS],
    }
    return payload


def write_dashboard_js(run_date: str | None = None) -> str:
    payload = build_payload(run_date)
    os.makedirs(os.path.dirname(DASHBOARD_DATA_JS), exist_ok=True)
    js = "window.__ASHARE__ = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    with open(DASHBOARD_DATA_JS, "w", encoding="utf-8") as f:
        f.write(js)
    log.info("仪表盘数据已写出: %s (%d 候选)", DASHBOARD_DATA_JS, len(payload["candidates"]))
    return DASHBOARD_DATA_JS


HISTORY_DIR = os.path.join(os.path.dirname(DASHBOARD_DATA_JS), "history")


def write_history_snapshot(run_date: str | None = None) -> str | None:
    """把某个 run_date 的候选榜写成"瘦身版"历史快照 (无K线明细/深度档案,
    体积 ~1MB), 供前端的日期切换器回看历史扫描结果:
      dashboard/history/day_<date>.json  +  dashboard/history/index.json (可用日期清单)
    auto_update.bat 会把 history/ 整目录同步到 docs/ 发布。"""
    payload = build_payload(run_date)
    rd = payload["meta"].get("run_date")
    if not rd:
        return None
    slim = {"meta": payload["meta"], "industries": payload["industries"],
            "candidates": payload["candidates"], "columns": payload["columns"]}
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"day_{rd}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False)
    # 更新清单 (按日期倒序, 只保留 history_days 天)
    keep = CONFIG["output"].get("history_days", 90)
    dates = sorted({fn[4:14] for fn in os.listdir(HISTORY_DIR)
                    if fn.startswith("day_") and fn.endswith(".json")}, reverse=True)
    for stale in dates[keep:]:
        try:
            os.remove(os.path.join(HISTORY_DIR, f"day_{stale}.json"))
        except OSError:
            pass
    dates = dates[:keep]
    idx_path = os.path.join(HISTORY_DIR, "index.json")
    hits: dict = {}
    try:
        with open(idx_path, encoding="utf-8") as f:
            hits = json.load(f).get("hits") or {}
    except Exception:
        hits = {}
    hits[rd] = len(payload["candidates"])
    hits = {d: hits[d] for d in dates if d in hits}
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump({"dates": dates, "hits": hits}, f)
    log.info("历史快照已写出: %s (%d 天可回看)", path, len(dates))
    return path


def write_csv(run_date: str | None = None) -> str:
    payload = build_payload(run_date)
    rd = payload["meta"]["run_date"] or dt.date.today().isoformat()
    path = os.path.join(DATA_DIR, f"candidates_{rd}.csv")
    headers = [lab for _, lab in MAIN_COLUMNS]
    keys = [k for k, _ in MAIN_COLUMNS]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(headers)
        for r in payload["candidates"]:
            wtr.writerow(["" if k == "spark" else ("—" if r.get(k) in (None, "") else r.get(k)) for k in keys])
    log.info("CSV 已导出: %s", path)
    return path


def _loads(s, default=None):
    """default 就是 default —— 传 None 必须真的返回 None
    (前端用 if(!p) 判断 plan 缺失, [] 在 JS 里是 truthy, 会渲染出一张空卡)。"""
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def write_watch_js() -> None:
    """全市场迷你行情表 -> dashboard/watch_data.js (以最近扫描价为准, 每日更新)。"""
    import sqlite3
    from . import datasource as ds
    from .config import DASHBOARD_DIR, DB_PATH
    try:
        uni = ds.get_universe()
        names, sectors = {}, {}
        if uni is not None and not uni.empty:
            for _, r in uni.iterrows():
                c = str(r["code"])
                names[c] = str(r.get("name") or "")
                sectors[c] = str(r.get("sector") or "")
        ps_path = os.path.join(os.path.dirname(DB_PATH), "pricestore.db")
        out = {}
        if os.path.exists(ps_path):
            conn = sqlite3.connect(ps_path)
            last, prev = {}, {}
            for code, px, rn in conn.execute(
                    "SELECT code, c, rn FROM (SELECT code, c, ROW_NUMBER() OVER "
                    "(PARTITION BY code ORDER BY d DESC) rn FROM bars) WHERE rn<=2"):
                try:
                    px = float(px)
                except (TypeError, ValueError):
                    continue
                if px > 0:
                    (last if rn == 1 else prev)[str(code)] = px
            for code, px in last.items():
                chg = round((px / prev[code] - 1) * 100, 2) if prev.get(code) else None
                out[code] = [names.get(code, ""), round(px, 2), chg, sectors.get(code, "")]
            conn.close()
        path = os.path.join(DASHBOARD_DIR, "watch_data.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write("window.__ALL__ = " + json.dumps(out, ensure_ascii=False) + ";\n")
        log.info("全市场迷你行情: %d 只 -> watch_data.js", len(out))
    except Exception as e:
        log.warning("全市场迷你行情导出失败: %s", e)
