"""网格 regime 判断：纯函数指标 + 急停开关决策。

设计原则（见 2026-07-17 讨论）：网格不预测方向，只做"能不能安全网格"的分类。
默认 NEUTRAL；强趋势(ADX高)或通道突破 → OFF 并平库存。全部指标可回测。
"""

from __future__ import annotations

from enum import Enum


class GridMode(Enum):
    NEUTRAL = "neutral"  # 震荡：跑对称中性网格
    OFF = "off"          # 强趋势/突破：停网格、平库存、站一边


def ema(values: list[float], period: int) -> list[float | None]:
    """指数移动平均，前 period-1 个为 None。"""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder_atr(highs, lows, closes, period):
    """Wilder ATR，返回与输入等长、前段 None。"""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs = [highs[i] - lows[i] if i == 0 else max(
        highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])
    ) for i in range(n)]
    atr = sum(trs[1:period + 1]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder ADX，返回与输入等长、前段 None。ADX 高=趋势强，低=震荡。"""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < 2 * period + 1:
        return out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    # Wilder 平滑
    def smooth(vals):
        s = sum(vals[1:period + 1])
        res = [None] * n
        res[period] = s
        for i in range(period + 1, n):
            s = s - s / period + vals[i]
            res[i] = s
        return res

    str_ = smooth(tr)
    spdm = smooth(plus_dm)
    smdm = smooth(minus_dm)

    dx = [None] * n
    for i in range(period, n):
        if not str_[i]:
            continue
        pdi = 100 * spdm[i] / str_[i]
        mdi = 100 * smdm[i] / str_[i]
        denom = pdi + mdi
        dx[i] = 100 * abs(pdi - mdi) / denom if denom else 0.0

    # ADX = DX 的 Wilder 平滑
    first = 2 * period
    valid = [dx[i] for i in range(period, first) if dx[i] is not None]
    if len(valid) < period:
        return out
    adx_val = sum(valid[:period]) / period
    out[first - 1] = adx_val
    for i in range(first, n):
        if dx[i] is None:
            out[i] = adx_val
        else:
            adx_val = (adx_val * (period - 1) + dx[i]) / period
            out[i] = adx_val
    return out


def donchian_prev(highs: list[float], lows: list[float], period: int) -> tuple[list[float | None], list[float | None]]:
    """前 period 根（不含当前）的最高/最低，用于突破判断。"""
    n = len(highs)
    up: list[float | None] = [None] * n
    lo: list[float | None] = [None] * n
    for i in range(period, n):
        up[i] = max(highs[i - period:i])
        lo[i] = min(lows[i - period:i])
    return up, lo


def decide_mode(
    *,
    adx_val: float | None,
    close: float,
    donchian_up: float | None,
    donchian_lo: float | None,
    fng: int | None = None,
    adx_off: float = 30.0,
    adx_resume: float = 27.0,
    prev_mode: "GridMode | None" = None,
    fng_extreme_low: int = 15,
    fng_extreme_high: int = 85,
) -> GridMode:
    """急停开关：强趋势或通道突破 → OFF；否则 NEUTRAL。

    - ADX > adx_off：趋势太强，停。
    - ADX 迟滞：已 OFF 时须回落到 adx_resume 以下才恢复，防止在阈值附近反复撤/挂单。
      不传 prev_mode 时无迟滞（兼容旧调用）。
    - close 突破前 N 根通道上/下沿：趋势启动，停。
    - 恐惧贪婪极值 + 趋势偏强：更保守地停（极端情绪常伴随剧烈单边）。
    """
    if adx_val is None:
        return GridMode.OFF  # 预热不足，保守不开
    threshold = adx_resume if prev_mode is GridMode.OFF else adx_off
    if adx_val > threshold:
        return GridMode.OFF
    if donchian_up is not None and close > donchian_up:
        return GridMode.OFF
    if donchian_lo is not None and close < donchian_lo:
        return GridMode.OFF
    # 情绪极值 + ADX 偏高（>20）时更保守
    if fng is not None and adx_val > 20 and (fng <= fng_extreme_low or fng >= fng_extreme_high):
        return GridMode.OFF
    return GridMode.NEUTRAL
