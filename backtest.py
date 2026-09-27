"""
backtest.py - 美股策略回测脚本
==============================
用法：
    # 回测单只股票（标准策略）
    python backtest.py --symbol AAPL --strategy macd_rsi --interval 1h --period 180d

    # 回测 NX 策略方案一（含 P&L 模拟）
    python backtest.py --symbol TSLA --strategy nx --scheme 1 --interval 1h --period 365d

    # 批量回测多个股票
    python backtest.py --symbols AAPL TSLA NVDA --strategy macd_rsi --interval 1d --period 365d

    # 显示完整信号列表
    python backtest.py --symbol AAPL --strategy macd_rsi --interval 1h --verbose
"""

import argparse
import sys
import logging
from typing import List, Optional, Dict, Any

import pandas as pd

from data_fetcher import DataFetcher
from strategy import StrategyEngine, STRATEGY_REGISTRY
from strategy_nx import NXStrategyEngine, NX_STRATEGY_CONFIGS

# ── 日志配置（回测时只输出 WARNING 以上，减少噪音）──────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────────────────────────────

def _ts_ms(ts) -> int:
    """Pandas Timestamp → 毫秒整数"""
    return int(ts.timestamp() * 1000)


# ─────────────────────────────────────────────────────────────────
# P&L 模拟引擎（做多-只-策略）
# ─────────────────────────────────────────────────────────────────

class PnLSimulator:
    """
    轻量级 P&L 模拟器。
    规则：
      - 每次「做多」信号建仓（全仓买入，不重复加仓）
      - 「减仓50%」信号：持仓时卖出一半
      - 「清仓」或「止损」信号：持仓时全部平仓
      - 无持仓时忽略平仓信号
      - 仅做多，不做空
    """

    def __init__(self, initial_capital: float = 100_000.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.position = 0.0          # 持有股数（支持小数）
        self.entry_price = 0.0       # 建仓均价
        self.entry_ts_ms = 0
        self.trades: List[Dict] = [] # 已平仓交易记录
        self.current_trade: Optional[Dict] = None

    # ── 信号处理 ──────────────────────────────────────────────────

    def on_buy(self, price: float, ts_ms: int, signal_desc: str = ""):
        """全仓建仓"""
        if self.position > 0:
            return  # 已有持仓，不重复买入

        shares = self.cash / price
        self.position = shares
        self.entry_price = price
        self.entry_ts_ms = ts_ms
        self.cash = 0.0
        self.current_trade = {
            "entry_ts_ms":  ts_ms,
            "entry_price":  price,
            "entry_signal": signal_desc,
            "shares":       shares,
            "partial_pnl":  0.0,     # 减仓已实现盈亏
        }

    def on_partial_sell(self, price: float, ts_ms: int, signal_desc: str = ""):
        """减仓 50%"""
        if self.position <= 0 or self.current_trade is None:
            return

        sell_shares = self.position * 0.5
        proceeds = sell_shares * price
        cost = sell_shares * self.entry_price
        partial_pnl = proceeds - cost

        self.cash += proceeds
        self.position -= sell_shares
        self.current_trade["partial_pnl"] += partial_pnl
        self.current_trade.setdefault("partial_events", []).append({
            "ts_ms":  ts_ms,
            "price":  price,
            "shares": sell_shares,
            "pnl":    round(partial_pnl, 2),
            "signal": signal_desc,
        })

    def on_sell(self, price: float, ts_ms: int, signal_desc: str = ""):
        """全部平仓"""
        if self.position <= 0 or self.current_trade is None:
            return

        proceeds = self.position * price
        cost = self.position * self.entry_price
        final_pnl = proceeds - cost

        total_pnl = final_pnl + self.current_trade["partial_pnl"]
        pct_return = total_pnl / (self.current_trade["shares"] * self.entry_price) * 100

        self.cash += proceeds
        trade = {
            **self.current_trade,
            "exit_ts_ms":   ts_ms,
            "exit_price":   price,
            "exit_signal":  signal_desc,
            "final_pnl":    round(final_pnl, 2),
            "total_pnl":    round(total_pnl, 2),
            "pct_return":   round(pct_return, 2),
            "hold_ms":      ts_ms - self.current_trade["entry_ts_ms"],
        }
        self.trades.append(trade)
        self.position = 0.0
        self.entry_price = 0.0
        self.current_trade = None

    # ── 按收盘价强制平仓未结头寸 ──────────────────────────────────

    def force_close(self, price: float, ts_ms: int):
        """回测结束时强制平仓（标记为未平仓）"""
        if self.position > 0 and self.current_trade is not None:
            self.on_sell(price, ts_ms, signal_desc="BACKTEST_END")

    # ── 统计 ──────────────────────────────────────────────────────

    @property
    def equity(self) -> float:
        """当前权益（含未平仓头寸的市值，此处用建仓价估算）"""
        return self.cash + self.position * self.entry_price

    def summary(self) -> Dict[str, Any]:
        """生成绩效统计"""
        if not self.trades:
            return {
                "total_trades":   0,
                "win_trades":     0,
                "lose_trades":    0,
                "win_rate":       "N/A",
                "total_pnl":      0.0,
                "total_return":   "N/A",
                "avg_pnl":        0.0,
                "avg_return":     "N/A",
                "best_trade":     0.0,
                "worst_trade":    0.0,
                "avg_hold_hours": "N/A",
            }

        wins = [t for t in self.trades if t["total_pnl"] > 0]
        total_pnl = sum(t["total_pnl"] for t in self.trades)
        avg_hold_ms = sum(t["hold_ms"] for t in self.trades) / len(self.trades)

        return {
            "total_trades":   len(self.trades),
            "win_trades":     len(wins),
            "lose_trades":    len(self.trades) - len(wins),
            "win_rate":       f"{len(wins)/len(self.trades)*100:.1f}%",
            "total_pnl":      round(total_pnl, 2),
            "total_return":   f"{total_pnl/self.initial_capital*100:.2f}%",
            "avg_pnl":        round(total_pnl / len(self.trades), 2),
            "avg_return":     f"{sum(t['pct_return'] for t in self.trades)/len(self.trades):.2f}%",
            "best_trade":     round(max(t["total_pnl"] for t in self.trades), 2),
            "worst_trade":    round(min(t["total_pnl"] for t in self.trades), 2),
            "avg_hold_hours": f"{avg_hold_ms/3_600_000:.1f}h",
        }


# ─────────────────────────────────────────────────────────────────
# 标准策略回测
# ─────────────────────────────────────────────────────────────────

def run_standard_backtest(
    df: pd.DataFrame,
    symbol: str,
    strategy_name: str,
    interval: str,
    verbose: bool = False,
) -> dict:
    """
    标准策略（MACD/RSI/EMA）回测 + P&L 模拟。

    做多规则（标准策略无内置平仓信号，使用简单规则）：
      - 触发信号 → 建仓（若无持仓）
      - 持仓超过 10 根 K 线后遇到下一个信号 → 平仓并记录
    """
    engine = StrategyEngine()
    df_ind = engine.add_indicators(df, strategy_name)
    config = STRATEGY_REGISTRY.get(strategy_name, {})
    sim = PnLSimulator()
    signals_log = []
    last_entry_bar = -1

    for i in range(2, len(df_ind)):
        sliced = df_ind.iloc[: i + 1]
        triggered, description = engine.check_signals(sliced, strategy_name, symbol)
        bar = df_ind.iloc[i]
        price = float(bar["Close"])
        ts_ms = _ts_ms(df_ind.index[i])

        if triggered:
            sig_line = description.split("\n")[-1].strip()
            signals_log.append({
                "ts_ms":  ts_ms,
                "time":   str(df_ind.index[i])[:19],
                "close":  round(price, 4),
                "signal": sig_line,
            })

            if sim.position <= 0:
                # 建仓
                sim.on_buy(price, ts_ms, sig_line)
                last_entry_bar = i
            elif i - last_entry_bar >= 10:
                # 持仓超过10根K线后遇到新信号，平仓
                sim.on_sell(price, ts_ms, sig_line)

    # 强制平仓最后一笔
    if sim.position > 0 and len(df_ind) > 0:
        sim.force_close(
            float(df_ind["Close"].iloc[-1]),
            _ts_ms(df_ind.index[-1]),
        )

    return _build_result(
        symbol=symbol,
        strategy=strategy_name,
        description=config.get("description", strategy_name),
        interval=interval,
        df=df_ind,
        signals_log=signals_log,
        sim=sim,
        verbose=verbose,
    )


# ─────────────────────────────────────────────────────────────────
# NX 策略回测
# ─────────────────────────────────────────────────────────────────

def run_nx_backtest(
    df: pd.DataFrame,
    symbol: str,
    scheme_type: int,
    interval: str,
    verbose: bool = False,
) -> dict:
    """
    NX 牛熊双通道 + MACD 波段背离策略回测 + P&L 模拟。

    信号对应操作：
      🟢 做多 → 建仓
      🟡 减仓 → 卖出50%
      🔴 止损/清仓 → 全部平仓
    """
    engine = NXStrategyEngine()
    df_ind = engine.add_indicators(df, interval=interval)

    if len(df_ind) < 91:
        print(f"⚠️  [{symbol}] 数据量不足 91 根 K 线，NX 策略需要更长历史，建议使用更长的 --period")
        return {}

    sim = PnLSimulator()
    signals_log = []

    for i in range(91, len(df_ind)):
        sliced = df_ind.iloc[: i + 1]
        triggered, description = engine.check_signals(
            sliced, symbol=symbol, scheme_type=scheme_type, interval=interval
        )
        if not triggered:
            continue

        bar = df_ind.iloc[i]
        price = float(bar["Close"])
        ts_ms = _ts_ms(df_ind.index[i])

        # 提取所有信号行
        for line in description.split("\n"):
            line = line.strip()
            if not line or (not line.startswith(("🟢", "🔴", "🟡"))):
                continue

            signals_log.append({
                "ts_ms":  ts_ms,
                "time":   str(df_ind.index[i])[:19],
                "close":  round(price, 4),
                "signal": line,
            })

            if "🟢" in line:
                sim.on_buy(price, ts_ms, line)
            elif "🟡" in line:
                sim.on_partial_sell(price, ts_ms, line)
            elif "🔴" in line:
                sim.on_sell(price, ts_ms, line)

    # 强制平仓最后一笔
    if sim.position > 0 and len(df_ind) > 0:
        sim.force_close(
            float(df_ind["Close"].iloc[-1]),
            _ts_ms(df_ind.index[-1]),
        )

    desc = f"NX方案{scheme_type}: {'右侧突破确认' if scheme_type == 1 else '左侧精准狙击'}"
    return _build_result(
        symbol=symbol,
        strategy=f"nx_scheme{scheme_type}",
        description=desc,
        interval=interval,
        df=df_ind,
        signals_log=signals_log,
        sim=sim,
        verbose=verbose,
    )


# ─────────────────────────────────────────────────────────────────
# 结果构建 & 打印
# ─────────────────────────────────────────────────────────────────

def _build_result(
    symbol: str,
    strategy: str,
    description: str,
    interval: str,
    df: pd.DataFrame,
    signals_log: list,
    sim: PnLSimulator,
    verbose: bool,
) -> dict:
    total_bars    = len(df)
    total_signals = len(signals_log)
    buy_cnt  = sum(1 for s in signals_log if "🟢" in s.get("signal", ""))
    sell_cnt = sum(1 for s in signals_log if "🔴" in s.get("signal", ""))
    warn_cnt = sum(1 for s in signals_log if "🟡" in s.get("signal", ""))

    perf = sim.summary()

    result = {
        "symbol":        symbol,
        "strategy":      strategy,
        "description":   description,
        "interval":      interval,
        "data_from":     str(df.index[0])[:19],
        "data_to":       str(df.index[-1])[:19],
        "total_bars":    total_bars,
        "total_signals": total_signals,
        "buy_signals":   buy_cnt,
        "sell_signals":  sell_cnt,
        "warn_signals":  warn_cnt,
        "signal_rate":   f"{total_signals / total_bars * 100:.2f}%" if total_bars else "N/A",
        "performance":   perf,
        "trades":        sim.trades,
        "signals_log":   signals_log,
    }

    _print_result(result, verbose)
    return result


def _print_result(result: dict, verbose: bool):
    sep  = "─" * 64
    sep2 = "═" * 64
    p    = result["performance"]

    print(f"\n{sep2}")
    print(f"  📊  {result['symbol']}  ·  {result['strategy']}  ·  {result['interval']}")
    print(sep2)
    print(f"  策略描述  : {result['description']}")
    print(f"  数据范围  : {result['data_from']}  →  {result['data_to']}")
    print(f"  总K线数   : {result['total_bars']}")
    print(sep)

    # 信号统计
    print(f"  信号触发  : {result['total_signals']} 次 (频率 {result['signal_rate']})")
    if result['buy_signals'] or result['sell_signals'] or result['warn_signals']:
        print(f"             🟢 做多 {result['buy_signals']} 次  "
              f"🟡 减仓 {result['warn_signals']} 次  "
              f"🔴 止损/清仓 {result['sell_signals']} 次")
    print(sep)

    # P&L 统计
    print(f"  交易次数  : {p['total_trades']} 笔  "
          f"(盈 {p['win_trades']} 亏 {p['lose_trades']})")
    if p['total_trades'] > 0:
        print(f"  胜率      : {p['win_rate']}")
        print(f"  总盈亏    : {'+' if p['total_pnl'] >= 0 else ''}{p['total_pnl']:.2f}  "
              f"({p['total_return']})")
        print(f"  单笔均值  : {'+' if p['avg_pnl'] >= 0 else ''}{p['avg_pnl']:.2f}  "
              f"({p['avg_return']})")
        print(f"  最佳/最差 : +{p['best_trade']:.2f}  /  {p['worst_trade']:.2f}")
        print(f"  平均持仓  : {p['avg_hold_hours']}")
    print(sep)

    # 交易明细（最近5笔）
    trades = result.get("trades", [])
    if trades and not verbose:
        print(f"\n  最近 {min(5,len(trades))} 笔交易:")
        print(f"  {'买入时间':<20} {'买入价':>8}  {'卖出时间':<20} {'卖出价':>8}  {'盈亏':>10}  {'收益率':>8}")
        print(f"  {'─'*20} {'─'*8}  {'─'*20} {'─'*8}  {'─'*10}  {'─'*8}")
        for t in trades[-5:]:
            sign = "+" if t["total_pnl"] >= 0 else ""
            print(
                f"  {t['entry_ts_ms'] and _fmt_ts(t['entry_ts_ms']):<20} "
                f"{t['entry_price']:>8.4f}  "
                f"{_fmt_ts(t['exit_ts_ms']):<20} "
                f"{t['exit_price']:>8.4f}  "
                f"{sign}{t['total_pnl']:>10.2f}  "
                f"{sign}{t['pct_return']:>7.2f}%"
            )

    if verbose:
        if trades:
            print(f"\n  全部 {len(trades)} 笔交易:")
            print(f"  {'买入时间':<20} {'买入价':>8}  {'卖出时间':<20} {'卖出价':>8}  {'盈亏':>10}  {'收益率':>8}")
            print(f"  {'─'*20} {'─'*8}  {'─'*20} {'─'*8}  {'─'*10}  {'─'*8}")
            for t in trades:
                sign = "+" if t["total_pnl"] >= 0 else ""
                print(
                    f"  {_fmt_ts(t['entry_ts_ms']):<20} "
                    f"{t['entry_price']:>8.4f}  "
                    f"{_fmt_ts(t['exit_ts_ms']):<20} "
                    f"{t['exit_price']:>8.4f}  "
                    f"{sign}{t['total_pnl']:>10.2f}  "
                    f"{sign}{t['pct_return']:>7.2f}%"
                )
        if result["signals_log"]:
            print(f"\n  全部 {len(result['signals_log'])} 条信号:")
            print(f"  {'时间':<20} {'收盘价':>10}  信号")
            print(f"  {'─'*20} {'─'*10}  {'─'*40}")
            for s in result["signals_log"]:
                print(f"  {s['time']:<20} {s['close']:>10.4f}  {s['signal']}")

    print(sep2)


def _fmt_ts(ts_ms: int) -> str:
    """毫秒时间戳 → 可读字符串"""
    return pd.Timestamp(ts_ms, unit="ms", tz="America/New_York").strftime("%m-%d %H:%M")


# ─────────────────────────────────────────────────────────────────
# 批量回测
# ─────────────────────────────────────────────────────────────────

def batch_backtest(
    symbols: List[str],
    strategy_name: str,
    interval: str,
    period: str,
    scheme: int = 1,
    verbose: bool = False,
):
    fetcher = DataFetcher()
    summary_rows = []

    for symbol in symbols:
        print(f"\n⏳ 正在回测 {symbol} ...")
        try:
            df = fetcher.fetch(symbol, interval=interval, period=period)
            if df is None or df.empty:
                print(f"  ⚠️  {symbol} 数据获取失败，跳过")
                continue

            if strategy_name == "nx":
                result = run_nx_backtest(df, symbol, scheme, interval, verbose)
            else:
                result = run_standard_backtest(df, symbol, strategy_name, interval, verbose)

            if result:
                p = result["performance"]
                summary_rows.append({
                    "Symbol":   symbol,
                    "Strategy": result["strategy"],
                    "Bars":     result["total_bars"],
                    "Signals":  result["total_signals"],
                    "Trades":   p["total_trades"],
                    "WinRate":  p["win_rate"],
                    "TotalPnL": p["total_pnl"],
                    "Return":   p["total_return"],
                    "BestTrade":p["best_trade"],
                })
        except Exception as e:
            print(f"  ❌ {symbol} 回测异常: {e}")
            import traceback
            if verbose:
                traceback.print_exc()

    if len(symbols) > 1 and summary_rows:
        print("\n" + "═" * 80)
        print("  📋  批量回测汇总")
        print("═" * 80)
        df_sum = pd.DataFrame(summary_rows)
        print(df_sum.to_string(index=False))
        print("═" * 80)


# ─────────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    all_strategies = list(STRATEGY_REGISTRY.keys()) + ["nx"]

    parser = argparse.ArgumentParser(
        description="美股策略回测工具（含 P&L 模拟）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python backtest.py --symbol AAPL --strategy macd_rsi --interval 1h --period 180d
  python backtest.py --symbol TSLA --strategy nx --scheme 1 --interval 1h --period 365d
  python backtest.py --symbol NVDA --strategy nx --scheme 2 --interval 15m --period 60d --verbose
  python backtest.py --symbols AAPL TSLA NVDA SPY --strategy macd_rsi --interval 1d --period 365d

可用策略:
  macd_rsi        MACD金叉死叉 + RSI超买超卖（默认）
  rsi_only        仅 RSI 超买超卖
  ma_cross        EMA均线金叉死叉
  tsla_aggressive TSLA激进策略: 敏感RSI + 快速EMA
  nvda_macd       NVDA快速MACD + RSI
  full            全信号策略: MACD + RSI + EMA
  nx              NX牛熊双通道 + MACD背离（用 --scheme 1/2 切换方案）

K线周期（yfinance 支持）:
  1m 2m 5m 15m 30m 60m 1h 1d 1wk

数据回溯 (period):
  7d 14d 30d 60d 90d 180d 365d 2y 5y max
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol",  type=str,           help="单只股票代码，如 AAPL")
    group.add_argument("--symbols", type=str, nargs="+", help="多只股票代码，如 AAPL TSLA NVDA")

    parser.add_argument("--strategy", type=str, default="macd_rsi",
                        choices=all_strategies, help="策略名称（默认 macd_rsi）")
    parser.add_argument("--scheme",   type=int, default=1, choices=[1, 2],
                        help="NX 策略方案：1=右侧突破  2=左侧狙击（仅 --strategy nx 时有效）")
    parser.add_argument("--interval", type=str, default="1h",
                        help="K 线周期（默认 1h）")
    parser.add_argument("--period",   type=str, default="180d",
                        help="数据回溯时长（默认 180d）")
    parser.add_argument("--capital",  type=float, default=100_000.0,
                        help="初始模拟本金（默认 100000）")
    parser.add_argument("--verbose",  action="store_true",
                        help="显示完整信号列表和全部交易明细")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    symbols: List[str] = (
        [args.symbol.upper()] if args.symbol
        else [s.upper() for s in args.symbols]
    )

    print(f"\n🚀 美股策略回测（初始资金 ${args.capital:,.0f}）")
    print(f"   股票    : {', '.join(symbols)}")
    print(f"   策略    : {args.strategy}" + (f" (方案{args.scheme})" if args.strategy == "nx" else ""))
    print(f"   周期    : {args.interval}    回溯: {args.period}")

    batch_backtest(
        symbols=symbols,
        strategy_name=args.strategy,
        interval=args.interval,
        period=args.period,
        scheme=args.scheme,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
