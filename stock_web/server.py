"""
server.py - 股票看板 Web 服务
==============================
FastAPI 后端：
  - 收藏多股票（按分类分组）
  - 历史数据 + 常见指标（调用根目录 stock 模块）
  - 一键调用 TradingAgents 多智能体分析（后台子进程）
  - 美化版 Markdown 报告展示

启动：
    cd stock_web && python3 server.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import market
import review


# ---------------------------------------------------------------- 路径配置

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
TA_DIR = ROOT_DIR / "TradingAgents"
# Dashboard needs the local TradingAgents source (not an older site-packages
# install) both for the model catalog endpoint and for analysis subprocesses.
sys.path.insert(0, str(TA_DIR))
FAVORITES_PATH = BASE_DIR / "favorites.json"
NAMES_PATH = BASE_DIR / "names.json"
JOBS_DIR = BASE_DIR / "jobs"
# 方案 B：所有报告统一存到 TradingAgents/reports/（与老 CLI 报告同目录）
REPORTS_DIR = TA_DIR / "reports"
PYTHON = os.environ.get("PYTHON3", "python3")

for d in (JOBS_DIR, REPORTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("stock_web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_jobs_from_disk()
    resumed = _auto_resume_interrupted()
    threading.Thread(target=_prewarm_names, daemon=True).start()
    threading.Thread(
        target=market.prewarm_indicator_bundles,
        args=([s["symbol"].upper() for s in load_favorites().get("stocks", [])],),
        daemon=True,
    ).start()
    threading.Thread(target=_trigger_decision_review, daemon=True).start()
    threading.Thread(target=_auto_scan_worker, daemon=True).start()
    logger.info(
        "Stock Dashboard 启动完成，收藏数: %d，自动续跑任务: %d，指标/胜率复盘已启动",
        len(load_favorites().get("stocks", [])),
        resumed,
    )
    yield


app = FastAPI(title="Stock Dashboard", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 收藏管理

def load_favorites() -> Dict[str, Any]:
    if FAVORITES_PATH.exists():
        try:
            return json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("favorites.json 解析失败: %s", exc)
    return {"stocks": []}


def save_favorites(data: Dict[str, Any]) -> None:
    FAVORITES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_stock(symbol: str) -> Optional[Dict[str, Any]]:
    data = load_favorites()
    for s in data.get("stocks", []):
        if s["symbol"].upper() == symbol.upper():
            return s
    return None


# ---------------------------------------------------------------- Pydantic 模型

class StockIn(BaseModel):
    symbol: str
    name: str = ""
    category: str = "未分类"
    strategy: str = "macd_rsi"
    interval: str = "15m"
    note: str = ""


class StockUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    strategy: Optional[str] = None
    interval: Optional[str] = None
    note: Optional[str] = None


class AnalyzeRequest(BaseModel):
    symbol: str
    trade_date: str = Field(default="", description="YYYY-MM-DD，默认今天")
    provider: Optional[str] = None
    deep_model: Optional[str] = None
    quick_model: Optional[str] = None
    research_depth: Optional[int] = None
    language: Optional[str] = None
    analysts: Optional[List[str]] = None
    temperature: Optional[float] = None


def _resolve_analysis_defaults(payload: AnalyzeRequest) -> None:
    """未显式传入的分析参数，套用公共设置里的默认值。"""
    analysis = _load_settings().get("analysis", {})
    payload.provider = payload.provider or analysis.get("provider", "deepseek")
    payload.deep_model = payload.deep_model or analysis.get("deep_model", "deepseek-v4-pro")
    payload.quick_model = payload.quick_model or analysis.get("quick_model", "deepseek-v4-flash")
    payload.research_depth = payload.research_depth if payload.research_depth is not None else analysis.get("research_depth", 1)
    payload.language = payload.language or analysis.get("language", "中文")
    payload.analysts = payload.analysts or analysis.get("analysts", ["market", "social", "news", "fundamentals"])
    payload.temperature = (
        payload.temperature
        if payload.temperature is not None
        else analysis.get("temperature")
    )


class Scan1234Request(BaseModel):
    symbols: List[str] = Field(default_factory=list, description="为空则扫描全部收藏")
    window_hours: int = Field(default=24, ge=1, le=168)


def _normalize_language(lang: str) -> str:
    """UI 中文标签 -> TradingAgents 期望值。"""
    return {"中文": "Chinese", "Chinese": "Chinese", "English": "English"}.get(lang.strip(), lang.strip())


def _create_job_record(
    payload: AnalyzeRequest,
    stamp: Optional[str] = None,
    checkpoint_enabled: bool = True,
) -> Dict[str, Any]:
    """创建任务记录（默认开启断点续跑，中断后可从中途继续）。"""
    return {
        "id": uuid.uuid4().hex[:12],
        "symbol": payload.symbol.strip().upper(),
        "trade_date": payload.trade_date,
        "stamp": stamp or datetime.now().strftime("%Y%m%d_%H%M%S"),
        "provider": payload.provider,
        "deep_model": payload.deep_model,
        "quick_model": payload.quick_model,
        "research_depth": payload.research_depth,
        "language": _normalize_language(payload.language),
        "analysts": payload.analysts,
        "temperature": payload.temperature,
        "checkpoint_enabled": checkpoint_enabled,
        "status": "queued",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- 收藏 API

@app.get("/api/config")
def api_config():
    info = market.config_info()
    info["models"] = _available_models()
    return info


def _available_models() -> Dict[str, List[str]]:
    """TradingAgents 各供应商的可用模型列表（含 custom 标记）。"""
    try:
        from tradingagents.llm_clients.model_catalog import get_known_models

        return get_known_models()
    except Exception:
        return {}


@app.get("/api/stocks")
def api_list_stocks(refresh: bool = False, no_snapshot: bool = False):
    """收藏列表 + 实时快照（no_snapshot=1 时跳过行情抓取）。"""
    data = load_favorites()
    stocks = data.get("stocks", [])
    symbols = [s["symbol"] for s in stocks]
    snaps = {} if no_snapshot else (market.snapshots(symbols, refresh=refresh) if symbols else {})
    result = []
    for s in stocks:
        item = {**s}
        snap = snaps.get(s["symbol"].upper(), {})
        item["snapshot"] = snap
        result.append(item)
    return {"stocks": result, "count": len(result)}


@app.get("/api/stocks/lookup")
def api_lookup(symbol: str):
    return market.lookup_info(symbol)


@app.post("/api/stocks")
def api_add_stock(payload: StockIn):
    data = load_favorites()
    symbol = payload.symbol.strip().upper()
    if not symbol:
        raise HTTPException(400, "股票代码不能为空")
    if get_stock(symbol):
        raise HTTPException(409, f"{symbol} 已在收藏中")
    if not payload.name:
        info = market.lookup_info(symbol)
        name = info.get("name") or symbol
        category = info.get("sector") or payload.category
    else:
        name = payload.name
        category = payload.category
    stock = {
        "symbol": symbol,
        "name": name,
        "category": category or "未分类",
        "strategy": payload.strategy,
        "interval": payload.interval,
        "note": payload.note,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    data.setdefault("stocks", []).append(stock)
    save_favorites(data)
    return stock


@app.put("/api/stocks/{symbol}")
def api_update_stock(symbol: str, payload: StockUpdate):
    data = load_favorites()
    for s in data.get("stocks", []):
        if s["symbol"].upper() == symbol.upper():
            for key in ("name", "category", "strategy", "interval", "note"):
                if getattr(payload, key) is not None:
                    s[key] = getattr(payload, key)
            save_favorites(data)
            return s
    raise HTTPException(404, f"{symbol} 不在收藏中")


@app.delete("/api/stocks/{symbol}")
def api_delete_stock(symbol: str):
    data = load_favorites()
    before = len(data.get("stocks", []))
    data["stocks"] = [s for s in data.get("stocks", []) if s["symbol"].upper() != symbol.upper()]
    if len(data["stocks"]) == before:
        raise HTTPException(404, f"{symbol} 不在收藏中")
    save_favorites(data)
    return {"ok": True, "symbol": symbol.upper()}


# ---------------------------------------------------------------- 行情与指标 API

@app.get("/api/stocks/{symbol}/data")
def api_stock_data(
    symbol: str,
    interval: str = "1d",
    period: Optional[str] = None,
    backtest: bool = True,
):
    return market.fetch_stock_data(symbol, interval, period, with_backtest=backtest)


@app.get("/api/stocks/{symbol}/resonance")
def api_stock_resonance(symbol: str):
    """共振指标状态：已算好返回 indicator_1234，未就绪返回 pending=true。"""
    symbol = symbol.upper().strip()
    market.ensure_indicator_bundle(symbol)
    bundle = market.get_indicator_bundle(symbol)
    res = (bundle or {}).get("resonance") if bundle else None
    return {"symbol": symbol, "indicator_1234": res, "pending": res is None}


# ---------------------------------------------------------------- 分析任务管理

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()

# ---------------------------------------------------------------- 并发设置
# 并发数可动态调整（web_config.json），不再硬编码
CONFIG_PATH = BASE_DIR / "web_config.json"
_DEFAULT_SETTINGS = {
    "max_concurrency": 5,
    "auto_scan": {"enabled": False},
    "analysis": {
        "provider": "deepseek",
        "deep_model": "deepseek-v4-pro",
        "quick_model": "deepseek-v4-flash",
        "research_depth": 1,
        "language": "中文",
        "analysts": ["market", "social", "news", "fundamentals"],
        "temperature": None,
    },
}
_settings_lock = threading.Lock()
_analysis_cond = threading.Condition()
_active_analyses = 0


def _load_settings() -> Dict[str, Any]:
    settings = dict(_DEFAULT_SETTINGS)
    if CONFIG_PATH.exists():
        try:
            settings.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return settings


def _save_settings(settings: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _acquire_analysis_slot() -> None:
    """等待可用并发位（每次读取最新配置，改并发立即生效）。"""
    global _active_analyses
    with _analysis_cond:
        while _active_analyses >= _load_settings()["max_concurrency"]:
            _analysis_cond.wait()
        _active_analyses += 1


def _release_analysis_slot() -> None:
    global _active_analyses
    with _analysis_cond:
        _active_analyses -= 1
        _analysis_cond.notify_all()


class AnalysisSettings(BaseModel):
    provider: Optional[str] = None
    deep_model: Optional[str] = None
    quick_model: Optional[str] = None
    research_depth: Optional[int] = Field(default=None, ge=1, le=10)
    language: Optional[str] = None
    analysts: Optional[List[str]] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)


class SettingsIn(BaseModel):
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=200)
    auto_scan_enabled: Optional[bool] = None
    analysis: Optional[AnalysisSettings] = None


@app.get("/api/settings")
def api_get_settings():
    settings = _load_settings()
    with _jobs_lock:
        jobs = list(_jobs.values()) or [
            _job_from_disk(jf.stem) for jf in JOBS_DIR.glob("*.json")
        ]
    active = sum(1 for j in jobs if j.get("status") == "running")
    queued = sum(1 for j in jobs if j.get("status") == "queued")
    return {
        "max_concurrency": settings["max_concurrency"],
        "auto_scan": {
            **settings.get("auto_scan", {"enabled": False}),
            "last_scan_at": _auto_scan_state.get("last_scan_at"),
            "last_error": _auto_scan_state.get("last_error"),
            "next_slots": _next_scan_slots(),
        },
        "analysis": settings.get("analysis", _DEFAULT_SETTINGS["analysis"]),
        "active_analyses": _active_analyses,
        "running": active,
        "queued": queued,
    }


@app.put("/api/settings")
def api_put_settings(payload: SettingsIn):
    settings = _load_settings()
    if payload.max_concurrency is not None:
        settings["max_concurrency"] = payload.max_concurrency
    if payload.auto_scan_enabled is not None:
        settings.setdefault("auto_scan", {})["enabled"] = payload.auto_scan_enabled
    if payload.analysis is not None:
        current = settings.setdefault("analysis", dict(_DEFAULT_SETTINGS["analysis"]))
        data = payload.analysis.model_dump(exclude_unset=True)
        for key, value in data.items():
            # 显式 null 仅允许用于 temperature（还原模型默认）；其余忽略未传字段
            if value is not None or key == "temperature":
                current[key] = value
    with _settings_lock:
        _save_settings(settings)
    with _analysis_cond:
        _analysis_cond.notify_all()
    return api_get_settings()


# ---------------------------------------------------------------- 自动共振扫描
# 美股交易时段（美东 09:31 ~ 16:01）每 30 分钟一个扫描节点（:01 与 :31），
# 即整点/半点后约 1 分钟触发；开关由公共设置 auto_scan.enabled 控制。
_auto_scan_state: Dict[str, Any] = {"last_scan_at": None, "last_error": None}
_auto_scan_lock = threading.Lock()
_scan_done_slots: set = set()
_SCAN_SLOT_MINUTES = {
    "09:31", "10:01", "10:31", "11:01", "11:31", "12:01", "12:31",
    "13:01", "13:31", "14:01", "14:31", "15:01", "15:31", "16:01",
}


def _et_now():
    import zoneinfo

    return datetime.now(zoneinfo.ZoneInfo("America/New_York"))


def _next_scan_slots(n: int = 3) -> List[str]:
    """最近 n 个扫描节点（美东时间 ISO 字符串）。"""
    now = _et_now()
    out = []
    cursor = now
    while len(out) < n:
        day = cursor.date()
        if day.weekday() < 5:  # 只展示交易日（周一至周五）
            for hhmm in sorted(_SCAN_SLOT_MINUTES):
                h, m = map(int, hhmm.split(":"))
                ts = datetime(day.year, day.month, day.day, h, m, tzinfo=cursor.tzinfo)
                if ts >= now and ts.strftime("%Y-%m-%d %H:%M") not in out:
                    out.append(ts.isoformat())
        cursor += timedelta(days=1)
    return out[:n]


def _auto_scan_run(symbols: List[str]) -> None:
    try:
        for sym in symbols:
            market.compute_indicator_bundle_sync(sym)
        with _auto_scan_lock:
            _auto_scan_state["last_scan_at"] = datetime.now().isoformat(timespec="seconds")
            _auto_scan_state["last_error"] = None
        logger.info("自动共振扫描完成，共 %d 只", len(symbols))
    except Exception as exc:
        with _auto_scan_lock:
            _auto_scan_state["last_error"] = str(exc)
        logger.warning("自动共振扫描失败: %s", exc)


def _auto_scan_worker() -> None:
    while True:
        try:
            now = _et_now()
            if now.weekday() < 5:  # 周一至周五
                enabled = _load_settings().get("auto_scan", {}).get("enabled", False)
                if enabled and now.strftime("%H:%M") in _SCAN_SLOT_MINUTES:
                    slot = now.strftime("%Y-%m-%d %H:%M")
                    if slot not in _scan_done_slots:
                        _scan_done_slots.add(slot)
                        symbols = [
                            s["symbol"].upper()
                            for s in load_favorites().get("stocks", [])
                        ]
                        threading.Thread(
                            target=_auto_scan_run, args=(symbols,), daemon=True
                        ).start()
                if len(_scan_done_slots) > 100:
                    _scan_done_slots.clear()
        except Exception:
            pass
        time.sleep(30)


def _load_jobs_from_disk() -> None:
    for jf in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(jf.read_text(encoding="utf-8"))
            if job.get("status") in ("queued", "running"):
                # 若中断前其实已写完报告（孤儿进程收尾），直接标记完成
                save_dir = Path(job.get("save_dir") or "")
                complete = save_dir / "complete_report.md"
                if save_dir.is_dir() and complete.exists():
                    job["status"] = "done"
                    _detect_report(job)
                else:
                    job["status"] = "interrupted"
                    job["error"] = "服务重启导致任务中断"
                _write_job(job)
            _jobs[job["id"]] = job
        except Exception:
            continue


def _resume_job_record(job: Dict[str, Any]) -> None:
    """把中断/失败的任务原地复活为排队中（同一 id/参数/报告目录，开启断点续跑）。"""
    job["status"] = "queued"
    job["checkpoint_enabled"] = True
    job["created_at"] = datetime.now().isoformat(timespec="seconds")
    for key in ("error", "finished_at", "pid", "canceled", "report_id",
                "report_ticker", "report_path", "decision"):
        job.pop(key, None)
    with _jobs_lock:
        _jobs[job["id"]] = job
        _write_job(job)


def _auto_resume_interrupted() -> int:
    """重启后自动重新启动因服务重启而中断的任务。"""
    count = 0
    for job in list(_jobs.values()):
        if job.get("status") == "interrupted" and job.get("error") == "服务重启导致任务中断":
            # 若报告目录里已有完整报告（其他任务已完成同一标的），标记完成，不重复跑
            save_dir = Path(job.get("save_dir") or "")
            complete = save_dir / "complete_report.md"
            if save_dir.is_dir() and complete.exists():
                job["status"] = "done"
                job.pop("error", None)
                _detect_report(job)
                with _jobs_lock:
                    _write_job(job)
                continue
            logger.info("自动续跑重启中断的任务 %s (%s)", job["id"], job["symbol"])
            _resume_job_record(job)
            threading.Thread(target=_run_analysis_job, args=(job,), daemon=True).start()
            count += 1
    return count


def _write_job(job: Dict[str, Any]) -> None:
    (JOBS_DIR / f"{job['id']}.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _job_log_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def _job_from_disk(job_id: str) -> Dict[str, Any]:
    jf = JOBS_DIR / f"{job_id}.json"
    if jf.exists():
        return json.loads(jf.read_text(encoding="utf-8"))
    return {}


def _log_tail(job: Dict[str, Any], lines: int = 120) -> str:
    log_path = _job_log_path(job["id"])
    if not log_path.exists():
        return ""
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(content.splitlines()[-lines:])
    except Exception:
        return ""


def _detect_report(job: Dict[str, Any]) -> None:
    """任务结束后从保存目录中找出报告 id。"""
    if not job.get("save_dir"):
        return
    save_dir = Path(job["save_dir"])
    complete = save_dir / "complete_report.md"
    if complete.exists():
        job["report_ticker"] = job["symbol"].upper()
        job["report_id"] = save_dir.name
        job["report_path"] = str(complete)
        try:
            meta = json.loads((save_dir / "meta.json").read_text(encoding="utf-8"))
            job["decision"] = meta.get("decision")
        except Exception:
            pass


def _monitor_job(job_id: str, proc: subprocess.Popen) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id, {})
    try:
        returncode = proc.wait()
    except Exception as exc:
        returncode = -1
        job["error"] = str(exc)

    with _jobs_lock:
        job = _jobs.get(job_id, {})
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if returncode == 0:
            job["status"] = "done"
        elif job.get("canceled"):
            job["status"] = "canceled"
        else:
            job["status"] = "failed"
            if not job.get("error"):
                job["error"] = f"进程退出码 {returncode}"
        _detect_report(job)
        _write_job(job)
        if job.get("status") == "done":
            # 每次分析完成，后台复盘全部历史决策（胜率随价格变化自动刷新）
            threading.Thread(target=_trigger_decision_review, daemon=True).start()


def _run_analysis_job(job: Dict[str, Any]) -> None:
    """后台线程：拉起子进程执行 run_analysis.py。"""
    job_id = job["id"]
    symbol = job["symbol"].upper()
    # 与老 CLI 报告命名一致：<TICKER>_<YYYYMMDD_HHMMSS>
    save_dir = REPORTS_DIR / f"{symbol}_{job['stamp']}"
    job["save_dir"] = str(save_dir)

    cmd = [
        PYTHON, str(BASE_DIR / "run_analysis.py"),
        "--symbol", symbol,
        "--date", job["trade_date"],
        "--save-dir", str(save_dir),
        "--provider", job.get("provider", "deepseek"),
        "--deep-model", job.get("deep_model", "deepseek-v4-pro"),
        "--quick-model", job.get("quick_model", "deepseek-v4-flash"),
        "--research-depth", str(job.get("research_depth", 1)),
        "--language", job.get("language", "中文"),
        "--analysts", ",".join(job.get("analysts", ["market", "social", "news", "fundamentals"])),
    ]
    if job.get("temperature") is not None:
        cmd += ["--temperature", str(job["temperature"])]
    if job.get("checkpoint_enabled"):
        cmd += ["--checkpoint"]

    log_path = _job_log_path(job_id)
    logger.info("启动分析任务 %s: %s", job_id, " ".join(cmd))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    _acquire_analysis_slot()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(TA_DIR),
            env=env,
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        job["pid"] = proc.pid
        job["status"] = "running"
        with _jobs_lock:
            _write_job(job)
        _monitor_job(job_id, proc)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        with _jobs_lock:
            _write_job(job)
    finally:
        _release_analysis_slot()


@app.post("/api/analyze")
def api_analyze(payload: AnalyzeRequest):
    symbol = payload.symbol.strip().upper()
    if not symbol:
        raise HTTPException(400, "股票代码不能为空")
    if not payload.trade_date:
        payload.trade_date = datetime.now().strftime("%Y-%m-%d")
    _resolve_analysis_defaults(payload)

    job = _create_job_record(payload)
    job_id = job["id"]
    with _jobs_lock:
        _jobs[job_id] = job
        _write_job(job)
    threading.Thread(target=_run_analysis_job, args=(job,), daemon=True).start()
    return job


@app.post("/api/jobs/{job_id}/resume")
def api_resume_job(job_id: str):
    """续跑中断/失败的任务：直接复用原任务记录（同一 id/参数/报告目录），并开启断点续跑。"""
    with _jobs_lock:
        src = _jobs.get(job_id) or _job_from_disk(job_id)
    if not src:
        raise HTTPException(404, "任务不存在")
    if src.get("status") not in ("interrupted", "failed"):
        raise HTTPException(400, "仅「中断」或「失败」的任务可以续跑")

    _resume_job_record(src)
    threading.Thread(target=_run_analysis_job, args=(src,), daemon=True).start()
    return src


class BatchAnalyzeRequest(BaseModel):
    symbols: List[str]
    trade_date: str = ""
    provider: Optional[str] = None
    deep_model: Optional[str] = None
    quick_model: Optional[str] = None
    research_depth: Optional[int] = None
    language: Optional[str] = None
    analysts: Optional[List[str]] = None
    temperature: Optional[float] = None


@app.post("/api/analyze/batch")
def api_analyze_batch(payload: BatchAnalyzeRequest):
    jobs = []
    trade_date = payload.trade_date or datetime.now().strftime("%Y-%m-%d")
    for raw in payload.symbols:
        symbol = raw.strip().upper()
        if not symbol:
            continue
        req = AnalyzeRequest(
            symbol=symbol,
            trade_date=trade_date,
            provider=payload.provider,
            deep_model=payload.deep_model,
            quick_model=payload.quick_model,
            research_depth=payload.research_depth,
            language=payload.language,
            analysts=payload.analysts,
            temperature=payload.temperature,
        )
        jobs.append(api_analyze(req))
    return {"created": jobs}


@app.get("/api/jobs")
def api_jobs():
    """只返回「当前任务」：进行中 + 3 天内中断/失败（可续跑的）。"""
    jobs = []
    for jf in sorted(JOBS_DIR.glob("*.json"), reverse=True):
        try:
            job = json.loads(jf.read_text(encoding="utf-8"))
            if not _is_current_job(job):
                continue
            job.pop("decision", None)
            job["name"] = _resolve_name(job.get("symbol", ""))
            job["log_tail"] = _log_tail(job, 80)
            jobs.append(job)
        except Exception:
            continue
    return {"jobs": jobs}


def _is_current_job(job: Dict[str, Any]) -> bool:
    """当前任务 = 进行中（queued/running）+ 3 天内中断/失败（可续跑）。"""
    status = job.get("status")
    if status in ("queued", "running"):
        return True
    if status not in ("interrupted", "failed"):
        return False
    try:
        created = datetime.fromisoformat(job.get("created_at", ""))
    except (TypeError, ValueError):
        return False
    return datetime.now() - created <= timedelta(days=3)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, tail: int = 120):
    with _jobs_lock:
        job = _jobs.get(job_id) or _job_from_disk(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    job = {**job}
    job["log_tail"] = _log_tail(job, tail)
    return job


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id) or _job_from_disk(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.get("status") in ("done", "failed", "canceled"):
        return job
    pid = job.get("pid")
    if pid:
        try:
            os.kill(pid, 15)  # SIGTERM
        except Exception:
            pass
    job["canceled"] = True
    job["status"] = "canceled"
    job["finished_at"] = datetime.now().isoformat(timespec="seconds")
    with _jobs_lock:
        _write_job(job)
    return job


# ---------------------------------------------------------------- 报告 API

_REPORT_STAMP_RE = re.compile(r"^(?P<ticker>.+)_(?P<stamp>\d{8}_\d{6})$")


def _iter_report_dirs():
    """遍历报告目录，兼容两种布局：
    扁平：TradingAgents/reports/<TICKER>_<stamp>/（老 CLI 与 web 新报告）
    嵌套：<ticker>/<stamp>/（早期 web 版本遗留）
    """
    if not REPORTS_DIR.is_dir():
        return
    for item in sorted(REPORTS_DIR.iterdir()):
        if not item.is_dir():
            continue
        m = _REPORT_STAMP_RE.match(item.name)
        if m:
            yield m.group("ticker"), item.name, item
            continue
        # 嵌套布局：ticker 目录下再按 stamp 分目录
        for sub in sorted(item.iterdir()):
            if not sub.is_dir():
                continue
            if _REPORT_STAMP_RE.match(sub.name) or (sub / "complete_report.md").exists():
                yield item.name, sub.name, sub


def _find_report_dir(ticker: str, report_id: str) -> Optional[Path]:
    """按 ticker + report_id 定位报告目录（兼容扁平/嵌套布局）。"""
    t = ticker.upper()
    candidates = []
    # 嵌套布局
    candidates.append(REPORTS_DIR / t / report_id)
    # 扁平布局
    candidates.append(REPORTS_DIR / report_id)
    if report_id.startswith(t + "_"):
        candidates.append(REPORTS_DIR / report_id)
    for c in candidates:
        if c.is_dir() and ((c / "complete_report.md").exists() or (c / "meta.json").exists()):
            return c
    # 兜底：ticker 前缀匹配的扁平目录（按名字倒序，优先最新）
    for item in sorted(REPORTS_DIR.iterdir(), reverse=True):
        if item.is_dir() and item.name.startswith(t + "_") and (item / "complete_report.md").exists():
            return item
    return None


def _list_report_dirs() -> Dict[str, List[Dict[str, Any]]]:
    """扫描报告目录，按股票分组返回报告清单。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for ticker, report_id, report_dir in _iter_report_dirs():
        complete = report_dir / "complete_report.md"
        meta = {}
        meta_path = report_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        out.setdefault(ticker, []).append({
            "id": report_id,
            "ticker": ticker,
            "name": _resolve_name(ticker),
            "date": meta.get("date") or _stamp_date(report_id),
            "status": meta.get("status", "done"),
            "created": meta.get("completed_at") or _stamp_datetime(report_id),
            "path": str(complete) if complete.exists() else "",
            "sections": _report_sections(report_dir),
            "decision": _extract_decision(report_dir),
        })
    # 按 ticker 字母排序、组内按报告目录名倒序（新在前）
    for ticker in out:
        out[ticker].sort(key=lambda x: x["id"], reverse=True)
    return out


def _favorites_name_map() -> Dict[str, str]:
    """收藏列表的 代码 -> 公司名 映射，用于报告/决策/任务展示。"""
    data = load_favorites()
    return {
        s["symbol"].upper(): (s.get("name") or "")
        for s in data.get("stocks", [])
    }


# 名称解析：收藏 -> names.json 缓存 -> yfinance 补全（落盘），避免已删除收藏的股票显示代码
_name_cache: Dict[str, str] = {}


def _load_names_cache() -> Dict[str, str]:
    if NAMES_PATH.exists():
        try:
            return json.loads(NAMES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_names_cache(names: Dict[str, str]) -> None:
    try:
        NAMES_PATH.write_text(
            json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _resolve_name(ticker: str) -> str:
    """解析股票显示名（多线程下以缓存命中为主，避免重复网络请求）。"""
    ticker = ticker.upper().strip()
    if ticker in _name_cache:
        return _name_cache[ticker]

    # 收藏优先
    fav = _favorites_name_map().get(ticker)
    if fav:
        _name_cache[ticker] = fav
        return fav

    # 磁盘缓存
    disk = _load_names_cache()
    if ticker in disk:
        _name_cache[ticker] = disk[ticker]
        return disk[ticker]

    # yfinance 补全（一次成功/失败都会缓存，避免反复请求）
    name = ""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info
        name = (
            info.get("shortName")
            or info.get("longName")
            or info.get("displayName")
            or ""
        )
    except Exception:
        name = ""
    _name_cache[ticker] = name
    disk[ticker] = name
    _save_names_cache(disk)
    return name


def _prewarm_names() -> None:
    """后台预热：为所有报告中的股票解析显示名。"""
    try:
        for ticker, _reports in _list_report_dirs().items():
            _resolve_name(ticker)
    except Exception:
        pass


def _trigger_decision_review() -> None:
    """后台复盘全部历史决策并刷新胜率库。"""
    try:
        fav_symbols = {
            s["symbol"].upper() for s in load_favorites().get("stocks", [])
        }
        reports = {
            t: rs
            for t, rs in _list_report_dirs().items()
            if t.upper() in fav_symbols
        }
        review.run_review(reports)
        logger.info("决策胜率复盘完成")
    except Exception as exc:
        logger.warning("决策胜率复盘失败: %s", exc)


def _stamp_date(report_id: str) -> str:
    """从报告目录名（..._YYYYMMDD_HHMMSS）推导日期，兼容历史 CLI 报告。"""
    m = _REPORT_STAMP_RE.match(report_id or "")
    if m:
        stamp = m.group("stamp")
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    return ""


def _stamp_datetime(report_id: str) -> str:
    m = _REPORT_STAMP_RE.match(report_id or "")
    if m:
        stamp = m.group("stamp")
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
    return ""


_RATING_CN = {
    "Buy": "买入",
    "Overweight": "增持",
    "Hold": "持有",
    "Underweight": "减持",
    "Sell": "卖出",
}
_RATING_ACTION = {
    "Buy": "buy",
    "Overweight": "overweight",
    "Hold": "hold",
    "Underweight": "underweight",
    "Sell": "sell",
}
_RATING_ALIAS = {
    "buy": "Buy", "买入": "Buy", "看多": "Buy",
    "overweight": "Overweight", "增持": "Overweight", "加仓": "Overweight",
    "hold": "Hold", "持有": "Hold", "观望": "Hold",
    "underweight": "Underweight", "减持": "Underweight", "减仓": "Underweight",
    "sell": "Sell", "卖出": "Sell", "看空": "Sell", "清仓": "Sell",
}
_TRADER_ACTION_CN = {"Buy": "买入", "Hold": "持有", "Sell": "卖出"}


def _md_field(text: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, text, re.I | re.S)
    return m.group(1).strip() if m else None


def _parse_rating(text: str) -> Optional[str]:
    """兼容多种评级写法：标准 **Rating**: X、中文 投资评级/评级：买入(卖出) 等。"""
    m = re.search(r"\*\*Rating\*\*\s*[:：]?\s*([A-Za-z]+)", text, re.I)
    if m:
        return _RATING_ALIAS.get(m.group(1).strip().lower(), m.group(1).title())
    m = re.search(
        # 兼容：**评级：Hold** / **投资评级：** **卖出** / **评级**：**Overweight**
        r"(?:投资评级|投资建议|最终评级|评级)\s*\*{0,2}\s*[:：]?\s*\*{0,2}\s*\*{0,2}\s*"
        r"(买入|增持|持有|减持|卖出|看多|看空|观望|加仓|减仓|清仓|[A-Za-z]+)",
        text,
        re.I,
    )
    if m:
        token = m.group(1).strip()
        return _RATING_ALIAS.get(token.lower(), token.title())
    return None


def _extract_decision(report_dir: Path) -> Dict[str, Any]:
    """抽取每份报告的综合决议。

    主字段来自投资组合经理（Portfolio Manager）的 decision.md —— 这是整条
    分析链路的最终裁决（Rating / 目标价 / 时间跨度 / 策略摘要）；交易员的
    trader.md 只是中间提案，作为参考信息保留（方向 / 入场价 / 止损 / 分仓）。
    历史与新建报告均为同一渲染格式。
    """
    trader_md = decision_md = ""
    trader_file = report_dir / "3_trading" / "trader.md"
    decision_file = report_dir / "5_portfolio" / "decision.md"
    if trader_file.exists():
        trader_md = trader_file.read_text(encoding="utf-8", errors="replace")
    if decision_file.exists():
        decision_md = decision_file.read_text(encoding="utf-8", errors="replace")
    if not trader_md and not decision_md:
        complete = report_dir / "complete_report.md"
        if complete.exists():
            full = complete.read_text(encoding="utf-8", errors="replace")
            trader_md = decision_md = full

    final_d: Dict[str, Any] = {}
    trader_d: Dict[str, Any] = {}

    if decision_md:
        rating = _parse_rating(decision_md)
        final_d["rating"] = rating
        final_d["rating_cn"] = _RATING_CN.get(rating or "", None)
        final_d["action"] = _RATING_ACTION.get(rating or "", None)
        final_d["price_target"] = _md_field(
            decision_md, r"\*\*Price Target\*\*\s*[:：]\s*([\d.,]+)"
        ) or _md_field(decision_md, r"(?:目标价|目标价格|价格目标)\s*[:：]?\s*([\d.,]+)")
        final_d["time_horizon"] = _md_field(
            decision_md, r"\*\*Time Horizon\*\*\s*[:：]\s*([^\n]+)"
        ) or _md_field(
            decision_md,
            r"(?:时间跨度|时间框架|持有期|时间窗口)\s*[:：]?\s*\*{0,2}\s*"
            r"([\u4e00-\u9fa5A-Za-z0-9（）()][^*\n|]{0,59})",
        )
        summary = _md_field(
            decision_md,
            r"\*\*Executive Summary\*\*\s*[:：]\s*(.+?)(?=\n\s*\n\s*\*\*|\Z)",
        )
        final_d["executive_summary"] = (summary or "")[:400]

    if trader_md:
        action = _md_field(trader_md, r"\*\*Action\*\*\s*[:：]\s*(Buy|Hold|Sell)")
        if not action:
            action = _md_field(
                trader_md, r"FINAL TRANSACTION PROPOSAL:\s*\*{0,2}(BUY|SELL|HOLD)"
            )
            action = action.title() if action else None
        trader_d["action"] = action.lower() if action else None
        trader_d["action_cn"] = _TRADER_ACTION_CN.get(action or "", None)
        trader_d["entry_price"] = _md_field(trader_md, r"\*\*Entry Price\*\*\s*[:：]\s*([\d.,]+)")
        trader_d["stop_loss"] = _md_field(trader_md, r"\*\*Stop Loss\*\*\s*[:：]\s*([\d.,]+)")
        position = _md_field(
            trader_md, r"\*\*Position Sizing\*\*\s*[:：]\s*(.+?)(?=\n\s*\n|\Z)"
        )
        trader_d["position_sizing"] = (position or "")[:300]

    return {"final": final_d, "trader": trader_d}


_SECTION_TITLES = {
    "market": "市场分析",
    "sentiment": "情绪分析",
    "news": "新闻分析",
    "fundamentals": "基本面分析",
    "bull": "多方研究员",
    "bear": "空方研究员",
    "manager": "研究主管",
    "trader": "交易员计划",
    "aggressive": "激进风险分析师",
    "neutral": "中性风险分析师",
    "conservative": "保守风险分析师",
    "decision": "投资组合经理决策",
}


def _report_sections(report_dir: Path) -> List[Dict[str, str]]:
    """把报告目录中的 markdown 文件整理为有序章节。"""
    sections: List[Dict[str, str]] = []
    order = [
        ("1_analysts", "market.md", "market", "市场分析"),
        ("1_analysts", "sentiment.md", "sentiment", "情绪分析"),
        ("1_analysts", "news.md", "news", "新闻分析"),
        ("1_analysts", "fundamentals.md", "fundamentals", "基本面分析"),
        ("2_research", "bull.md", "bull", "多方研究员"),
        ("2_research", "bear.md", "bear", "空方研究员"),
        ("2_research", "manager.md", "manager", "研究主管决策"),
        ("3_trading", "trader.md", "trader", "交易团队计划"),
        ("4_risk", "aggressive.md", "aggressive", "激进风险分析师"),
        ("4_risk", "neutral.md", "neutral", "中性风险分析师"),
        ("4_risk", "conservative.md", "conservative", "保守风险分析师"),
        ("5_portfolio", "decision.md", "decision", "投资组合经理决策"),
    ]
    for sub, fname, key, title in order:
        f = report_dir / sub / fname
        if f.exists():
            sections.append({
                "key": key,
                "title": title,
                "markdown": f.read_text(encoding="utf-8", errors="replace"),
            })
    return sections


@app.get("/api/reports")
def api_reports():
    return _list_report_dirs()


@app.get("/api/decisions")
def api_decisions():
    """全部报告的结构化决策总览（买入/卖出/持有 + 价格 + 分仓）。"""
    fav_symbols = {
        s["symbol"].upper() for s in load_favorites().get("stocks", [])
    }
    rows = []
    for ticker, reports in _list_report_dirs().items():
        if ticker.upper() not in fav_symbols:
            continue
        for r in reports:
            rows.append({
                "ticker": ticker,
                "name": r.get("name", ""),
                "report_id": r["id"],
                "date": r["date"],
                "created": r["created"],
                "status": r["status"],
                "decision": r.get("decision") or {},
            })
    rows.sort(key=lambda x: (x["date"] or "", x["created"] or ""), reverse=True)
    return {"decisions": rows}


@app.get("/api/review")
def api_review():
    """决策胜率复盘：摘要 + 每条决策的评估明细。"""
    fav_symbols = {
        s["symbol"].upper() for s in load_favorites().get("stocks", [])
    }
    store = {
        rid: d
        for rid, d in review.load_store().items()
        if isinstance(d, dict) and str(d.get("ticker", "")).upper() in fav_symbols
    }
    summary = review.summarize(store)
    name_map = _favorites_name_map()
    items = []
    for d in store.values():
        if not isinstance(d, dict):
            continue
        items.append({**d, "name": name_map.get(str(d.get("ticker", "")).upper(), "")})
    items.sort(key=lambda x: (x.get("date") or "", x.get("report_id") or ""), reverse=True)
    return {"summary": summary, "decisions": items}


@app.post("/api/review/run")
def api_review_run():
    """立即触发一次后台复盘（胜率随最新价格刷新）。"""
    threading.Thread(target=_trigger_decision_review, daemon=True).start()
    return {"started": True}


@app.post("/api/scan/1234")
def api_scan_1234(payload: Scan1234Request):
    """批量扫描 1234 共振（异步指标引擎）：
    按请求的时间窗口计算 MRMC 共振，立即返回已就绪的结果 + 仍在计算中的列表，前端轮询补齐。
    """
    symbols = [s.strip().upper() for s in payload.symbols if s.strip()]
    if not symbols:
        symbols = [s["symbol"].upper() for s in load_favorites().get("stocks", [])]
    window_hours = payload.window_hours
    if not symbols:
        return {"results": [], "computing": [], "window_hours": window_hours}

    results = []
    computing = []
    for sym in symbols:
        # 顺带预热指标包：卡片/详情页复用同一份缓存
        try:
            market.ensure_indicator_bundle(sym)
        except Exception:
            pass
        # 共振按请求的时间窗口单独计算（指标包里的那份固定 24h，供卡片展示）
        if not market.ensure_resonance(sym, window_hours):
            computing.append(sym)
            continue
        resonance = market.get_resonance(sym, window_hours)
        if resonance:
            results.append({"symbol": sym, "name": _resolve_name(sym), **resonance})
        else:
            computing.append(sym)

    order = {"full": 0, "major": 1, "none": 2}
    results.sort(
        key=lambda x: (
            order.get(x.get("tier"), 2),
            -x.get("fired_count", 0),
            x["symbol"],
        )
    )
    return {
        "results": results,
        "computing": computing,
        "window_hours": window_hours,
    }


@app.get("/api/reports/{ticker}/{report_id}")
def api_report(ticker: str, report_id: str):
    report_dir = _find_report_dir(ticker, report_id)
    if report_dir is None:
        raise HTTPException(404, "报告不存在")
    complete = report_dir / "complete_report.md"
    sections = _report_sections(report_dir)
    meta = {}
    meta_path = report_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "ticker": ticker.upper(),
        "report_id": report_id,
        "meta": meta,
        "sections": sections,
        "decision": _extract_decision(report_dir),
        "raw": complete.read_text(encoding="utf-8", errors="replace") if complete.exists() else "",
    }


@app.get("/api/reports/{ticker}/{report_id}/raw")
def api_report_raw(ticker: str, report_id: str):
    report_dir = _find_report_dir(ticker, report_id)
    if report_dir is None:
        raise HTTPException(404, "报告不存在")
    complete = report_dir / "complete_report.md"
    if not complete.exists():
        raise HTTPException(404, "报告不存在")
    return PlainTextResponse(complete.read_text(encoding="utf-8", errors="replace"))


@app.delete("/api/reports/{ticker}/{report_id}")
def api_delete_report(ticker: str, report_id: str):
    report_dir = _find_report_dir(ticker, report_id)
    if report_dir is None:
        raise HTTPException(404, "报告不存在")
    report_dir = report_dir.resolve()
    if not report_dir.is_relative_to(REPORTS_DIR.resolve()):
        raise HTTPException(400, "非法路径")
    shutil.rmtree(report_dir)
    return {"ok": True}


# ---------------------------------------------------------------- 静态资源

app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("STOCK_WEB_PORT", "8787"))
    logger.info("Stock Dashboard 运行在 http://127.0.0.1:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
