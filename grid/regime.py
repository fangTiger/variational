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
    close: float | None = None,
    donchian_up: float | None = None,
    donchian_lo: float | None = None,
    fng: int | None = None,
    adx_off: float = 999.0,
    adx_resume: float = 999.0,
    prev_mode: "GridMode | None" = None,
    fng_extreme_low: int = 15,
    fng_extreme_high: int = 85,
) -> GridMode:
    """急停开关（2026-07-21 改版：只保留可选的 ADX 熔断，去掉通道突破/情绪急停）。

    网格初衷是持续挂单等成交。原"通道突破 → 撤全网格 + 平库存"会在每次拉盘刷新
    N 小时新高时把整排单撤光，与网格连续性正面冲突，故移除；情绪极值急停一并移除。
    趋势风险改由库存上限（grid_engine._within_cap）+ 格距承担。

    - adx_val is None：预热不足，保守 OFF（仅冷启动瞬时）。
    - ADX 熔断为**可选**：默认 adx_off=999 即禁用；如需强趋势保护把 adx_off/adx_resume 调低。
      迟滞：已 OFF 时须回落到 adx_resume 以下才恢复。
    - donchian_up/lo、fng 参数保留仅为兼容与日志，**不再触发 OFF**。
    """
    if adx_val is None:
        return GridMode.OFF  # 预热不足，保守不开
    threshold = adx_resume if prev_mode is GridMode.OFF else adx_off
    if adx_val > threshold:
        return GridMode.OFF
    return GridMode.NEUTRAL


def drop_forming_candle(values: list[float]) -> list[float]:
    """去掉最后一根（形成中的当前 K 线），只用已收盘数据。"""
    return values[:-1] if values else values


def trend_gate(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    adx_off: float,
    adx_resume: float,
    prev_mode: "GridMode | None" = None,
    adx_period: int = 14,
) -> GridMode:
    """二态趋势门控（设计 v3 Component 1）：强趋势=OFF（停止新增），否则 NEUTRAL。

    与 decide_mode 的区别：这里 ADX 熔断强制启用（阈值来自 config），
    调用方须先用 drop_forming_candle 传入已收盘 K 线。
    """
    a = adx(highs, lows, closes, adx_period)
    return decide_mode(adx_val=a[-1] if a else None,
                       adx_off=adx_off, adx_resume=adx_resume, prev_mode=prev_mode)


def close_slope(closes: list[float], period: int) -> float:
    """近端收盘斜率，归一化为"每根变化占当前价比例"（%/根）。

    数据不足或当前价非正返回 0.0。正=上行，负=下行，≈0=走平。
    """
    if len(closes) <= period or closes[-1] <= 0:
        return 0.0
    return (closes[-1] - closes[-1 - period]) / period / closes[-1]


def describe_regime(mode, adx, slope_short, slope_long, atr_pct) -> str:
    """规则拼句的中文市况解读（无 LLM）。mode 接受 GridMode 或其 value 字符串。"""
    m = mode.value if isinstance(mode, GridMode) else str(mode)
    # 斜率方向（两周期同向才明确表态，用较小量级判走平）
    thr = 0.001  # 每根 0.1% 以内算走平

    def _dir(s: float) -> int:
        return 1 if s > thr else (-1 if s < -thr else 0)

    ds, dl = _dir(slope_short), _dir(slope_long)
    if ds == dl == 1:
        slope_txt = "斜率上行"
    elif ds == dl == -1:
        slope_txt = "斜率下行"
    else:
        slope_txt = "斜率接近零"
    # 波动
    if atr_pct < 0.003:
        atr_txt = "波动小"
    elif atr_pct > 0.01:
        atr_txt = "波动大"
    else:
        atr_txt = "ATR 适中"
    head = "震荡 · 适合网格" if m == "neutral" else "趋势 · 已停铺"
    return f"{head}｜ADX {adx:.1f} · {slope_txt} · {atr_txt}"
