"""
market.py - 行情与指标数据层
================================
封装根目录 stock 项目的 data_fetcher / strategy / strategy_nx，
对外提供：
  - fetch_stock_data():  OHLCV + 常见指标 + 统计 + 回测摘要
  - snapshot():          收藏列表卡片用的轻量快照（带缓存）
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data_fetcher import DataFetcher  # noqa: E402
from strategy import StrategyEngine, STRATEGY_REGISTRY  # noqa: E402
from strategy_nx import NXStrategyEngine  # noqa: E402
from mrmc import calc_mrmc  # noqa: E402

# 行情/策略模块日志默认只保留 WARNING 以上，避免刷屏
for _name in ("data_fetcher", "strategy", "strategy_nx", "indicators_nx"):
    logging.getLogger(_name).setLevel(logging.WARNING)

logger = logging.getLogger("stock_web.market")


# ---------------------------------------------------------------- 基础配置

INTERVALS = ["1m", "5m", "15m", "30m", "90m", "1h", "2h", "3h", "4h", "1d", "1wk"]
PERIOD_MAP = {
    "1m": "7d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "90m": "60d",
    "1h": "730d",
    "2h": "730d",
    "3h": "730d",
    "4h": "730d",
    "1d": "5y",
    "1wk": "10y",
}

# yfinance 不支持 2h/3h（4h 也统一重采样保证对齐）：用 1h 数据聚合
_RESAMPLE_RULES = {"2h": "2h", "3h": "3h", "4h": "4h"}

_fetcher = DataFetcher()
_engine = StrategyEngine()
_nx_engine = NXStrategyEngine()

_snapshot_cache: Dict[str, Dict[str, Any]] = {}
_snapshot_lock = threading.Lock()
SNAPSHOT_TTL = 90  # 秒

# 行情 OHLCV 缓存：共振扫描会反复取 1h/30m/日线，避免每次重拉 yfinance
_ohlcv_cache: Dict[str, Dict[str, Any]] = {}
_ohlcv_lock = threading.RLock()
_OHLCV_TTL = 300  # 秒

# 1234 强共振指标缓存（多周期 MRMC 扫描较重，带 TTL）
_s1234_cache: Dict[str, Dict[str, Any]] = {}
_s1234_lock = threading.Lock()
S1234_TTL = 300  # 秒
_s1234_pending: set = set()  # 正在后台计算的 "<SYMBOL>:<窗口小时>"
_s1234_slots = threading.BoundedSemaphore(4)  # 后台最多同时算 4 只

# ---------------------------------------------------------------- 统一指标引擎
# 拿数据后异步把「所有指标」一次算好并缓存（日线全套 + 多周期MRMC + 1234共振），
# 共振只是其中一项结果；扫描/详情/收藏都消费同一份缓存。
_IND_TTL = 300
_ind_cache: Dict[str, Dict[str, Any]] = {}
_ind_pending: set = set()
_ind_lock = threading.RLock()
_ind_slots = threading.BoundedSemaphore(4)  # 后台最多同时算 4 只


def _compute_indicator_bundle(symbol: str) -> Dict[str, Any]:
    """一次取数，计算该股票的全部指标。"""
    symbol = symbol.upper().strip()
    bundle: Dict[str, Any] = {
        "symbol": symbol,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "daily": None,
        "resonance": None,
    }
    # 1) 日线全套指标（统计 / 信号 / 迷你走势）
    try:
        df = fetch_ohlcv(symbol, "1d", "120d")
        if df is not None and not df.empty:
            df = enrich_indicators(df, "1d")
            signals = _latest_signals(df, symbol, "1d")
            stats = _compute_stats(df, symbol, "1d")
            bundle["daily"] = {
                "stats": stats,
                "signals": {k: v for k, v in signals.items() if v.get("triggered")},
                "spark": [_row_to_float(c) for c in df["Close"].tail(30).tolist()],
            }
    except Exception:
        pass
    # 2) 多周期 MRMC + 1234 共振（内部一次拉取 1h 并重采样）
    try:
        bundle["resonance"] = calc_1234(symbol)
    except Exception:
        pass
    return bundle


def ensure_indicator_bundle(symbol: str, force: bool = False) -> bool:
    """确保指标缓存新鲜；未就绪则后台异步计算（不阻塞调用方）。返回是否已就绪。"""
    symbol = symbol.upper().strip()
    with _ind_lock:
        cached = _ind_cache.get(symbol)
        if cached and not force and time.time() - cached.get("_at", 0) < _IND_TTL:
            return True
        if symbol in _ind_pending:
            return False
        _ind_pending.add(symbol)

    def _worker():
        # Wait for a slot *inside* the worker. Acquiring on the caller's thread
        # would block whoever asked (e.g. a /api/stocks/{sym}/data request) even
        # though this function promises to be non-blocking.
        _ind_slots.acquire()
        try:
            bundle = _compute_indicator_bundle(symbol)
            bundle["_at"] = time.time()
            with _ind_lock:
                _ind_cache[symbol] = bundle
        except Exception as exc:
            logger.warning("[%s] 指标包计算失败: %s", symbol, exc, exc_info=True)
        finally:
            with _ind_lock:
                _ind_pending.discard(symbol)
            _ind_slots.release()

    threading.Thread(target=_worker, daemon=True).start()
    return False


def get_indicator_bundle(symbol: str) -> Optional[Dict[str, Any]]:
    """读取已算好的指标包；未缓存/过期返回 None。"""
    symbol = symbol.upper().strip()
    with _ind_lock:
        cached = _ind_cache.get(symbol)
        if cached and time.time() - cached.get("_at", 0) < _IND_TTL:
            return {k: v for k, v in cached.items() if not k.startswith("_")}
    return None


def prewarm_indicator_bundles(symbols: List[str]) -> None:
    """后台预热全部收藏的指标包。"""
    for sym in symbols:
        try:
            ensure_indicator_bundle(sym)
        except Exception:
            continue


def compute_indicator_bundle_sync(symbol: str) -> Dict[str, Any]:
    """同步计算并写入指标包（供自动共振扫描等场景直接使用）。"""
    symbol = symbol.upper().strip()
    bundle = _compute_indicator_bundle(symbol)
    bundle["_at"] = time.time()
    with _ind_lock:
        _ind_cache[symbol] = bundle
    return {k: v for k, v in bundle.items() if not k.startswith("_")}


# ---------------------------------------------------------------- 指标工具

def _row_to_float(value) -> Optional[float]:
    """NaN / None -> None，其余转 float。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 6)


def _series(df: pd.DataFrame, col: str, n: int) -> List[Optional[float]]:
    if col not in df.columns:
        return [None] * n
    return [_row_to_float(v) for v in df[col].tolist()][-n:]


def _latest(df: pd.DataFrame, col: str) -> Optional[float]:
    if col not in df.columns or df[col].empty:
        return None
    return _row_to_float(df[col].iloc[-1])


# ---------------------------------------------------------------- 数据获取

def fetch_ohlcv(symbol: str, interval: str = "1d", period: Optional[str] = None) -> pd.DataFrame:
    """获取标准 OHLCV DataFrame（含时区归一化，带 300s 缓存避免重复拉取）。"""
    period = period or PERIOD_MAP.get(interval, "1y")
    key = f"{symbol.upper().strip()}:{interval}:{period}"
    now = time.time()
    with _ohlcv_lock:
        cached = _ohlcv_cache.get(key)
        if cached and now - cached["_t"] < _OHLCV_TTL:
            return cached["df"].copy()
    if interval in _RESAMPLE_RULES:
        # 1h -> 2h/3h/4h 重采样
        base = _fetcher.fetch(symbol.upper(), interval="1h", period=period)
        if base is None or base.empty:
            return base
        df = _resample_ohlcv(base, _RESAMPLE_RULES[interval])
    else:
        df = _fetcher.fetch(symbol.upper(), interval=interval, period=period)
    with _ohlcv_lock:
        _ohlcv_cache[key] = {"df": df, "_t": now}
    return df


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """按规则聚合 OHLCV（右闭右标：K线时间戳为区间结束时刻）。"""
    agg = (
        df.resample(rule, closed="right", label="right")
        .agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        })
        .dropna(subset=["Open", "Close", "High", "Low"])
    )
    return agg


def enrich_indicators(df: pd.DataFrame, interval: str = "1d") -> pd.DataFrame:
    """追加全套常见指标：RSI / MACD / EMA + NX 牛熊通道。"""
    if df is None or df.empty:
        return df
    df = _engine.add_indicators(df, "full")
    df = _nx_engine.add_indicators(df, interval=interval)
    try:
        _mrmc = calc_mrmc(df["Close"].values)
        df["mrmc_buy"] = _mrmc["buy"]
        df["mrmc_sell"] = _mrmc["sell"]
    except Exception:
        df["mrmc_buy"] = False
        df["mrmc_sell"] = False
    return df


def _compute_stats(df: pd.DataFrame, symbol: str, interval: str) -> Dict[str, Any]:
    """从含指标的数据框计算快照统计。"""
    if df is None or df.empty:
        return {"error": "暂无数据"}

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    close = float(last["Close"])
    prev_close = float(prev["Close"]) if prev_close_nonzero(prev) else close

    high52 = low52 = None
    try:
        year_df = _fetcher.fetch(symbol, interval="1d", period="1y")
        if year_df is not None and not year_df.empty:
            high52 = float(year_df["High"].max())
            low52 = float(year_df["Low"].min())
    except Exception:
        pass

    change = (close - prev_close) / prev_close * 100 if prev_close else None
    day_high = float(last["High"]) if not pd.isna(last["High"]) else None
    day_low = float(last["Low"]) if not pd.isna(last["Low"]) else None
    volume = int(last["Volume"]) if not pd.isna(last["Volume"]) else None

    stats = {
        "symbol": symbol,
        "interval": interval,
        "ts": df.index[-1].isoformat(),
        "ts_ms": int(df.index[-1].timestamp() * 1000),
        "close": round(close, 4),
        "prev_close": round(prev_close, 4),
        "change": round(change, 4) if change is not None else None,
        "change_pct": round(change, 4) if change is not None else None,
        "open": _row_to_float(last.get("Open")),
        "day_high": round(day_high, 4) if day_high is not None else None,
        "day_low": round(day_low, 4) if day_low is not None else None,
        "volume": volume,
        "high_52w": round(high52, 4) if high52 is not None else None,
        "low_52w": round(low52, 4) if low52 is not None else None,
        "range_pos_52w": (
            round((close - low52) / (high52 - low52) * 100, 2)
            if high52 is not None and low52 is not None and high52 > low52
            else None
        ),
        "rsi": _latest(df, "RSI_14"),
        "macd": _latest(df, "MACD_12_26_9"),
        "macd_signal": _latest(df, "MACDs_12_26_9"),
        "macd_hist": _latest(df, "MACDh_12_26_9"),
        "ema9": _latest(df, "EMA_9"),
        "ema21": _latest(df, "EMA_21"),
        "nx_a": _latest(df, "nx_A"),
        "nx_b": _latest(df, "nx_B"),
    }
    return stats


def prev_close_nonzero(row) -> bool:
    try:
        return bool(row["Close"]) and not pd.isna(row["Close"])
    except Exception:
        return False


def _latest_signals(df: pd.DataFrame, symbol: str, interval: str) -> Dict[str, Any]:
    """返回最近信号与 NX 方案信号。"""
    out: Dict[str, Any] = {}
    if df is None or df.empty or len(df) < 2:
        return out

    try:
        triggered, desc = _engine.check_signals(df, "full", symbol)
        out["full"] = {"triggered": triggered, "description": desc}
    except Exception as exc:
        out["full"] = {"triggered": False, "description": f"检查失败: {exc}"}

    if len(df) >= 91:
        try:
            t1, d1 = _nx_engine.check_signals(df, symbol=symbol, scheme_type=1, interval=interval)
            out["nx_scheme1"] = {"triggered": t1, "description": d1}
        except Exception as exc:
            out["nx_scheme1"] = {"triggered": False, "description": f"检查失败: {exc}"}
        try:
            t2, d2 = _nx_engine.check_signals(df, symbol=symbol, scheme_type=2, interval=interval)
            out["nx_scheme2"] = {"triggered": t2, "description": d2}
        except Exception as exc:
            out["nx_scheme2"] = {"triggered": False, "description": f"检查失败: {exc}"}

    if "mrmc_buy" in df.columns and "mrmc_sell" in df.columns:
        latest_buy = bool(df["mrmc_buy"].iloc[-1])
        latest_sell = bool(df["mrmc_sell"].iloc[-1])
        ts = str(df.index[-1])
        close = float(df["Close"].iloc[-1])
        _note = "（MRMC 需按原版教程使用，建议结合 NX 指标；来源：富途公式，YouTube 搜“股票博主DAVID”）"
        out["mrmc_buy"] = {
            "triggered": latest_buy,
            "description": (
                f"股票: {symbol}\n指标: MRMC(买入卖出) - MACD底背离抄底\n"
                f"时间: {ts}\n收盘价: {close:.2f}\n信号: 抄底{_note}"
                if latest_buy
                else ""
            ),
        }
        out["mrmc_sell"] = {
            "triggered": latest_sell,
            "description": (
                f"股票: {symbol}\n指标: MRMC(买入卖出) - MACD顶背离卖出\n"
                f"时间: {ts}\n收盘价: {close:.2f}\n信号: 卖出{_note}"
                if latest_sell
                else ""
            ),
        }
    return out


def _backtest_summary(df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
    try:
        result = _engine.run_backtest(df, "full", symbol)
        return {k: v for k, v in result.items() if k != "last_5_signals"}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------- 对外 API

def fetch_stock_data(
    symbol: str,
    interval: str = "1d",
    period: Optional[str] = None,
    with_backtest: bool = True,
) -> Dict[str, Any]:
    """详情页数据：OHLCV + 指标序列 + 统计 + 信号 + 回测摘要。"""
    symbol = symbol.upper().strip()
    raw = fetch_ohlcv(symbol, interval, period)
    if raw is None or raw.empty:
        return {"error": f"未获取到 {symbol} 的数据，请检查代码或网络", "symbol": symbol}

    df = enrich_indicators(raw, interval)
    n = len(df)
    base_cols = ["Open", "High", "Low", "Close", "Volume"]
    ohlcv = [
        {
            "t": int(ts.timestamp() * 1000),
            "o": _row_to_float(row["Open"]),
            "h": _row_to_float(row["High"]),
            "l": _row_to_float(row["Low"]),
            "c": _row_to_float(row["Close"]),
            "v": _row_to_float(row["Volume"]),
        }
        for ts, row in df.iterrows()
    ]

    indicators = {
        "rsi": _series(df, "RSI_14", n),
        "macd": _series(df, "MACD_12_26_9", n),
        "macd_signal": _series(df, "MACDs_12_26_9", n),
        "macd_hist": _series(df, "MACDh_12_26_9", n),
        "ema9": _series(df, "EMA_9", n),
        "ema21": _series(df, "EMA_21", n),
        "nx_a": _series(df, "nx_A", n),
        "nx_b": _series(df, "nx_B", n),
        "nx_a1": _series(df, "nx_A1", n),
        "nx_b1": _series(df, "nx_B1", n),
        "div_bull": _series(df, "div_bull", n),
        "div_bear": _series(df, "div_bear", n),
        "mrmc_buy": _series(df, "mrmc_buy", n),
        "mrmc_sell": _series(df, "mrmc_sell", n),
    }

    payload = {
        "symbol": symbol,
        "interval": interval,
        "period": period or PERIOD_MAP.get(interval, "1y"),
        "rows": n,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "ohlcv": ohlcv,
        "indicators": indicators,
        "stats": _compute_stats(df, symbol, interval),
        "signals": _latest_signals(df, symbol, interval),
    }
    # 1234 强共振（统一指标包的一部分）：K线先返回，共振异步补算
    try:
        _bundle = get_indicator_bundle(symbol)
        if _bundle and _bundle.get("resonance"):
            s1234 = _bundle["resonance"]
            payload["indicator_1234"] = s1234
            if s1234.get("active"):
                tier_txt = "4/4 全周期共振（最强）" if s1234.get("tier") == "full" else "3/4 周期共振"
                payload["signals"]["1234"] = {
                    "triggered": True,
                    "description": (
                        f"股票: {symbol}\n指标: 1234 强共振\n"
                        f"信号: {tier_txt}，1h/2h/3h/4h 在 {s1234.get('window_hours', 24)} 小时内出现 MRMC 抄底，"
                        "多周期资金共振，级别强\n"
                        "建议: 结合 NX 通道与大盘确认后重点关注"
                    ),
                }
        else:
            ensure_indicator_bundle(symbol)
            payload["indicator_1234"] = None
            payload["indicator_1234_pending"] = True
    except Exception:
        pass
    if with_backtest:
        payload["backtest"] = _backtest_summary(df, symbol)
    return payload


def snapshot(symbol: str, refresh: bool = False, ttl: int = SNAPSHOT_TTL) -> Dict[str, Any]:
    """收藏列表卡片快照（带 TTL 缓存，优先消费统一指标包）。"""
    symbol = symbol.upper().strip()
    now = time.time()
    with _snapshot_lock:
        cached = _snapshot_cache.get(symbol)
        if cached and not refresh and now - cached.get("_fetched_at", 0) < ttl:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    try:
        bundle = get_indicator_bundle(symbol)
        if bundle and bundle.get("daily"):
            daily = bundle["daily"]
            payload = {
                **daily["stats"],
                "spark": daily["spark"],
                "signals": dict(daily["signals"]),
            }
            res = bundle.get("resonance") or {}
            payload["resonance"] = _resonance_summary(res)
            if res.get("active"):
                tier_txt = "4/4" if res.get("tier") == "full" else "3/4"
                payload["signals"]["1234"] = {
                    "triggered": True,
                    "description": f"1234 强共振（{tier_txt}）：多周期 MRMC 抄底共振",
                }
        else:
            # 指标包未就绪：现场算日线保证首屏有数据，同时后台补算全量
            ensure_indicator_bundle(symbol)
            df = fetch_ohlcv(symbol, "1d", "120d")
            if df is None or df.empty:
                payload = {"symbol": symbol, "error": "暂无数据"}
            else:
                df = enrich_indicators(df, "1d")
                stats = _compute_stats(df, symbol, "1d")
                signals = _latest_signals(df, symbol, "1d")
                try:
                    s1234 = calc_1234(symbol)
                    payload["resonance"] = _resonance_summary(s1234)
                    if s1234.get("active"):
                        tier_txt = "4/4" if s1234.get("tier") == "full" else "3/4"
                        signals["1234"] = {
                            "triggered": True,
                            "description": f"1234 强共振（{tier_txt}）：多周期 MRMC 抄底共振",
                        }
                except Exception:
                    pass
                payload = {
                    **stats,
                    "spark": [_row_to_float(c) for c in df["Close"].tail(30).tolist()],
                    "signals": {k: v for k, v in signals.items() if v.get("triggered")},
                }
        payload["_fetched_at"] = now
        with _snapshot_lock:
            _snapshot_cache[symbol] = payload
        return {k: v for k, v in payload.items() if not k.startswith("_")}
    except Exception as exc:
        logger.warning("snapshot(%s) failed: %s", symbol, exc)
        return {"symbol": symbol, "error": str(exc)}


def snapshots(symbols: List[str], refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """并行获取多个快照。"""
    out: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as pool:
        futures = {pool.submit(snapshot, s, refresh): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception as exc:
                out[sym] = {"symbol": sym, "error": str(exc)}
    return out


def lookup_info(symbol: str) -> Dict[str, Any]:
    """添加收藏时自动补全：名称 / 行业 / 交易所。"""
    try:
        import yfinance as yf

        info = yf.Ticker(symbol.upper().strip()).info
        name = info.get("shortName") or info.get("longName") or info.get("displayName")
        sector_en = info.get("sector")
        return {
            "symbol": symbol.upper().strip(),
            "name": name,
            "sector": map_sector(sector_en),
            "industry": info.get("industry"),
            "exchange": info.get("exchange"),
        }
    except Exception as exc:
        return {"symbol": symbol.upper().strip(), "error": str(exc)}


_SECTOR_MAP = {
    "technology": "科技",
    "communication services": "通信",
    "consumer cyclical": "消费",
    "consumer defensive": "消费",
    "consumer staples": "消费",
    "financial services": "金融",
    "healthcare": "医疗",
    "energy": "能源",
    "industrials": "工业",
    "materials": "材料",
    "real estate": "地产",
    "utilities": "公用事业",
    "basic materials": "材料",
}


def map_sector(sector_en: Optional[str]) -> Optional[str]:
    """把 yfinance 英文行业映射为中文分类。"""
    if not sector_en:
        return None
    return _SECTOR_MAP.get(sector_en.strip().lower(), sector_en)


# ---------------------------------------------------------------- 1234 强共振指标

def calc_1234(symbol: str, window_hours: int = 24) -> Dict[str, Any]:
    """1234 指标：1h/2h/3h/4h 多周期在 window_hours 内出现 MRMC 抄底共振。

    实测四个周期全命中几乎不可能（2h/4h 重采样后 MRMC 触发稀少），
    因此采用分档：>=3/4 周期共振 = 强提示（major），4/4 = 最强提示（full）。
    多周期同源共振（同一份 1h 数据重采样），信号足够严格。
    """
    symbol = symbol.upper().strip()
    key = f"{symbol}:{window_hours}"
    now = time.time()
    with _s1234_lock:
        cached = _s1234_cache.get(key)
        if cached and now - cached["_fetched_at"] < S1234_TTL:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    base = fetch_ohlcv(symbol, "1h", "60d")  # 60d 保证 MRMC 有足够预热
    if base is None or base.empty:
        payload = {
            "active": False,
            "timeframes": {},
            "window_hours": window_hours,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": "暂无 1h 数据",
        }
    else:
        latest = base.index[-1]
        cutoff = latest - pd.Timedelta(hours=window_hours)
        timeframes: Dict[str, Any] = {}
        # 1h-4h 多周期（同一份 1h 数据重采样，保证同源可比）
        for interval, rule in (("1h", None), ("2h", "2h"), ("3h", "3h"), ("4h", "4h")):
            df = base if rule is None else _resample_ohlcv(base, rule)
            if df is None or df.empty:
                continue
            result = calc_mrmc(df["Close"].values)
            buy_times = [
                df.index[i] for i, fired in enumerate(result["buy"]) if fired
            ]
            within = [ts for ts in buy_times if ts >= cutoff]
            timeframes[interval] = {
                "fired": bool(within),
                "last_ts": int(within[-1].timestamp() * 1000) if within else None,
                "recent_count": len(within),
            }
        # 30m 节点（单独拉取 30m K线）
        try:
            m30 = fetch_ohlcv(symbol, "30m", "30d")
            if m30 is not None and not m30.empty:
                r30 = calc_mrmc(m30["Close"].values)
                buys30 = [m30.index[i] for i, fired in enumerate(r30["buy"]) if fired]
                within30 = [ts for ts in buys30 if ts >= cutoff]
                timeframes["30m"] = {
                    "fired": bool(within30),
                    "last_ts": int(within30[-1].timestamp() * 1000) if within30 else None,
                    "recent_count": len(within30),
                }
        except Exception:
            pass

        # 1234 分档仍以 1h-4h 四节点为准（30m 作为附加共振节点展示）
        hour_nodes = {k: v for k, v in timeframes.items() if k != "30m"}
        fired_count = sum(1 for v in hour_nodes.values() if v["fired"])
        if fired_count >= 4:
            tier = "full"
        elif fired_count >= 3:
            tier = "major"
        else:
            tier = "none"
        fired_nodes = sorted(
            [k for k, v in timeframes.items() if v["fired"]],
            key=lambda x: {"30m": 0, "1h": 1, "2h": 2, "3h": 3, "4h": 4}.get(x, 9),
        )
        payload = {
            "active": tier != "none",
            "tier": tier,
            "fired_count": fired_count,
            "all_fired_count": len(fired_nodes),
            "fired_nodes": fired_nodes,
            "timeframes": timeframes,
            "window_hours": window_hours,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    payload["_fetched_at"] = now
    with _s1234_lock:
        _s1234_cache[key] = payload
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _s1234_key(symbol: str, window_hours: int) -> str:
    return f"{symbol.upper().strip()}:{int(window_hours)}"


def _s1234_cached(key: str) -> Optional[Dict[str, Any]]:
    """取指定 key 的新鲜缓存；过期/不存在返回 None。"""
    with _s1234_lock:
        cached = _s1234_cache.get(key)
    if cached and time.time() - cached.get("_fetched_at", 0) < S1234_TTL:
        return cached
    return None


def ensure_resonance(symbol: str, window_hours: int = 24) -> bool:
    """确保指定时间窗口的 1234 共振结果新鲜；未就绪则后台异步计算。

    与 ensure_indicator_bundle 一样是非阻塞的：返回是否已就绪，
    未就绪时由调用方稍后轮询 get_resonance。
    """
    key = _s1234_key(symbol, window_hours)
    if _s1234_cached(key) is not None:
        return True
    with _s1234_lock:
        if key in _s1234_pending:
            return False
        _s1234_pending.add(key)

    def _worker():
        # Same rule as ensure_indicator_bundle: queue for the slot here so the
        # caller is never the one that blocks.
        _s1234_slots.acquire()
        try:
            calc_1234(symbol, window_hours)
        except Exception as exc:
            logger.warning("[%s] 1234 共振计算失败: %s", symbol, exc, exc_info=True)
        finally:
            with _s1234_lock:
                _s1234_pending.discard(key)
            _s1234_slots.release()

    threading.Thread(target=_worker, daemon=True).start()
    return False


def get_resonance(symbol: str, window_hours: int = 24) -> Optional[Dict[str, Any]]:
    """读取指定时间窗口已算好的 1234 共振结果；未缓存/过期返回 None。"""
    cached = _s1234_cached(_s1234_key(symbol, window_hours))
    if cached is None:
        return None
    return {k: v for k, v in cached.items() if not k.startswith("_")}


def _resonance_summary(res: Dict[str, Any]) -> Dict[str, Any]:
    """从共振结果里提取卡片展示所需的摘要字段。"""
    return {
        "fired_nodes": res.get("fired_nodes", []),
        "tier": res.get("tier"),
        "all_fired_count": res.get("all_fired_count", 0),
        "timeframes": {
            k: {"fired": v.get("fired"), "last_ts": v.get("last_ts")}
            for k, v in (res.get("timeframes") or {}).items()
        },
    }


def config_info() -> Dict[str, Any]:
    """前端所需静态配置。"""
    strategies = [
        {
            "name": name,
            "description": cfg.get("description", ""),
            "enabled_signals": cfg.get("enabled_signals", []),
        }
        for name, cfg in STRATEGY_REGISTRY.items()
    ]
    return {
        "intervals": INTERVALS,
        "periods": PERIOD_MAP,
        "strategies": strategies,
        "categories": [
            "科技", "消费", "金融", "医疗", "能源", "工业",
            "指数", "加密货币", "其他", "未分类",
        ],
    }
