"""K 线周期（interval）的单一事实来源。

「一个周期」这件事以前散在四张表里，各写各的：

  * ``data_fetcher.INTERVAL_PERIOD_MAP``   能否取、默认回溯多久
  * ``stock_web.market.PERIOD_MAP`` / ``INTERVALS`` / ``_RESAMPLE_RULES``
  * ``indicators_nx.INTERVAL_MS``          一根 K 线多少毫秒
  * ``main.interval_to_minutes``           调度间隔

四张表互相之间没有约束，改一处漏一处 —— 90m 就漏过一份，前端选到它直接报
``ValueError``。现在每个周期只在这里声明一次，其余模块从这里取。

用法::

    from intervals import period_of, ms_of, minutes_of, resample_rule

    period_of("90m")        # '60d'
    ms_of("2h")             # 7_200_000
    minutes_of("1d")        # 1440      （给调度器）
    resample_rule("4h")     # '4h'      （None 表示 yfinance 原生支持）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    """一个 K 线周期的全部已知信息。"""

    name: str                     # 对外周期名，如 "90m"
    period: str                   # 默认回溯范围，如 "60d"
    ms: int                       # 一根 K 线多少毫秒
    fetch_as: str                 # 真正向 yfinance 请求的周期
    resample: str | None = None   # 非空：yfinance 没有这个周期，用它聚合出来
    ui: bool = False              # 是否出现在看板的周期下拉里


# yfinance 原生支持 1m/2m/5m/15m/30m/60m/90m/1h/1d/1wk/1mo，直接取；
# 2h/3h/4h 它没有，统一从 1h 聚合 —— 这样三者与 1h 同源、K 线边界天然对齐。
_SPECS: tuple[Interval, ...] = (
    Interval("1m",  "7d",          60_000,          "1m",  ui=True),
    Interval("2m",  "60d",         120_000,         "2m"),
    Interval("5m",  "60d",         300_000,         "5m",  ui=True),
    Interval("15m", "60d",         900_000,         "15m", ui=True),
    Interval("30m", "60d",         1_800_000,       "30m", ui=True),
    Interval("60m", "730d",        3_600_000,       "60m"),
    Interval("90m", "60d",         5_400_000,       "90m", ui=True),
    Interval("1h",  "730d",        3_600_000,       "1h",  ui=True),
    Interval("2h",  "730d",        7_200_000,       "1h",  resample="2h", ui=True),
    Interval("3h",  "730d",        10_800_000,      "1h",  resample="3h", ui=True),
    Interval("4h",  "730d",        14_400_000,      "1h",  resample="4h", ui=True),
    Interval("1d",  "5y",          86_400_000,      "1d",  ui=True),
    Interval("1wk", "10y",         604_800_000,     "1wk", ui=True),
    Interval("1mo", "max",         2_592_000_000,   "1mo"),
)

INTERVALS: dict[str, Interval] = {spec.name: spec for spec in _SPECS}

# 认不出来的周期，调度器按 15 分钟跑（沿用旧行为）
DEFAULT_MINUTES = 15


def get(name: str) -> Interval | None:
    """按名字取周期定义，未知返回 None。"""
    return INTERVALS.get(name)


def is_supported(name: str) -> bool:
    """这个周期能不能取到数据。"""
    return name in INTERVALS


def period_of(name: str, default: str = "1y") -> str:
    """默认回溯范围；未知周期回退到 default。"""
    spec = INTERVALS.get(name)
    return spec.period if spec else default


def ms_of(name: str, default: int = -1) -> int:
    """一根 K 线的毫秒数；未知周期返回 default。"""
    spec = INTERVALS.get(name)
    return spec.ms if spec else default


def minutes_of(name: str, default: int = DEFAULT_MINUTES) -> int:
    """给调度器用：一根 K 线多少分钟。"""
    spec = INTERVALS.get(name)
    if spec is None:
        return default
    return max(1, round(spec.ms / 60_000))


def fetch_as(name: str) -> str:
    """真正要请求 yfinance 的周期（2h/3h/4h 会退化成 1h）。"""
    spec = INTERVALS.get(name)
    return spec.fetch_as if spec else name


def resample_rule(name: str) -> str | None:
    """重采样规则；None 表示 yfinance 原生支持、不用聚合。"""
    spec = INTERVALS.get(name)
    return spec.resample if spec else None


def ui_names() -> list[str]:
    """看板周期下拉的选项，保持声明顺序。"""
    return [spec.name for spec in _SPECS if spec.ui]


def periods() -> dict[str, str]:
    """{周期: 默认回溯范围}，看板 /api/config 用。"""
    return {spec.name: spec.period for spec in _SPECS}
