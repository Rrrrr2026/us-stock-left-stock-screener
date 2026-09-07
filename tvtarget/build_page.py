#!/usr/bin/env python3
"""
用 data/screen.json 渲染:
  output/index.html      交互筛选页（自包含单文件，可直接放进网站任意路径）
  output/watchlist.txt   TradingView「导入列表…」文件（按行业分组成一个板块）

无第三方依赖。先跑 fetch_screen.py 刷新数据，再跑本脚本。
"""
import json
import datetime
import pathlib
from collections import defaultdict

BASE = pathlib.Path(__file__).parent
TEMPLATE = BASE / "web" / "page_template.html"
DATA = BASE / "data" / "screen.json"
OUT_DIR = BASE / "output"


def main() -> None:
    data = json.load(open(DATA, encoding="utf-8"))
    rows = data["rows"]
    fetched = datetime.datetime.fromisoformat(data["fetchedAt"].replace("Z", "+00:00"))

    payload = json.dumps({"sectors": data["sectors"], "rows": rows},
                         ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    html = open(TEMPLATE, encoding="utf-8").read()
    assert "/*__DATA__*/" in html
    html = (html
            .replace("/*__DATA__*/", "const DATA=" + payload + ";")
            .replace("__N_MATCH__", str(len(rows)))
            .replace("__N_TOTAL__", str(data.get("totalCovered", "?")))
            .replace("__FETCH_DATE__", fetched.strftime("%Y-%m-%d %H:%M UTC")))

    OUT_DIR.mkdir(exist_ok=True)
    open(OUT_DIR / "index.html", "w", encoding="utf-8").write(html)

    # watchlist：###板块名 分组，组内按市值降序（rows 本身已按市值排序）
    by_sec = defaultdict(list)
    for r in rows:
        by_sec[data["sectors"][r[2]]].append(r[0])
    parts = []
    for sec in sorted(by_sec, key=lambda s: -len(by_sec[s])):
        parts.append("###" + sec.upper().replace(",", " "))
        parts.extend(by_sec[sec])
    open(OUT_DIR / "watchlist.txt", "w", encoding="utf-8").write(",".join(parts))

    print(f"✓ output/index.html（{len(rows)} 只） + output/watchlist.txt")


if __name__ == "__main__":
    main()
