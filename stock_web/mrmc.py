"""
mrmc.py - MRMC(买入卖出)指标
============================
由用户提供的富途/通达信公式翻译而来：
MACD 底背离「抄底」+ 顶背离「卖出」信号系统。

原公式参数 S/P/M 未给出，按 MACD 标准参数 S=12 / P=26 / M=9 处理。
公式原说明：务必严格按照原版使用教程使用，建议结合 NX 指标一起判断。

信号定义（与公式一致）：
  buy  = DXDX   -> DRAWTEXT(..., DIFF/0.81, '抄底')
  sell = DBJGXC -> DRAWTEXT(..., DIFF*1.21, '卖出')
"""

from __future__ import annotations

from collections import deque
from typing import Dict, List

import numpy as np


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """TDX 风格 EMA：alpha = 2/(span+1)，与 pandas ewm(span, adjust=False) 一致。"""
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _ref_num(arr: np.ndarray, k: np.ndarray | int) -> np.ndarray:
    """REF(arr, k)：取 k 根K线前的值，越界为 NaN。k 可为数组（逐根不同偏移）。"""
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    if isinstance(k, int):
        if 0 <= k < n:
            out[k:] = arr[: n - k]
    else:
        k = np.asarray(k, dtype=int)
        for i in range(n):
            j = i - k[i]
            if j >= 0:
                out[i] = arr[j]
    return out


def _ref_bool(arr: np.ndarray, k: np.ndarray | int) -> np.ndarray:
    """REF 的布尔版本：越界视为 False。"""
    return np.nan_to_num(_ref_num(arr.astype(float), k), nan=0.0).astype(bool)


def _barslast(cond: np.ndarray) -> np.ndarray:
    """BARSLAST(cond)：距离上一次 cond 为真的K线数（当前为真则 0；从未为真则覆盖全部）。"""
    n = len(cond)
    out = np.zeros(n, dtype=int)
    last = -1
    for i in range(n):
        if cond[i]:
            last = i
        out[i] = i - last if last >= 0 else i
    return out


def _llv(arr: np.ndarray, windows: np.ndarray) -> np.ndarray:
    """LLV(arr, windows[i])：单调队列实现，O(n)。窗口 = [i-windows[i]+1, i]。"""
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    dq = deque()
    for i in range(n):
        w = int(windows[i])
        while dq and dq[0] < i - w + 1:
            dq.popleft()
        while dq and arr[dq[-1]] >= arr[i]:
            dq.pop()
        dq.append(i)
        out[i] = arr[dq[0]]
    return out


def _hhv(arr: np.ndarray, windows: np.ndarray) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    dq = deque()
    for i in range(n):
        w = int(windows[i])
        while dq and dq[0] < i - w + 1:
            dq.popleft()
        while dq and arr[dq[-1]] <= arr[i]:
            dq.pop()
        dq.append(i)
        out[i] = arr[dq[0]]
    return out


def _count(cond: np.ndarray, n: int) -> np.ndarray:
    """COUNT(cond, n)：最近 n 根K线（含当前）中 cond 为真的次数。"""
    out = np.zeros(len(cond), dtype=int)
    run = 0
    for i in range(len(cond)):
        run += 1 if cond[i] else 0
        if i >= n:
            run -= 1 if cond[i - n] else 0
        out[i] = run
    return out


def calc_mrmc(close, s: int = 12, p: int = 26, m: int = 9) -> Dict[str, List]:
    """计算 MRMC 指标，返回与行情等长的信号数组。

    Args:
        close: 收盘价序列（可迭代）。
        s/p/m: MACD 参数，默认 12/26/9。

    Returns:
        dict: 含 diff/dea/macd 曲线，以及 buy/sell 布尔信号数组。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    if n < 10:
        return {
            "diff": [], "dea": [], "macd": [],
            "buy": [False] * n, "sell": [False] * n,
        }

    diff = _ema(close, s) - _ema(close, p)
    dea = _ema(diff, m)
    macd = (diff - dea) * 2.0

    # 零轴上下穿越
    cross_dn = np.zeros(n, bool)
    cross_up = np.zeros(n, bool)
    for i in range(1, n):
        cross_dn[i] = macd[i - 1] >= 0 and macd[i] < 0
        cross_up[i] = macd[i - 1] <= 0 and macd[i] > 0

    N1 = _barslast(cross_dn)
    MM1 = _barslast(cross_up)

    # ---- 底部（抄底） ----
    CC1 = _llv(close, N1 + 1)
    CC2 = _ref_num(CC1, MM1 + 1)
    CC3 = _ref_num(CC2, MM1 + 1)
    DIFL1 = _llv(diff, N1 + 1)
    DIFL2 = _ref_num(DIFL1, MM1 + 1)
    DIFL3 = _ref_num(DIFL2, MM1 + 1)

    return _calc_full(
        close, diff, dea, macd,
        N1, MM1,
        CC1, CC2, CC3, DIFL1, DIFL2, DIFL3,
    )


def _calc_full(
    close, diff, dea, macd,
    N1, MM1,
    CC1, CC2, CC3, DIFL1, DIFL2, DIFL3,
) -> Dict[str, List]:
    n = len(close)
    ref_macd1 = _ref_num(macd, 1)
    ref_diff1 = _ref_num(diff, 1)

    # ---- 底部（抄底） ----
    AAA = (CC1 < CC2) & (DIFL1 > DIFL2) & (ref_macd1 < 0) & (diff < 0)
    BBB = (CC1 < CC3) & (DIFL1 < DIFL2) & (DIFL1 > DIFL3) & (ref_macd1 < 0) & (diff < 0)
    CCC = (AAA | BBB) & (diff < 0)
    LLL = (~_ref_bool(CCC, 1)) & CCC
    XXX = (_ref_bool(AAA, 1) & (DIFL1 <= DIFL2) & (diff < dea)) | (
        _ref_bool(BBB, 1) & (DIFL1 <= DIFL3) & (diff < dea)
    )
    JJJ = _ref_bool(CCC, 1) & (np.abs(ref_diff1) >= np.abs(diff) * 1.01)
    DXDX = (~_ref_bool(JJJ, 1)) & JJJ
    DJGXX = ((close < CC2) | (close < CC1)) & (
        _ref_bool(JJJ, MM1 + 1) | _ref_bool(JJJ, MM1)
    ) & (~_ref_bool(LLL, 1)) & (_count(JJJ, 24) >= 1)
    DJXX = (~(_ref_bool(DJGXX, 1) | _ref_bool(DJGXX, 2))) & DJGXX
    DXX = (XXX | DJXX) & ~CCC
    buy = DXDX

    # ---- 顶部（卖出） ----
    CH1 = _hhv(close, MM1 + 1)
    CH2 = _ref_num(CH1, N1 + 1)
    CH3 = _ref_num(CH2, N1 + 1)
    DIFH1 = _hhv(diff, MM1 + 1)
    DIFH2 = _ref_num(DIFH1, N1 + 1)
    DIFH3 = _ref_num(DIFH2, N1 + 1)

    ZJDBL = (CH1 > CH2) & (DIFH1 < DIFH2) & (ref_macd1 > 0) & (diff > 0)
    GXDBL = (CH1 > CH3) & (DIFH1 > DIFH2) & (DIFH1 < DIFH3) & (ref_macd1 > 0) & (diff > 0)
    DBBL = (ZJDBL | GXDBL) & (diff > 0)
    DBL = (~_ref_bool(DBBL, 1)) & DBBL & (diff > dea)
    DBLXS = (_ref_bool(ZJDBL, 1) & (DIFH1 >= DIFH2) & (diff > dea)) | (
        _ref_bool(GXDBL, 1) & (DIFH1 >= DIFH3) & (diff > dea)
    )
    DBJG = _ref_bool(DBBL, 1) & (ref_diff1 >= diff * 1.01)
    DBJGXC = (~_ref_bool(DBJG, 1)) & DBJG
    ZZZZZ = ((close > CH2) | (close > CH1)) & (
        _ref_bool(DBJG, N1 + 1) | _ref_bool(DBJG, N1)
    ) & (~_ref_bool(DBL, 1)) & (_count(DBJG, 23) >= 1)
    YYYYY = (~(_ref_bool(ZZZZZ, 1) | _ref_bool(ZZZZZ, 2))) & ZZZZZ
    WWWWW = (DBLXS | YYYYY) & ~DBBL
    sell = DBJGXC

    return {
        "diff": diff.tolist(),
        "dea": dea.tolist(),
        "macd": macd.tolist(),
        "buy": buy.tolist(),
        "sell": sell.tolist(),
    }
