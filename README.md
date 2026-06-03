# 美股盯盘+回测系统

基于 Python 的美股实时监控系统，支持多股票独立策略配置、技术指标计算、定时监控和 Bark 消息推送。

## 功能特性

- 📈 支持多个股票同时监控，每只股票可配置独立策略
- 🔔 通过 Bark 推送异常信号通知（iOS）
- 📊 内置 MACD、RSI、均线等技术指标
- ⏰ 基于 APScheduler 的定时任务调度
- 🐳 Docker 容器化，支持 NAS 长期稳定运行
- 📝 完整的日志记录

## 目录结构

```
stock/
├── data_fetcher.py      # 数据获取模块
├── strategy.py          # 策略与信号检测模块
├── notifier.py          # Bark 推送模块
├── main.py              # 主入口 + 调度器
├── .env.example         # 环境变量示例
├── .env                 # 实际配置（不提交 Git）
├── requirements.txt     # Python 依赖
├── Dockerfile
├── docker-compose.yml
└── logs/                # 日志目录（自动创建）
```

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的股票代码和 Bark URL
```

### 2. 多股票独立策略配置示例（`.env`）

```env
# 单股票（兼容旧版）
SYMBOL=AAPL
INTERVAL=15m
BARK_URL=https://api.day.app/your_key/

# 多股票独立策略（JSON 格式）
STOCK_STRATEGIES=[
  {"symbol": "AAPL", "interval": "15m", "strategy": "macd_rsi"},
  {"symbol": "TSLA", "interval": "5m",  "strategy": "rsi_only"},
  {"symbol": "NVDA", "interval": "1h",  "strategy": "ma_cross"}
]
```

### 3. 可用策略类型

| 策略名称 | 说明 |
|---------|------|
| `macd_rsi` | MACD 金叉/死叉 + RSI 超买超卖（默认） |
| `rsi_only` | 仅 RSI 超买超卖信号 |
| `ma_cross` | 均线交叉信号（MA5/MA20） |

### 4. 本地运行

```bash
pip install -r requirements.txt
python main.py
```

### 5. Docker 运行

```bash
docker-compose up -d
# 查看日志
docker-compose logs -f
```

## 部署到 NAS

### 方式一：直接使用 docker-compose

1. 在 NAS 上安装 Docker
2. 将项目文件上传到 NAS（或 git clone）
3. 配置 `.env` 文件
4. 运行：
   ```bash
   docker-compose up -d
   ```

### 方式二：推送到 GitHub + NAS 拉取

#### 推送到 GitHub

```bash
cd /Users/edward/stock

# 初始化 Git
git init
git add .
git commit -m "feat: 美股盯盘系统初始版本"

# 关联远程仓库（替换为你的 GitHub 用户名）
git remote add origin https://github.com/YOUR_USERNAME/stock-monitor.git
git branch -M main
git push -u origin main
```

> ⚠️ 确保 `.gitignore` 中已忽略 `.env` 和 `logs/`，避免泄露密钥。

#### 在 NAS 上部署

```bash
# SSH 登录 NAS，然后：
git clone https://github.com/YOUR_USERNAME/stock-monitor.git
cd stock-monitor
cp .env.example .env
# 编辑 .env 填入配置
nano .env

# 启动
docker-compose up -d
```

#### 更新部署

```bash
# NAS 上执行
cd stock-monitor
git pull
docker-compose down
docker-compose up -d --build
```

### 方式三：使用 GitHub Actions 自动构建镜像推送到 GHCR

项目已包含 `.github/workflows/docker-publish.yml`，每次推送到 `main` 分支时，自动构建并推送镜像到 GitHub Container Registry (ghcr.io)。

NAS 上使用预构建镜像：
```bash
docker pull ghcr.io/YOUR_USERNAME/stock-monitor:latest
```

修改 `docker-compose.yml` 中的 `image` 字段即可。

## 日志查看

```bash
# 实时日志
tail -f logs/monitor.log

# Docker 日志
docker-compose logs -f app
```

## 信号说明

系统检测到以下信号时会推送 Bark 通知：

- **MACD 金叉**：快线从下方穿越慢线，看涨信号
- **MACD 死叉**：快线从上方穿越慢线，看跌信号
- **RSI 超买**：RSI > 70，可能回调
- **RSI 超卖**：RSI < 30，可能反弹
- **均线金叉**：MA5 上穿 MA20，短期看涨
- **均线死叉**：MA5 下穿 MA20，短期看跌
