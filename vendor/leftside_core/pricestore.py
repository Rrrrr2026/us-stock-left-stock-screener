#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地价格库 (Local price store) — **schema v2**
=============================================
量化研究的地基: 全市场长历史日线 (含成交量) 抓一次存本地 SQLite, 之后每天增量
更新。所有回测/形态扫描只读本地库 —— 数据源抖动最多耽误一次更新, 不再影响研究。

v1 (2026-08 及以前) 只存**前复权价**, 于是有两个结构性毛病:
  ① 前复权基准是"拉取那天", 不同日拉的历史不能混库 (混了就有跳变) → 每次拉长历史都得整库重抓;
  ② 除权当天全序列平移, 增量更新永远在"局部改写历史", 对不上就只能重抓。
v2 (2026-09-07, Tushare 适配 P1) 改为 **原始价 + 复权因子落库, 前复权物化**:

  bars_raw(code,d,o,h,l,c,v,amt)   原始价 (未复权)。**v 恒为股, amt 恒为元**
  adj(code,d,factor)               复权因子 (Tushare adj_factor)
  adj_base(code,d,factor)          该股当前 qfq 基准 = 库内最新因子 (物化 bars 的除数, 可审计)
  bars(code,d,o,h,l,c,v,amt)       **物化前复权 (qfq)** = raw × factor / adj_base.factor
  idx_bars(d,o,h,l,c,v)            基准指数 (沪深300 / SPY), **v 为手**
  idx_multi(sym,d,o,h,l,c,v)       研究用多指数 (000001.SH/399001.SZ/...), v 为手
  universe(code,name,list_date,delist_date,status)   含退市股 (修幸存者偏差)
                                   日期为 NULL = 源没给 (**绝不写 '1970-01-01' 哨兵**);
                                   list_date 为 NULL 时 universe_at() 以首根 bar 代替
  meta(key,value)                  source / unit_v / unit_amt / rebase_date / max_trade_date

**兼容性**: `bars` 的前 6 列与 v1 逐字相同 (`SELECT d,o,h,l,c,v` 照旧), `load()` 默认
`adjust="qfq"` 返回的结构与 v1 完全一致 —— 所有回测/扫描/研究脚本一行都不用改。
美股库没有 bars_raw/adj (yfinance 直接给复权价), v2 的表建了但为空, 走的仍是 v1 路径。

取数走 Market 钩子 (fetch_bars_bulk / fetch_index_bars / universe_codes; A 股 Tushare
路径另有 fetch_bars_by_date / fetch_adj_by_date / trading_days / fetch_universe_rows),
市场差异 (腾讯分页 vs yfinance 批量、按日拉全市场 vs 逐股) 全部在各仓库 market.py 里。
"""
from __future__ import annotations
import datetime as dt
import logging
import os
import sqlite3

import numpy as np

from .market import current

log = logging.getLogger("leftside_core.pricestore")

YEARS = 5
STALE_DAYS_FULL = 3650      # backfill: 完全没有该股才算缺
BATCH = 400                 # 每批写库/打日志的代码数
MIN_BARS = 60               # load(): 少于这么多根的代码不返回 (与 v1 一致)
MAT_BATCH = 300             # 物化 qfq 时每多少只提交一次
PENDING_MAT_KEY = "_pending_materialize"   # 崩溃面包屑: 待整段重物化的代码 (逗号分隔)

ADJUSTS = ("qfq", "hfq", "raw")


def _db_path() -> str:
    return os.path.join(current().data_dir, "pricestore.db")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表 + 老库就地升级 (只加列/加表, 不改动既有数据)。幂等。"""
    conn.execute("CREATE TABLE IF NOT EXISTS bars("
                 "code TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, amt REAL, "
                 "PRIMARY KEY(code, d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS bars_raw("
                 "code TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, amt REAL, "
                 "PRIMARY KEY(code, d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS adj("
                 "code TEXT, d TEXT, factor REAL, PRIMARY KEY(code, d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS adj_base("
                 "code TEXT PRIMARY KEY, d TEXT, factor REAL) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS idx_bars("
                 "d TEXT PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS idx_multi("
                 "sym TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, "
                 "PRIMARY KEY(sym, d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS universe("
                 "code TEXT PRIMARY KEY, name TEXT, list_date TEXT, delist_date TEXT, "
                 "status TEXT) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID")
    # v1 老库的 bars 没有 amt 列 -> 补上 (老行为 NULL, 消费者按 6 列读, 无感)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bars)")}
    if cols and "amt" not in cols:
        conn.execute("ALTER TABLE bars ADD COLUMN amt REAL")
        log.info("价格库升级: bars 增加 amt 列")


def _conn(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or _db_path(), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _upsert(conn: sqlite3.Connection, code: str, rows: list) -> None:
    """v1 路径 (美股 / A 股旧源): rows = [(d,o,h,l,c,v), ...] 已是前复权价, 直接进 bars。"""
    conn.executemany(
        "INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
        [(code, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])


# ------------------------------------------------------------------ 股票池日期规整

#: 各数据源用来表示 "这个日期没有" 的哨兵值。**为什么要有这张表**: 老板买的 Tushare 兼容镜像
#: 在 stock_basic 里把缺失的 list_date 返回成 epoch 0 的 '19700101' (2026-09-07 实测 4 只刚上市
#: 的次新股), 而 '1970-01-01' 是个**合法日期字符串** —— 直接落库, `universe_at(任意历史日)` 就
#: 会把 2026 年才上市的票算成 "1970 年就在市", 点时股票池被前视污染, 九年研究的分母全歪。
#: 所以: 空/哨兵一律落 NULL, 让 "不知道" 就是 "不知道"; 怎么用 NULL 由 `universe_at` 决定。
_NULL_DATE_TOKENS = {"", "-", "0", "00000000", "0000-00-00", "19700101", "1970-01-01",
                     "none", "null", "nan", "nat", "nattype"}

#: 点时股票池的进程内缓存 (库指纹 -> (universe 行, 首根 bar 表)), 见 `_pit_tables`。
_PIT_CACHE: dict = {}


def norm_date(v) -> str | None:
    """任意来源的日期 -> 'YYYY-MM-DD'; 空值 / 哨兵 / 解析不出来的 -> **None** (落库即 NULL)。

    接受 '20260907' / '2026-09-07' / '2026/09/07' / date / datetime / None / NaN / NaT。
    1900 年以前一律判为哨兵 (A 股最早上市日是 1990-12-19, epoch 0 与 Excel 的 1899-12-30
    都在这条线以下), 避免再冒出一个新的 "看起来像日期的空值"。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in _NULL_DATE_TOKENS:
        return None
    s = s[:10]
    if len(s) == 10 and not s[4].isdigit():                  # 'YYYY-MM-DD' / 'YYYY/MM/DD'
        s = s.replace("/", "-").replace(".", "-")
    elif s.count("-") == 2 or s.count("/") == 2:             # 'YYYY-M-D' 之类
        parts = s.replace("/", "-").split("-")
        if len(parts) != 3 or not all(x.isdigit() for x in parts):
            return None
        s = f"{parts[0].zfill(4)}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    else:
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) < 8:
            return None
        s = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"    # '20260907120000' 一类带时分秒的也认
    if len(s) != 10 or s in _NULL_DATE_TOKENS or s < "1900-01-01":
        return None
    return s


def normalize_universe_rows(rows) -> list:
    """`fetch_universe_rows` 的产物 -> 可直接落 universe 表的行。

    [(code, name, list_date, delist_date, status), ...] 同构返回, 但两个日期都过 `norm_date`
    (空 -> None = NULL, **不是 '1970-01-01' 也不是 ''**), delist_date 有值就原样带上 (退市股
    的出池日是幸存者偏差修正的另一半, 丢了等于没修)。没有 code 的行丢掉。
    """
    out = []
    for r in rows or []:
        r = list(r) + [None] * (5 - len(r))
        code = str(r[0] or "").strip()
        if not code:
            continue
        out.append((code, "" if r[1] is None else str(r[1]).strip(),
                    norm_date(r[2]), norm_date(r[3]),
                    str(r[4] or "").strip().upper()))
    return out


# ------------------------------------------------------------------ meta


def meta_set(pairs: dict, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or _conn()
    conn.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                     [(str(k), "" if v is None else str(v)) for k, v in pairs.items()])
    conn.commit()
    if own:
        conn.close()


def meta_all(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or _conn()
    out = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    if own:
        conn.close()
    return out


def meta_get(key: str, default=None):
    return meta_all().get(key, default)


# ------------------------------------------------------------------ 前复权物化


def _factor_series(conn: sqlite3.Connection, code: str) -> list:
    return conn.execute("SELECT d, factor FROM adj WHERE code=? ORDER BY d", (code,)).fetchall()


def _qfq_rows(raw_rows: list, fac_rows: list):
    """(raw_rows, fac_rows) -> ([(d,o,h,l,c,v,amt) qfq...], base_factor, base_date)。

    因子按日期前向填充 (某日缺因子时沿用上一个已知因子; 序列开头缺则用第一个已知因子回填)。
    基准 = **该股库内最新因子** —— 与 v1 "前复权以拉取日为基准" 口径一致, 所以换库后
    价格水平不跳变 (只在除权日之后才有 <1 个因子步长的差, 验收门 4 就是量这个)。
    """
    if not raw_rows:
        return [], None, None
    if not fac_rows:
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in raw_rows], None, None
    fd = [f[0] for f in fac_rows]
    fv = [float(f[1]) for f in fac_rows]
    base, base_d = fv[-1], fd[-1]
    if not base or base <= 0:
        return [], None, None
    out, j, cur = [], 0, fv[0]          # 开头缺因子 -> 用第一个已知因子回填
    for r in raw_rows:
        d = r[0]
        while j < len(fd) and fd[j] <= d:
            cur = fv[j]
            j += 1
        k = cur / base
        out.append((d, r[1] * k, r[2] * k, r[3] * k, r[4] * k, r[5], r[6]))
    return out, base, base_d


def materialize(codes=None, conn: sqlite3.Connection | None = None,
                progress_every: int = 0) -> int:
    """把 bars_raw × adj 物化成前复权 `bars` (整段重写这些代码), 并刷新 adj_base。

    codes=None -> bars_raw 里的全部代码 (整库重物化, 5,900 只 × 2,400 根约 2-4 分钟)。
    """
    own = conn is None
    conn = conn or _conn()
    if codes is None:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM bars_raw ORDER BY code")]
    codes = list(codes)
    n = 0
    for i, code in enumerate(codes, 1):
        raw = conn.execute("SELECT d,o,h,l,c,v,amt FROM bars_raw WHERE code=? ORDER BY d",
                           (code,)).fetchall()
        if not raw:
            continue
        rows, base, base_d = _qfq_rows(raw, _factor_series(conn, code))
        if not rows:
            continue
        conn.execute("DELETE FROM bars WHERE code=?", (code,))
        conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v,amt) "
                         "VALUES(?,?,?,?,?,?,?,?)", [(code, *r) for r in rows])
        if base is not None:
            conn.execute("INSERT OR REPLACE INTO adj_base(code,d,factor) VALUES(?,?,?)",
                         (code, base_d, base))
        n += 1
        if i % MAT_BATCH == 0:
            conn.commit()
            if progress_every and i % progress_every == 0:
                log.info("物化前复权 %d/%d", i, len(codes))
    conn.commit()
    if own:
        conn.close()
    return n


# ------------------------------------------------------------------ 通用查询


def last_dates(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or _conn()
    out = {r[0]: r[1] for r in conn.execute("SELECT code, MAX(d) FROM bars GROUP BY code")}
    if own:
        conn.close()
    return out


def universe_rows(conn: sqlite3.Connection | None = None) -> list:
    """-> [{"code","name","list_date","delist_date","status"}, ...] (按代码排序)。"""
    own = conn is None
    conn = conn or _conn()
    rows = conn.execute("SELECT code,name,list_date,delist_date,status FROM universe "
                        "ORDER BY code").fetchall()
    if own:
        conn.close()
    return [{"code": r[0], "name": r[1], "list_date": r[2], "delist_date": r[3],
             "status": r[4]} for r in rows]


def _db_fingerprint(conn: sqlite3.Connection) -> tuple:
    """(主库路径, 主库 mtime/size, -wal mtime/size, 本连接改动数) —— 库一变缓存就失效。

    WAL 下写入先落在 `-wal`, 所以两个文件都要看; `total_changes` 兜住 "同一个连接刚写完
    又读" 的场景 (mtime 粒度可能吃掉毫秒级的先写后读)。内存库返回 () = 不缓存。
    """
    try:
        path = ""
        for _seq, name, f in conn.execute("PRAGMA database_list"):
            if name == "main":
                path = f or ""
                break
        if not path:
            return ()
        out = [path, conn.total_changes]
    except sqlite3.Error:
        return ()
    for f in (path, path + "-wal"):
        try:
            st = os.stat(f)
            out += [st.st_mtime_ns, st.st_size]
        except OSError:
            out += [0, 0]
    return tuple(out)


def _pit_tables(conn: sqlite3.Connection, refresh: bool = False) -> tuple:
    """点时股票池要用的两张小表, 按库指纹缓存在进程内: (universe 行, 首根 bar 表)。

    universe 行 = [(code, list_date|None, delist_date|None), ...], 日期一律过 `norm_date`
    (老库里遗留的 '' / '1970-01-01' 哨兵在这里就被当成 "没有" 处理, 不必等重建)。
    首根 bar 表 = {code: 库内最早的一根 bar 的日期}。九年重放会按天调 universe_at() 上千次,
    每次都全表聚合一遍 9 百万行就是分钟级的浪费, 所以缓存; universe 为空 (美股库) 时直接
    短路, 连聚合都不做。
    """
    key = _db_fingerprint(conn)
    if key and not refresh and key in _PIT_CACHE:
        return _PIT_CACHE[key]
    uni = [(r[0], norm_date(r[1]), norm_date(r[2]))
           for r in conn.execute("SELECT code, list_date, delist_date FROM universe")]
    first: dict = {}
    if uni:
        first = {r[0]: r[1] for r in conn.execute(
            "SELECT code, MIN(d) FROM bars_raw GROUP BY code") if r[1]}
        if not first:                    # v1 老库 / 美股库没有 bars_raw
            first = {r[0]: r[1] for r in conn.execute(
                "SELECT code, MIN(d) FROM bars GROUP BY code") if r[1]}
    val = (uni, first)
    if key:
        if len(_PIT_CACHE) > 3:
            _PIT_CACHE.clear()
        _PIT_CACHE[key] = val
    return val


def first_bar_dates(conn: sqlite3.Connection | None = None, refresh: bool = False) -> dict:
    """{code: 库内第一根 bar 的日期} (universe 为空时返回 {})。进程内按库指纹缓存。"""
    own = conn is None
    conn = conn or _conn()
    try:
        return _pit_tables(conn, refresh)[1]
    finally:
        if own:
            conn.close()


def universe_at(date: str, conn: sqlite3.Connection | None = None) -> list:
    """某日**在市且买得到**的代码 —— 点时股票池, 修幸存者偏差用。

    三条判据同时成立才入池:
      · **退市**: delist_date 为空 (还在市) 或 > d —— 退市当日即出池;
      · **上市**: list_date <= d。**list_date 为 NULL 时改用"库内第一根 bar"** ——
        数据源对少数新股不给上市日 (镜像把它返回成 epoch 哨兵 19700101, 入库时已规整成
        NULL, 见 `norm_date`); 没有上市日就绝不能当成 "1970 年就在市" (那会让 2016 年的
        点时股票池混进 2026 年才上市的票 = 前视污染), 只能说 "从我们有第一根 bar 那天起
        在市"。连一根 bar 都没有的 NULL 上市日代码不入池 —— 没有任何在市证据。
      · **有价**: 该股在库里有 bar 时, 第一根 bar 必须 <= d。上市日早于 d 但首根 bar 晚于
        d 的 (长期停牌后复牌、或上市早于库起点却直到 d 之后才恢复交易) 那天根本买不到,
        进池只会虚增分母。

    universe 表为空 (美股库 / 尚未重建) 时返回 []; 调用方应据此决定是否退回 last_dates()。
    """
    own = conn is None
    conn = conn or _conn()
    try:
        d = str(date)[:10]
        uni, first = _pit_tables(conn)
        out = []
        for code, ld, dd in uni:
            if dd and dd <= d:                      # 已退市
                continue
            fb = first.get(code)
            if fb and fb > d:                       # 首根 bar 还没到 = 当日买不到
                continue
            if ld:
                if ld > d:                          # 还没上市
                    continue
            elif not fb:                            # 没上市日又没 bar: 无从判断
                continue
            out.append(code)
        out.sort()
        return out
    finally:
        if own:
            conn.close()


# ------------------------------------------------------------------ 回填 / 增量


def backfill(codes: list | None = None, years: int = YEARS) -> dict:
    """缺哪补哪: 库里没有的代码抓全量 5 年; 已有的跳过 (增量交给 update_daily)。"""
    m = current()
    if codes is None:
        codes = (m.universe_codes or (lambda: []))()
    start = (dt.date.today() - dt.timedelta(days=365 * years + 30)).isoformat()
    conn = _conn()
    have = set(last_dates(conn))
    todo = [c for c in codes if c not in have]
    log.info("价格库回填: 目标 %d, 已有 %d, 待抓 %d (起点 %s)",
             len(codes), len(have), len(todo), start)
    n_ok = 0
    aborted = False
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        got = m.fetch_bars_bulk(chunk, start)
        for code, rows in got.items():
            _upsert(conn, code, rows)
            n_ok += 1
        conn.commit()
        log.info("回填批 %d/%d: +%d (累计 %d)",
                 i // BATCH + 1, (len(todo) + BATCH - 1) // BATCH, len(got), n_ok)
        if not got:                       # 数据源配额已尽: 立即停, 剩下的留给下一轮续传
            log.warning("回填批全空 -> 判定配额/封禁, 提前结束本轮 (已 %d)", n_ok)
            aborted = True
            break
    # 基准指数 (被配额掐断时跳过, 留给下一轮)
    if not aborted:
        idx = (m.fetch_index_bars or (lambda s: []))(start)
        if idx:
            conn.executemany("INSERT OR REPLACE INTO idx_bars(d,o,h,l,c,v) VALUES(?,?,?,?,?,?)", idx)
            conn.commit()
            log.info("基准指数: %d 根", len(idx))
    conn.close()
    return {"target": len(codes), "fetched": n_ok, "skipped": len(have), "aborted": aborted}


def _next_day(d: str) -> str:
    return (dt.date.fromisoformat(str(d)[:10]) + dt.timedelta(days=1)).isoformat()


# ------------------------------------------------------------------ 就绪判定 (给等待循环用)

READY_YES, READY_NO, READY_UNKNOWN = 0, 1, 2      # 也是 `-m ...pricestore ready` 的退出码


def _iso_day(v) -> str:
    """'20260908' / '2026-09-08' / date -> '2026-09-08'。

    看着多余, 其实是本节唯一一个必须有的函数: 库里的日期是 `YYYY-MM-DD`, 而人和 systemd
    传进来的是 Tushare 那种 `YYYYMMDD`, 两者**字符串直接比大小是错的** —— '-' (0x2D) 小于
    任何数字, 所以 '2026-09-08' < '20260908' 恒成立, 不归一化会让 `have >= target` 永远为假,
    再顺手把 (次日, 目标) 这个**逆序**区间喂给 trade_cal, 得到 0 个开市日, 最后判成
    "周末/长假, 无需等待" —— 一个"已就绪"的假答案。首版就是这么错的 (实测
    `ready 20260907` 在库停在 09-07 时走的是这条假路径), 留此为记。
    """
    s = str(v or "").strip()[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _ready_verdict(have: str | None, target: str, days) -> dict:
    """纯判定 (无 IO, 便于单测): 库末日 `have` 相对目标日 `target` 算不算追平了。

    `days` = 交易日历给出的 (have, target] 之间的**开市日**列表; None = 日历不可用。
    -> {"ready": bool, "code": READY_*, "missing": [...], "reason": "..."}

    三态而不是两态: "还没到" 与 "判不了" 必须分开 —— 但**两者都不许放行**。首版这里写的
    理由是 "判不了就按老规矩往下走, 免得日历一抖动就空等两小时", 那个理由是错的:
    `have >= target` 的短路发生在问日历**之前**, 所以能走到"判不了"的前提就是
    **库里没有目标日**; 日历不可信时唯一还站得住的事实恰恰是"这份库是旧的"。放行 =
    拿昨日行情当今天发布, 正是本闸门要挡的那件事。两个码仍要分开, 是因为值班看到 2 该去
    查数据源/日历, 看到 1 只是等源入库 (等待循环怎么用见 server/run_a.sh.v2)。

    **`days` 必须是可信的日历答案**: 空列表在这里被当成"区间内没有开市日 (周末/长假)"
    -> 已就绪。调用方有义务先分清"真的没有开市日"与"日历坏了返了个空列表" —— 生产的
    `ashare.market.trading_days` 在 2026-09-08 返工之前正是把 trade_cal 的异常吞成 `[]`,
    于是 **镜像挂掉 == 周末**, 闸门给出假的"已就绪" (实测: 库停在 09-07、问 09-08、
    trade_cal 抛 500 -> code=0)。守法的调用方见下面的 `_calendar_days`。
    """
    if not have:
        return {"ready": False, "code": READY_UNKNOWN, "missing": [],
                "reason": "库里一根 bar 都没有 (bars_raw 空) —— 该整库重建, 不是等一等的事"}
    have = _iso_day(have)
    target = _iso_day(target)
    if have >= target:
        return {"ready": True, "code": READY_YES, "missing": [],
                "reason": f"库内已到 {have} (>= 目标 {target})"}
    if days is None:
        return {"ready": False, "code": READY_UNKNOWN, "missing": [],
                "reason": f"库内到 {have}, 但交易日历不可用 (非 Tushare 按日路径/取历失败), 无法判定"}
    missing = sorted(d for d in (_iso_day(x) for x in days) if have < d <= target)
    if not missing:
        return {"ready": True, "code": READY_YES, "missing": [],
                "reason": f"库内到 {have}, {have} 与 {target} 之间没有开市日 (周末/长假), 无需等待"}
    return {"ready": False, "code": READY_NO, "missing": missing,
            "reason": f"库内到 {have}, 还缺 {len(missing)} 个交易日 "
                      f"({missing[0]}{'..' + missing[-1] if len(missing) > 1 else ''})"}


def _calendar_days(fn, start: str, target: str, have: str):
    """问日历要 (start, target] 的开市日 -> (days, why)。`days is None` = **判不了**, why 说原因。

    存在的理由只有一条: **把"区间内真的没有开市日"和"日历这会儿坏了"分开**。
    `_ready_verdict` 把空列表读成"周末/长假 -> 已就绪", 所以任何一个把取历失败吞成 `[]` 的
    Market 实现都能让闸门放行昨日库 —— 2026-09-08 的返工就是修这个 (生产实现
    `ashare.market.trading_days` 当时 `except -> return []`)。

    两道防线, 都要:
      ① 源头: `ashare.market.trading_days` 改成失败**抛出**, 由这里的 try 接住 -> 判不了;
      ② 这里: 拿到空列表时**反问一句必然非空的问题** —— "库末日 `have` 那天开不开市"。
         `have` 是库里真有行情的那天, 按定义必是开市日, 日历若连它都不给, 说明这份答案
         不可信, 空列表就不能被当成周末。代价是**只有空列表那一路**多 1 次调用 (周末/长假
         每轮多一次, 工作日 0 次), 换掉的是"数据源挂掉当天照发昨日行情"。
    """
    try:
        days = fn(start, target)
    except Exception as e:                        # noqa: BLE001
        return None, f"交易日历取失败 ({type(e).__name__}: {str(e)[:80]})"
    if days is None:
        return None, "交易日历不可用 (非 Tushare 按日路径 / 源开关关着)"
    if days:
        return days, ""
    try:
        probe = fn(have, have)
    except Exception as e:                        # noqa: BLE001
        return None, f"交易日历自检抛错 ({type(e).__name__}: {str(e)[:80]})"
    if not probe or _iso_day(have) not in {_iso_day(x) for x in probe}:
        return None, (f"交易日历自检没过 (问它库末日 {have} 开不开市, 它连这天都不给), "
                      f"这会儿的日历不可信, 不能把它的空答案当成周末")
    return days, ""


def ready_for(target: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """价格库是否已经含有 `target` (默认今天) 这个交易日 -> `_ready_verdict` 的字典。

    **为什么要有这个函数** (老板 2026-09-08 决定⑤): A 股流水线要从 14:00 CEST (北京 20:00)
    往前挪到收盘后不久, 于是"源还没入库"从一个理论风险变成日常会撞上的事。原来的处置是
    `_update_daily_by_date` 的未就绪守卫就地停下 + run_a.sh 那行末尾的 `|| echo ... non-fatal`,
    结果是**流水线照跑昨日库、把昨日行情当今天发布**。run_a.sh v2 改成"调 update -> 问一句
    ready -> 没到就睡 10 分钟再来", 超时宁可 exit 1 告警也不出榜 —— 本函数就是那句"问一句"。

    **今天是哪天, 必须和 update 用同一把尺子**: 这里用 `dt.date.today()` (进程本地日期),
    与 `_update_daily_by_date` 里的 `today` 逐字一致。服务器是 Europe/Berlin, 北京 = CEST+6h,
    所以本地日期只在 **18:00 CEST 之后**才会与北京日历差一天; A 流水线跑在 08:00-18:00 CEST
    区间内, 两者同日。**若哪天把定时器挪到 18:00 CEST 之后, 这条注释就得重看**: 那时本地日期
    还是"今天"而北京已经是明天, 两边仍然一致 (都用本地日期), 但 `target` 会比北京日历晚一天,
    等待循环会以为已经就绪 —— 也就是说这个函数在晚场是**偏宽松**的, 不会空等, 只会少等。

    **看 daily 的末日就够了, 因为写入侧连坐** (2026-09-08 返工补): 本函数只读
    `MAX(bars_raw.d)`, 一眼看去是"只把关日线、不把关复权因子"; 真正的把关在
    `_update_daily_by_date` —— 那里当日 daily 与 **adj_factor 两个端点都够 90% 才写这一天**,
    所以 bars_raw 里有某天 => 那天的因子当时也是齐的。闸门因此不必再查 adj 表 (查了反而会
    被历史上的老洞永久卡住)。**要是哪天把写入侧那道 adj 守卫拆了, 这里就得自己查 adj**。

    只读: 不写库, 也不联网取行情 —— 唯一的外部调用是交易日历 (`Market.trading_days`,
    A 股实现是 Tushare `trade_cal`): 工作日 1 次; 只有日历返回空列表 (看着像周末/长假)
    那一路会多问 1 次做自检, 见 `_calendar_days`。
    """
    own = conn is None
    conn = conn or _conn()
    try:
        have = conn.execute("SELECT MAX(d) FROM bars_raw").fetchone()[0]
        if not have:                              # v1 老库 / 美股库没有 bars_raw
            have = conn.execute("SELECT MAX(d) FROM bars").fetchone()[0]
    finally:
        if own:
            conn.close()
    have = _iso_day(have) if have else have
    target = _iso_day(target or dt.date.today().isoformat())
    days, why = None, "Market 没有交易日历 (trading_days=None)"
    if have and _iso_day(have) < target:          # 已经追平就不必问日历 (省一次调用)
        fn = getattr(current(), "trading_days", None)
        if fn is not None:
            days, why = _calendar_days(fn, _next_day(have), target, have)
            if days is None:
                log.warning("ready_for: %s -> 判不了 (UNKNOWN), 等待循环按'还没到'处理", why)
    out = _ready_verdict(have, target, days)
    if have and days is None and out["code"] == READY_UNKNOWN and why:
        out["reason"] = f"库内到 {have}, {why} —— 判不了, 不放行"
    out["have"], out["target"] = have, target
    return out


def update_daily(codes: list | None = None, lookback_days: int = 150) -> int:
    """增量更新。A 股 (Tushare 按日路径) 与 美股/旧源 (逐股回看) 走两条腿。

    A 股按日路径: 从库内最后一个交易日的次日起, 逐 trade_date 拉 **全市场 daily +
    adj_factor** (每天 2 次调用, 不碰 daily_basic), 写 bars_raw/adj, 然后
      · 因子未变的代码 -> 只物化当日新增那几根 (除数 = adj_base);
      · 因子变了的代码 (除权除息) -> 整段重物化, 并更新 adj_base。
    这样"前复权历史平移"不再需要重抓, 也不会出现 v1 那种混库跳变。
    落后多天时**从最早的那天开始一块块补**, 一直补到追平 (见 `_update_daily_by_date`);
    落后超过一年就拒绝增量, 改走全量重建。
    """
    m = current()
    if m.name == "ashare" and m.fetch_bars_by_date is not None:
        n = _update_daily_by_date(m)
        if n is not None:
            return n
        log.info("价格库增量: Tushare 按日路径未启用, 回退逐股回看")
    return _update_daily_legacy(m, codes, lookback_days)


def _resume_pending_materialize(conn: sqlite3.Connection) -> int:
    """上一次按日增量崩在 materialize 之前/之中留下的面包屑, 开门先补上。-> 补了几只。

    见 `_update_daily_by_date` 的「崩溃续跑到底保证到哪」。materialize 幂等 (整段 DELETE
    再重写), 重复跑没有副作用; 面包屑删干净才算修完, 所以删和 materialize 之间再 commit 一次
    (中间又崩就是原样重来)。"""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (PENDING_MAT_KEY,)).fetchone()
    if row is None:
        return 0
    codes = sorted({c for c in str(row[0] or "").split(",") if c})
    if not codes:                            # 空值 = 上次已经物化完只是没删干净
        conn.execute("DELETE FROM meta WHERE key=?", (PENDING_MAT_KEY,))
        conn.commit()
        return 0
    log.warning("价格库增量: 上一次有 %d 只票没来得及整段重物化 (进程被中断?) "
                "—— 先把它们补回来再往下走 (%s%s)", len(codes), ",".join(codes[:5]),
                "…" if len(codes) > 5 else "")
    n = materialize(codes, conn)             # 内含 commit
    conn.execute("DELETE FROM meta WHERE key=?", (PENDING_MAT_KEY,))
    conn.commit()
    log.info("价格库增量: 续跑重物化 %d 只完成, 面包屑已清", n)
    return n


def _update_daily_by_date(m, max_days: int = 40, ready_ratio: float = 0.9,
                          max_total_days: int = 250) -> int | None:
    """按 trade_date 增量, **最早优先分块追平**。-> 写入的 bar 数;
    None = 该路径未启用 (调用方回退旧路径)。

    **为什么是最早优先** (2026-09-08 修): 原来这里写的是 `days[-max_days:]` —— 取待补
    交易日的**最后** 40 天。副本落后 65 天时那一跑会写最新 40 天, `MAX(d)` 一步越到末日,
    而中间那 25 天从此再也进不来: 下一次 `last = MAX(bars_raw.d)` 已经是末日, `days` 里
    根本不会再出现它们。一个静默的洞, 越补越像补好了。现在改成 `days[:max_days]` 一块块
    从最早的补, 循环到追平或撞守卫为止, 每块提交一次。

    **崩溃续跑到底保证到哪** (2026-09-08 复验补; 之前这里写的是一句更强的"中途崩了下次从
    洞口续", 对下面这类票不成立): 块内每天写完 bars_raw/adj 就 commit, 但**因子变了的票
    (除权除息 / 新股 / 缺因子) 只进 `changed` 集合, 要等本块末尾的 materialize 才落 bars**。
    进程若正好在这中间被 kill (OOM / 断电 / 手工 Ctrl-C), bars_raw 与 `meta.max_trade_date`
    已经推进到崩溃那天, 这些票的 `bars` 却缺了那几天; 而下一次的起点 `last = MAX(bars_raw.d)`
    已经越过去了, 靠日更本身永远补不回, 残存的 bars 还停在除权前的旧基准 (前复权口径静默变错)
    —— 与本函数修掉的那个洞是同一类失败, 只是发生在 bars_raw 与 bars 之间。
    所以这里落一个**面包屑**: 待重物化的代码表跟当天那一笔写在**同一个事务**里
    (`meta[_pending_materialize]`), materialize 成功才删。下一次按日增量一开门先看它, 有就先
    把这些票整段重物化再往下走 (materialize 幂等: 整段 DELETE 再重写, 重跑无副作用)。
    局限也说清楚: 修复靠的是"下次还会跑按日增量"。崩溃后若改跑别的路径 (逐股回看 / 只读分析
    / 直接读库出信号), 那几天的 bars 仍是缺的 —— 但面包屑还留在 meta 里, `server/
    pricestore_fingerprint.sql` 会把它照成多出来的一行, 别把它当"两份库不一致"。

    **未就绪守卫** (设计 §4 风险条, 2026-09-07 换库时下沉到这里): 某个交易日返回的行数
    < 在市股数 × ready_ratio 就判"源当天还没入库", **就地停下**并沿用昨日库 —— 不是跳过。
    必须是停下: `days` 是连续的, 跳过 D 却写了 D+1, `MAX(d)` 就越过了 D, 那一天永远补不回来。
    守卫放在核心而不是只放在 run_a.sh 里: update_daily 的调用方不止 run_a.sh (factor_export、
    r1shadow、研究脚本、手工 `python -m ashare.pricestore update` 都会调), 而 Tushare 日线
    15-17 点北京才入库 —— 任何一个赶在那之前跑的调用方都能把半天的残缺行情写死进库。
    **daily 与 adj_factor 两个端点各判各的, 都够了才写这一天** (2026-09-08 返工补): 两个
    端点不同源、到达时间不同步 (09-08 17:34 实测 daily 5551 行 / adj 5558 行), 而"提前跑"
    正是 daily 先到、因子后到的那个窗口。只把关 daily 的话, 因子空着的那天照样把 bars_raw
    写死, 下次增量的起点 `MAX(bars_raw.d)` 就越过去了 —— **那天缺的因子永远补不回来**:
    `_qfq_rows` 对缺因子是按日期前向填充, 于是当天除权除息的票拿除权前的因子算 qfq, 在前
    复权序列上留一个永久台阶, 事后整库 materialize 也修不好 (adj 表本身有洞), 只有重拉那天
    的 adj_factor 才行。顺带也省掉"因子全空 -> 全员进 changed -> 每轮整库重物化"的空烧。

    **交易日历取不到 = 停下, 不是"无新交易日"** (2026-09-08 返工补): `m.trading_days` 抛错
    时就地 return 0, 既不写也**不推 `meta.max_trade_date`**。以前这里没有 try, 而生产的
    `ashare.market.trading_days` 把异常吞成 `[]`, 于是"日历挂了"长得和"今天休市"一模一样:
    日志打一句"库内已到 X, 无新交易日"、meta 顺手写上昨天, update 与 ready 两步一起静默放行。

    **显式上限 max_total_days**: 落后超过这么多交易日 (默认 250 ≈ 一年) 就**拒绝增量、
    一根不写**, 让人去 `research/rebuild_a_pricestore_tushare.py` 全量重建 —— 免得一次
    update 拉几年 (每交易日 2 次 Tushare 调用 + 逐块重物化), 也免得把"这份库其实早就该
    重建了"混在日更里悄悄糊过去。

    **围栏是 systemd, 不是看门狗** (2026-09-08 复验纠正, 之前这里写的"会顶穿 run_a.sh 的
    看门狗心跳"是错的): 档案 `server/run-scripts.txt` 里 run_a.sh 的实情是
    `python3 -m ashare.pricestore update` **单独占一行, 排在 `watchdog.py` 的上一行** ——
    它压根不在"25min 无心跳杀掉重试一次"的覆盖范围内。真正拦住它的是 stock-a.service 的
    `TimeoutStartSec=4h` 与 `MemoryMax=2800M`: 超时/超内存**杀掉整个 stock-a 单元**并触发
    `OnFailure=stock-notify@` 告警, 当天 A 股流水线全线不出数 —— 与"看门狗杀掉重试一次"
    是完全不同的失败形态和处置动作, 值班排障别看错地方。
    另外那一行末尾是 `|| echo "pricestore update warn (non-fatal, 沿用昨日库)"`, 所以
    **本函数的退出码/异常都到不了 systemd** (`set -e` 也拦不住 `||`): 判"追平了没有"要看
    库的 `MAX(d)`/`meta.max_trade_date` 或 /data/ 总览, 不能看单元状态。
    """
    conn = _conn()
    try:
        last = conn.execute("SELECT MAX(d) FROM bars_raw").fetchone()[0]
        if not last:
            log.warning("bars_raw 为空 -> 按日增量无起点 (先跑 "
                        "research/rebuild_a_pricestore_tushare.py), 回退旧路径")
            return None
        _resume_pending_materialize(conn)     # 上次崩在 materialize 中间的话, 先把 bars 补齐
        today = dt.date.today().isoformat()
        try:
            days = (m.trading_days or (lambda a, b: None))(_next_day(last), today)
        except Exception as e:               # noqa: BLE001  (取历失败 != 今天休市, 见 docstring)
            log.error("价格库增量: 交易日历取失败 (%s) —— 本次一根未写, 也不推 "
                      "meta.max_trade_date (推了就等于把'昨天'伪装成'已追平')。库停在 %s。",
                      str(e)[:120], last)
            return 0
        if days is None:
            return None                      # 交易日历不可用 = 路径未启用
        pending = sorted(d for d in days if d > last)
        n_total = len(pending)
        if not pending:
            log.info("价格库增量: 库内已到 %s, 无新交易日", last)
            _refresh_universe(m, conn)
            meta_set({"max_trade_date": last}, conn)
            return 0
        if n_total > max_total_days:
            log.error("价格库增量: 落后 %d 个交易日 (%s..%s) 超过上限 %d —— 拒绝增量, 一根未写。"
                      "落后这么多说明这份库该整库重建: research/rebuild_a_pricestore_tushare.py"
                      " (增量每交易日 2 次 Tushare 调用 + 逐块重物化, 拉一年会撞 stock-a 的"
                      " TimeoutStartSec=4h / MemoryMax=2800M, 整个单元被杀 + OnFailure 告警)。"
                      "库停在 %s。", n_total, pending[0], pending[-1], max_total_days, last)
            return 0
        log.info("价格库增量: 落后 %d 个交易日 (%s..%s), 按 %d 日一块最早优先追平",
                 n_total, pending[0], pending[-1], max_days)
        n_bars, touched = 0, set()
        n_done, stopped = 0, False
        listed_n = conn.execute("SELECT COUNT(*) FROM universe WHERE status='L'").fetchone()[0] or 0
        min_rows = listed_n * ready_ratio    # 不取整: 2 只在市时 90% 是 1.8, 回来 1 只就该判残缺
        while pending and not stopped:
            chunk, pending = pending[:max_days], pending[max_days:]
            changed, done = set(), []
            # 每块重读: 上一块的 materialize 可能刚把某些票的 adj_base 换成新基准
            base_map = {r[0]: (r[1], r[2]) for r in
                        conn.execute("SELECT code, d, factor FROM adj_base")}
            for d in chunk:
                raw = m.fetch_bars_by_date(d)
                if raw is None:              # 路径未启用 (源开关/无 token)
                    if not n_done and not done:
                        return None          # 第一天就 None = 从没启用过, 让调用方回退
                    # 中途才变 None: 路径本来是通的 (前面已经写进去了), 按"停下"处理,
                    # 不能返回 None —— 那会让调用方回退逐股路径, 且跳过下面的 meta 收尾
                    log.warning("价格库增量: 数据源中途不再返回数据 (%s), 停在 %s, 下次再补",
                                d, done[-1] if done else "本块之前")
                    stopped = True
                    break
                if not raw or len(raw) < min_rows:
                    log.warning("Tushare 当日未就绪, 沿用昨日库 (%s 日线 %d 行 < 在市 %d 只的 %.0f%%) "
                                "—— 增量停在 %s, 下次再补", d, len(raw), listed_n,
                                ready_ratio * 100, done[-1] if done else last)
                    stopped = True
                    break
                fetch_adj = getattr(m, "fetch_adj_by_date", None)
                facs = fetch_adj(d) if fetch_adj is not None else None
                if fetch_adj is not None and (facs is None or len(facs) < min_rows):
                    # 日线到了不算到: 因子这天没写进去, 以后就永远补不回来 (见 docstring)
                    log.warning("Tushare 当日复权因子未就绪, 沿用昨日库 (%s adj_factor %s 行 "
                                "< 在市 %d 只的 %.0f%%) —— 日线本身已经够了, 但这天先不写: "
                                "写了 MAX(bars_raw.d) 就越过去, 缺的因子再也回不来。停在 %s。",
                                d, "取不到" if facs is None else len(facs), listed_n,
                                ready_ratio * 100, done[-1] if done else last)
                    stopped = True
                    break
                facs = facs or {}
                conn.executemany("INSERT OR REPLACE INTO bars_raw(code,d,o,h,l,c,v,amt) "
                                 "VALUES(?,?,?,?,?,?,?,?)",
                                 [(c, d, *vals) for c, vals in raw.items()])
                if facs:
                    conn.executemany("INSERT OR REPLACE INTO adj(code,d,factor) VALUES(?,?,?)",
                                     [(c, d, float(f)) for c, f in facs.items()])
                fresh = []
                for code, vals in raw.items():
                    touched.add(code)
                    bd, bf = base_map.get(code, (None, None))
                    f = facs.get(code)
                    if bf is None or f is None:
                        changed.add(code)    # 新股/缺因子 -> 整段重物化最稳
                        continue
                    f = float(f)
                    if abs(f - bf) > 1e-9 * max(1.0, abs(bf)):
                        changed.add(code)    # 除权除息: 基准变了, 整段平移
                        continue
                    k = f / bf
                    fresh.append((code, d, vals[0] * k, vals[1] * k, vals[2] * k,
                                  vals[3] * k, vals[4], vals[5]))
                if fresh:
                    conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v,amt) "
                                     "VALUES(?,?,?,?,?,?,?,?)", fresh)
                if changed:
                    # 面包屑与当天这一笔同一个事务: 崩在本块末尾的 materialize 里也补得回来
                    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                                 (PENDING_MAT_KEY, ",".join(sorted(changed))))
                n_bars += len(raw)
                done.append(d)
                conn.commit()
                log.info("价格库增量 %s: %d 根 (因子 %d), 待重物化 %d",
                         d, len(raw), len(facs), len(changed))
            if changed:
                materialize(sorted(changed), conn)   # 内含 commit
                conn.execute("DELETE FROM meta WHERE key=?", (PENDING_MAT_KEY,))
                log.info("价格库增量: 因子变动 %d 只已整段重物化", len(changed))
            conn.commit()
            n_done += len(done)
            left = len(pending) + len(chunk) - len(done)
            if done:
                log.info("价格库增量: 本块补 %s..%s 共 %d 日, 剩 %d 日",
                         done[0], done[-1], len(done), left)
            else:
                log.info("价格库增量: 本块一日未补 (停在块首 %s), 剩 %d 日", chunk[0], left)
        _refresh_universe(m, conn)
        newest = conn.execute("SELECT MAX(d) FROM bars_raw").fetchone()[0]
        base_d = conn.execute("SELECT MAX(d) FROM adj_base").fetchone()[0]
        meta_set({"max_trade_date": newest or last, "rebase_date": base_d or "",
                  "unit_v": "股", "unit_amt": "元"}, conn)
        log.info("价格库增量: %d/%d 个交易日入库, %d 根, 触及 %d 只 (末日 %s)%s",
                 n_done, n_total, n_bars, len(touched), newest,
                 "" if n_done == n_total else ", 剩 %d 日下次再补" % (n_total - n_done))
        return n_bars
    finally:
        conn.close()


def _refresh_universe(m, conn: sqlite3.Connection) -> int:
    """刷新 universe (含退市股)。取不到就保持原样 —— 股票池不该被一次网络抖动清空。"""
    fn = getattr(m, "fetch_universe_rows", None)
    if fn is None:
        return 0
    try:
        rows = fn()
    except Exception as e:                   # noqa: BLE001
        log.warning("universe 刷新失败 (保持原样): %s", str(e)[:120])
        return 0
    rows = normalize_universe_rows(rows)
    if not rows:
        return 0
    conn.executemany("INSERT OR REPLACE INTO universe(code,name,list_date,delist_date,status) "
                     "VALUES(?,?,?,?,?)", rows)
    conn.commit()
    _PIT_CACHE.clear()
    n_null = sum(1 for r in rows if not r[2])
    log.info("universe 刷新: %d 只 (其中 %d 只源未给上市日 -> NULL)", len(rows), n_null)
    return len(rows)


def _update_daily_legacy(m, codes: list | None, lookback_days: int) -> int:
    """增量: 对库里已有代码抓最近 lookback_days 补上新bar (前复权价可能因除权
    整体平移 —— 增量只适合日常; 检测到大偏差的代码应重新全量, 这里先记日志)。
    lookback 必须 >= ~90自然日: fetch_bars_bulk 有 len<60 根即弃的残缺序列检查
    (为长历史重建而设), 10天回看会被整批吞掉 — 2026-09-01 r1shadow 增量静默零写入
    事故的根因, 守卫直到指数先更新才暴露。150天还顺带刷新近期复权漂移。

    ⚠ **v2 库上禁止走这条路** (2026-09-07 换库): 这里 _upsert 的是数据源直给的前复权价,
    复权基准是"抓取那天", 与 v2 的 adj_base 无关 —— 一旦写进 bars 就会把物化 qfq 口径改花、
    amt 抹成 NULL、bars 与 bars_raw/adj 脱钩。v2 的日更只有一条腿: `_update_daily_by_date`。
    调用方 (run_a.sh / factor_export / 研究脚本) 若在 CONFIG.source.bars 还没切 tushare 时
    调 update_daily, 宁可什么都不写并告警, 也不能悄悄把库改花。"""
    conn = _conn()
    try:
        if conn.execute("SELECT 1 FROM bars_raw LIMIT 1").fetchone() is not None:
            log.error("价格库是 schema v2 (原始价+因子), 但按日增量路径未启用 "
                      "(CONFIG.source.bars 未切 tushare 或无 token) —— 拒绝用逐股回看写库, "
                      "本次不更新。库停在 %s。", meta_get("max_trade_date", "?"))
            conn.close()
            return 0
    except sqlite3.OperationalError:
        pass                                 # 老库没有 bars_raw 表 -> 正常走 v1 路径
    have = last_dates(conn)
    if codes is None:
        codes = sorted(have)
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    n = 0
    for i in range(0, len(codes), BATCH):
        chunk = [c for c in codes[i:i + BATCH] if c in have]
        got = m.fetch_bars_bulk(chunk, start)
        for code, rows in got.items():
            _upsert(conn, code, rows)
            n += 1
        conn.commit()
    idx = (m.fetch_index_bars or (lambda s: []))(start)
    if idx:
        conn.executemany("INSERT OR REPLACE INTO idx_bars(d,o,h,l,c,v) VALUES(?,?,?,?,?,?)", idx)
        conn.commit()
    conn.close()
    log.info("价格库增量: %d 只已更新", n)
    return n


# ------------------------------------------------------------------ 装载


def load(codes: list, adjust: str = "qfq", with_amt: bool = False) -> dict:
    """-> {code: {"dates": [...], "ohlcv": ndarray[N,5]}} (o,h,l,c,v) 升序。

    adjust: "qfq" (默认, 读物化表 = v1 行为, 全部现有消费者不变) / "hfq" (后复权, 历史
    可复现: raw × factor, 与拉取日无关) / "raw" (原始价, 算涨跌停价与真实成交额用)。
    with_amt=True 时每只额外带 "amt": ndarray[N] (成交额, 元; 老库为 NaN)。
    hfq/raw 需要 v2 的 bars_raw —— 老库 (美股 / A 股换库前) 没有, 会记一条 warning
    并退回 qfq, 免得研究脚本因为库版本静默拿到空结果。
    """
    adjust = (adjust or "qfq").lower()
    if adjust not in ADJUSTS:
        raise ValueError(f"adjust 只能是 {ADJUSTS}, 收到 {adjust!r}")
    conn = _conn()
    try:
        if adjust != "qfq":
            has_raw = conn.execute("SELECT 1 FROM bars_raw LIMIT 1").fetchone() is not None
            if not has_raw:
                log.warning("load(adjust=%s): 本库无 bars_raw (schema v1), 退回 qfq", adjust)
                adjust = "qfq"
        out = {}
        for code in codes:
            if adjust == "qfq":
                rows = conn.execute("SELECT d,o,h,l,c,v,amt FROM bars WHERE code=? ORDER BY d",
                                    (code,)).fetchall()
            else:
                raw = conn.execute("SELECT d,o,h,l,c,v,amt FROM bars_raw WHERE code=? ORDER BY d",
                                   (code,)).fetchall()
                if adjust == "raw":
                    rows = raw
                else:
                    rows = _hfq_rows(raw, _factor_series(conn, code))
            if len(rows) < MIN_BARS:
                continue
            ser = {"dates": [r[0] for r in rows],
                   "ohlcv": np.array([r[1:6] for r in rows], dtype=float)}
            if with_amt:
                ser["amt"] = np.array([np.nan if r[6] is None else r[6] for r in rows],
                                      dtype=float)
            out[code] = ser
        return out
    finally:
        conn.close()


def _hfq_rows(raw_rows: list, fac_rows: list) -> list:
    """后复权: raw × factor (因子前向填充)。与拉取日无关, 历史逐位可复现。"""
    if not raw_rows or not fac_rows:
        return raw_rows
    fd = [f[0] for f in fac_rows]
    fv = [float(f[1]) for f in fac_rows]
    out, j, cur = [], 0, fv[0]
    for r in raw_rows:
        while j < len(fd) and fd[j] <= r[0]:
            cur = fv[j]
            j += 1
        out.append((r[0], r[1] * cur, r[2] * cur, r[3] * cur, r[4] * cur, r[5], r[6]))
    return out


def load_index(sym: str | None = None) -> dict | None:
    """基准指数 (沪深300 / SPY) 长历史; sym 给定时从 idx_multi 读研究用的其他指数。"""
    conn = _conn()
    try:
        if sym:
            rows = conn.execute("SELECT d,o,h,l,c,v FROM idx_multi WHERE sym=? ORDER BY d",
                                (sym,)).fetchall()
        else:
            rows = conn.execute("SELECT d,o,h,l,c,v FROM idx_bars ORDER BY d").fetchall()
    finally:
        conn.close()
    if len(rows) < MIN_BARS:
        return None
    return {"dates": [r[0] for r in rows],
            "ohlcv": np.array([r[1:] for r in rows], dtype=float)}


def index_symbols() -> list:
    conn = _conn()
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT sym FROM idx_multi ORDER BY sym")]
    finally:
        conn.close()


def coverage() -> dict:
    conn = _conn()
    try:
        n_codes, n_bars = conn.execute("SELECT COUNT(DISTINCT code), COUNT(*) FROM bars").fetchone()
        dmin, dmax = conn.execute("SELECT MIN(d), MAX(d) FROM bars").fetchone()
        n_idx = conn.execute("SELECT COUNT(*) FROM idx_bars").fetchone()[0]
        n_raw = conn.execute("SELECT COUNT(*) FROM bars_raw").fetchone()[0]
        n_uni = conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
        n_del = conn.execute("SELECT COUNT(*) FROM universe WHERE delist_date IS NOT NULL "
                             "AND delist_date<>''").fetchone()[0]
        mt = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()
    return {"codes": n_codes, "bars": n_bars, "from": dmin, "to": dmax, "index_bars": n_idx,
            "bars_raw": n_raw, "universe": n_uni, "delisted": n_del,
            "source": mt.get("source", ""), "schema": "v2" if n_raw else "v1"}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    if cmd == "ready":                      # 退出码 0=已到 / 1=还没到 / 2=判不了 (见 ready_for)
        v = ready_for(sys.argv[2] if len(sys.argv) > 2 else None)
        print(f"ready={v['ready']} {v['reason']}")
        raise SystemExit(v["code"])
    if cmd == "backfill":
        print(backfill())
    elif cmd == "update":
        print(update_daily())
    elif cmd == "materialize":
        print("物化", materialize(progress_every=1000), "只")
    print(coverage())
