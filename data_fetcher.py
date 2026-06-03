"""
数据层：使用 yfinance 获取美股历史 K 线数据
"""

import logging
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# yfinance 支持的 interval 对应的合理历史数据范围
INTERVAL_PERIOD_MAP = {
    "1m":  "7d",
    "2m":  "60d",
    "5m":  "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "1h":  "730d",
    "1d":  "5y",
    "1wk": "10y",
    "1mo": "max",
}


class DataFetcher:
    """
    美股数据获取器
    封装 yfinance，提供统一的 K 线数据接口
    """

    def __init__(self, symbol: str):
        """
        初始化数据获取器

        :param symbol: 股票代码，例如 'AAPL', 'TSLA', 'SPY'
        """
        self.symbol = symbol.upper()

    def fetch(self, interval: str = "15m", period: Optional[str] = None) -> pd.DataFrame:
        """
        获取指定时间框架的历史 K 线数据

        :param interval: K 线周期，如 '1m', '15m', '1h', '1d'
        :param period:   数据回溯时长，不传则根据 interval 自动选择
        :return:         标准化 DataFrame（包含 Open/High/Low/Close/Volume）
        :raises ValueError: 当 interval 不受支持时
        :raises RuntimeError: 当数据获取失败时
        """
        if interval not in INTERVAL_PERIOD_MAP:
            raise ValueError(
                f"不支持的 interval: {interval}，"
                f"可选值为: {list(INTERVAL_PERIOD_MAP.keys())}"
            )

        period = period or INTERVAL_PERIOD_MAP[interval]
        logger.info(f"[{self.symbol}] 正在获取 {interval} K 线，回溯周期: {period}")

        try:
            ticker = yf.Ticker(self.symbol)
            df = ticker.history(period=period, interval=interval, auto_adjust=True)

            if df is None or df.empty:
                raise RuntimeError(
                    f"[{self.symbol}] 获取数据为空，请检查股票代码或网络连接"
                )

            # 标准化列名（yfinance 默认已是首字母大写，这里做防御处理）
            df = self._normalize_columns(df)

            # 处理时区：统一转换为美东时间（ET）
            df = self._normalize_timezone(df)

            # 去除含有 NaN 的行
            df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

            logger.info(
                f"[{self.symbol}] 数据获取成功，共 {len(df)} 条，"
                f"最新时间: {df.index[-1]}"
            )
            return df

        except Exception as e:
            logger.error(f"[{self.symbol}] 数据获取失败: {e}", exc_info=True)
            raise RuntimeError(f"数据获取失败: {e}") from e

    def fetch_latest(self, interval: str = "15m", n: int = 100) -> pd.DataFrame:
        """
        获取最近 n 条 K 线（用于实时监控，只取近期数据）

        :param interval: K 线周期
        :param n:        需要的 K 线数量
        :return:         DataFrame
        """
        df = self.fetch(interval=interval)
        return df.tail(n)

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """标准化列名，确保包含必要字段"""
        rename_map = {col: col.capitalize() for col in df.columns}
        df = df.rename(columns=rename_map)

        required_cols = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise RuntimeError(f"数据缺少必要字段: {missing}")

        return df[required_cols]

    @staticmethod
    def _normalize_timezone(df: pd.DataFrame) -> pd.DataFrame:
        """
        统一时区处理：
        - 若索引已有时区信息，转换为美东时间（America/New_York）
        - 若无时区，默认视为 UTC 并转换为美东时间
        """
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert("America/New_York")
        return df
