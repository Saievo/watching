"""
run_analysis.py - TradingAgents 分析执行器（子进程）
=====================================================
由 server.py 以子进程方式调用，cwd 指向 TradingAgents 源码目录，
保证使用本地 v0.3.1 而非 site-packages 里的旧版本。

用法:
    python3 run_analysis.py --symbol AAPL --date 2026-08-07 \
        --save-dir /abs/path/stock_web/reports/AAPL/20260807_120000 \
        --provider deepseek --deep-model deepseek-v4-pro \
        --quick-model deepseek-v4-flash --research-depth 1 --language 中文 \
        --analysts market,news,social,fundamentals
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TradingAgents 一键分析")
    p.add_argument("--symbol", required=True)
    p.add_argument("--date", required=True, help="分析日期 YYYY-MM-DD")
    p.add_argument("--save-dir", required=True)
    p.add_argument("--provider", default=None)
    p.add_argument("--deep-model", default=None)
    p.add_argument("--quick-model", default=None)
    p.add_argument("--research-depth", type=int, default=None)
    p.add_argument("--language", default=None)
    p.add_argument("--analysts", default="market,social,news,fundamentals")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--checkpoint", action="store_true", help="启用 LangGraph 断点续跑")
    return p.parse_args()


def load_dotenv(path: Path) -> None:
    """极简 .env 加载（只注入未设置的环境变量）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def write_meta(save_dir: Path, payload: dict) -> None:
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / "meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    save_dir = Path(args.save_dir)
    ta_dir = Path(__file__).resolve().parent.parent / "TradingAgents"

    # 1) 环境变量：优先加载 TradingAgents/.env，再补根目录 .env
    load_dotenv(ta_dir / ".env")
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    # 2) 强制使用本地源码（v0.3.1）
    sys.path.insert(0, str(ta_dir))
    os.environ["PYTHONPATH"] = str(ta_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")

    print(f"[runner] cwd={os.getcwd()}", flush=True)
    print(f"[runner] symbol={args.symbol} date={args.date}", flush=True)
    print(f"[runner] save_dir={save_dir}", flush=True)
    print(f"[runner] provider={args.provider} deep={args.deep_model} quick={args.quick_model}", flush=True)
    print(f"[runner] research_depth={args.research_depth} language={args.language}", flush=True)
    print(f"[runner] analysts={args.analysts}", flush=True)

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    if args.provider:
        config["llm_provider"] = args.provider
    if args.deep_model:
        config["deep_think_llm"] = args.deep_model
    if args.quick_model:
        config["quick_think_llm"] = args.quick_model
    if args.research_depth is not None:
        config["max_debate_rounds"] = args.research_depth
        config["max_risk_discuss_rounds"] = args.research_depth
    if args.language:
        config["output_language"] = args.language
    if args.temperature is not None:
        config["temperature"] = args.temperature
    if args.checkpoint:
        config["checkpoint_enabled"] = True

    selected_analysts = [a.strip().lower() for a in args.analysts.split(",") if a.strip()]
    print(
        f"[runner] 最终配置: provider={config['llm_provider']} "
        f"deep={config['deep_think_llm']} quick={config['quick_think_llm']} "
        f"debate={config['max_debate_rounds']} risk={config['max_risk_discuss_rounds']}",
        flush=True,
    )
    print("[runner] 初始化 TradingAgentsGraph ...", flush=True)

    ta = TradingAgentsGraph(
        debug=True,
        config=config,
        selected_analysts=selected_analysts,
    )
    print("[runner] 开始 propagate ...", flush=True)

    try:
        final_state, decision = ta.propagate(args.symbol, args.date)
    except Exception:
        print("[runner] 分析失败:", flush=True)
        traceback.print_exc()
        write_meta(
            save_dir,
            {
                "symbol": args.symbol,
                "date": args.date,
                "status": "failed",
                "error": traceback.format_exc(),
            },
        )
        return 1

    print("[runner] propagate 完成，正在写报告 ...", flush=True)
    report_path = ta.save_reports(final_state, args.symbol, save_path=str(save_dir))
    print(f"[runner] 报告已写入: {report_path}", flush=True)

    decision_text = ""
    if isinstance(decision, str):
        decision_text = decision
    else:
        try:
            decision_text = json.dumps(decision, ensure_ascii=False, indent=2, default=str)
        except Exception:
            decision_text = str(decision)

    write_meta(
        save_dir,
        {
            "symbol": args.symbol,
            "date": args.date,
            "status": "done",
            "report_path": str(report_path),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "decision": decision_text[:20000],
        },
    )
    print("[runner] 全部完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
