"""
review.py - 决策胜率复盘机制（对齐业内研报评估口径）
=====================================================
统计每条「综合决议」（Buy/Overweight/Hold/Underweight/Sell）在其有效期内的
正确性，区分短 / 中 / 长，并对齐业内常见做法：

  A. 基准超额（alpha）：同期基准收益对比（美股 SPY / 港股 ^HSI / A股 沪深300）
  B. 幅度统计：胜率之外记录平均收益 / 平均超额，避免"方向对但赚得少"失真
  C. 目标价触达命中率：周期内最高/最低价是否触达目标价（TipRanks 口径）
  D. 持有单独统计：不参与方向胜率，单独看 ±5% 带宽内正确率
  E. 小样本标注：已评估 < 10 条时标记 sample_warning

胜率口径（方向）：
  - 买入/增持（看多）：到期价 > 入场价 -> 胜；超额口径：收益 > 基准 -> 胜
  - 卖出/减持（看空）：到期价 < 入场价 -> 胜；超额口径：收益 < 基准 -> 胜
  - 持有（中性）：    |涨跌幅| <= 5%   -> 胜（单独统计）
  - 未到有效期的决策记为 pending（不计入胜率）
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from market import fetch_ohlcv

REVIEW_PATH = Path(__file__).resolve().parent / "decision_review.json"
REVIEW_LOCK = threading.RLock()

_price_cache: Dict[str, Dict[str, Any]] = {}
_PRICE_TTL = 300  # 秒

_HOLD_BAND = 0.05  # 持有判定为"正确"的涨跌幅带宽 ±5%
_DIRECTIONAL = {"Buy", "Overweight", "Sell", "Underweight"}

_RATING_DEFAULT_DAYS = {
    "Buy": 90,
    "Overweight": 90,
    "Hold": 60,
    "Underweight": 60,
    "Sell": 60,
}

_HORIZON_RE = re.compile(
    r"(\d+)\s*(?:[-~—至到]\s*(\d+))?\s*(?:个)?(天|周|个月|月|年|季度|季)"
)
_UNIT_DAYS = {
    "天": 1,
    "周": 7,
    "个月": 30,
    "月": 30,
    "年": 365,
    "季度": 90,
    "季": 90,
}

# 基准指数映射：A股/港股用对应指数，其余用 SPY
_BENCHMARKS = ((".HK", "^HSI"), (".SS", "000300.SS"), (".SZ", "000300.SS"))

# 市场当地时区：用于把 ET 时间戳还原成交易日的当地日期
_MARKET_TZ = {
    ".HK": "Asia/Hong_Kong",
    ".SS": "Asia/Shanghai",
    ".SZ": "Asia/Shanghai",
}


def _market_tz(ticker: str) -> str:
    up = ticker.upper().strip()
    for suffix, tz in _MARKET_TZ.items():
        if up.endswith(suffix):
            return tz
    return "America/New_York"


def horizon_to_days(horizon: Optional[str], rating: str) -> int:
    """把时间跨度文本解析为持有天数（取区间上界；解析失败按评级默认）。"""
    if horizon:
        m = _HORIZON_RE.search(horizon)
        if m:
            upper = max(int(m.group(1)), int(m.group(2) or m.group(1)))
            return upper * _UNIT_DAYS[m.group(3)]
    return _RATING_DEFAULT_DAYS.get(rating, 60)


def bucket_for(days: int) -> str:
    """短 / 中 / 长 分档：<=30天短，<=180天中，>180天长。"""
    if days <= 30:
        return "short"
    if days <= 180:
        return "medium"
    return "long"


def _benchmark_for(ticker: str) -> str:
    up = ticker.upper().strip()
    for suffix, bench in _BENCHMARKS:
        if up.endswith(suffix):
            return bench
    return "SPY"


def _daily_prices(ticker: str) -> Optional[pd.DataFrame]:
    ticker = ticker.upper().strip()
    now = time.time()
    with REVIEW_LOCK:
        cached = _price_cache.get(ticker)
        if cached and now - cached["_t"] < _PRICE_TTL:
            return cached["df"]
    try:
        df = fetch_ohlcv(ticker, "1d", "3y")
        with REVIEW_LOCK:
            _price_cache[ticker] = {"df": df, "_t": time.time()}
        return df
    except Exception:
        return None


def _close_at(df: pd.DataFrame, ts: pd.Timestamp, prefer_after: bool = True) -> Optional[float]:
    """取离 ts 最近的收盘价（±1 天容差；否则按方向取最近一根）。"""
    if df is None or df.empty:
        return None
    near = df.index[
        (df.index >= ts - pd.Timedelta(days=1))
        & (df.index <= ts + pd.Timedelta(days=1))
    ]
    if len(near):
        pick = min(near, key=lambda t: abs((t - ts).total_seconds()))
        return float(df["Close"].loc[pick])
    if prefer_after:
        after = df.index >= ts
        if after.any():
            return float(df["Close"].loc[df.index[after][0]])
        return float(df["Close"].iloc[-1])
    before = df.index <= ts
    if before.any():
        return float(df["Close"].loc[df.index[before][-1]])
    return float(df["Close"].iloc[0])


def _win_for(rating: str, ret: float) -> bool:
    if rating in ("Buy", "Overweight"):
        return ret > 0
    if rating in ("Sell", "Underweight"):
        return ret < 0
    return abs(ret) <= _HOLD_BAND


def evaluate_decision(
    ticker: str,
    date_str: Optional[str],
    rating: str,
    horizon: Optional[str],
    price_target: Optional[str] = None,
) -> Dict[str, Any]:
    """评估单条决策：方向 + 基准超额 + 幅度 + 目标价触达。"""
    df = _daily_prices(ticker)
    if df is None or df.empty or not date_str:
        return {"status": "error", "reason": "无行情数据"}

    closes = df["Close"]
    try:
        target_date = pd.Timestamp(date_str)
    except Exception:
        return {"status": "error", "reason": "日期解析失败"}

    # 入场：把 ET 时间戳还原为市场当地日期，取「分析日当天或之前」的最近一根 bar，
    # 非交易日（周末/节假日）自然落到上一个开盘日；行情滞后时取最新一根。
    local_dates = (
        df.index.tz_convert(_market_tz(ticker)).normalize().tz_localize(None)
    )
    before = local_dates <= target_date
    if before.any():
        entry_ts = df.index[before][-1]
    else:
        entry_ts = df.index[0]
    entry = float(closes.loc[entry_ts])

    days = horizon_to_days(horizon, rating)
    eval_ts = entry_ts + pd.Timedelta(days=days)
    last_ts = df.index[-1]

    if eval_ts > last_ts:
        exit_ts = last_ts
        exit_px = float(closes.iloc[-1])
        status = "pending"
        win: Optional[bool] = None
    else:
        upto = df.index <= eval_ts
        exit_ts = df.index[upto][-1]
        exit_px = float(closes.loc[exit_ts])
        status = "evaluated"
        win = _win_for(rating, exit_px / entry - 1 if entry else 0.0)

    ret = exit_px / entry - 1 if entry else 0.0

    # A. 基准超额（alpha）：同期基准收益
    bench_ticker = _benchmark_for(ticker)
    bench_df = _daily_prices(bench_ticker)
    bench_ret = alpha_pct = None
    win_alpha: Optional[bool] = None
    if bench_df is not None and not bench_df.empty:
        b_entry = _close_at(bench_df, entry_ts, prefer_after=True)
        b_exit = _close_at(bench_df, exit_ts, prefer_after=False)
        if b_entry:
            bench_ret = (b_exit / b_entry - 1) * 100 if b_exit else None
            alpha_pct = round(ret * 100 - (bench_ret or 0), 2)
            if rating in ("Buy", "Overweight"):
                win_alpha = (alpha_pct or 0) > 0
            elif rating in ("Sell", "Underweight"):
                win_alpha = (alpha_pct or 0) < 0

    # C. 目标价触达（TipRanks 口径：周期内触碰即算命中）
    target_hit = None
    if price_target:
        try:
            target = float(price_target)
            window = df.loc[entry_ts:exit_ts]
            if rating in ("Buy", "Overweight"):
                target_hit = bool((window["High"] >= target).any())
            elif rating in ("Sell", "Underweight"):
                target_hit = bool((window["Low"] <= target).any())
        except Exception:
            pass

    return {
        "status": status,
        "win": win,
        "win_alpha": win_alpha,
        "bucket": bucket_for(days),
        "horizon_days": days,
        "entry_ts": int(entry_ts.timestamp() * 1000),
        "entry_date": str(pd.Timestamp(entry_ts).tz_convert(_market_tz(ticker)).date()),
        "exit_ts": int(exit_ts.timestamp() * 1000),
        "entry_price": round(entry, 4),
        "exit_price": round(exit_px, 4),
        "return_pct": round(ret * 100, 2),
        "benchmark": bench_ticker,
        "bench_ret_pct": round(bench_ret, 2) if bench_ret is not None else None,
        "alpha_pct": alpha_pct,
        "target_hit": target_hit,
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_store() -> Dict[str, Any]:
    if REVIEW_PATH.exists():
        try:
            return json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_store(store: Dict[str, Any]) -> None:
    with REVIEW_LOCK:
        REVIEW_PATH.write_text(
            json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def run_review(reports_by_ticker: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """复盘全部报告决策：评估/刷新每条，写盘并返回胜率摘要。"""
    store = load_store()
    allowed_tickers = set(reports_by_ticker.keys())
    for ticker, reports in reports_by_ticker.items():
        for r in reports:
            final = (r.get("decision") or {}).get("final") or {}
            rating = final.get("rating")
            if not rating:
                continue
            rid = r.get("id") or f"{ticker}_{r.get('date', '')}"
            try:
                ev = evaluate_decision(
                    ticker,
                    r.get("date"),
                    rating,
                    final.get("time_horizon"),
                    final.get("price_target"),
                )
            except Exception:
                ev = {"status": "error", "reason": "评估异常"}
            ev.update({
                "ticker": ticker,
                "report_id": rid,
                "date": r.get("date", ""),
                "rating": rating,
                "rating_cn": final.get("rating_cn"),
                "horizon": final.get("time_horizon"),
                "price_target": final.get("price_target"),
            })
            store[rid] = ev
    # 不在本次范围内的（如已移出收藏的）决策不再收录
    store = {
        rid: d
        for rid, d in store.items()
        if isinstance(d, dict) and str(d.get("ticker", "")).upper() in allowed_tickers
    }
    save_store(store)
    return summarize(store)


def _avg(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
    vals = [d.get(key) for d in rows if isinstance(d.get(key), (int, float))]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def _rate(rows: List[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    return round(sum(1 for d in rows if d.get("win")) / len(rows) * 100, 1)


def _target_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    with_target = [d for d in rows if d.get("target_hit") is not None]
    hits = sum(1 for d in with_target if d["target_hit"])
    return {
        "target_hit": hits,
        "target_total": len(with_target),
        "target_rate": round(hits / len(with_target) * 100, 1) if with_target else None,
    }


def summarize(store: Dict[str, Any]) -> Dict[str, Any]:
    """胜率摘要：方向胜率（含基准超额）+ 持有单独 + 短中长 + 按评级 + 目标价命中。"""
    items = [d for d in store.values() if isinstance(d, dict)]
    evaluated = [d for d in items if d.get("status") == "evaluated" and d.get("win") is not None]
    directional = [d for d in evaluated if d.get("rating") in _DIRECTIONAL]
    holds = [d for d in evaluated if d.get("rating") == "Hold"]
    pending = [d for d in items if d.get("status") == "pending"]
    errors = [d for d in items if d.get("status") == "error"]

    def slot(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        s = _target_stats(rows)
        return {
            "wins": sum(1 for d in rows if d.get("win")),
            "total": len(rows),
            "rate": _rate(rows),
            "avg_return": _avg(rows, "return_pct"),
            "avg_alpha": _avg(rows, "alpha_pct"),
            **s,
        }

    by_bucket: Dict[str, Dict[str, Any]] = {}
    for b in ("short", "medium", "long"):
        rows = [d for d in directional if d.get("bucket") == b]
        hold_rows = [d for d in holds if d.get("bucket") == b]
        by_bucket[b] = {
            **slot(rows),
            "hold_wins": sum(1 for d in hold_rows if d.get("win")),
            "hold_total": len(hold_rows),
            "hold_rate": _rate(hold_rows),
        }

    by_rating: Dict[str, Dict[str, Any]] = {}
    for d in evaluated:
        r = d.get("rating") or "未知"
        by_rating.setdefault(r, []).append(d)
    by_rating = {r: slot(rows) for r, rows in by_rating.items()}

    overall = slot(directional)
    return {
        "total": len(items),
        "evaluated": len(evaluated),
        "pending": len(pending),
        "errors": len(errors),
        "wins": overall["wins"],
        "overall_rate": overall["rate"],
        "avg_return": overall["avg_return"],
        "avg_alpha": overall["avg_alpha"],
        "target_rate": overall["target_rate"],
        "target_hit": overall["target_hit"],
        "target_total": overall["target_total"],
        "hold": {
            "wins": sum(1 for d in holds if d.get("win")),
            "total": len(holds),
            "rate": _rate(holds),
        },
        "sample_warning": len(directional) < 10,
        "by_bucket": by_bucket,
        "by_rating": by_rating,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
