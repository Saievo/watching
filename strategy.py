"""
strategy.py - 策略与回测层
支持每个股票配置不同的盯盘策略，通过策略注册表实现插件化扩展
"""

import pandas as pd
import pandas_ta as ta
import logging
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# 策略注册表：每种策略定义自己的指标参数和信号检查逻辑
# ──────────────────────────────────────────────────────────────
STRATEGY_REGISTRY: Dict[str, Dict[str, Any]] = {
    # MACD + RSI 综合策略（默认）
    "macd_rsi": {
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "enabled_signals": ["rsi", "macd"],
        "description": "MACD金叉死叉 + RSI超买超卖",
    },
    # 仅 RSI 策略（适合高波动标的如 TSLA）
    "rsi_only": {
        "rsi_period": 14,
        "rsi_overbought": 75,
        "rsi_oversold": 25,
        "enabled_signals": ["rsi"],
        "description": "仅 RSI 超买超卖",
    },
    # 均线交叉策略（适合趋势型标的如 SPY/QQQ）
    "ma_cross": {
        "ema_short": 9,
        "ema_long": 21,
        "enabled_signals": ["ema_cross"],
        "description": "EMA均线金叉死叉",
    },
    # TSLA 专属策略：敏感 RSI + 快速均线
    "tsla_aggressive": {
        "rsi_period": 14,
        "rsi_overbought": 72,
        "rsi_oversold": 28,
        "ema_short": 5,
        "ema_long": 20,
        "enabled_signals": ["rsi", "ema_cross"],
        "description": "TSLA激进策略: 敏感RSI + 快速EMA",
    },
    # NVDA 专属策略：快速 MACD
    "nvda_macd": {
        "macd_fast": 8,
        "macd_slow": 21,
        "macd_signal": 5,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "enabled_signals": ["macd", "rsi"],
        "description": "NVDA快速MACD + RSI",
    },
    # 全信号策略（同时检查 MACD + RSI + EMA）
    "full": {
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "ema_short": 9,
        "ema_long": 21,
        "enabled_signals": ["rsi", "macd", "ema_cross"],
        "description": "全信号策略: MACD + RSI + EMA",
    },
}

# 默认策略配置（用于补全缺失的字段）
_DEFAULT_CONFIG = {
    "rsi_period": 14,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "ema_short": 9,
    "ema_long": 21,
    "enabled_signals": ["rsi", "macd"],
}


def get_strategy_config(strategy_name: str) -> Dict[str, Any]:
    """从注册表获取策略配置，未找到则使用默认策略"""
    base = _DEFAULT_CONFIG.copy()
    if strategy_name in STRATEGY_REGISTRY:
        base.update(STRATEGY_REGISTRY[strategy_name])
        logger.debug(f"使用策略: {strategy_name} ({base.get('description', '')})")
    else:
        logger.warning(f"未找到策略 '{strategy_name}'，使用默认配置 (macd_rsi)")
        base.update(STRATEGY_REGISTRY.get("macd_rsi", {}))
    return base


# ──────────────────────────────────────────────────────────────
# 单项信号检查函数
# ──────────────────────────────────────────────────────────────

def _check_rsi(df: pd.DataFrame, config: Dict) -> Optional[str]:
    """检查 RSI 超买/超卖信号"""
    col = f"RSI_{config['rsi_period']}"
    if col not in df.columns:
        return None
    val = df[col].iloc[-1]
    if pd.isna(val):
        return None
    ob, os_ = config["rsi_overbought"], config["rsi_oversold"]
    if val >= ob:
        return f"RSI 超买 ({val:.1f} ≥ {ob})"
    if val <= os_:
        return f"RSI 超卖 ({val:.1f} ≤ {os_})"
    return None


def _check_macd(df: pd.DataFrame, config: Dict) -> Optional[str]:
    """检查 MACD 金叉/死叉（柱状图由负转正或由正转负）"""
    f, s, sig = config["macd_fast"], config["macd_slow"], config["macd_signal"]
    hist_col = f"MACDh_{f}_{s}_{sig}"
    macd_col = f"MACD_{f}_{s}_{sig}"
    if hist_col not in df.columns or len(df) < 2:
        return None
    prev, curr = df[hist_col].iloc[-2], df[hist_col].iloc[-1]
    if pd.isna(prev) or pd.isna(curr):
        return None
    macd_val = df[macd_col].iloc[-1] if macd_col in df.columns else float("nan")
    if prev < 0 and curr >= 0:
        return f"MACD 金叉 (柱转正, MACD={macd_val:.4f})"
    if prev > 0 and curr <= 0:
        return f"MACD 死叉 (柱转负, MACD={macd_val:.4f})"
    return None


def _check_ema_cross(df: pd.DataFrame, config: Dict) -> Optional[str]:
    """检查 EMA 均线金叉/死叉"""
    sc = f"EMA_{config['ema_short']}"
    lc = f"EMA_{config['ema_long']}"
    if sc not in df.columns or lc not in df.columns or len(df) < 2:
        return None
    ps, pl = df[sc].iloc[-2], df[lc].iloc[-2]
    cs, cl = df[sc].iloc[-1], df[lc].iloc[-1]
    if any(pd.isna(v) for v in [ps, pl, cs, cl]):
        return None
    if ps <= pl and cs > cl:
        return f"EMA 金叉 (EMA{config['ema_short']} 上穿 EMA{config['ema_long']}, 当前={cs:.2f}/{cl:.2f})"
    if ps >= pl and cs < cl:
        return f"EMA 死叉 (EMA{config['ema_short']} 下穿 EMA{config['ema_long']}, 当前={cs:.2f}/{cl:.2f})"
    return None


_SIGNAL_CHECKERS = {
    "rsi": _check_rsi,
    "macd": _check_macd,
    "ema_cross": _check_ema_cross,
}


# ──────────────────────────────────────────────────────────────
# StrategyEngine：统一的策略引擎入口
# ──────────────────────────────────────────────────────────────

class StrategyEngine:
    """策略引擎：根据策略名称计算指标并检查信号"""

    def add_indicators(self, df: pd.DataFrame, strategy_name: str) -> pd.DataFrame:
        """根据策略配置为 DataFrame 添加技术指标"""
        if df is None or df.empty:
            return df

        config = get_strategy_config(strategy_name)
        enabled = config.get("enabled_signals", [])
        df = df.copy()

        try:
            if "rsi" in enabled:
                df.ta.rsi(length=config["rsi_period"], append=True)

            if "macd" in enabled:
                df.ta.macd(
                    fast=config["macd_fast"],
                    slow=config["macd_slow"],
                    signal=config["macd_signal"],
                    append=True,
                )

            if "ema_cross" in enabled:
                df.ta.ema(length=config["ema_short"], append=True)
                df.ta.ema(length=config["ema_long"], append=True)

            logger.debug(f"[策略={strategy_name}] 指标计算完成，启用: {enabled}")
        except Exception as e:
            logger.error(f"[策略={strategy_name}] 指标计算失败: {e}")

        return df

    def check_signals(
        self, df: pd.DataFrame, strategy_name: str, symbol: str = ""
    ) -> Tuple[bool, str]:
        """
        检查最新 K 线是否触发信号。

        Returns:
            (triggered: bool, description: str)
        """
        if df is None or df.empty or len(df) < 2:
            return False, ""

        config = get_strategy_config(strategy_name)
        enabled = config.get("enabled_signals", [])
        fired: List[str] = []

        for sig_type in enabled:
            checker = _SIGNAL_CHECKERS.get(sig_type)
            if checker:
                result = checker(df, config)
                if result:
                    fired.append(result)
                    logger.info(f"[{symbol}][策略={strategy_name}] 触发: {result}")

        if not fired:
            return False, ""

        # 组合多个信号描述
        close_price = df["Close"].iloc[-1]
        ts = str(df.index[-1])
        body = "\n".join(fired)
        description = (
            f"股票: {symbol}\n"
            f"策略: {strategy_name} ({config.get('description', '')})\n"
            f"时间: {ts}\n"
            f"收盘价: {close_price:.2f}\n"
            f"信号:\n{body}"
        )
        return True, description

    def run_backtest(self, df: pd.DataFrame, strategy_name: str, symbol: str = "") -> Dict[str, Any]:
        """
        简单回测：统计历史数据中策略信号触发频率。
        仅供评估策略参数，不作为实盘依据。
        """
        if df is None or df.empty:
            return {"error": "数据为空"}

        df_ind = self.add_indicators(df, strategy_name)
        config = get_strategy_config(strategy_name)
        total_signals = 0
        signal_log = []

        for i in range(2, len(df_ind)):
            sliced = df_ind.iloc[: i + 1]
            triggered, _ = self.check_signals(sliced, strategy_name, symbol)
            if triggered:
                total_signals += 1
                signal_log.append({
                    "time": str(df_ind.index[i]),
                    "close": float(df_ind["Close"].iloc[i]),
                })

        total_bars = len(df_ind)
        result = {
            "symbol": symbol,
            "strategy": strategy_name,
            "description": config.get("description", ""),
            "enabled_signals": config.get("enabled_signals", []),
            "total_bars": total_bars,
            "total_signals": total_signals,
            "signal_rate": f"{total_signals / total_bars * 100:.1f}%" if total_bars else "N/A",
            "last_5_signals": signal_log[-5:],
        }
        logger.info(
            f"[{symbol}][{strategy_name}] 回测完成: "
            f"{total_bars} 根K线, 触发信号 {total_signals} 次 ({result['signal_rate']})"
        )
        return result
