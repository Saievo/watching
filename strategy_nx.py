"""
strategy_nx.py - NX牛熊双通道 + MACD波段背离 策略模块
=====================================================
集成到现有 StrategyEngine 的扩展策略，支持两种方案切换。

工程规范：
  - 时间戳日志统一使用毫秒 (ms)
  - 支持多周期 K 线（interval 作为参数传入）
  - DataFrame 列名规范：Open / High / Low / Close / Volume

使用方法：
    from strategy_nx import NXStrategyEngine
    engine = NXStrategyEngine()
    df = engine.add_indicators(df, interval="15m")
    triggered, description = engine.check_signals(df, symbol="AAPL", scheme_type=1)
"""

import pandas as pd
import logging
from typing import Tuple

# 导入指标模块
from indicators_nx import (
    calc_nx_channel,
    calc_macd_divergence,
)
from intervals import ms_of

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# NX 策略引擎
# ─────────────────────────────────────────────────────────────────

class NXStrategyEngine:
    """
    NX牛熊双通道 + MACD波段背离 策略引擎。

    支持通过 scheme_type 参数切换两种做多方案：
      - scheme_type=1 : 右侧突破确认模型（突破通道上沿 + 近期底背离）
      - scheme_type=2 : 左侧精准狙击模型（回调至通道下沿 + 同步底背离）

    平仓/减仓逻辑两种方案共用：
      - 顶背离出现 → 减仓 50%（信号 "reduce_half"）
      - 收盘 < 短中线通道下沿 → 全部清仓（信号 "close_all"）
      - 收盘 < 长线通道下沿 → 绝对止损（信号 "stop_loss"）
    """

    # 方案一中：回溯近 N 根 K 线内是否有底背离
    LOOKBACK_BARS: int = 15

    def add_indicators(self, df: pd.DataFrame, interval: str = "15m") -> pd.DataFrame:
        """
        为 DataFrame 添加 NX 通道 + MACD 背离指标列。

        Args:
            df:       标准 K 线 DataFrame（含 High / Low / Close）
            interval: K 线周期字符串，如 '15m', '1h', '4h'

        Returns:
            含所有新指标列的 DataFrame（浅拷贝）
        """
        if df is None or df.empty:
            return df

        interval_ms = ms_of(interval, 900_000)
        ts_latest_ms = int(df.index[-1].timestamp() * 1000)
        logger.debug(
            f"[NX指标] 计算开始 | 周期={interval}({interval_ms}ms) "
            f"| 最新K线时间戳={ts_latest_ms}ms | 共{len(df)}根K线"
        )

        df = calc_nx_channel(df)
        df = calc_macd_divergence(df)

        logger.debug(f"[NX指标] 计算完成 | 新增列: nx_A, nx_B, nx_A1, nx_B1, div_bull, div_bear")
        return df

    def check_signals(
        self,
        df: pd.DataFrame,
        symbol: str = "",
        scheme_type: int = 1,
        interval: str = "15m",
    ) -> Tuple[bool, str]:
        """
        检查当前最新 K 线是否触发 NX 策略信号。

        Args:
            df:          含指标的 DataFrame（须先调用 add_indicators）
            symbol:      股票代码（用于日志）
            scheme_type: 1=右侧突破确认模型  2=左侧精准狙击模型
            interval:    K 线周期（用于时间戳日志）

        Returns:
            (triggered: bool, description: str)
              description 为信号描述字符串，多信号换行拼接。
        """
        if df is None or len(df) < 91:
            logger.warning(
                f"[{symbol}] 数据不足（需 ≥91 根，当前 {len(df) if df is not None else 0} 根），跳过信号检查"
            )
            return False, ""

        # 确保指标列已存在
        required_cols = ["nx_A", "nx_B", "nx_A1", "nx_B1", "div_bull", "div_bear"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            logger.error(f"[{symbol}] 缺少指标列: {missing}，请先调用 add_indicators()")
            return False, ""

        latest = df.iloc[-1]
        ts_ms = int(df.index[-1].timestamp() * 1000)

        close  = latest["Close"]
        nx_A   = latest["nx_A"]
        nx_B   = latest["nx_B"]
        nx_A1  = latest["nx_A1"]
        nx_B1  = latest["nx_B1"]

        signals: list[str] = []

        # ── 1. 大趋势多头前提 ──────────────────────────────────────
        bull_trend = (nx_A > nx_A1) and (nx_B > nx_B1)
        logger.debug(
            f"[{symbol}][{ts_ms}ms] 大趋势多头={bull_trend} | "
            f"A={nx_A:.4f} A1={nx_A1:.4f} B={nx_B:.4f} B1={nx_B1:.4f} Close={close:.4f}"
        )

        # ── 2. 做多信号 ────────────────────────────────────────────
        if bull_trend:
            if scheme_type == 1:
                signals += self._check_scheme1(df, symbol, close, nx_A, ts_ms)
            elif scheme_type == 2:
                signals += self._check_scheme2(df, symbol, close, nx_B, ts_ms)
            else:
                logger.warning(f"[{symbol}] 未知 scheme_type={scheme_type}，跳过做多检查")

        # ── 3. 平仓/减仓信号（不依赖大趋势）─────────────────────────
        signals += self._check_exit_signals(df, symbol, close, nx_B, nx_B1, ts_ms)

        if not signals:
            return False, ""

        description = (
            f"股票: {symbol} | 方案: scheme_{scheme_type} | 时间戳: {ts_ms}ms\n"
            + "\n".join(signals)
        )
        return True, description

    # ──────────────────────────────────────────────────────────────
    # 私有方法
    # ──────────────────────────────────────────────────────────────

    def _check_scheme1(
        self, df: pd.DataFrame, symbol: str,
        close: float, nx_A: float, ts_ms: int
    ) -> list[str]:
        """
        方案一：右侧突破确认模型。
        条件：过去 LOOKBACK_BARS 根内有底背离 AND 当前收盘 > 短中线通道上沿。
        """
        signals: list[str] = []

        # 近 N 根 K 线内是否出现过底背离
        lookback = min(self.LOOKBACK_BARS, len(df))
        recent_bull_div = df["div_bull"].iloc[-lookback:].any()

        # 收盘价突破短中线通道上沿
        breakout = close > nx_A

        logger.debug(
            f"[{symbol}][{ts_ms}ms] 方案1 | 近{lookback}根底背离={recent_bull_div} "
            f"| 突破上沿={breakout}(Close={close:.4f} > A={nx_A:.4f})"
        )

        if recent_bull_div and breakout:
            signals.append(
                f"🟢 [方案1-做多] 右侧突破确认 | "
                f"收盘{close:.4f}上穿通道上沿{nx_A:.4f} | "
                f"近{lookback}根K线内存在MACD底背离 | ts={ts_ms}ms"
            )
        return signals

    def _check_scheme2(
        self, df: pd.DataFrame, symbol: str,
        close: float, nx_B: float, ts_ms: int
    ) -> list[str]:
        """
        方案二：左侧精准狙击模型。
        条件：收盘 ≤ 短中线通道下沿 AND 当前 K 线触发底背离。
        """
        signals: list[str] = []

        # 当前 K 线刚好确认底背离
        current_bull_div = bool(df["div_bull"].iloc[-1])

        # 价格回调至通道下沿附近或跌破
        at_lower = close <= nx_B

        logger.debug(
            f"[{symbol}][{ts_ms}ms] 方案2 | 当前底背离={current_bull_div} "
            f"| 回调下沿={at_lower}(Close={close:.4f} <= B={nx_B:.4f})"
        )

        if at_lower and current_bull_div:
            signals.append(
                f"🟢 [方案2-做多] 左侧狙击 | "
                f"收盘{close:.4f}≤通道下沿{nx_B:.4f} | "
                f"当前K线确认MACD底背离 | ts={ts_ms}ms"
            )
        return signals

    def _check_exit_signals(
        self,
        df: pd.DataFrame,
        symbol: str,
        close: float,
        nx_B: float,
        nx_B1: float,
        ts_ms: int,
    ) -> list[str]:
        """
        统一平仓/减仓逻辑（两种方案共用）。

        优先级（由高到低）：
          1. 绝对止损：Close < 长线通道下沿 B1
          2. 清仓：Close < 短中线通道下沿 B
          3. 减仓50%：当前 K 线出现 MACD 顶背离
        """
        signals: list[str] = []

        current_bear_div = bool(df["div_bear"].iloc[-1])

        logger.debug(
            f"[{symbol}][{ts_ms}ms] 平仓检查 | Close={close:.4f} "
            f"B={nx_B:.4f} B1={nx_B1:.4f} | 顶背离={current_bear_div}"
        )

        # 绝对止损（优先级最高，直接返回不叠加其他信号）
        if close < nx_B1:
            signals.append(
                f"🔴 [止损] 收盘{close:.4f}跌破长线通道下沿{nx_B1:.4f} | "
                f"action=stop_loss | ts={ts_ms}ms"
            )
            return signals  # 止损直接返回，不再附加其他信号

        # 全部清仓
        if close < nx_B:
            signals.append(
                f"🟠 [清仓] 收盘{close:.4f}跌破短中线通道下沿{nx_B:.4f} | "
                f"action=close_all | ts={ts_ms}ms"
            )

        # 减仓50%（顶背离，与清仓信号可能同时出现）
        if current_bear_div:
            signals.append(
                f"🟡 [减仓50%] 当前K线出现MACD顶背离 | "
                f"action=reduce_half | ts={ts_ms}ms"
            )

        return signals


# ─────────────────────────────────────────────────────────────────
# 集成到现有 StrategyEngine 的注册入口
# ─────────────────────────────────────────────────────────────────

# 新增策略注册信息，可直接 merge 到 strategy.py 中的 STRATEGY_REGISTRY
NX_STRATEGY_CONFIGS = {
    "nx_scheme1": {
        "enabled_signals": ["nx_channel", "macd_divergence"],
        "scheme_type": 1,
        "description": "NX牛熊通道+MACD背离 - 右侧突破确认模型",
    },
    "nx_scheme2": {
        "enabled_signals": ["nx_channel", "macd_divergence"],
        "scheme_type": 2,
        "description": "NX牛熊通道+MACD背离 - 左侧精准狙击模型",
    },
}


def integrate_with_main_engine(strategy_engine, df: pd.DataFrame, strategy_name: str, symbol: str, interval: str) -> Tuple[bool, str]:
    """
    适配器函数：将 NXStrategyEngine 无缝集成到现有 main.py 的调度流程。

    在 main.py 的 monitor_job 中，当 strategy_name 为 'nx_scheme1' 或 'nx_scheme2' 时，
    替换原有 engine.add_indicators / engine.check_signals 调用。

    示例（main.py 中修改 monitor_job）：
        from strategy_nx import integrate_with_main_engine, NX_STRATEGY_CONFIGS
        # 在 strategy.py 中注册：
        # STRATEGY_REGISTRY.update(NX_STRATEGY_CONFIGS)

        # monitor_job 中：
        if strategy_name.startswith("nx_"):
            triggered, description = integrate_with_main_engine(engine, df, strategy_name, symbol, interval)
        else:
            df = engine.add_indicators(df, strategy_name)
            triggered, description = engine.check_signals(df, strategy_name, symbol)

    Args:
        strategy_engine: 现有 StrategyEngine 实例（本函数不使用，仅保持接口一致）
        df:              原始 K 线 DataFrame
        strategy_name:   策略名称（'nx_scheme1' 或 'nx_scheme2'）
        symbol:          股票代码
        interval:        K 线周期字符串

    Returns:
        (triggered: bool, description: str)
    """
    config = NX_STRATEGY_CONFIGS.get(strategy_name, {})
    scheme_type = config.get("scheme_type", 1)

    nx_engine = NXStrategyEngine()
    df = nx_engine.add_indicators(df, interval=interval)
    return nx_engine.check_signals(df, symbol=symbol, scheme_type=scheme_type, interval=interval)
