#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块7 — 信号回测 (Signal Backtest)
==================================
把历史每日快照里"当天发出的买卖点建议"与其后真实行情对照, 统计各类信号
(标签 × 成长质量 × 入场方式 × 市场温度)的真实触发率/胜率/期望收益, 并据此
给今天的候选打"历史同类信号胜率"标注, 生成回测优选榜。

原则 (多智能体对抗评审后收敛的口径):
  * 只用信号日当天快照里已有的字段重建交易计划 (无未来函数);
  * 锚定bar靠"快照价==该bar收盘价"匹配, 不信任快照标注的日期 —— 美股快照
    在北京时间早晨生成, 标注日与真实数据日差一个交易日, 直接按日期锚定会把
    "第二天的涨跌"泄漏进所有价位 (评审抓出的关键前视偏差);
  * **锚定用原始价, 收益用前复权价** (2026-09-07): 快照价是当天的成交价(原始价),
    而前复权序列的基准是"最新一天" —— 只要这只票在快照日之后除过权, 它那天的
    qfq 价就不等于当天的成交价 (实测 08-26..09-07 有 73 只平移, 中位 0.89%,
    最大 28.1%), 0.25% 的锚定容差被直接打穿。所以: 找"哪一根bar"用不会被未来
    事件改写的 raw 收盘 (取价层多带一条 raw_close), 找到后一切价位/止损/目标/
    收益仍在同索引的 qfq 序列上算 (跨除权只有 qfq 的涨跌幅是真涨跌幅)。
    取不到 raw_close 的序列 (美股 / stock_detail 兜底 / v1 老库) 逐字退回旧行为;
  * 胜率只统计"观察窗完整走完"的信号 (fill后满 HORIZON 根bar)。已了结但窗口
    未满的样本一并剔除, 否则"快出结果的交易"会被优先计入, 胜率被截尾偏差推高;
  * 成交日回落破止损按止损位成交 (挂着的止损单), 不许按成交价记零损失;
    成交日不记目标达成 (bar内先后次序不可知, 保守);
  * 开盘已破止损 -> 计划失效不进场 (不接飞刀); 突破追高不超过计划买入带上限;
  * 同日目标/止损双触发按"先止损"保守处理, 与产品自身的事件回测口径一致;
  * 同一股票同一时间只允许一个信号事件, 结束+冷却后才能再开;
  * 已知局限 (记录在案, 未建模): 信号按日聚集、彼此相关, 分段胜率的有效样本
    小于名义 n; 拿不到价格序列的退市/停牌股 (约2%) 未计入; bar内先后次序按
    保守约定近似。样本仅覆盖近几周的单一市场环境。
"""
from __future__ import annotations
import datetime as dt
import glob
import inspect
import json
import logging
import os
import sqlite3

import numpy as np

from .market import current

log = logging.getLogger("leftside_core.backtest")


def _paths():
    m = current()
    return (os.path.join(m.dashboard_dir, "history"),
            os.path.join(m.dashboard_dir, "backtest_data.js"),
            os.path.join(m.data_dir, "backtest_result.json"))

# ---- 市场规则开关: 来自 Market (ashare: T+1/涨跌停; us: 无) -------------------

# ---- 交易规则参数 -----------------------------------------------------------
ENTRY_VALID_BARS = 10      # 回踩买入的等待窗口 (交易日); 超时未触及 -> 未成交
BREAKOUT_VALID_BARS = 15   # 突破买入的等待窗口
HORIZON_BARS = 20          # 回测判定窗口: fill后20个交易日 (产品计划的60日窗
                           # 在当前几周历史下必然截尾, 20日窗才能有"完整样本")
HEADLINE_GAIN = 0.10       # 主胜率口径: 止损前先到 +10%
SOFT_GAIN = 0.05           # 辅助口径: 最大浮盈曾达 +5%
COOLDOWN_DAYS = 5          # 事件结束后同一股票再开新事件的冷却 (自然日)
MIN_STOP, MAX_STOP = 0.05, 0.15   # 与 tradeplan 一致
SHRINK_K = 12.0            # 分段胜率向全池先验收缩的伪样本数
MIN_SEG_N = 12             # 段"完整窗口"样本数达标才有资格进推荐
FETCH_START_PAD_DAYS = 10  # 价格序列起点 = 最早快照日 - 该天数
ANCHOR_TOL_EXACT = 0.0025  # 锚定: 收盘价与快照价偏差 <=0.25% 视为同一天
ANCHOR_TOL_NEAR = 0.02     # 锚定: 兜底容差


WATCH_CORE_FIELDS = ("roe", "pe_ttm", "netprofit_yoy", "gross_margin", "debt_ratio")


def _watch_subtag(c: dict) -> str:
    """旧快照的 🔎观察 兜底桶追溯拆分 (与 module4 新打标规则一致, 阈值两市同为2.5/40/60)。"""
    if not any(c.get(k) is not None for k in WATCH_CORE_FIELDS):
        return "🔎 观察·缺数据"
    ts, fs = c.get("tech_score"), c.get("fund_score")
    if ts is not None and ts < 2.5:
        return "🔎 观察·技术弱"
    if fs is not None and 40.0 <= fs < 60.0:
        return "☑️ 次强左侧"
    return "🔎 观察·景气冷"


def _prosp_bucket(s):
    if s is None:
        return "na"
    return "lt40" if s < 40 else ("40-60" if s < 60 else "ge60")


def _tier(c):
    return current().growth_tier.get(c.get("growth_quality"), "NA")


# ===========================================================================
#  快照加载
# ===========================================================================
#: **演示种子快照**: 仓库里随代码发布的一份合成快照 (让新克隆出来的仓库日期选择器不是空的),
#: 候选是 `make_demo_data.py` 造的假票 —— 名字一律以"演示"开头, 价格是合成序列的值,
#: 与真实行情毫无关系。前端照常给人看 (它就是演示用的), 但**任何按价格做的重放/锚定统计
#: 都必须把它排除**: 它的假价格会在真代码的真 bar 上乱锚 (实测 14 条全部锚不到 exact)。
#:
#: **它到底害在哪 —— 2026-09-08 实测重写, 旧说法是编的, 别再抄**: 首版这里写"与
#: day_2026-07-01.json 撞 as_of, 按文件名排序在前, 会在 `build_and_run` 的'同一 (code, as_of)
#: 先到先得'里把 200 条真候选顶掉"。**两处都不成立**: ① `build_and_run` 里压根没有按
#: (code, as_of) 去重这回事, 拦截条件是 `code in busy_until and as_of <= busy_until[code]`,
#: 那是一条**按代码**的事件冷却 (事件没走完就把 busy_until 置 9999-12-31), 与两份快照的
#: as_of 撞不撞无关; ② 种子的 14 个代码与 07-01 那份的 200 个代码**交集为空**, 撞了也顶不掉
#: 任何一条。真正的害处是**种子借用了真实存在的股票代码, 配上假名字假价格**: 把它放回样本
#: 重跑 build_and_run 实测多出 **6 笔**纯由合成价造出来的假事件 (002666 / 002999 / 300222 /
#: 300777 / 301010 / 600111 @ 2026-06-30), 而其中 600111 那笔假事件的冷却又**挡掉 1 笔真信号**
#: (600111 @ 2026-07-07)。所以"排除"这件事是对的, 但量级是「6 笔假事件 + 1 笔真信号被挡」,
#: 不是「200 条被顶掉」—— 差两个数量级, 而且错的理由曾被抄进三个仓库, 这条注释是纠正源。
#: 判据 (满足其一): ① meta 里显式标了 demo/seed (以后新造种子请打这个标);
#: ② 该市场的已知种子文件名白名单; ③ 兜底 —— 候选名全部以"演示"开头 (合成盘的签名,
#: 真实 A 股没有这种名字, 美股是英文名, 都不会误伤)。**不删文件、不改文件**, 只在装载处跳过。
DEMO_SNAPSHOT_FILES = {
    "ashare": {"day_2026-06-30.json"},      # 14 条合成候选, n_scanned=14
    "us": set(),
}


def is_demo_snapshot(path: str, meta: dict, cands: list) -> bool:
    """这份快照是不是 `make_demo_data.py` 造的演示种子 (价格是假的)?"""
    if meta.get("demo") or meta.get("seed") or meta.get("demo_seed"):
        return True
    try:
        known = DEMO_SNAPSHOT_FILES.get(current().name) or set()
    except Exception:                                    # Market 未注入 (纯离线自测)
        known = set()
    if os.path.basename(path) in known:
        return True
    return bool(cands) and all(str(c.get("name") or "").startswith("演示") for c in cands)


#: **快照标注日修正表** (2026-09-08 GM 决定1)。两份历史快照自称的 `meta.data_date` 与它里面
#: 的价格不是同一天, 按"价格证明的日期"改正 (逐值证据见 design/backtest_price_from_store.md §5)。
#:
#: **为什么修正必须写在代码里, 只改文件不够**: 回放/模拟盘读的是 `dashboard/history/`, 那是
#: **运行时目录**, 在两个筛选器仓库里都被 .gitignore 掉; 服务器上它只由 run_a.sh 的
#: `rsync -a --ignore-existing docs/history/ dashboard/history/` 回种, 而 `--ignore-existing`
#: 的语义是"目标已存在就一个字节都不写" —— 这两份快照 2026-08-28 17:52 就已经躺在服务器上,
#: 所以**改文件永远到不了生产** (2026-09-08 校验实证: 服务器 dashboard/history 里两份 meta
#: 仍是 07-01 / 08-21, 且 `rsync -a dashboard/ .../stock-screener/docs/a/` 会把这份陈旧副本
#: 发布到线上站, 与 GitHub Pages 那份修正过的 docs/ 长期不一致)。写成代码就跟着 git 走:
#: 服务器每次跑之前 `git reset --hard origin/...`, 修正必然到位, 且哪天要撤销就是一次 revert。
#:
#: 语义 `{market: {文件名: (预期的错值, 应该是)}}` —— 只有当文件里的 `data_date` **正好等于
#: 预期的错值**时才改; 已经在文件里修好的副本 (PC / GitHub Pages) 落到"应该是"那一支, 是
#: no-op; 读到第三种值说明文件被别人动过, **不改并打 warning**, 不许静默按老规则套。
SNAPSHOT_DATA_DATE_FIX = {
    "ashare": {
        # 整份存的是"昨收": 200 条里 184 条只命中 06-30 的原始收盘, 只命中 07-01 的 0 条
        "day_2026-07-01.json": ("2026-07-01", "2026-06-30"),
        # 周一跑却贴了上周五: 275 条里 271 条命中 08-24 的原始收盘, 只命中 08-21 的 0 条
        "day_2026-08-24.json": ("2026-08-21", "2026-08-24"),
    },
    "us": {},
}


def corrected_data_date(path: str, as_of: str | None) -> tuple[str | None, str | None, bool]:
    """按 `SNAPSHOT_DATA_DATE_FIX` 校正一份快照的标注日。

    `as_of` 传已按 data_date -> run_date -> 文件名 兜底解析出来的标注日。
    -> (要用的标注日, 说明或 None, 是否真的改了)。说明非空而"改了"为 False = 该报警的情况。
    """
    try:
        tbl = SNAPSHOT_DATA_DATE_FIX.get(current().name) or {}
    except Exception:                                    # Market 未注入 (纯离线自测)
        tbl = {}
    ent = tbl.get(os.path.basename(path))
    if not ent:
        return as_of, None, False
    wrong, right = ent
    if as_of == right:
        return as_of, None, False                        # 文件本身已经是修正后的副本
    if as_of == wrong:
        return right, "%s: 标注日 %s -> %s (按价格证据修正, GM 决定1)" % (
            os.path.basename(path), wrong, right), True
    return as_of, ("%s: 标注日 %r 既不是已知错值 %r 也不是修正值 %r —— 文件被动过, "
                   "本次不修正" % (os.path.basename(path), as_of, wrong, right)), False


def load_snapshots() -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(_paths()[0], "day_*.json"))):
        try:
            j = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            log.warning("快照 %s 读取失败: %s", p, e)
            continue
        meta = j.get("meta") or {}
        cands = j.get("candidates") or []
        if not cands:
            continue
        if is_demo_snapshot(p, meta, cands):
            log.info("快照 %s 是演示种子 (合成价格), 不进回放样本", os.path.basename(p))
            continue
        as_of = meta.get("data_date") or meta.get("run_date") or os.path.basename(p)[4:14]
        as_of, note, applied = corrected_data_date(p, as_of)
        if note:
            (log.info if applied else log.warning)("快照标注日修正表: %s", note)
        out.append({
            "run_date": meta.get("run_date") or os.path.basename(p)[4:14],
            "as_of": as_of,
            "opp_score": ((meta.get("opp") or {}).get("score")),
            "cands": cands,
        })
    return out


# ===========================================================================
#  价格序列: 主源 yfinance, 兜底 stock_detail 里存过的K线
# ===========================================================================
def _series_from_stock_detail(codes: set[str]) -> dict:
    """从 stock_detail 取每只股票最近一次存档的K线 (echarts [o,c,l,h]) 作兜底。"""
    out = {}
    try:
        conn = sqlite3.connect(current().db_path)
        rows = conn.execute(
            "SELECT code, detail_json FROM stock_detail WHERE (code, run_date) IN "
            "(SELECT code, MAX(run_date) FROM stock_detail GROUP BY code)").fetchall()
        conn.close()
    except Exception as e:
        log.warning("stock_detail 兜底读取失败: %s", e)
        return out
    for code, dj in rows:
        if code not in codes:
            continue
        try:
            d = json.loads(dj)
            dates = d.get("dates") or []
            ohlc = d.get("ohlc") or []
            if len(dates) < 30 or len(dates) != len(ohlc):
                continue
            arr = np.array([[o, h, l, c] for (o, c, l, h) in ohlc], dtype=float)
            out[code] = {"dates": [str(x) for x in dates], "ohlc": arr}
        except Exception:
            continue
    return out


def _call_market_series(fn, codes: list[str], start: str, need_date: str | None) -> dict:
    """调用 Market.fetch_price_series。

    钩子的**老签名是 (codes, start)** (美股至今如此); A 股换成读价格库之后多接一个
    `need_date` —— "这批价格最终要重放到哪一天", 用来判库是不是落后了。签名里没有这个
    参数就按两参调用, 所以老钩子一行不用改。
    (不用 try/TypeError 兜底: 钩子内部抛的 TypeError 会被误当成签名不匹配, 静默降级。)
    """
    if need_date:
        try:
            params = inspect.signature(fn).parameters
            if "need_date" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
                return fn(codes, start, need_date=need_date) or {}
        except (TypeError, ValueError):
            pass
    return fn(codes, start) or {}


def fetch_price_series(codes: list[str], start: str, need_date: str | None = None) -> dict:
    """委托给 Market.fetch_price_series (A股: 价格库直读, 缺的回落腾讯; 美股: yfinance),
    兜底 stock_detail。

    返回 {code: {"dates": [...], "ohlc": ndarray[N,4] 前复权(o,h,l,c),
                 可选 "raw_close": ndarray[N] 原始收盘}}。
    `need_date`: 调用方要重放到的最后一天 (回测=最新快照日, 模拟盘/双周=最新信号日)。
    """
    fn = current().fetch_price_series
    res = dict(_call_market_series(fn, codes, start, need_date)) if fn else {}
    missing = set(codes) - set(res)
    if missing:
        fb = _series_from_stock_detail(missing)
        res.update(fb)
        log.info("stock_detail 兜底补了 %d 只 (仍缺 %d)", len(fb), len(missing) - len(fb))
    return res


def _qfq_closes(ser: dict) -> np.ndarray:
    """序列里的前复权收盘 —— ohlc/ohlcv 的第 4 列 (index 3) 都是收盘。"""
    arr = ser.get("ohlc")
    if arr is None:
        arr = ser.get("ohlcv")
    return np.asarray(arr, dtype=float)[:, 3]


def xd_rebased_closes(ser: dict) -> np.ndarray | None:
    """**除权日快照专用**的比价序列 —— "raw × 因子比", 逐根算出来。

    背景 (2026-09-08 卡 R3-4, 生产同款样本上唯一一笔"新口径更差"的根因): 快照写出来那天
    如果某只票**当天除权**, 那么当天导出的前复权序列基准已经是除权后的因子, 而榜单里的
    `price` 取的是序列最后一根 (=昨天) 的收盘 —— 于是快照存下来的是一个**除权后的昨收**,
    它既不等于昨天的原始收盘, 也不等于任何一天的成交价。600061 XD国投资 2026-06-30:
    raw 收盘 6.55, 快照存 6.40 (= 6.55 × 9.8854/10.1171), raw 锚定只能退到 06-26 (near
    1.72%) —— 反而比旧的 qfq 锚定错了一格。

    修法是双保险: **生成侧**把这种票的 price 改记原始价 (`ashare/export_data.py` 的
    `xd_fix_snapshot_prices`, price_basis='raw_close'); 拿不到原始价时才退到这里 ——
    快照给该候选打 `xd: true`, 锚定就换成这条序列比。

    第 i 位 = 「若 i+1 那天除权, 当天导出的复权后昨收」
             = raw[i] × f[i]/f[i+1] = qfq[i] × raw[i+1] / qfq[i+1]
    因子比不用另外查表: qfq[i] = raw[i]·f[i]/F 里的公共基准 F 在相除时被约掉了。
    **没除权的那些 bar 上它逐值退化成 raw[i]** (f[i]==f[i+1]), 所以这条序列对非除权日
    零影响, 不会把本来锚得好好的样本带偏。最后一根没有"下一根"可用, 直接放 raw[-1]。
    -> 缺 raw_close / 长度对不上 / 无法计算时返回 None (调用方退回旧行为)。
    """
    raw = ser.get("raw_close")
    if raw is None:
        return None
    raw = np.asarray(raw, dtype=float)
    if raw.size < 2 or not np.any(raw > 0):
        return None
    try:
        qfq = _qfq_closes(ser)
    except Exception:                                    # noqa: BLE001
        return None
    if qfq.size != raw.size:
        return None
    out = np.full(raw.size, np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[:-1] = qfq[:-1] * raw[1:] / qfq[1:]
    out[-1] = raw[-1]
    out[~np.isfinite(out)] = np.nan
    return out if np.any(out > 0) else None


def anchor_closes(ser: dict, xd: bool = False) -> np.ndarray:
    """锚定该用哪条收盘价序列: 有原始价用原始价, 没有就退回前复权 (旧行为)。

    为什么必须是原始价: 快照里的 price 是当天的成交价; 前复权序列的基准是"库内最新一天",
    快照日之后的每一次除权都会把那天的 qfq 价整体平移, 拿它去比 0.25% 的容差必然错配
    (实测 7,299 条样本: raw exact 97.5% vs qfq 92.9%, 其中 235 条锚到了不同的 bar)。
    序列里 ohlc/ohlcv 的第 4 列 (index 3) 都是收盘。

    `xd=True` (快照里该候选带 `xd` 标记 = 写快照那天它除权、而生成侧没能拿到原始价):
    改用 `xd_rebased_closes` 的 "raw × 因子比" 序列比 —— 直接拿 raw 比会漏掉那一次除权,
    锚定会退到几根之前的错 bar。拿不到该序列时逐字退回旧行为。
    """
    if xd:
        alt = xd_rebased_closes(ser)
        if alt is not None:
            return alt
    raw = ser.get("raw_close")
    if raw is not None:
        raw = np.asarray(raw, dtype=float)
        if raw.size and np.any(raw > 0):
            return raw
    return _qfq_closes(ser)


# ===========================================================================
#  计划重建 (与 tradeplan.build_trade_plan 同公式; 有存档 plan 优先用存档)
# ===========================================================================
def reconstruct_plan(c: dict) -> dict | None:
    px = c.get("price")
    if not px or px <= 0:
        return None
    stored = c.get("plan") if isinstance(c.get("plan"), dict) else None
    atr_pct = c.get("atr_pct") or 3.0
    a = float(np.clip(atr_pct / 100.0, 0.008, 0.08))

    box_hi, box_lo = c.get("box_hi"), c.get("box_lo")
    # 突破剧本以"存档计划"为准: A股已停发新突破买点(M1九年判负), 旧快照的在途
    # breakout 事件按原剧本走完保留为对照组, 不追溯改账; 无存档时按旧行为兜底。
    _has_box = bool(box_hi and box_lo and 0 < box_lo < box_hi)
    if _has_box and ((stored or {}).get("entry_mode") == "breakout"
                     or (stored is None and c.get("coil"))):
        ref = float(box_hi)
        stop = max(float(box_lo) * 0.995, ref * (1.0 - MAX_STOP))
        if stored and stored.get("entry_mode") == "breakout":
            ref = float(stored.get("entry_ref") or ref)
            stop = float(stored.get("stop_price") or stop)
        return {"kind": "breakout", "entry_ref": ref, "entry_high": ref * (1.0 + 0.5 * a),
                "stop": stop, "box_lo": float(box_lo)}

    support = c.get("support_price")
    if support and support > 0 and px >= support * 0.985:
        ref, mode = float(support), "support"
    elif support and support > 0:
        ref, mode = float(px), "market"
    else:
        ref, mode = float(px), "none"
    entry_low, entry_high = ref * (1.0 - 0.4 * a), ref * (1.0 + 0.5 * a)
    sret = float(np.clip(1.8 * a, MIN_STOP, MAX_STOP))
    stop = ref * (1.0 - sret)
    bp = c.get("breakdown_price")
    if bp and 0 < bp < ref:
        stop = min(stop, float(bp) * 0.995)
    stop = max(stop, ref * (1.0 - MAX_STOP))
    if stored and stored.get("entry_mode") in ("support", "market", "none"):
        mode = stored["entry_mode"]
        entry_low = float(stored.get("entry_low") or entry_low)
        entry_high = float(stored.get("entry_high") or entry_high)
        stop = float(stored.get("stop_price") or stop)
        ref = float(stored.get("entry_ref") or ref)
    return {"kind": "pullback", "mode": mode, "entry_ref": ref,
            "entry_low": entry_low, "entry_high": entry_high, "stop": stop}


# ===========================================================================
#  涨跌停判定: 全天几乎无振幅 + 涨跌幅接近主板停板 -> 视作一字封死
#  (20cm板的10%一字理论可交易, 但按封死保守处理; 只拦真封死情形)
# ===========================================================================
def _limit_up_oneline(o, h, l, c, prev_c):
    fn = current().limit_up_oneline
    return bool(fn(o, h, l, c, prev_c)) if fn else False


def _limit_down_oneline(o, h, l, c, prev_c):
    fn = current().limit_down_oneline
    return bool(fn(o, h, l, c, prev_c)) if fn else False


# ===========================================================================
#  锚定: 用"快照价 == 某bar收盘价"找信号真实数据日 (防错标日期泄漏未来)
# ===========================================================================
def find_anchor(closes: np.ndarray, idx0: int, snap_px: float) -> int | None:
    """从 idx0 (最后一根日期<=标注as_of的bar) 往前找收盘价与快照价吻合的bar。
    优先取容差0.25%内最近的; 否则2%内最接近的; 都没有 -> 用 idx0 兜底。

    `closes` 请用 `anchor_closes(ser)` 取 —— A 股走价格库时那是**原始收盘价**,
    与快照价同口径; 其余市场仍是前复权收盘 (旧行为)。
    """
    lo = max(0, idx0 - 6)
    best, best_d = None, 1e9
    for i in range(idx0, lo - 1, -1):
        cv = closes[i]
        if not (cv > 0):          # 含 NaN: raw_close 允许有缺口, 缺的那根直接跳过
            continue
        d = abs(cv / snap_px - 1.0)
        if d <= ANCHOR_TOL_EXACT:
            return i
        if d < best_d:
            best, best_d = i, d
    if best is not None and best_d <= ANCHOR_TOL_NEAR:
        return best
    return idx0


# ===========================================================================
#  单事件模拟
# ===========================================================================
def simulate(plan: dict, dates: list[str], ohlc: np.ndarray, start_idx: int,
             scale: float) -> dict:
    """从 start_idx (信号数据日后第一根bar) 起模拟。价位按 scale 缩放对齐复权序列。
    同日双触发按先止损; 窗口完整走完才有资格进胜率统计 (complete 标志)。"""
    ref = plan["entry_ref"] * scale
    stop = plan["stop"] * scale
    ehigh = plan["entry_high"] * scale
    kind = plan["kind"]
    n = len(dates)
    valid = BREAKOUT_VALID_BARS if kind == "breakout" else ENTRY_VALID_BARS

    fill_i, fill_px = None, None
    i = start_idx
    while i < n and i < start_idx + valid:
        o, h, l, c = ohlc[i]
        prev_c = ohlc[i - 1][3] if i > 0 else o
        sealed_up = current().limit_boards and _limit_up_oneline(o, h, l, c, prev_c)
        if kind == "breakout":
            px = max(o, ref)
            if h >= ref and not sealed_up and px <= ehigh:
                fill_i, fill_px = i, px           # 突破且未超计划追高带
                break
            if l <= plan["box_lo"] * scale:       # (未成交前提下)破位 -> 剧本失效
                return {"status": "box_broke", "end_i": i}
        else:
            if o <= stop:                          # 开盘已破止损: 不接飞刀
                return {"status": "gap_invalid", "end_i": i}
            if plan.get("mode") in ("market", "none"):
                if not sealed_up:
                    fill_i, fill_px = i, o
                    break
            elif l <= ehigh:                       # 回踩进入买入区 (限价单口径)
                fill_i, fill_px = i, min(o, ehigh)
                break
        i += 1
    if fill_i is None:
        return {"status": "no_fill" if i >= start_idx + valid else "pending",
                "end_i": min(i, n - 1)}

    tgt = fill_px * (1.0 + HEADLINE_GAIN)
    end = min(fill_i + HORIZON_BARS, n - 1)
    complete = (fill_i + HORIZON_BARS) <= (n - 1)
    status = exit_i = exit_px = None

    # 成交当日: 只判止损 (止损单挂在stop位), 不判目标 (bar内次序不可知);
    # A股 T+1 当日不可卖, 连止损也顺延到次日起判。
    if not current().t_plus_one:
        o, h, l, c = ohlc[fill_i]
        if l <= stop:
            status, exit_i = "stopped", fill_i
            exit_px = stop if fill_px > stop else c
    if status is None:
        j = fill_i + 1
        while j <= end:
            o, h, l, c = ohlc[j]
            prev_c = ohlc[j - 1][3]
            if l <= stop:
                if (current().limit_boards and _limit_down_oneline(o, h, l, c, prev_c)
                        and j < n - 1):
                    # 一字跌停卖不出 -> 次日开盘才能离场
                    status, exit_i, exit_px = "stopped", j + 1, ohlc[j + 1][0]
                else:
                    status, exit_i, exit_px = "stopped", j, min(o, stop)
                break
            if h >= tgt:
                status, exit_i, exit_px = "won", j, (o if o > tgt else tgt)
                break
            j += 1
        else:
            if complete:
                status, exit_i, exit_px = "expired", end, ohlc[end][3]
            else:
                status, exit_i, exit_px = "open", n - 1, ohlc[n - 1][3]

    # 浮盈/回撤只算持仓期内 (成交日与止损离场日的极值可能发生在持仓之外, 剔除)
    seg = ohlc[fill_i + 1:exit_i]
    max_h = float(np.max(seg[:, 1])) if len(seg) else fill_px
    min_l = float(np.min(seg[:, 2])) if len(seg) else fill_px
    if status in ("won", "expired", "open"):
        max_h = max(max_h, ohlc[exit_i][1])
        min_l = min(min_l, ohlc[exit_i][2])
    max_h = max(max_h, fill_px)
    min_l = min(min_l, min(fill_px, exit_px))

    ret = exit_px / fill_px - 1.0
    cost = current().cost_rt
    ret -= cost if status != "open" else cost / 2.0   # 持仓中也已付了买入侧成本

    # tR 并行口径 (exitgrid 九年裁决 2026-08-31: 目标=1.4×止损距 两市两族OOS全胜)。
    # 独立重放同一窗口: 只统计、不改任何行为; 影子达标后再议切换主口径。
    win_tR = ret_tR = None
    if complete and fill_px > 0 and stop < fill_px:
        _sret = 1.0 - stop / fill_px
        _tgt = fill_px * (1.0 + 1.4 * _sret)
        _it = _is = None
        _j = fill_i + 1
        _end = min(fill_i + HORIZON_BARS, n - 1)
        while _j <= _end:
            if ohlc[_j][2] <= stop:
                _is = _j
                break                       # 同bar先止损, 与主口径一致
            if _it is None and ohlc[_j][1] >= _tgt:
                _it = _j
                break
            _j += 1
        if _it is not None:
            win_tR, ret_tR = True, _tgt / fill_px - 1.0 - cost
        elif _is is not None:
            win_tR, ret_tR = False, stop / fill_px - 1.0 - cost
        else:
            win_tR, ret_tR = False, float(ohlc[_end][3]) / fill_px - 1.0 - cost
    return {"status": status, "fill_i": fill_i, "fill_px": fill_px,
            "fill_date": dates[fill_i], "exit_i": exit_i, "exit_px": exit_px,
            "exit_date": dates[exit_i], "ret": ret,
            "days": exit_i - fill_i, "complete": bool(complete),
            "max_gain": max_h / fill_px - 1.0, "max_dd": min_l / fill_px - 1.0,
            "win_tR": win_tR, "ret_tR": (round(ret_tR, 4) if ret_tR is not None else None),
            "end_i": exit_i}


# ===========================================================================
#  事件流构建 + 汇总
# ===========================================================================
def _opp_bucket(s):
    if s is None:
        return "na"
    return "cold" if s < 40 else ("hot" if s >= 60 else "mid")


def build_and_run(snaps: list[dict], prices: dict, rkeys=None, rmap=None) -> list[dict]:
    episodes = []
    rkeys, rmap = (rkeys or []), (rmap or {})
    busy_until = {}          # code -> date str, 该日期(含冷却)前不开新事件
    for snap in snaps:
        as_of = snap["as_of"]
        for c in snap["cands"]:
            code = c.get("code")
            if not code or code not in prices:
                continue
            if code in busy_until and as_of <= busy_until[code]:
                continue
            plan = reconstruct_plan(c)
            if plan is None:
                continue
            ser = prices[code]
            dates, ohlc = ser["dates"], ser["ohlc"]
            idx0 = int(np.searchsorted(np.array(dates), as_of, side="right")) - 1
            if idx0 < 0:
                continue
            snap_px = c.get("price")
            if not snap_px or snap_px <= 0:
                continue
            # 锚定bar = 快照价真正来自的那根bar (防标注日错位泄漏次日行情)。
            # 锚在原始价上找 (与快照价同口径), 但 scale 与后续模拟一律用 qfq 同索引值 ——
            # 计划价位被 scale 搬进 qfq 空间, 跨除权的收益才对。
            # `xd` = 写快照那天该票除权且生成侧没拿到原始价 -> 换 "raw×因子比" 序列比。
            anchor = find_anchor(anchor_closes(ser, xd=bool(c.get("xd"))),
                                 idx0, float(snap_px))
            if anchor is None or anchor + 1 >= len(dates):
                continue
            if ohlc[anchor][3] <= 0:
                continue
            scale = float(ohlc[anchor][3]) / float(snap_px)
            if not (0.2 < scale < 5.0):
                continue
            r = simulate(plan, dates, ohlc, anchor + 1, scale)
            gt = _tier(c)
            tag = (c.get("tag") or "").strip()
            if tag == "🔎 观察":
                tag = _watch_subtag(c)     # 追溯拆分: 旧527笔按新分类归因
            if tag == "🪸 深跌抄底":         # ⚡快弹 追溯细分 (与 module4 同条件)
                _r, _a = c.get("rsi"), c.get("atr_pct")
                if _r is not None and _r <= 28.0 and _a is not None and _a >= 5.0:
                    tag = "🪸 深跌抄底·⚡快弹"
            ep = {
                "code": code, "name": c.get("name"), "sig_date": as_of,
                "tag": tag, "growth": gt,
                "prosp": _prosp_bucket(c.get("prosperity_score")),
                "kind": plan["kind"], "mode": plan.get("mode", "breakout"),
                "final_score": c.get("final_score"), "fund_score": c.get("fund_score"),
                "opp": _opp_bucket(snap.get("opp_score")),
                "industry": c.get("industry"),
                "cuosha": ("cs" if c.get("cuosha_score")
                           else ("elig" if c.get("cuosha_eligible") else "other")),
                "regime": _at(rkeys, rmap, dates[anchor], "na") if rkeys else "na",   # 锚定bar而非标注日: 防一日前视
                **{k: r.get(k) for k in ("status", "fill_date", "fill_px", "exit_date",
                                          "exit_px", "ret", "days", "complete",
                                          "max_gain", "max_dd", "win_tR", "ret_tR")},
            }
            episodes.append(ep)
            busy_end = r.get("end_i")
            if busy_end is not None:
                end_date = prices[code]["dates"][busy_end]
                cd = (dt.date.fromisoformat(end_date) + dt.timedelta(days=COOLDOWN_DAYS))
                busy_until[code] = cd.isoformat()
            else:
                busy_until[code] = "9999-12-31"
    return episodes



# ===========================================================================
#  市场状态 (指数 vs 50日均线) + 榜单战绩 (错杀/优质 按"次日开盘买入"的前瞻收益)
# ===========================================================================
import bisect as _bisect

PICK_H = (10, 30, 60)
PICK_COOLDOWN_DAYS = 30


def _bench_frame():
    try:
        fn = current().fetch_benchmark
        df = fn() if fn else None
        if df is None or len(df) < 60 or "close" not in df.columns:
            return None
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        return df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        log.warning("基准指数获取失败(状态/相对收益降级): %s", e)
        return None


def regime_map(df) -> tuple[list, dict, dict]:
    """-> (排序日期, date->regime, date->close)。regime: 指数收盘>50日均线 bull, 否则 bear。"""
    if df is None:
        return [], {}, {}
    c = df["close"].astype(float)
    ma = c.rolling(50).mean()
    reg, px = {}, {}
    for d, cv, m in zip(df["date"], c, ma):
        px[d] = float(cv)
        if m == m:
            reg[d] = "bull" if cv > m else "bear"
    return sorted(reg), reg, px


def _at(keys: list, m: dict, d: str, default=None):
    i = _bisect.bisect_right(keys, d) - 1
    return m[keys[i]] if i >= 0 else default


def eval_picks(day_items: list, prices: dict, bkeys: list, bpx: dict) -> dict:
    """day_items: [(as_of, [codes])] 按日升序。每只票同一30天内只计首次入选。
    买入 = 信号后第一根bar开盘; 统计 +10/+30/+60 bar 收盘收益、30bar内最高价曾达+20%、
    30bar 收益是否跑赢指数。只有窗口走完的样本才进对应统计。"""
    last_pick = {}
    rows = []
    for as_of, codes in day_items:
        for code in codes:
            lp = last_pick.get(code)
            if lp and (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(lp)).days < PICK_COOLDOWN_DAYS:
                continue
            ser = prices.get(code)
            if not ser:
                continue
            dates, ohlc = ser["dates"], ser["ohlc"]
            idx = int(np.searchsorted(np.array(dates), as_of, side="right"))
            if idx >= len(dates) or ohlc[idx][0] <= 0:
                continue
            last_pick[code] = as_of
            entry = float(ohlc[idx][0])
            r = {"code": code, "d": as_of}
            for h in PICK_H:
                j = idx + h
                if j < len(dates):
                    r[f"r{h}"] = float(ohlc[j][3]) / entry - 1.0
            if idx + 30 < len(dates):
                r["hit20"] = bool(float(np.max(ohlc[idx + 1:idx + 31, 1])) >= entry * 1.2)
                b0, b1 = _at(bkeys, bpx, dates[idx]), _at(bkeys, bpx, dates[idx + 30])
                if b0 and b1:
                    r["beat30"] = (r["r30"] - (b1 / b0 - 1.0)) > 0
            rows.append(r)
    def _avg(k):
        v = [x[k] for x in rows if k in x]
        return (round(float(np.mean(v)) * 100.0, 2), len(v)) if v else (None, 0)
    out = {"n": len(rows)}
    for h in PICK_H:
        out[f"r{h}"], out[f"n{h}"] = _avg(f"r{h}")
    h20 = [x["hit20"] for x in rows if "hit20" in x]
    out["hit20"] = round(sum(h20) / len(h20) * 100.0, 1) if h20 else None
    bt = [x["beat30"] for x in rows if "beat30" in x]
    out["beat30"] = round(sum(bt) / len(bt) * 100.0, 1) if bt else None
    out["first"] = rows[0]["d"] if rows else None
    out["last"] = rows[-1]["d"] if rows else None
    return out


def _quality_history() -> list:
    items = []
    for p in sorted(glob.glob(os.path.join(_paths()[0], "quality_*.json"))):
        try:
            j = json.load(open(p, encoding="utf-8"))
            items.append((j.get("date") or os.path.basename(p)[8:18],
                          [x["code"] for x in (j.get("picks") or []) if x.get("code")]))
        except Exception:
            continue
    return items

RESOLVED = ("won", "stopped", "expired")
UNFILLED = ("no_fill", "box_broke", "gap_invalid")


def _stats_pool(eps: list[dict]) -> list[dict]:
    """进胜率统计的样本 = 已了结 且 观察窗完整 (剔除截尾偏差)。"""
    return [e for e in eps if e["status"] in RESOLVED and e.get("complete")]


def _seg_stats(eps: list[dict], p0: float) -> dict:
    fills = [e for e in eps if e["status"] in RESOLVED + ("open",)]
    res = _stats_pool(eps)
    n_sig = sum(1 for e in eps if e["status"] != "pending")
    won = sum(1 for e in res if e["status"] == "won")
    soft = sum(1 for e in res if (e.get("max_gain") or 0) >= SOFT_GAIN)
    rets = [e["ret"] for e in res if e.get("ret") is not None]
    days = [e["days"] for e in res if e.get("days") is not None]
    win = won / len(res) if res else None
    return {
        "n_signals": n_sig, "n_filled": len(fills), "n_resolved": len(res),
        "n_open": len(fills) - len(res),      # 持仓中 + 窗口未满不计入统计的
        "fill_rate": round(len(fills) / n_sig, 3) if n_sig else None,
        "win10": round(win, 3) if win is not None else None,
        "win10_post": round((won + SHRINK_K * p0) / (len(res) + SHRINK_K), 3) if res else None,
        "reach5": round(soft / len(res), 3) if res else None,
        "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
        "med_days": int(np.median(days)) if days else None,
        "mfe_q50": (round(float(np.median([e["max_gain"] for e in res if e.get("max_gain") is not None])), 4)
                    if any(e.get("max_gain") is not None for e in res) else None),
        "mfe_q75": (round(float(np.percentile([e["max_gain"] for e in res if e.get("max_gain") is not None], 75)), 4)
                    if any(e.get("max_gain") is not None for e in res) else None),
        "mae_q50": (round(float(np.median([e["max_dd"] for e in res if e.get("max_dd") is not None])), 4)
                    if any(e.get("max_dd") is not None for e in res) else None),
        "status_counts": {st: sum(1 for e in res if e["status"] == st)
                          for st in ("won", "stopped", "expired")} if res else None,
        "win_tR": (round(sum(1 for e in res if e.get("win_tR")) /
                         max(1, sum(1 for e in res if e.get("win_tR") is not None)), 3)
                   if any(e.get("win_tR") is not None for e in res) else None),
        "avg_ret_tR": (round(float(np.mean([e["ret_tR"] for e in res
                                            if e.get("ret_tR") is not None])), 4)
                       if any(e.get("ret_tR") is not None for e in res) else None),
    }


def aggregate(episodes: list[dict]) -> dict:
    res_all = _stats_pool(episodes)
    p0 = (sum(1 for e in res_all if e["status"] == "won") / len(res_all)) if res_all else 0.4
    out = {"pool": _seg_stats(episodes, p0), "p0": round(p0, 3),
           "by_tag": {}, "by_growth": {}, "by_combo": {}, "by_mode": {}, "by_opp": {},
           "by_cuosha": {}}
    def _group(keyf):
        g = {}
        for e in episodes:
            g.setdefault(keyf(e), []).append(e)
        return g
    for k, eps in _group(lambda e: e["tag"] or "?").items():
        out["by_tag"][k] = _seg_stats(eps, p0)
    for k, eps in _group(lambda e: e["growth"]).items():
        out["by_growth"][k] = _seg_stats(eps, p0)
    for k, eps in _group(lambda e: f'{e["tag"]}|{e["growth"]}').items():
        s = _seg_stats(eps, p0)
        if s["n_signals"] >= 6:
            out["by_combo"][k] = s
    for k, eps in _group(lambda e: e["mode"]).items():
        out["by_mode"][k] = _seg_stats(eps, p0)
    for k, eps in _group(lambda e: e["opp"]).items():
        out["by_opp"][k] = _seg_stats(eps, p0)
    # 三段: 达标(cs) / 过门槛未达标(elig) / 其它 —— elig 组固定了准入门槛的
    # 选择效应(深回撤+基本面前40%), cs vs elig 才是对打分本身的检验
    for k, eps in _group(lambda e: e.get("cuosha") or "other").items():
        out["by_cuosha"][k] = _seg_stats(eps, p0)
    out["by_regime"] = {}
    for k, eps in _group(lambda e: e.get("regime") or "na").items():
        out["by_regime"][k] = _seg_stats(eps, p0)
    # 标签x温度 交叉 (复活门与温度影子分析的数据底座)
    out["by_tag_opp"] = {}
    for k, eps in _group(lambda e: f'{e["tag"]}|{e["opp"]}').items():
        s = _seg_stats(eps, p0)
        if s["n_signals"] >= 6:
            out["by_tag_opp"][k] = s
    # 景气分段 (景气门从未被度量过)
    out["by_prosp"] = {}
    for k, eps in _group(lambda e: e.get("prosp") or "na").items():
        out["by_prosp"][k] = _seg_stats(eps, p0)
    out["metric_note"] = ("win10 依赖出场参数(止损/目标/窗口), 跨配置不可比; "
                          "横向比较请用扣成本 avg_ret")
    return out


def recommend(latest_cands: list[dict], agg: dict) -> tuple[list[dict], dict]:
    """给今天的候选贴历史同类段位战绩; 段位达标(完整样本够+收缩后胜率优于全池+
    期望为正)的入围, 再按(段位胜率, 今日综合分)取前20进推荐榜。
    返回 (推荐榜, code->段位战绩 映射, 供前端行内展示)。"""
    p0 = agg.get("p0") or 0.4
    recos, today_map = [], {}
    for c in latest_cands:
        gt = _tier(c)
        combo = agg["by_combo"].get(f'{(c.get("tag") or "").strip()}|{gt}')
        # 细分段样本不足时退回标签级 (细分段小样本不许贴大胜率)
        if combo and combo["n_resolved"] >= MIN_SEG_N:
            seg, seg_kind = combo, "combo"
        else:
            seg, seg_kind = agg["by_tag"].get((c.get("tag") or "").strip()), "tag"
        if not seg:
            continue
        item = {"code": c["code"], "name": c.get("name"), "tag": c.get("tag"),
                "growth": gt, "seg_kind": seg_kind,
                "seg_n": seg["n_resolved"], "seg_win": seg["win10"],
                "seg_win_post": seg["win10_post"], "seg_ret": seg["avg_ret"],
                "seg_days": seg["med_days"]}
        today_map[c["code"]] = item
        ok = (seg["n_resolved"] >= MIN_SEG_N and seg["win10_post"] is not None
              and seg["win10_post"] >= max(0.55, p0 + 0.03)
              and (seg["avg_ret"] or 0) > 0)
        if ok:
            item = dict(item, fs=c.get("final_score") or 0)
            recos.append(item)
    # 段位达标只是入围; 榜单按 (段位收缩胜率, 今日综合分) 取前20, 避免"全场都是优选"
    recos.sort(key=lambda x: (-(x["seg_win_post"] or 0), -(x["fs"] or 0)))
    recos = recos[:20]
    for item in recos:
        item["reco"] = 1
        today_map[item["code"]] = item
    return recos, today_map


# ===========================================================================
#  入口
# ===========================================================================
def run_backtest(write_js: bool = True) -> dict | None:
    snaps = load_snapshots()
    if len(snaps) < 3:
        log.info("快照不足 3 天, 跳过回测")
        return None
    # 给每天的快照候选重算错杀分 (输入字段快照里都有), 供 by_cuosha 分段验证
    try:
        from . import cuosha
        for s_ in snaps:
            cuosha.annotate(s_["cands"])
    except Exception as e:
        log.warning("错杀标注失败(回测继续): %s", e)
    qhist = _quality_history()
    qcodes = {code for _, cs in qhist for code in cs}
    codes = sorted({c["code"] for s in snaps for c in s["cands"] if c.get("code")} | qcodes)
    start = (dt.date.fromisoformat(snaps[0]["as_of"])
             - dt.timedelta(days=FETCH_START_PAD_DAYS)).isoformat()
    log.info("回测: %d 天快照, %d 只股票, 价格起点 %s", len(snaps), len(codes), start)
    prices = fetch_price_series(codes, start, need_date=snaps[-1]["as_of"])
    log.info("价格覆盖 %d/%d", len(prices), len(codes))
    bdf = _bench_frame()
    rkeys, rmap, bpx = regime_map(bdf)
    episodes = build_and_run(snaps, prices, rkeys, rmap)
    agg = aggregate(episodes)
    # 榜单战绩: 错杀候选 (每日快照重算) / 优质公司 (history/quality_*.json)
    try:
        cs_items = [(s["as_of"], [c["code"] for c in s["cands"] if c.get("cuosha_score")]) for s in snaps]
        picks_bt = {"cuosha": eval_picks(cs_items, prices, rkeys, bpx),
                    "quality": eval_picks(qhist, prices, rkeys, bpx)}
    except Exception as e:
        log.warning("榜单战绩计算失败: %s", e)
        picks_bt = {}
    latest = snaps[-1]["cands"]
    recos, today_map = recommend(latest, agg)

    by_code = {}
    for e in episodes:
        if e["status"] in RESOLVED:
            by_code.setdefault(e["code"], []).append(
                {"d": e["sig_date"], "s": e["status"], "ret": round((e["ret"] or 0) * 100, 1),
                 "days": e["days"]})
    recent = sorted([e for e in episodes if e["status"] in RESOLVED],
                    key=lambda x: x["exit_date"] or "", reverse=True)[:100]
    opens = [e for e in episodes if e["status"] == "open"]
    result = {
        "meta": {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "n_days": len(snaps), "first_day": snaps[0]["run_date"],
                 "last_day": snaps[-1]["run_date"],
                 "n_codes": len(codes), "px_cover": len(prices),
                 "n_episodes": len(episodes),
                 "horizon": HORIZON_BARS, "headline_gain": HEADLINE_GAIN,
                 "cost_rt": current().cost_rt},
        "agg": agg, "recos": recos[:40], "today": today_map, "picks_bt": picks_bt,
        "recent": [{k: e.get(k) for k in ("code", "name", "tag", "sig_date", "fill_date",
                                           "exit_date", "status", "ret", "days", "growth")}
                   for e in recent],
        "open": [{k: e.get(k) for k in ("code", "name", "tag", "sig_date", "fill_date",
                                         "status", "ret", "growth")} for e in opens][:80],
        "by_code": {k: v[-3:] for k, v in by_code.items()},
    }
    _hist, bt_js, bt_json = _paths()
    json.dump(result, open(bt_json, "w", encoding="utf-8"), ensure_ascii=False)
    if write_js:
        with open(bt_js, "w", encoding="utf-8") as f:
            f.write("window.__BT__ = ")
            json.dump(result, f, ensure_ascii=False)
            f.write(";\n")
        log.info("回测导出: %s (episodes=%d, 完整窗口已了结=%d, 推荐=%d)", bt_js,
                 len(episodes), agg["pool"]["n_resolved"], len(recos))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_backtest()
