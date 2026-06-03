# 美股盯盘+回测系统

基于 Python 的美股实时监控系统，支持多股票独立策略配置、技术指标计算、定时监控和 Bark 消息推送。

## 功能特性

- 📈 多股票同时监控，每只股票独立策略
- 🔔 Bark 推送异常信号通知（iOS）
- 📊 内置 MACD、RSI、均线、NX牛熊双通道等指标
- 🔍 MACD 波段背离识别（底背离/顶背离）
- ⏰ APScheduler 定时任务调度
- 🐳 Docker 容器化，支持 NAS 长期稳定运行
- 📝 完整日志记录（控制台 + 滚动文件）

## 目录结构

```
stock/
├── data_fetcher.py      # 数据获取模块（yfinance）
├── strategy.py          # 策略引擎（MACD/RSI/EMA）
├── strategy_nx.py       # NX策略引擎（牛熊通道+背离）
├── indicators_nx.py     # NX指标计算模块
├── notifier.py          # Bark 推送模块
├── main.py              # 主入口 + 调度器
├── .env.example         # 环境变量示例
├── .env                 # 实际配置（不提交 Git）
├── requirements.txt     # Python 依赖
├── Dockerfile
├── docker-compose.yml
└── logs/                # 日志目录（自动创建）
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入股票代码和 Bark URL
```

`.env` 配置示例（每只股票独立策略）：

```env
# 每只股票格式：股票代码,策略名,K线周期
STOCK_0=AAPL,macd_rsi,15m
STOCK_1=TSLA,tsla_aggressive,15m
STOCK_2=NVDA,nvda_macd,15m
STOCK_3=SPY,ma_cross,1h
STOCK_4=QQQ,ma_cross,1h

# Bark 推送链接
BARK_URL=https://api.day.app/your_key/
```

### 3. 启动盯盘（实时监控）

```bash
python main.py
```

启动后会立即执行一次全量检查，之后按配置周期定时轮询。按 `Ctrl+C` 退出。

---

## 回测使用说明

### 方式一：命令行快速回测（推荐新手）

直接在终端运行，无需修改代码：

```bash
python3 -c "
from data_fetcher import DataFetcher
from strategy import StrategyEngine

symbol   = 'AAPL'       # 股票代码
strategy = 'macd_rsi'   # 策略名称（见下方策略列表）
interval = '1h'         # K线周期
period   = '60d'        # 回测历史数据范围

fetcher = DataFetcher()
engine  = StrategyEngine()

df = fetcher.fetch(symbol, interval=interval, period=period)
df = engine.add_indicators(df, strategy)
result = engine.run_backtest(df, strategy, symbol)

print('=== 回测结果 ===')
for k, v in result.items():
    print(f'{k}: {v}')
"
```

### 方式二：NX 牛熊通道+背离策略回测

```bash
python3 -c "
from data_fetcher import DataFetcher
from strategy_nx import NXStrategyEngine

symbol   = 'AAPL'
interval = '1h'
period   = '60d'
scheme   = 1            # 1=右侧突破确认  2=左侧精准狙击

fetcher = DataFetcher()
engine  = NXStrategyEngine()

df = fetcher.fetch(symbol, interval=interval, period=period)
df = engine.add_indicators(df, interval=interval)

# 逐根K线扫描，统计信号
signals = []
for i in range(91, len(df)):
    sliced = df.iloc[:i+1]
    triggered, desc = engine.check_signals(sliced, symbol=symbol, scheme_type=scheme)
    if triggered:
        ts_ms = int(df.index[i].timestamp() * 1000)
        close = df['Close'].iloc[i]
        signals.append({'ts_ms': ts_ms, 'close': close, 'desc': desc.splitlines()[1]})

print(f'=== {symbol} scheme_{scheme} 回测结果 ===')
print(f'总K线数: {len(df)}，触发信号: {len(signals)} 次')
print()
for s in signals[-10:]:   # 打印最近10条信号
    print(f'  [{s[\"ts_ms\"]}ms] close={s[\"close\"]:.2f}  {s[\"desc\"]}')
"
```

### 方式三：对比多策略回测效果

```bash
python3 -c "
from data_fetcher import DataFetcher
from strategy import StrategyEngine, STRATEGY_REGISTRY

fetcher = DataFetcher()
engine  = StrategyEngine()
symbol  = 'AAPL'
df_raw  = fetcher.fetch(symbol, interval='1h', period='60d')

for strategy_name in STRATEGY_REGISTRY:
    df = engine.add_indicators(df_raw.copy(), strategy_name)
    r  = engine.run_backtest(df, strategy_name, symbol)
    print(f'{strategy_name:20s}  信号次数={r[\"total_signals\"]:3d}  触发率={r[\"signal_rate\"]}')
"
```

---

## 内置策略列表

### 基础策略（`strategy.py`）

| 策略名 | 信号类型 | 适合标的 |
|--------|---------|---------|
| `macd_rsi` | MACD金叉死叉 + RSI超买超卖 | 通用默认 |
| `rsi_only` | 仅RSI（阈值75/25）| 高波动股 TSLA |
| `ma_cross` | EMA9/21均线交叉 | SPY/QQQ指数 |
| `tsla_aggressive` | 敏感RSI + EMA5/20 | TSLA 专属 |
| `nvda_macd` | 快速MACD(8/21/5) + RSI | NVDA 专属 |
| `full` | MACD + RSI + EMA三合一 | 全量信号 |

### NX高级策略（`strategy_nx.py`）

| 策略名 | 方案 | 触发条件 |
|--------|------|---------|
| `nx_scheme1` | 右侧突破确认 | 近15根K线内有MACD底背离 AND 收盘突破短中线通道上沿 |
| `nx_scheme2` | 左侧精准狙击 | 收盘≤短中线通道下沿 AND 当前K线确认MACD底背离 |

**NX策略共用平仓逻辑：**
- 绝对止损：收盘 < 长线通道下沿 B1
- 清仓：收盘 < 短中线通道下沿 B
- 减仓50%：出现MACD顶背离

**NX通道参数：**
- 短中线蓝色通道：上沿 A = EMA(High,24)，下沿 B = EMA(Low,23)
- 长线黄色通道：上沿 A1 = EMA(High,89)，下沿 B1 = EMA(Low,90)

---

## 支持的 K 线周期

| interval | 说明 | 最长回测范围 |
|----------|------|------------|
| `1m` | 1分钟 | 7天 |
| `5m` | 5分钟 | 60天 |
| `15m` | 15分钟 | 60天 |
| `30m` | 30分钟 | 60天 |
| `1h` / `60m` | 1小时 | 2年 |
| `1d` | 日线 | 5年 |

---

## Docker 部署

```bash
# 构建并启动
docker-compose up -d

# 查看实时日志
docker-compose logs -f

# 停止
docker-compose down
```

## 部署到 NAS（群晖/QNAP）

### 方式一：GitHub Actions 自动构建（推荐）

每次 `git push` 到 `main` 分支，GitHub Actions 自动构建镜像推送到 `ghcr.io`。

在 NAS 上：

```bash
mkdir -p ~/stock-monitor/logs && cd ~/stock-monitor

# 创建 .env（填入你的配置）
cat > .env << 'EOF'
STOCK_0=AAPL,macd_rsi,15m
STOCK_1=TSLA,tsla_aggressive,15m
STOCK_2=NVDA,nvda_macd,15m
BARK_URL=https://api.day.app/your_key/
EOF

# 创建 docker-compose.yml
cat > docker-compose.yml << 'EOF'
version: "3.8"
services:
  stock-monitor:
    image: ghcr.io/saievo/watching:latest
    container_name: stock-monitor
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./logs:/app/logs
    environment:
      - TZ=America/New_York
EOF

docker compose pull && docker compose up -d
```

### 方式二：直接 git clone 构建

```bash
git clone https://github.com/Saievo/watching.git
cd watching
cp .env.example .env
nano .env   # 填入配置
docker compose up -d --build
```

### 更新部署

```bash
cd watching
git pull
docker compose down && docker compose up -d --build
```

---

## 日志查看

```bash
# 本地实时日志
tail -f logs/monitor.log

# Docker 日志
docker-compose logs -f stock-monitor
```

## 信号说明

| 信号 | 含义 |
|------|------|
| MACD 金叉 | 柱状图由负转正，看涨 |
| MACD 死叉 | 柱状图由正转负，看跌 |
| RSI 超买 | RSI ≥ 70，注意回调 |
| RSI 超卖 | RSI ≤ 30，注意反弹 |
| EMA 金叉 | 短期均线上穿长期，看涨 |
| EMA 死叉 | 短期均线下穿长期，看跌 |
| NX 底背离 | 价格新低但MACD更高，潜在反转 |
| NX 顶背离 | 价格新高但MACD更低，潜在见顶 |
