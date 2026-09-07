#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Market — 两个筛选器共用核心的"市场适配器"
==========================================
leftside_core 里的回测/错杀/新闻标记等模块不知道自己跑在哪个市场; 每个仓库
定义一个 Market 实例 (ashare/market.py, screener/market.py), 把市场差异集中
在这一个对象里: 交易规则开关、成本、成长质量标签映射、价格序列/基准指数/
新闻标题的取数函数、路径。核心模块通过 `MARKET` 全局读取 (由各仓库的 shim
在导入时注入)。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Market:
    name: str                                   # "ashare" | "us"
    # 路径
    dashboard_dir: str
    data_dir: str
    db_path: str
    # 回测交易规则
    t_plus_one: bool = False
    limit_boards: bool = False
    cost_rt: float = 0.002
    # 成长质量标签 -> G/M/W
    growth_tier: dict = field(default_factory=dict)
    tier_label: dict = field(default_factory=dict)
    # 取数钩子
    # (codes, start[, need_date=]) -> {code: {dates, ohlc[, raw_close]}}
    #   ohlc      = 前复权 (o,h,l,c); 收益/止损/目标一律算在它上面
    #   raw_close = 可选, 与 ohlc 同索引的**原始收盘价**, 只用来锚定快照价 (backtest.anchor_closes);
    #               不带这个键 = 退回"用前复权收盘锚定"的旧行为 (美股至今如此)
    #   need_date = 可选关键字, "这批价格要重放到哪一天" (判本地库是否落后); 钩子签名里没有
    #               这个参数时核心自动按两参调用, 所以老钩子不用改
    fetch_price_series: Optional[Callable[..., dict]] = None
    fetch_benchmark: Optional[Callable[[], object]] = None              # () -> DataFrame(date, close)
    limit_up_oneline: Optional[Callable] = None                         # (o,h,l,c,prev_c) -> bool
    limit_down_oneline: Optional[Callable] = None
    news_titles: Optional[Callable[[str], list]] = None                 # code -> [(date, title, url)]
    news_keywords: list = field(default_factory=list)                   # [(keyword, label)]
    # 长历史研究钩子 (pricestore/coilscan 用): 带成交量的日线批量取数 + 基准指数长历史
    fetch_bars_bulk: Optional[Callable[[list, str], dict]] = None       # (codes, start) -> {code: [(d,o,h,l,c,v),...]}
    fetch_index_bars: Optional[Callable[[str], list]] = None            # (start) -> [(d,o,h,l,c,v),...]
    universe_codes: Optional[Callable[[], list]] = None                 # () -> 全市场代码
    # 按交易日拉全市场的钩子 (pricestore schema v2 增量; 只有 A 股 Tushare 路径实现)。
    # 约定: 返回 None = "本路径未启用" (源开关不指向 Tushare / 无 token) -> pricestore 回退旧逐股增量;
    #       返回 {} = "启用了但当日无数据" (非交易日/源未就绪)。这个区分是**故意**的, 别改成 {}。
    fetch_bars_by_date: Optional[Callable[[str], dict]] = None          # (d) -> {code: (o,h,l,c,v,amt)} 原始价, v=股 amt=元
    fetch_adj_by_date: Optional[Callable[[str], dict]] = None           # (d) -> {code: factor} 复权因子
    trading_days: Optional[Callable[[str, str], list]] = None           # (start,end) -> ['YYYY-MM-DD',...] 开市日
    fetch_universe_rows: Optional[Callable[[], list]] = None            # () -> [(code,name,list_date,delist_date,status)]
    log_prefix: str = "leftside_core"


_CURRENT: Market | None = None


def set_market(m: Market) -> Market:
    global _CURRENT
    _CURRENT = m
    return m


def current() -> Market:
    if _CURRENT is None:
        raise RuntimeError("leftside_core: Market 未注入 — 请先 import 仓库的 market 模块 (set_market)")
    return _CURRENT
