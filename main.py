"""
main.py - 调度与主入口
组装所有模块，使用 APScheduler 实现定时监控
支持每支股票配置不同的监控策略
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from data_fetcher import DataFetcher
from strategy import StrategyEngine, STRATEGY_REGISTRY
from notifier import BarkNotifier

# ── 加载环境变量 ──────────────────────────────────────────────
load_dotenv()

# ── 日志配置 ──────────────────────────────────────────────────
def setup_logger() -> logging.Logger:
    """配置日志：同时输出到控制台与滚动文件"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger("stock_monitor")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台 Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 文件 Handler（最大 10MB，保留 5 个备份）
    file_handler = RotatingFileHandler(
        "logs/monitor.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


logger = setup_logger()


# ── 解析配置 ──────────────────────────────────────────────────
def parse_stock_configs() -> list[dict]:
    """
    从环境变量解析多股票配置。
    支持两种格式：
    1. 简单格式：SYMBOLS=AAPL,TSLA,NVDA（所有股票使用同一策略）
    2. 详细格式（每支股票独立配置）：
         STOCK_0=AAPL,macd_rsi,15m
         STOCK_1=TSLA,rsi_only,1h
         STOCK_2=NVDA,ma_cross,1d
    """
    configs = []

    # 尝试读取详细的每股配置
    i = 0
    while True:
        entry = os.getenv(f"STOCK_{i}")
        if not entry:
            break
        parts = [p.strip() for p in entry.split(",")]
        if len(parts) >= 3:
            symbol, strategy_name, interval = parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            symbol, strategy_name = parts[0], parts[1]
            interval = os.getenv("INTERVAL", "15m")
        else:
            symbol = parts[0]
            strategy_name = os.getenv("DEFAULT_STRATEGY", "macd_rsi")
            interval = os.getenv("INTERVAL", "15m")

        configs.append({
            "symbol": symbol.upper(),
            "strategy": strategy_name,
            "interval": interval,
        })
        i += 1

    # 回退到简单格式
    if not configs:
        symbols_env = os.getenv("SYMBOLS", os.getenv("SYMBOL", "AAPL"))
        symbols = [s.strip().upper() for s in symbols_env.split(",") if s.strip()]
        default_strategy = os.getenv("DEFAULT_STRATEGY", "macd_rsi")
        default_interval = os.getenv("INTERVAL", "15m")
        for sym in symbols:
            configs.append({
                "symbol": sym,
                "strategy": default_strategy,
                "interval": default_interval,
            })

    return configs


def interval_to_minutes(interval: str) -> int:
    """将 interval 字符串转换为分钟数（用于调度器）"""
    mapping = {
        "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
        "60m": 60, "1h": 60, "90m": 90, "1d": 1440,
    }
    return mapping.get(interval, 15)


# ── 核心监控任务 ──────────────────────────────────────────────
def monitor_job(symbol: str, strategy_name: str, interval: str,
                fetcher: DataFetcher, engine: StrategyEngine, notifier: BarkNotifier):
    """
    单个股票的完整监控流程：
    拉取数据 -> 计算指标 -> 检查信号 -> 推送 Bark
    """
    logger.info(f"❤️  心跳 | {symbol} | 策略={strategy_name} | 周期={interval}")

    try:
        # 1. 拉取最新 K 线
        df = fetcher.fetch(symbol, interval=interval, period="5d")
        if df is None or df.empty:
            logger.warning(f"[{symbol}] 数据为空，跳过本次检查")
            return
        logger.info(f"[{symbol}] 获取 {len(df)} 条 K 线，最新时间: {df.index[-1]}")

        # 2. 计算技术指标
        df = engine.add_indicators(df, strategy_name)

        # 3. 检查信号
        triggered, description = engine.check_signals(df, strategy_name, symbol)

        if triggered:
            logger.warning(f"[{symbol}] 触发信号: {description}")
            # 4. Bark 推送
            notifier.send(title=f"🚨 美股盯盘报警 [{symbol}]", body=description)
        else:
            logger.info(f"[{symbol}] 无异常信号")

    except Exception as exc:
        logger.error(f"[{symbol}] 监控任务异常: {exc}", exc_info=True)


# ── 主函数 ────────────────────────────────────────────────────
def main():
    logger.info("=" * 60)
    logger.info("美股盯盘系统启动")
    logger.info(f"可用策略: {list(STRATEGY_REGISTRY.keys())}")

    # 初始化公共组件
    fetcher = DataFetcher()
    engine = StrategyEngine()
    notifier = BarkNotifier(bark_url=os.getenv("BARK_URL", ""))

    # 解析每支股票的配置
    stock_configs = parse_stock_configs()
    if not stock_configs:
        logger.error("未找到任何股票配置，请检查 .env 中的 SYMBOLS / STOCK_0 等变量")
        return

    logger.info(f"监控股票列表（共 {len(stock_configs)} 支）:")
    for cfg in stock_configs:
        logger.info(f"  {cfg['symbol']} | 策略={cfg['strategy']} | 周期={cfg['interval']}")

    # 创建调度器
    scheduler = BlockingScheduler(timezone="America/New_York")

    for cfg in stock_configs:
        symbol = cfg["symbol"]
        strategy_name = cfg["strategy"]
        interval = cfg["interval"]
        minutes = interval_to_minutes(interval)

        scheduler.add_job(
            monitor_job,
            trigger=IntervalTrigger(minutes=minutes),
            kwargs={
                "symbol": symbol,
                "strategy_name": strategy_name,
                "interval": interval,
                "fetcher": fetcher,
                "engine": engine,
                "notifier": notifier,
            },
            id=f"monitor_{symbol}",
            name=f"监控 {symbol}",
            misfire_grace_time=60,
            max_instances=1,
        )
        logger.info(f"已注册任务: {symbol} 每 {minutes} 分钟执行一次")

    # 启动时立即执行一次全部任务
    logger.info("启动时立即执行一次全量检查...")
    for cfg in stock_configs:
        monitor_job(
            symbol=cfg["symbol"],
            strategy_name=cfg["strategy"],
            interval=cfg["interval"],
            fetcher=fetcher,
            engine=engine,
            notifier=notifier,
        )

    logger.info("调度器启动，按 Ctrl+C 退出")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止，程序退出")


if __name__ == "__main__":
    main()
