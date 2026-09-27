# 📈 股票分析看板（TradingAgents 外挂 Web）

基于本目录已有的两个工具做的本地 Web 外挂：

- 行情与指标来自根目录 `stock/` 的 `data_fetcher.py` / `strategy.py` / `strategy_nx.py`
- 一键分析调用 `TradingAgents/` 的多智能体框架（本地 v0.3.1 源码）

## 功能

1. **收藏多股票**：添加/编辑/删除，自动识别名称与行业分类（可手改），按分类分组展示
2. **一键 TradingAgents 分析**：收藏卡片一键默认配置跑分析；详情页可自定义 LLM 供应商、模型、研究深度、分析师团队；支持批量排队分析（同时最多跑 2 个任务）
3. **美化版 Markdown 报告**：报告按章节渲染（市场/情绪/新闻/基本面 → 多空辩论 → 交易计划 → 风控 → 组合决策），带目录导航、代码高亮，可复制/下载原始 Markdown
4. **历史数据与常见指标**：K 线 + EMA9/21 + NX 牛熊通道 + MACD + RSI + 背离标记，52 周高低、最新信号、full 策略回测摘要

## 快速开始

```bash
# 1. 安装依赖（首次）
python3 -m pip install --user fastapi "uvicorn[standard]"

# 2. 启动
cd stock_web
python3 server.py
# 或
./start.sh
```

浏览器打开 <http://127.0.0.1:8787>

> 默认端口 8787，可用环境变量 `STOCK_WEB_PORT` 修改。

## 分析任务说明

- 一键分析默认使用 `deepseek` 供应商（读取 `TradingAgents/.env` 里的 `DEEPSEEK_API_KEY`），模型为 `deepseek-v4-pro` / `deepseek-v4-flash`，研究深度 1，中文报告。GLM 支持 `glm`（Z.AI 国际，`ZHIPU_API_KEY`）与 `glm-cn`（智谱国内，`ZHIPU_CN_API_KEY`）两个端点。
- 分析在后台子进程运行（cwd = `TradingAgents/`，强制使用本地源码而非 site-packages），进度在「运行记录」页实时滚动。
- 所有报告统一存到 `TradingAgents/reports/<代码>_<时间戳>/`（与老 CLI 报告同一目录、同一命名），网页只负责读取渲染；历史报告也会自动出现在「分析报告」页。
- 同时最多运行 2 个分析任务，其余排队，避免打爆 LLM 限流。
- 报告统一在 `TradingAgents/reports/`，任务记录在 `stock_web/jobs/`，删除报告可在网页报告列表操作。

## 目录结构

```
stock_web/
├── server.py            # FastAPI 后端（收藏 / 行情 / 任务 / 报告 API）
├── market.py            # 行情与指标数据层（封装根目录 stock 模块）
├── run_analysis.py      # TradingAgents 分析执行器（子进程）
├── favorites.json       # 收藏列表（运行时生成）
├── jobs/                # 分析任务记录 + 日志（运行时生成）
├── static/              # 前端页面（无构建步骤）
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── vendor/          # 本地化的 echarts / marked / highlight.js
└── start.sh
```

## 常见问题

- **行情卡片一直加载**：需要能访问 Yahoo Finance（yfinance），行情快照缓存 90 秒。
- **一键分析失败**：先确认 `TradingAgents/.env` 里对应的 LLM key 已配置；再到「运行记录」看实时日志定位原因。
- **换 LLM 供应商**：在详情页点「⚡ 一键 TradingAgents 分析」旁的按钮或从卡片进详情，弹窗里可切换 DeepSeek / GLM / OpenAI / Gemini / Claude / OpenAI 兼容端点，并填对应模型。
- **前端库离线可用**：ECharts / marked / highlight.js 已下载到 `static/vendor/`，无需外网 CDN。

> 仅供研究学习，不构成投资建议。
