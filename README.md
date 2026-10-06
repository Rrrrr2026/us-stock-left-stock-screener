# 美股「左侧支撑位」筛选 + 基本面交叉 + 交互监控台

在**高景气 GICS 板块**内,自动发现全美股(市值≥$100M, 约4100只)里正回踩支撑 / 接近前低的**左侧机会**,拉取基本面并技术×基本面交叉打分,再对每只候选做**支撑回踩事件回测**给出买卖点建议(买入区/止损/目标梯子+历史胜率),在一个**全中文、可交互**的监控台里一屏看全。数据源:**Yahoo Finance (yfinance)**。价格单位美元。

> ⚠️ **免责声明**:本系统仅做技术/基本面数据的自动化整理与形态筛选,**不构成任何投资建议**。"左侧买入"风险天然更高(可能继续下跌或破位)。买卖点与胜率为历史回测统计,不构成对未来的保证。所有标的需**人工复核**,自负盈亏与风控。

---

## 在线查看 (GitHub Pages)
**https://rrrrr2026.github.io/us-stock-left-screener/**(任何设备浏览器直接打开)

## 一分钟上手
```bash
pip install -r requirements.txt
py -3 run_pipeline.py           # 抓全美股数据 -> 打分 -> 买卖点回测 -> 导出看板 (约10-30分钟)
# 然后双击打开 dashboard/index.html
```
更新线上数据:双击 `auto_update.bat`(跑完整流水线并推送 docs/ 到 GitHub, Pages 1-2分钟后刷新)。已配置计划任务「美股左侧监控台每日更新」工作日 08:00 自动运行。2026-10-06 起该任务**隐藏运行**: Action 是 `wscript.exe run_hidden.vbs` 包装 (无窗口跑 `auto_update.bat auto`, 关不掉也看不见), bat 的屏幕输出进 `logs/task_YYYYMMDD.log` (本地保留 60 天, 不入 git), 退出码原样传回任务的「上次运行结果」; 流水线日志仍在 `data/update.log`。

## 结构
- `screener/` — config / datasource(yfinance) / indicators / module1_industry(板块景气) / module2_tech(技术左侧) / module3_fundamentals / module4_crossscore / tradeplan(买卖点回测) / module6_profile(深度档案) / db / export_data
- `dashboard/index.html` — 全中文交互看板 (Tailwind + ECharts, 中英双语/明暗主题)
- `docs/` — GitHub Pages 托管目录
- `ALGORITHMS.md` — 全部算法公式与参数说明(含 v2 升级说明)

## 说明
- **股票池**:NASDAQ 官方筛选器全美股(市值≥$100M, 剔除优先股/挂牌债券/权证/SPAC单位);板块以 Yahoo GICS 口径为准(NASDAQ 分类仅兜底)。可在 `screener/config.py` 调整。
- **板块景气**:用 11 个 SPDR 行业 ETF(XLK/XLF/XLV…)作板块指数代理算 趋势/动量,成分股算广度;基准 SPY。
- **技术打分**:通道下轨/前低/均线/超跌背离/回撤 + v2 新增 支撑强度/趋势规整/相对强度(vs SPY)。
- **基本面**:来自 `yf.info`(PE/PB/ROE/毛利/增长/股息/FCF收益率等);PE 分位为候选池内横截面分位,并与所属板块中位对比;数据覆盖不足会折价。
- **买卖点建议**:详情弹窗内,基于本股近2年支撑回踩事件回测 + 全池先验贝叶斯收缩,给出 建议买入区/止损位/三档目标(+胜率+中位到达天数)/盈亏比。
- 每只失败只跳过并记录,不中断整轮。


## 共用核心 (leftside_core)

回测 / 错杀检测 / 新闻标记 / 买卖点计划 / 指标等逻辑与 A股/美股 另一个筛选器共用，源码在 `stock-core` 仓库的 `leftside_core/`；本仓库 `vendor/leftside_core/` 是每次更新时同步进来的副本，所以克隆本仓库即可直接运行。本机若存在同级目录 `../stock-core`（或设置 `STOCK_CORE_DIR`）则优先使用它。修改共用逻辑请改 stock-core，不要直接改 vendor 副本。
