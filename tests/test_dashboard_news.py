#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股看板: 新闻源不可用时的显示 (无头 Chromium, 零联网) —— 2026-10-04 卡 NEWS-OVERDUE。

流水线导出时写 meta.news_source_ok (整轮一条标题都没有 / 有标题的票不到两成 = false)。false 时:
  · 详情弹窗「消息与博弈」页签: 不再显示「利好 0 / 利空 0 / 中性 0」+「暂无新闻」, 改成「新闻源不可用」药丸 + 一句说明;
  · 错杀卡片: 不再显示「近30天无相关新闻标题」, 改成「新闻源不可用…不代表没有利空」, 🚩 位置出「🚩 新闻源不可用」;
  · 这只票自己有标题时照常显示 (源整体不行不等于这只票的标题是假的)。
news_source_ok 为 true / 缺失 (旧快照) 时一切照旧。

页面取仓库里的 dashboard/index.html 与同目录的静态数据文件, dashboard_data.js 用下面的合成数据 (真文件不入 git);
tailwind / echarts 两个 CDN 用空桩顶替, 其余外联一律掐掉。没有 playwright → 整个模块 skip (服务器 venv 没有)。
运行 (仓库根): python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_dashboard_news.py -q
"""
from __future__ import annotations

import datetime as dt
import functools
import http.server
import json
import os
import shutil
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "dashboard")
TODAY = dt.date.today()

ECHARTS_STUB = """
window.echarts = { init: function(){ var c = new Proxy(function(){}, { get: function(t, k){
    if(k === 'getOption') return function(){ return {}; };
    if(k === 'getWidth' || k === 'getHeight') return function(){ return 0; };
    return function(){ return c; }; } }); return c; } };
"""

COLUMNS = [("code", "代码"), ("name", "名称"), ("industry", "所属板块"), ("tag", "结论标签"), ("final_score", "综合分"),
           ("price", "现价$"), ("cuosha_score", "错杀分"), ("cuosha_upside", "修复空间%")]


def cand(code: str, **kw) -> dict:
    c = {"code": code, "name": code + " Corp", "industry": "Information Technology", "tag": "🟢 强左侧",
         "final_score": 80.0, "tech_score": 70.0, "fund_score": 60.0, "price": 100.0, "streak": 1, "spark": [],
         "roe_trend": [], "roe_trend_q": [], "fund_flags": [], "ni_qoq": [], "ni_parent_qoq": [], "rev_qoq": [],
         "ni_q_labels": [], "plan": None}
    c.update(kw)
    return c


def payload(news_ok) -> dict:
    meta = {"run_date": TODAY.isoformat(), "data_date": (TODAY - dt.timedelta(days=1)).isoformat(),
            "updated_at": f"{TODAY.isoformat()} 09:40:00", "n_scanned": 4321, "n_hit": 3,
            "selected_industries": ["Information Technology"], "disclaimer": "test", "opp": None}
    if news_ok is not None:
        meta = {**meta, "news_source_ok": news_ok, "news_stats": {"n": 3, "with_news": 1, "items": 2, "rate": 0.333}}
    return {
        "meta": meta, "industries": [], "details": {},
        "candidates": [
            cand("QUIET", cuosha_score=82, cuosha_upside=25, cuosha_note="deep drawdown"),            # 错杀候选, 没取到标题
            cand("NOISY", cuosha_score=75, cuosha_upside=10, cuosha_note="x", news_flags=["lawsuit"],
                 news=[{"d": TODAY.isoformat(), "t": "Class action filed", "u": "https://example.com/n", "f": ["lawsuit"]}]),
            cand("PLAIN")],
        "profiles": {
            "QUIET": {"summary": "s", "officers": [], "news": [], "options": None, "darkpool": None},
            "NOISY": {"summary": "s", "officers": [], "options": None, "darkpool": None, "news": [
                {"title": "Regulator opens probe", "publisher": "Wire", "time": "2026-01-01 10:00", "url": "https://example.com/a",
                 "tone": "利空", "source": "yahoo_search"},
                {"title": "Record quarter", "publisher": "Wire", "time": "2026-01-01 09:00", "url": "https://example.com/b",
                 "tone": "利好", "source": "yahoo_search"}]},
            "PLAIN": {"summary": "s", "officers": [], "news": [], "options": None, "darkpool": None}},
        "columns": [{"key": k, "label": lab} for k, lab in COLUMNS],
    }


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("us_dash")
    for name in os.listdir(DASH):
        p = os.path.join(DASH, name)
        if os.path.isfile(p) and name != "dashboard_data.js" and (name.endswith(".js") or name == "index.html"):
            shutil.copy(p, root / name)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"root": str(root), "base": f"http://127.0.0.1:{srv.server_address[1]}"}
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def browser():
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch()
    except Exception as e:                   # noqa: BLE001 —— 装了库没装浏览器
        pw.stop()
        pytest.skip(f"Chromium 起不来: {e}")
    yield b
    b.close()
    pw.stop()


def open_dash(browser, site, news_ok, lang="zh"):
    with open(os.path.join(site["root"], "dashboard_data.js"), "w", encoding="utf-8") as f:
        f.write("window.__ASHARE__ = " + json.dumps(payload(news_ok), ensure_ascii=False) + ";\n")
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_init_script(f"try{{localStorage.setItem('us_lang_v1','{lang}');}}catch(e){{}}")
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))

    def gate(route):
        url = route.request.url
        if url.startswith(site["base"]):
            return route.continue_()
        if "echarts" in url:
            return route.fulfill(status=200, content_type="application/javascript", body=ECHARTS_STUB)
        if "tailwindcss" in url:
            return route.fulfill(status=200, content_type="application/javascript", body="")
        return route.abort()
    page.route("**/*", gate)
    page.goto(site["base"] + "/index.html")
    page.wait_for_function("() => document.querySelectorAll('#tableBody tr').length === 3")
    return ctx, page, errs


READ = r"""() => {
  var tx = function(s){ var e = document.querySelector(s); return e ? e.innerText.replace(/\s+/g, ' ').trim() : null; };
  return {sum: tx('#dNewsSum'), list: tx('#dNewsList'), cs: tx('#dCuosha'), down: tx('#nwDown'), flagDown: tx('#csFlagDown'),
          csNone: tx('#csNewsNone'), rows: document.querySelectorAll('#dNewsList .newsrow').length,
          csHidden: document.querySelector('#dCuosha').classList.contains('hidden')};
}"""


def show(page, code: str) -> dict:
    """像用户那样操作: 点主表里这只票的行 (开详情抽屉) → 点「消息与博弈」页签 → 读新闻区与错杀卡片。脚本整体包在 IIFE 里, 没有全局函数可调。"""
    page.evaluate("(code) => [...document.querySelectorAll('#tableBody tr')].filter(function(tr){ return tr.innerText.indexOf(code) >= 0; })[0].click()", code)
    page.click("button[data-tab=tabNw]")
    page.wait_for_function("() => !document.querySelector('#tabNw').classList.contains('hidden') && document.querySelector('#dNewsList').innerHTML !== ''")
    return page.evaluate(READ)


def test_news_source_down_replaces_zero_counts_and_no_headlines_text(browser, site):
    ctx, page, errs = open_dash(browser, site, False)
    r = show(page, "QUIET")
    assert r["down"] == "新闻源不可用" and r["sum"] == "新闻源不可用", r                 # 不是「利好 0 利空 0 中性 0」
    assert "利空 0" not in (r["sum"] or "") and "数据源故障" in r["list"] and "不代表没有" in r["list"] and r["rows"] == 0, r
    assert r["flagDown"] == "🚩 新闻源不可用", r
    assert r["csNone"].startswith("新闻源不可用") and "不代表没有利空" in r["csNone"], r   # 不是「近30天无相关新闻标题」
    # 这只票自己有标题: 照常显示, 不盖「不可用」
    r2 = show(page, "NOISY")
    assert r2["down"] is None and r2["rows"] == 2 and r2["sum"] == "利好 1 利空 1 中性 0", r2
    assert r2["flagDown"] is None and "🚩 lawsuit" in r2["cs"] and "Class action filed" in r2["cs"], r2
    # 不是错杀候选: 没有错杀卡片, 新闻页签同样写「新闻源不可用」
    r3 = show(page, "PLAIN")
    assert r3["down"] == "新闻源不可用" and r3["csHidden"] is True, r3
    assert not errs, errs
    ctx.close()


@pytest.mark.parametrize("news_ok", [True, None])
def test_news_source_ok_or_legacy_snapshot_keeps_the_old_display(browser, site, news_ok):
    ctx, page, errs = open_dash(browser, site, news_ok)
    r = show(page, "QUIET")
    assert r["down"] is None and r["sum"] == "利好 0 利空 0 中性 0" and r["list"] == "暂无新闻", r
    assert r["flagDown"] is None and r["csNone"] == "近30天无相关新闻标题", r
    assert not errs, errs
    ctx.close()


def test_news_source_down_english_strings(browser, site):
    ctx, page, errs = open_dash(browser, site, False, lang="en")
    r = show(page, "QUIET")
    assert r["down"] == "News feed unavailable" and "data-source failure" in r["list"], r
    assert r["flagDown"] == "🚩 news feed unavailable" and r["csNone"].startswith("News feed unavailable"), r
    assert not errs, errs
    ctx.close()
