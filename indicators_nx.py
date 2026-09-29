"""
indicators_nx.py - NX牛熊双通道 + MACD波段背离识别
=====================================
工程规范：
  - 所有时间戳日志统一使用毫秒 (ms) 输出
  - 支持多周期 K 线（interval 字符串作为参数传入）
  - DataFrame 列名规范与主工程一致：Open / High / Low / Close / Volume
"""

import numpy as np
import pandas as pd
import logging
from typing import Optional, Tuple, List

from intervals import ms_of

logger = logging.getLogger(__name__)

def _interval_to_ms(interval: str) -> int:
    """将周期字符串转为毫秒，未知周期返回 -1（周期表见 intervals.py）"""
    return ms_of(interval)


def _ts_ms(ts) -> int:
    """将 pandas Timestamp 转为毫秒整数（兼容 tz-aware）"""
    if hasattr(ts, "value"):
        return ts.value // 1_000_000
    return int(pd.Timestamp(ts).timestamp() * 1000)


# ─────────────────────────────────────────────────────────────────
# 一、NX 牛熊双通道
# ─────────────────────────────────────────────────────────────────

def calc_nx_channel(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算 NX 牛熊双通道（向量化）。

    新增列：
        nx_A   : 短中线通道上沿 = EMA(High, 24)
        nx_B   : 短中线通道下沿 = EMA(Low,  23)
        nx_A1  : 长线通道上沿   = EMA(High, 89)
        nx_B1  : 长线通道下沿   = EMA(Low,  90)

    Args:
        df: 标准 K 线 DataFrame，必须含 High / Low 列，索引为 DatetimeIndex。

    Returns:
        含新列的 DataFrame（浅拷贝）。
    """
    if df is None or df.empty:
        logger.warning("calc_nx_channel: 输入 DataFrame 为空，跳过")
        return df

    required = {"High", "Low"}
    if not required.issubset(df.columns):
        raise ValueError(f"calc_nx_channel: DataFrame 缺少必要列 {required - set(df.columns)}")

    df = df.copy()

    # 短中线通道
    df["nx_A"]  = df["High"].ewm(span=24, adjust=False).mean()
    df["nx_B"]  = df["Low"].ewm(span=23,  adjust=False).mean()

    # 长线通道
    df["nx_A1"] = df["High"].ewm(span=89, adjust=False).mean()
    df["nx_B1"] = df["Low"].ewm(span=90,  adjust=False).mean()

    # 日志（毫秒时间戳）
    if len(df) > 0:
        latest_ts_ms = _ts_ms(df.index[-1])
        last = df.iloc[-1]
        logger.debug(
            f"[NX通道] ts_ms={latest_ts_ms} "
            f"A={last['nx_A']:.4f} B={last['nx_B']:.4f} "
            f"A1={last['nx_A1']:.4f} B1={last['nx_B1']:.4f}"
        )

    return df


def nx_channel_trend(df: pd.DataFrame) -> Optional[str]:
    """
    判断当前大趋势方向。

    Returns:
        "bull"  : 短中线通道整体在长线通道上方（A > A1 且 B > B1）
        "bear"  : 短中线通道整体在长线通道下方
        "mixed" : 混合/震荡
        None    : 数据不足
    """
    cols = ["nx_A", "nx_B", "nx_A1", "nx_B1"]
    if not all(c in df.columns for c in cols):
        return None

    row = df.iloc[-1]
    if any(pd.isna(row[c]) for c in cols):
        return None

    if row["nx_A"] > row["nx_A1"] and row["nx_B"] > row["nx_B1"]:
        return "bull"
    if row["nx_A"] < row["nx_A1"] and row["nx_B"] < row["nx_B1"]:
        return "bear"
    return "mixed"


# ─────────────────────────────────────────────────────────────────
# 二、MACD 波段背离识别
# ─────────────────────────────────────────────────────────────────

def calc_macd_divergence(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    interval: str = "15m",
) -> pd.DataFrame:
    """
    计算标准 MACD 并标记波段背离信号。

    新增列：
        macd_diff      : MACD 线 (DIF)
        macd_signal    : 信号线 (DEA)
        macd_hist      : 柱状图 (MACD bar = diff - signal)
        div_phase      : 当前波段类型，"pos"（正波段）/ "neg"（负波段）
        div_bull       : bool，当前 K 线确认底背离（做多参考）
        div_bear       : bool，当前 K 线确认顶背离（减仓参考）

    Args:
        df       : 标准 K 线 DataFrame（含 Close / High / Low）。
        fast     : MACD 快线周期，默认 12。
        slow     : MACD 慢线周期，默认 26。
        signal   : 信号线周期，默认 9。
        interval : K 线周期字符串，用于日志毫秒换算。

    Returns:
        含新列的 DataFrame（浅拷贝）。
    """
    if df is None or df.empty:
        logger.warning("calc_macd_divergence: 输入 DataFrame 为空，跳过")
        return df

    required = {"Close", "High", "Low"}
    if not required.issubset(df.columns):
        raise ValueError(f"calc_macd_divergence: DataFrame 缺少必要列 {required - set(df.columns)}")

    interval_ms = _interval_to_ms(interval)
    df = df.copy()

    # ── 1. 计算 MACD ──────────────────────────────────────────────
    ema_fast   = df["Close"].ewm(span=fast,   adjust=False).mean()
    ema_slow   = df["Close"].ewm(span=slow,   adjust=False).mean()
    df["macd_diff"]   = ema_fast - ema_slow
    df["macd_signal"] = df["macd_diff"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"]   = df["macd_diff"] - df["macd_signal"]

    # ── 2. 划分正负波段 ───────────────────────────────────────────
    # 波段以 hist 穿越 0 轴为分界
    hist = df["macd_hist"].values
    n = len(hist)
    phases = np.where(hist >= 0, "pos", "neg")      # 初始化
    df["div_phase"] = phases

    # ── 3. 提取历史波段极值 ───────────────────────────────────────
    # 扫描所有完整波段（不含最后一个未完成波段），记录极值
    segments = _split_segments(hist)                 # 返回 [(start_idx, end_idx, phase_type), ...]

    # ── 4. 计算背离（向量化标记到最后一列） ───────────────────────
    bull_signals = np.zeros(n, dtype=bool)
    bear_signals = np.zeros(n, dtype=bool)

    low_vals  = df["Low"].values
    high_vals = df["High"].values
    diff_vals = df["macd_diff"].values

    # 需要至少两个同相波段才能比较背离
    neg_segs = [(s, e) for s, e, p in segments if p == "neg"]
    pos_segs = [(s, e) for s, e, p in segments if p == "pos"]

    # 底背离：连续两个负波段比较
    for i in range(1, len(neg_segs)):
        s0, e0 = neg_segs[i - 1]
        s1, e1 = neg_segs[i]

        price_low_prev = low_vals[s0: e0 + 1].min()
        price_low_curr = low_vals[s1: e1 + 1].min()
        diff_low_prev  = diff_vals[s0: e0 + 1].min()
        diff_low_curr  = diff_vals[s1: e1 + 1].min()

        # 底背离判定
        if price_low_curr < price_low_prev and diff_low_curr > diff_low_prev:
            # 信号标记在波段结束 K 线（即 hist 穿越 0 那根）
            confirm_idx = min(e1 + 1, n - 1)   # 穿越 0 的下一根确认
            bull_signals[confirm_idx] = True
            ts_ms = _ts_ms(df.index[confirm_idx])
            logger.info(
                f"[MACD背离] 底背离确认 ts_ms={ts_ms} interval_ms={interval_ms} "
                f"price_low: {price_low_prev:.4f} -> {price_low_curr:.4f} | "
                f"diff_low: {diff_low_prev:.6f} -> {diff_low_curr:.6f}"
            )

    # 顶背离：连续两个正波段比较
    for i in range(1, len(pos_segs)):
        s0, e0 = pos_segs[i - 1]
        s1, e1 = pos_segs[i]

        price_high_prev = high_vals[s0: e0 + 1].max()
        price_high_curr = high_vals[s1: e1 + 1].max()
        diff_high_prev  = diff_vals[s0: e0 + 1].max()
        diff_high_curr  = diff_vals[s1: e1 + 1].max()

        # 顶背离判定
        if price_high_curr > price_high_prev and diff_high_curr < diff_high_prev:
            confirm_idx = min(e1 + 1, n - 1)
            bear_signals[confirm_idx] = True
            ts_ms = _ts_ms(df.index[confirm_idx])
            logger.info(
                f"[MACD背离] 顶背离确认 ts_ms={ts_ms} interval_ms={interval_ms} "
                f"price_high: {price_high_prev:.4f} -> {price_high_curr:.4f} | "
                f"diff_high: {diff_high_prev:.6f} -> {diff_high_curr:.6f}"
            )

    df["div_bull"] = bull_signals
    df["div_bear"] = bear_signals

    return df


def _split_segments(hist: np.ndarray) -> List[Tuple[int, int, str]]:
    """
    将 MACD 柱状图按 0 轴穿越分割为完整波段。

    Args:
        hist: MACD histogram 数组。

    Returns:
        [(start_idx, end_idx, phase_type), ...]
        phase_type: "pos" 或 "neg"
        仅返回已**完整结束**（在数组中有穿越点）的波段。
    """
    segments = []
    if len(hist) == 0:
        return segments

    current_phase = "pos" if hist[0] >= 0 else "neg"
    start = 0

    for i in range(1, len(hist)):
        prev_sign = hist[i - 1] >= 0
        curr_sign = hist[i] >= 0

        if prev_sign != curr_sign:
            # 穿越 0 轴：前一根为波段末尾
            segments.append((start, i - 1, current_phase))
            current_phase = "pos" if curr_sign else "neg"
            start = i

    # 最后一个波段可能是未完成的，不纳入背离计算
    # （如需包含当前未完成波段做实时判断，可以在调用层另行处理）
    return segments


# ─────────────────────────────────────────────────────────────────
# 三、便捷组合接口：同时计算双通道 + 背离
# ─────────────────────────────────────────────────────────────────

def add_nx_indicators(df: pd.DataFrame, interval: str = "15m") -> pd.DataFrame:
    """
    一次性为 DataFrame 添加 NX 通道和 MACD 背离所有列。

    Args:
        df       : 标准 K 线 DataFrame。
        interval : K 线周期字符串（用于日志 ms 输出）。

    Returns:
        含所有 NX 指标列的 DataFrame。
    """
    interval_ms = _interval_to_ms(interval)
    logger.debug(f"[add_nx_indicators] interval={interval} ({interval_ms} ms), bars={len(df)}")

    df = calc_nx_channel(df)
    df = calc_macd_divergence(df, interval=interval)
    return df


def get_latest_divergence_status(df: pd.DataFrame) -> dict:
    """
    获取最新 K 线的背离状态字典，便于在监控任务中直接查询。

    Returns:
        {
          "has_bull_div"   : bool,   # 最新 K 线是否触发底背离
          "has_bear_div"   : bool,   # 最新 K 线是否触发顶背离
          "recent_bull_div": bool,   # 近 15 根内是否有底背离
          "ts_ms"          : int,    # 最新 K 线时间戳（毫秒）
        }
    """
    result = {
        "has_bull_div":    False,
        "has_bear_div":    False,
        "recent_bull_div": False,
        "ts_ms":           0,
    }
    if df is None or df.empty:
        return result
    if "div_bull" not in df.columns or "div_bear" not in df.columns:
        return result

    last = df.iloc[-1]
    result["has_bull_div"] = bool(last.get("div_bull", False))
    result["has_bear_div"] = bool(last.get("div_bear", False))
    result["ts_ms"]        = _ts_ms(df.index[-1])

    # 近 15 根内是否存在底背离
    lookback = df.tail(15)
    result["recent_bull_div"] = bool(lookback["div_bull"].any())

    return result
