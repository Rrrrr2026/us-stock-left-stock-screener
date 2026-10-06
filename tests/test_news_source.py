#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新闻多源兜底 + 整轮体检的离线用例 (2026-10-04 卡 NEWS-OVERDUE) —— 零联网: 四家新闻源全部打桩。

起因: 雅虎 xhr/ncp 新闻端点 2026-09-29 起 404, yfinance 吞错返回空列表, 这里原来写 `tk.news or []`:
全市场 0 条不告警, 看板 10-02 起「利好 0 / 利空 0」「近30天无相关新闻标题」, 读起来像没有利空。

锁的规矩:
  · 取数链: Ticker.news → Search(...).news → Yahoo RSS → Google News RSS, 第一家非空即用, 后面的不问;
    某一家连续 N 只票没取到 → 本轮熔断不再问它; 同一只票本轮只取一次 (空结果也记住); 兜底源不并发;
    任何一家抛错都不外泄 (新闻永远不许打断流水线); 每次调用有墙钟硬期限。
  · 解析: yfinance 新旧两种结构、RSS 两种形态 (Yahoo: 域名当出版方 + 去 .tsrc; Google: <source> + 去标题尾巴)、拒 DTD。
  · 档案新闻 fetch_news 与错杀红旗 market.news_titles 走同一条链。
  · 体检 news_health: 总数 0 或有标题的票 < 20% → False (meta.news_source_ok=false + WARNING); 没有票需要新闻 → None。
  · 非本公司新闻过滤 (2026-10-06 回修): 一条算本票新闻 = 标题提到公司名 (股票池叫法归一: 去 Inc / Corporation / Class A Common Stock /
    ADS …, 多词名的非泛词首词也认) 或代码 (≤2 字母只认 $X / (X) / :X), 或新闻源只把它挂在本票代码下 (relatedTickers 去掉指数/期货后只剩本票);
    挂着本代码的别家稿子 (yahoo_search 每条都挂查询代码, 10-06 服务器实测) 与综述不算; 一家返回的全是别家新闻 = 没取到 (计入熔断);
    过滤条数进 news_round_stats()["filtered"] → meta.news_stats.fetch; 当日缓存键 news4。
运行 (仓库根): python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests -q
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import datasource as ds      # noqa: E402
from screener import export_data as ex     # noqa: E402
from screener import market as mk          # noqa: E402

TODAY = dt.date.today()
D1 = TODAY - dt.timedelta(days=1)


def item(title: str, source: str = "yfinance", day: dt.date = D1, tickers: list | None = None) -> dict:
    """桩返回的一条。tickers 不给 = 由 Feeds.chain 补成 [本票代码] (新闻源说这条讲的就是这只票); 给 [] / 别的代码 = 测相关性过滤。"""
    h = {"title": title, "publisher": "Wire", "time": f"{day.isoformat()} 10:00", "date": day.isoformat(),
         "url": "https://example.com/" + title.lower().replace(" ", "-"), "source": source}
    if tickers is not None:
        h["tickers"] = list(tickers)
    return h


class Feeds:
    """四家新闻源的桩: 每家一个 {代码: 条目列表 | 异常} 表, 没登记的代码返回空表。calls 记下问过谁。"""

    def __init__(self, **tables):
        self.tables = {p: tables.get(p, {}) for p in ds._NEWS_PROVIDERS}
        self.calls: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.delay = 0.0
        self._lock = threading.Lock()

    def chain(self, sym: str) -> list:
        def mk_fn(name):
            def fn():
                with self._lock:
                    self.calls.append((name, sym))
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    if self.delay:
                        time.sleep(self.delay)
                    v = self.tables[name].get(sym, [])
                    if isinstance(v, Exception):
                        raise v
                    # 没写 tickers 的条目默认带本票代码 (= 新闻源认为这条讲的是这只票), 让老用例不用关心相关性过滤
                    return [dict(x, tickers=x.get("tickers", [sym])) if isinstance(x, dict) else x for x in v]
                finally:
                    with self._lock:
                        self.active -= 1
            return fn
        return [(n, mk_fn(n), n != "yfinance") for n in ds._NEWS_PROVIDERS]

    def asked(self, name: str) -> list[str]:
        return [s for n, s in self.calls if n == name]


@pytest.fixture()
def feeds(monkeypatch, tmp_path):
    """干净的一轮: 清记忆与熔断计数, 文件缓存指到临时目录, 兜底源间隔归零。"""
    f = Feeds()
    ds.reset_news_round()
    monkeypatch.setattr(ds, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(ds, "_NEWS_GAP_S", 0.0)
    monkeypatch.setattr(ds, "_news_chain", f.chain)
    monkeypatch.setitem(ds.CONFIG["source"], "use_cache", True)
    yield f
    ds.reset_news_round()


# =========================================================================== 取数链
def test_primary_empty_falls_back_in_order_and_first_non_empty_wins(feeds):
    feeds.tables["yahoo_search"]["AAA"] = [item("Search story", "yahoo_search")]
    feeds.tables["google_rss"]["AAA"] = [item("unused", "google_rss")]
    feeds.tables["yahoo_rss"]["BBB"] = RuntimeError("HTTP 429")
    feeds.tables["google_rss"]["BBB"] = [item("Google story", "google_rss")]
    assert [x["title"] for x in ds.news_items("AAA")] == ["Search story"]
    assert [n for n, s in feeds.calls if s == "AAA"] == ["yfinance", "yahoo_search"]          # 取到就不再往后问
    assert [x["source"] for x in ds.news_items("BBB")] == ["google_rss"]
    assert [n for n, s in feeds.calls if s == "BBB"] == ["yfinance", "yahoo_search", "yahoo_rss", "google_rss"]
    assert ds.news_items("CCC") == []                                                       # 四家都空 → 空表, 不抛
    st = ds.news_round_stats()
    assert (st["fetched"], st["with_news"], st["items"]) == (3, 2, 2)
    assert st["by_source"] == {"yahoo_search": 1, "google_rss": 1} and st["tripped"] == []


def test_each_code_is_fetched_once_per_round_even_when_empty(feeds):
    feeds.tables["yfinance"]["AAA"] = [item("Primary story")]
    for _ in range(3):                                   # 导出阶段 build_payload 要跑三遍
        assert len(ds.news_items("AAA")) == 1 and ds.news_items("ZZZ") == []
    assert feeds.asked("yfinance") == ["AAA", "ZZZ"]
    assert feeds.asked("google_rss") == ["ZZZ"]
    assert ds.news_items("brk.b") == [] and feeds.asked("yfinance")[-1] == "BRK-B"          # 代码按 yfinance 写法归一


def test_dead_provider_is_circuit_broken_after_n_consecutive_empties(feeds, caplog):
    n = ds._NEWS_BREAK_AFTER
    codes = [f"T{i:02d}" for i in range(n + 5)]
    for c in codes:
        feeds.tables["yahoo_search"][c] = [item(f"Story for {c}", "yahoo_search")]
    with caplog.at_level(logging.WARNING, logger="screener.datasource"):
        for c in codes:
            assert len(ds.news_items(c)) == 1
    assert feeds.asked("yfinance") == codes[:n]                                             # 第 n+1 只起不再问坏掉的那一家
    assert feeds.asked("yahoo_search") == codes
    assert ds.news_round_stats()["tripped"] == ["yfinance"]
    assert sum("新闻源 yfinance 连续" in r.getMessage() for r in caplog.records) == 1       # 只吵一次
    ds.reset_news_round()                                                                   # 中途成功一次就清零, 不误熔断
    feeds.calls.clear()
    feeds.tables["yfinance"]["OK1"] = [item("Primary works")]
    fresh = [f"U{i:02d}" for i in range(2 * n - 2)]      # 换一批代码 (上一批的结果已进当日文件缓存, 不会再走取数链)
    for c in fresh:
        feeds.tables["yahoo_search"][c] = [item(f"Story for {c}", "yahoo_search")]
    for c in fresh[:n - 1] + ["OK1"] + fresh[n - 1:]:
        ds.news_items(c)
    assert ds.news_round_stats()["tripped"] == [] and len(feeds.asked("yfinance")) == 2 * n - 1


def test_fallback_providers_are_serialized_but_primary_is_not(feeds):
    feeds.delay = 0.05
    codes = [f"S{i}" for i in range(6)]
    for c in codes:
        feeds.tables["yahoo_search"][c] = [item(f"Story {c}", "yahoo_search")]
    # 第一家 (空) 可以并发; 第二家起走串行闸: 任一时刻最多 1 个兜底调用在途
    seen = {"fallback_max": 0, "cur": 0}
    lock = threading.Lock()
    orig = feeds.chain

    def chain(sym):
        out = []
        for name, fn, serial in orig(sym):
            def wrap(fn=fn, serial=serial):
                if serial:
                    with lock:
                        seen["cur"] += 1
                        seen["fallback_max"] = max(seen["fallback_max"], seen["cur"])
                try:
                    return fn()
                finally:
                    if serial:
                        with lock:
                            seen["cur"] -= 1
            out.append((name, wrap, serial))
        return out
    ds._news_chain = chain                               # fixture 已 monkeypatch 过这个名字, 用例结束自动还原
    ths = [threading.Thread(target=ds.news_items, args=(c,)) for c in codes]
    [t.start() for t in ths]
    [t.join(20) for t in ths]
    assert seen["fallback_max"] == 1, seen
    assert feeds.max_active >= 2, "第一家应当允许并发 (与改动前一致)"
    assert sorted(feeds.asked("yahoo_search")) == codes


def test_provider_exceptions_and_hangs_never_escape(feeds, monkeypatch):
    feeds.tables["yfinance"]["AAA"] = RuntimeError("boom")
    feeds.tables["yahoo_search"]["AAA"] = ValueError("bad json")
    feeds.tables["yahoo_rss"]["AAA"] = TimeoutError("slow")
    feeds.tables["google_rss"]["AAA"] = OSError("dns")
    assert ds.news_items("AAA") == [] and ds.fetch_news("AAA") == [] and mk.news_titles("AAA") == []
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="硬期限"):
        ds._hard_deadline(lambda: time.sleep(5), 0.2)
    assert time.monotonic() - t0 < 2
    assert ds._hard_deadline(lambda: 7, 1) == 7
    with pytest.raises(KeyError):
        ds._hard_deadline(lambda: {}["x"], 1)
    # 挂死的那一家按硬期限放弃, 后面的照常问
    ds.reset_news_round()
    monkeypatch.setattr(ds, "_NEWS_DEADLINE_S", 0.2)
    feeds.tables["yfinance"] = {}
    slow = Feeds(google_rss={"HNG": [item("After the hang", "google_rss")]})

    def chain(sym):
        c = slow.chain(sym)
        return [c[0], ("yahoo_search", lambda: time.sleep(5), True), c[2], c[3]]
    monkeypatch.setattr(ds, "_news_chain", chain)
    t0 = time.monotonic()
    assert [x["title"] for x in ds.news_items("HNG")] == ["After the hang"]
    assert time.monotonic() - t0 < 3


def test_non_empty_result_is_cached_for_the_day_empty_is_not(feeds):
    feeds.tables["yahoo_search"]["AAA"] = [item("Cached story", "yahoo_search")]
    assert len(ds.news_items("AAA")) == 1 and ds.news_items("EMP") == []
    ds.reset_news_round()                                # 新进程 (同一天重跑)
    feeds.calls.clear()
    feeds.tables["yahoo_search"]["AAA"] = []
    assert [x["title"] for x in ds.news_items("AAA")] == ["Cached story"] and feeds.asked("yfinance") == []
    feeds.tables["google_rss"]["EMP"] = [item("Now it has news", "google_rss")]
    assert len(ds.news_items("EMP")) == 1               # 空结果没进文件缓存, 重跑会再问


# =========================================================================== 消费方: 档案新闻 / 错杀红旗
def test_fetch_news_and_news_titles_share_the_chain_and_keep_their_shapes(feeds):
    feeds.tables["yahoo_search"]["AAA"] = [
        item("Regulator opens probe into pricing", "yahoo_search"), item("Shares jump on record quarter", "yahoo_search"),
        dict(item("Undated item", "yahoo_search"), time="—", date="", url="#")]
    news = ds.fetch_news("AAA", limit=2)
    assert news == [
        {"title": "Regulator opens probe into pricing", "publisher": "Wire", "time": f"{D1.isoformat()} 10:00",
         "url": "https://example.com/regulator-opens-probe-into-pricing", "tone": "利空", "source": "yahoo_search"},
        {"title": "Shares jump on record quarter", "publisher": "Wire", "time": f"{D1.isoformat()} 10:00",
         "url": "https://example.com/shares-jump-on-record-quarter", "tone": "利好", "source": "yahoo_search"}]
    titles = mk.news_titles("AAA")
    assert titles == [(D1.isoformat(), "Regulator opens probe into pricing", "https://example.com/regulator-opens-probe-into-pricing"),
                      (D1.isoformat(), "Shares jump on record quarter", "https://example.com/shares-jump-on-record-quarter")]
    assert feeds.asked("yahoo_search") == ["AAA"]        # 两个消费方共用一次取数
    # 错杀红旗接上去: 命中风险关键词的标题带 🚩 标签
    from screener import newsflag
    cands = [{"code": "AAA", "cuosha_score": 80}, {"code": "QQQ", "cuosha_score": 70}, {"code": "NOP"}]
    assert newsflag.annotate(cands, as_of=TODAY.isoformat()) == 1
    assert cands[0]["news_flags"] == ["investigation"] and [n["f"] for n in cands[0]["news"]] == [["investigation"], []]
    assert "news" not in cands[1] and "news" not in cands[2]


# =========================================================================== 解析
def _rss(items: list[str]) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<rss version="2.0"><channel><title>t</title>'
            + "".join(f"<item>{x}</item>" for x in items) + "</channel></rss>").encode("utf-8")


def _rfc822(day: dt.date, hh: int, tz: str) -> str:
    return f"{day.strftime('%a, %d %b %Y')} {hh:02d}:30:00 {tz}"


def test_parse_rss_yahoo_and_google_shapes_and_rejects_dtd():
    yahoo = _rss([
        f"<link>https://finance.yahoo.com/markets/stocks/articles/a-023534508.html?.tsrc=rss</link>"
        f"<pubDate>{_rfc822(D1, 2, '+0000')}</pubDate><title>Backlog visibility</title>",
        f"<link>https://www.fool.com/investing/split.aspx?a=1&amp;.tsrc=rss</link>"
        f"<pubDate>{_rfc822(D1, 23, '-0400')}</pubDate><title> Stock split coming? </title>",
        "<link>https://x/no-date</link><title>skipped: no pubDate</title>",
        f"<link>javascript:alert(1)</link><pubDate>{_rfc822(D1, 2, 'GMT')}</pubDate><title>skipped: bad scheme</title>",
        f"<link>https://finance.yahoo.com/markets/stocks/articles/a-023534508.html</link>"
        f"<pubDate>{_rfc822(D1, 3, 'GMT')}</pubDate><title>skipped: duplicate link</title>"])
    got = ds.parse_rss(yahoo, "yahoo_rss")
    assert got == [
        {"title": "Stock split coming?", "publisher": "fool.com", "time": f"{TODAY.isoformat()} 03:30", "date": TODAY.isoformat(),
         "url": "https://www.fool.com/investing/split.aspx?a=1", "source": "yahoo_rss"},
        {"title": "Backlog visibility", "publisher": "finance.yahoo.com", "time": f"{D1.isoformat()} 02:30", "date": D1.isoformat(),
         "url": "https://finance.yahoo.com/markets/stocks/articles/a-023534508.html", "source": "yahoo_rss"}]
    google = _rss([
        f"<title>MSFT lands on a tactical list - Yahoo Finance</title><link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>"
        f"<pubDate>{_rfc822(D1, 17, 'GMT')}</pubDate><source url=\"https://finance.yahoo.com\">Yahoo Finance</source>"])
    assert ds.parse_rss(google.decode("utf-8"), "google_rss") == [
        {"title": "MSFT lands on a tactical list", "publisher": "Yahoo Finance", "time": f"{D1.isoformat()} 17:30",
         "date": D1.isoformat(), "url": "https://news.google.com/rss/articles/CBMiAAA?oc=5", "source": "google_rss"}]
    with pytest.raises(ValueError, match="DTD"):
        ds.parse_rss('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>', "yahoo_rss")
    with pytest.raises(ValueError, match="上限"):
        ds.parse_rss(b" " * (ds._NEWS_RSS_MAX_BYTES + 1), "yahoo_rss")
    with pytest.raises(Exception):
        ds.parse_rss("<rss><channel><item>", "yahoo_rss")
    many = _rss([f"<title>Item {i}</title><link>https://x/{i}</link><pubDate>{_rfc822(D1, 2, 'GMT')}</pubDate>"
                 for i in range(ds._NEWS_FETCH_N + 15)])
    assert len(ds.parse_rss(many, "google_rss")) == ds._NEWS_FETCH_N


def test_parse_yf_news_nested_and_flat_search_shapes():
    ts = int(dt.datetime.combine(D1, dt.time(12, 0), dt.timezone.utc).timestamp())
    raw = [{"id": "1", "content": {"title": "Nested story", "pubDate": f"{D1.isoformat()}T11:41:02Z",
                                   "provider": {"displayName": "Reuters"}, "canonicalUrl": {"url": "https://x/a"}}},
           {"uuid": "u", "title": "Flat search story", "publisher": "Motley Fool", "link": "https://x/b",
            "providerPublishTime": ts, "relatedTickers": ["MSFT"]},
           {"content": {"title": "", "pubDate": f"{D1.isoformat()}T00:00:00Z"}}, "garbage", None,
           {"title": "No time no link"}]
    got = ds.parse_yf_news(raw, "yahoo_search")
    assert got == [
        {"title": "Nested story", "publisher": "Reuters", "time": f"{D1.isoformat()} 11:41", "date": D1.isoformat(),
         "url": "https://x/a", "source": "yahoo_search", "tickers": []},
        {"title": "Flat search story", "publisher": "Motley Fool", "time": f"{D1.isoformat()} 12:00", "date": D1.isoformat(),
         "url": "https://x/b", "source": "yahoo_search", "tickers": ["MSFT"]},                    # relatedTickers 原样带出 (相关性判据)
        {"title": "No time no link", "publisher": "—", "time": "—", "date": "", "url": "#", "source": "yahoo_search", "tickers": []}]
    assert ds.parse_yf_news(None, "yfinance") == [] and ds.parse_yf_news([], "yfinance") == []
    nested = ds.parse_yf_news([{"content": {"title": "Nested with finance block", "pubDate": f"{D1.isoformat()}T11:00:00Z",
                                            "canonicalUrl": {"url": "https://x/n"},
                                            "finance": {"stockTickers": [{"symbol": "brk-b"}, {"symbol": "AAPL"}]}},
                                "relatedTickers": ["NVDA"]}], "yfinance")
    assert nested[0]["tickers"] == ["AAPL", "BRK-B", "NVDA"]


# =========================================================================== 非本公司新闻过滤 (2026-10-06 回修)
def test_company_name_variants_and_title_matching():
    """股票池叫法 → 认标题的叫法; 10-05 看板上误标的三条红旗与精研页混进的别家标题在这里必须判为不相关。"""
    v = ds.company_name_variants
    assert v("Teradata Corporation Common Stock") == ["Teradata"]
    assert v("Oceaneering International Inc. Common Stock") == ["Oceaneering International", "Oceaneering"]
    assert v("Array Digital Infrastructure Inc. Common Shares") == ["Array Digital Infrastructure", "Array"]
    assert v("SAP  SE ADS") == ["SAP"] and v("UiPath Inc. Class A Common Stock") == ["UiPath"]
    assert v("American Airlines Group Inc.") == ["American Airlines"]              # 泛词首词不单独认
    assert v("Eli Lilly and Company") == ["Eli Lilly"] and v("The Walt Disney Company") == ["Walt Disney", "Walt"]
    assert v("Bank of America Corporation") == ["Bank of America"] and v("AT&T Inc.") == ["AT&T"]
    assert v("Berkshire Hathaway Inc. Class B") == ["Berkshire Hathaway", "Berkshire"]
    assert v("Lowe's Companies, Inc.") == ["Lowe's"] and v("") == [] and v(None) == []
    ds.reset_news_round()
    ds.register_news_names({"TDC": "Teradata Corporation Common Stock", "FRPT": "Freshpet, Inc.", "PGNY": "Progyny, Inc.",
                            "NVDA": "NVIDIA Corporation", "TGT": "Target Corporation", "OII": "Oceaneering International Inc. Common Stock",
                            "brk.b": "Berkshire Hathaway Inc. Class B", "GM": "General Motors Company"})
    j = ds._news_judge("TDC")
    assert j({"title": "Blackbaud CEO Mike Gianoni Invests in NGS, Joins as Strategic Advisor"}, "yfinance") is None   # 10-05 误标 TDC
    assert j({"title": "Teradata's Chief Operating Officer Dumps Over 48,000 Shares"}, "yfinance") == "title"
    assert j({"title": "3 Value Stocks We're Skeptical Of (NYSE:TDC)"}, "yahoo_rss") == "title" and j({"title": "Buy $TDC now"}, "yahoo_rss") == "title"
    assert j({"title": "ATDC merger talk"}, "yahoo_rss") is None and j({"title": "tdc lowercase"}, "yahoo_rss") is None
    assert j({"title": "Anything", "tickers": ["TDC"]}, "yahoo_search") == "ticker"                    # 只挂在 TDC 下
    assert j({"title": "Market wrap", "tickers": ["TDC", "^GSPC", "CL=F"]}, "yahoo_search") == "ticker"   # 指数 / 期货忽略
    assert j({"title": "Teradata's COO sells shares", "tickers": ["XYZ"]}, "yahoo_search") == "title"   # 标题提到就算, 不管挂在哪
    assert j({"title": "Teradata's COO sells shares"}, "yahoo_search") == "title"
    # 10-06 服务器实测的形态: 搜索接口把别家稿子也挂在 TDC 下 → 单看 relatedTickers 挡不住, 这里必须判不相关
    assert j({"title": "Blackbaud CEO Mike Gianoni Invests in NGS, Joins as Strategic Advisor", "tickers": ["BLKB", "TDC"]}, "yahoo_search") is None
    assert j({"title": "Sandisk Corporation (SNDK) Up 23.6% Since Last Earnings Report", "tickers": ["SNDK", "TDC", "^GSPC"]}, "yahoo_search") is None
    assert j({"title": "NTAP Q1 Earnings Beat Estimates on Hybrid, Public Cloud Revenue Growth", "tickers": ["NTAP", "SMCIP", "SNDK", "TDC"]}, "yahoo_search") is None
    assert j({"title": "Teradata (TDC) Stock Trades At a Discount After a 48% Slump", "tickers": ["TDC"]}, "yahoo_search") == "title"
    assert ds._news_judge("FRPT")({"title": "Exxon Mobil downgraded, BP upgraded: Wall Street's top analyst calls"}, "yfinance") is None
    assert ds._news_judge("PGNY")({"title": "3 Profitable Stocks with Warning Signs"}, "yfinance") is None
    assert ds._news_judge("NVDA")({"title": "Nvidia (NVDA) Is At The Center Of An $8 Billion AI Financing Shift"}, "google_rss") == "title"
    assert ds._news_judge("NVDA")({"title": "Why nvidia keeps winning"}, "google_rss") == "title"      # 全大写名字不分大小写
    assert ds._news_judge("TGT")({"title": "Analysts raise price target on Walmart"}, "google_rss") is None    # 普通词小写不算
    assert ds._news_judge("TGT")({"title": "Target Q3 comps fall short"}, "google_rss") == "title"
    assert ds._news_judge("OII")({"title": "Should Oceaneering's New Five-Year U.S. Navy Contract Change Your View?"}, "yfinance") == "title"
    assert ds._news_judge("BRK-B")({"title": "Berkshire trims Apple stake", "tickers": []}, "yfinance") == "title"
    assert ds._news_judge("BRK-B")({"title": "x", "tickers": ["BRK.B"]}, "yahoo_search") == "ticker"
    assert ds._news_judge("GM")({"title": "GM recalls trucks"}, "yfinance") is None                     # 2 字母代码只认 $GM / (GM) / :GM
    assert ds._news_judge("GM")({"title": "Auto stocks: (GM) leads"}, "yfinance") == "title"
    assert ds._news_judge("GM")({"title": "General Motors recalls trucks"}, "yfinance") == "title"
    assert ds._news_judge("ZZZZ")({"title": "ZZZZ files 8-K"}, "yfinance") == "title"                  # 没登记公司名: 只认代码
    assert ds._news_judge("ZZZZ")({"title": "Some company files 8-K"}, "yfinance") is None
    # Google News 按公司名搜 (没登记的按代码)
    assert ds._google_query("TDC") == '"Teradata" stock when:7d' and ds._google_query("ZZZZ") == "ZZZZ stock when:7d"
    ds.reset_news_round()


def test_unrelated_items_are_filtered_per_source_and_counted(feeds, caplog):
    """10-05 线上的形态: yahoo_search 返回一堆别家新闻混着几条本票的 → 只留本票的 (严格: 只认 relatedTickers);
    yfinance / RSS 认 relatedTickers 或标题; 一家返回的全是别家新闻 = 这一家没取到 (计熔断, 问下一家); 过滤条数进统计。"""
    ds.register_news_names({"TDC": "Teradata Corporation Common Stock"})
    feeds.tables["yahoo_search"]["TDC"] = [
        item("Blackbaud CEO Mike Gianoni Invests in NGS", "yahoo_search", tickers=["BLKB", "TDC"]),   # 挂着 TDC 的别家稿子 (10-06 实测形态)
        item("Teradata's COO sells shares", "yahoo_search", tickers=[]),                             # 标题提到 → 留
        item("UBS Adjusts Teradata Price Target to $32", "yahoo_search", tickers=["TDC"]),           # 留
        item("3 Value Stocks We're Skeptical Of", "yahoo_search", tickers=["BBWI", "BFAM", "TDC"]),  # 综述里列了本票: 不算
        item("NTAP Q1 Earnings Beat Estimates", "yahoo_search", tickers=["NTAP", "TDC"]),            # 别家稿子
        item("Reflecting On Data Infrastructure Stocks' Q2 Earnings: Teradata (NYSE:TDC)", "yahoo_search", tickers=["TDC", "^GSPC"])]
    got = ds.news_items("TDC")
    assert [x["title"][:20] for x in got] == ["Teradata's COO sells", "UBS Adjusts Teradata", "Reflecting On Data I"]
    assert all("tickers" not in x for x in got)
    # 第一家 (yfinance) 返回的全是别家新闻: 等于没取到, 接着问 yahoo_search; RSS 按标题认
    feeds.tables["yfinance"]["FRPT"] = [item("Exxon Mobil downgraded, BP upgraded", tickers=[]), item("Oil majors rally", tickers=["XOM"])]
    feeds.tables["yahoo_search"]["FRPT"] = [item("Pet food demand", "yahoo_search", tickers=["CHWY"])]
    feeds.tables["yahoo_rss"]["FRPT"] = [item("Freshpet (FRPT) raises guidance", "yahoo_rss", tickers=[]),
                                         item("3 Profitable Stocks with Warning Signs", "yahoo_rss", tickers=[])]
    got = ds.news_items("FRPT")
    assert [(x["title"], x["source"]) for x in got] == [("Freshpet (FRPT) raises guidance", "yahoo_rss")]
    assert [n for n, s in feeds.calls if s == "FRPT"] == ["yfinance", "yahoo_search", "yahoo_rss"]
    st = ds.news_round_stats()
    assert (st["fetched"], st["with_news"], st["items"]) == (2, 2, 4)                       # items 只数留下的本票标题 (3 + 1)
    assert st["filtered"] == 7 and st["filtered_by_source"] == {"yfinance": 2, "yahoo_search": 4, "yahoo_rss": 1}   # 3 + (2 + 1 + 1)
    assert st["by_source"] == {"yahoo_search": 1, "yahoo_rss": 1}
    # 四家都只有别家新闻: 空表 (不抛), 不进当日缓存, 熔断计数照加
    feeds.tables["yfinance"]["XXX"] = [item("Someone else", tickers=["ABC"])]
    feeds.tables["google_rss"]["XXX"] = [item("Nothing about us", "google_rss", tickers=[])]
    assert ds.news_items("XXX") == [] and ds.fetch_news("XXX") == [] and mk.news_titles("XXX") == []
    assert ds.news_round_stats()["filtered"] == 9                                           # + yfinance 1 + google_rss 1
    # 错杀红旗不再被别家标题点亮
    from screener import newsflag
    cands = [{"code": "TDC", "cuosha_score": 80}, {"code": "FRPT", "cuosha_score": 70}]
    newsflag.annotate(cands, as_of=TODAY.isoformat())
    assert [n["t"][:20] for n in cands[0]["news"]] == ["Teradata's COO sells", "UBS Adjusts Teradata", "Reflecting On Data I"]
    assert cands[0]["news_flags"] == []                                                      # 「Blackbaud CEO」不再给 TDC 点 executive change
    assert [n["t"] for n in cands[1]["news"]] == ["Freshpet (FRPT) raises guidance"] and cands[1]["news_flags"] == ["guidance"]


# =========================================================================== 整轮体检 → meta.news_source_ok
def _prof(n: int) -> dict:
    return {"summary": "x", "news": [{"title": f"t{i}"} for i in range(n)]}


@pytest.mark.parametrize("profiles,cands,want", [
    ({}, [], (None, 0, 0, 0)),                                                               # 没有票需要新闻: 无从判断
    ({"A": _prof(0), "B": _prof(0)}, [{"code": "C", "cuosha_score": 70}], (False, 3, 0, 0)),  # 10-02..10-04 的形态: 总数 0
    ({f"P{i}": _prof(1 if i == 0 else 0) for i in range(6)}, [], (False, 6, 1, 1)),           # 1/6 = 16.7% < 20%
    ({f"P{i}": _prof(3 if i == 0 else 0) for i in range(5)}, [], (True, 5, 1, 3)),            # 1/5 = 20%: 刚好够
    ({"A": _prof(4)}, [{"code": "A", "cuosha_score": 60, "news": [{"t": "x"}]},              # 同一只票档案与错杀都有: 只算一只
                       {"code": "B", "cuosha_score": 60}, {"code": "C"}], (True, 2, 1, 4)),
    ({"A": {"summary": None}}, [{"code": "B", "cuosha_score": 60, "news": [{"t": "x"}, {"t": "y"}]}], (True, 1, 1, 2)),
])
def test_news_health_thresholds(profiles, cands, want):
    h = ex.news_health(profiles, cands)
    assert (h["ok"], h["n"], h["with_news"], h["items"]) == want
    assert h["rate"] == (None if not want[1] else round(want[2] / want[1], 3))


def _payload(monkeypatch, profiles: dict) -> dict:
    rows = [{"code": c, "profile_json": json.dumps(p)} for c, p in profiles.items()]
    monkeypatch.setattr(ex.db, "latest_run_date", lambda: TODAY.isoformat())
    monkeypatch.setattr(ex.db, "fetch_run_log", lambda d: {"data_date": D1.isoformat(), "finished_at": f"{TODAY} 09:40:00",
                                                           "n_scanned": 4321})
    monkeypatch.setattr(ex.db, "fetch_table", lambda name, d: rows if name == "profile" else [])
    monkeypatch.setattr(ex.db, "recent_run_dates", lambda n: [])
    monkeypatch.setattr(ex.db, "recent_appearance_counts", lambda ds_: {})
    monkeypatch.setattr(ds, "fetch_benchmark", lambda: None)
    return ex.build_payload()


def test_build_payload_writes_news_source_ok_early_in_meta_and_warns(feeds, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="ashare.export"):
        p = _payload(monkeypatch, {"AAA": _prof(0), "BBB": _prof(0)})
    assert p["meta"]["news_source_ok"] is False
    assert {k: p["meta"]["news_stats"][k] for k in ("n", "with_news", "items", "rate")} == {"n": 2, "with_news": 0, "items": 0, "rate": 0.0}
    assert p["meta"]["news_stats"]["fetch"]["filtered"] == 0 and "filtered_by_source" in p["meta"]["news_stats"]["fetch"]   # 过滤条数随统计进 meta
    assert any("新闻源不可用" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    js = "window.__ASHARE__ = " + json.dumps(p, ensure_ascii=False)
    assert 0 < js.index('"news_source_ok": false') < 600          # 数据总览只读文件头抠这个键: 必须在 meta 靠前的位置
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ashare.export"):
        p2 = _payload(monkeypatch, {"AAA": _prof(2), "BBB": _prof(0)})
    assert p2["meta"]["news_source_ok"] is True and not any("新闻源不可用" in r.getMessage() for r in caplog.records)
    assert _payload(monkeypatch, {})["meta"]["news_source_ok"] is None
