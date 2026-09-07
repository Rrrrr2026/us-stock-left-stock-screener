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


def _update_daily_by_date(m, max_days: int = 40, ready_ratio: float = 0.9,
                          max_total_days: int = 250) -> int | None:
    """按 trade_date 增量, **最早优先分块追平**。-> 写入的 bar 数;
    None = 该路径未启用 (调用方回退旧路径)。

    **为什么是最早优先** (2026-09-08 修): 原来这里写的是 `days[-max_days:]` —— 取待补
    交易日的**最后** 40 天。副本落后 65 天时那一跑会写最新 40 天, `MAX(d)` 一步越到末日,
    而中间那 25 天从此再也进不来: 下一次 `last = MAX(bars_raw.d)` 已经是末日, `days` 里
    根本不会再出现它们。一个静默的洞, 越补越像补好了。现在改成 `days[:max_days]` 一块块
    从最早的补, 循环到追平或撞守卫为止, 每块提交 (中途崩了下次从洞口续)。

    **未就绪守卫** (设计 §4 风险条, 2026-09-07 换库时下沉到这里): 某个交易日返回的行数
    < 在市股数 × ready_ratio 就判"源当天还没入库", **就地停下**并沿用昨日库 —— 不是跳过。
    必须是停下: `days` 是连续的, 跳过 D 却写了 D+1, `MAX(d)` 就越过了 D, 那一天永远补不回来。
    守卫放在核心而不是只放在 run_a.sh 里: update_daily 的调用方不止 run_a.sh (factor_export、
    r1shadow、研究脚本、手工 `python -m ashare.pricestore update` 都会调), 而 Tushare 日线
    15-17 点北京才入库 —— 任何一个赶在那之前跑的调用方都能把半天的残缺行情写死进库。

    **显式上限 max_total_days**: 落后超过这么多交易日 (默认 250 ≈ 一年) 就**拒绝增量、
    一根不写**, 让人去 `research/rebuild_a_pricestore_tushare.py` 全量重建 —— 免得一次
    update 拉几年 (每交易日 2 次 Tushare 调用 + 逐块重物化, 会顶穿 run_a.sh 的看门狗心跳),
    也免得把"这份库其实早就该重建了"混在日更里悄悄糊过去。
    """
    conn = _conn()
    try:
        last = conn.execute("SELECT MAX(d) FROM bars_raw").fetchone()[0]
        if not last:
            log.warning("bars_raw 为空 -> 按日增量无起点 (先跑 "
                        "research/rebuild_a_pricestore_tushare.py), 回退旧路径")
            return None
        today = dt.date.today().isoformat()
        days = (m.trading_days or (lambda a, b: None))(_next_day(last), today)
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
                      " (增量每交易日 2 次 Tushare 调用 + 逐块重物化, 拉一年会顶穿看门狗)。"
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
                facs = (m.fetch_adj_by_date or (lambda x: {}))(d) or {}
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
                n_bars += len(raw)
                done.append(d)
                conn.commit()
                log.info("价格库增量 %s: %d 根 (因子 %d), 待重物化 %d",
                         d, len(raw), len(facs), len(changed))
            if changed:
                materialize(sorted(changed), conn)   # 内含 commit
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
    if cmd == "backfill":
        print(backfill())
    elif cmd == "update":
        print(update_daily())
    elif cmd == "materialize":
        print("物化", materialize(progress_every=1000), "只")
    print(coverage())
