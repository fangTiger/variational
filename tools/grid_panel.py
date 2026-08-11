"""网格监控面板 HTML 渲染。"""

from __future__ import annotations

import argparse
import http.server
import json
import math
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from grid.regime import describe_regime


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_live(path) -> dict:
    """读取 live 快照；文件缺失、损坏或结构异常时返回空字典。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _latest_equity(jsonl_path) -> dict:
    """读取监控 JSONL 最后一条权益快照。"""
    try:
        lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    if not lines:
        return {}
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in ("equity", "pnl_since_start") if key in data}


class GridPanelHandler(http.server.BaseHTTPRequestHandler):
    """本地面板 HTTP handler。"""

    live_path = PROJECT_ROOT / "data" / "grid_live.json"
    monitor_path = PROJECT_ROOT / "data" / "grid_monitor.jsonl"

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return

        html = render_html(_read_live(self.live_path), _latest_equity(self.monitor_path))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass


def render_html(live: dict, equity: dict) -> str:
    """把 live/equity 快照渲染成自包含深色 HTML。"""
    live = live if isinstance(live, dict) else {}
    equity = equity if isinstance(equity, dict) else {}

    def _num(value) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def _truthy(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def _raw_text(value) -> str:
        if value is None or value == "":
            return "—"
        return str(value)

    def _html(value) -> str:
        return escape(_raw_text(value), quote=True)

    def _fmt_number(value, digits: int = 2) -> str:
        parsed = _num(value)
        if parsed is None:
            return "—"
        if abs(parsed) >= 100:
            digits = min(digits, 2)
        text = f"{parsed:.{digits}f}".rstrip("0").rstrip(".")
        return text if text != "-0" else "0"

    def _fmt_signed(value) -> str:
        parsed = _num(value)
        if parsed is None:
            return "—"
        text = _fmt_number(abs(parsed), 2)
        if parsed > 0:
            return f"+{text}"
        if parsed < 0:
            return f"-{text}"
        return "0"

    def _fmt_pct(value, digits: int = 2) -> str:
        parsed = _num(value)
        if parsed is None:
            return "—"
        return f"{parsed * 100:.{digits}f}%"

    def _fmt_ts(value) -> str:
        parsed = _num(value)
        if parsed is None:
            return "—"
        if parsed > 946684800:
            return datetime.fromtimestamp(parsed).strftime("%Y-%m-%d %H:%M:%S")
        return _fmt_number(parsed, 0)

    css = """
        :root {
            color-scheme: dark;
            --bg: #080b10;
            --panel: #111821;
            --panel-2: #151f2b;
            --line: #263241;
            --text: #eef4ff;
            --muted: #8fa0b2;
            --green: #42d392;
            --red: #ff5c7a;
            --amber: #f6b73c;
            --blue: #62a7ff;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .shell {
            width: min(1180px, calc(100vw - 32px));
            margin: 0 auto;
            padding: 26px 0 34px;
        }
        .topbar {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 10px;
            align-items: stretch;
            margin-bottom: 14px;
        }
        .stat, .card {
            border: 1px solid var(--line);
            background: var(--panel);
            border-radius: 8px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.26);
        }
        .stat {
            padding: 12px 14px;
            min-width: 0;
        }
        .label {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.35;
        }
        .value {
            margin-top: 4px;
            font-size: 20px;
            font-weight: 700;
            line-height: 1.15;
            overflow-wrap: anywhere;
        }
        .alive {
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--green);
            box-shadow: 0 0 18px rgba(66, 211, 146, 0.7);
        }
        .dot.dead {
            background: var(--red);
            box-shadow: 0 0 18px rgba(255, 92, 122, 0.55);
        }
        .up { color: var(--green); }
        .down { color: var(--red); }
        .muted { color: var(--muted); }
        .grid {
            display: grid;
            grid-template-columns: 1.45fr 1fr;
            gap: 14px;
        }
        .card {
            padding: 18px;
            min-width: 0;
        }
        .card.frozen {
            border-color: rgba(246, 183, 60, 0.9);
            box-shadow: 0 0 0 1px rgba(246, 183, 60, 0.26), 0 18px 48px rgba(246, 183, 60, 0.12);
        }
        .title-row {
            display: flex;
            align-items: start;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 14px;
        }
        h1, h2, p { margin: 0; }
        h1 {
            font-size: 34px;
            line-height: 1.05;
            letter-spacing: 0;
        }
        h2 {
            font-size: 16px;
            line-height: 1.25;
        }
        .badges {
            display: flex;
            flex-wrap: wrap;
            justify-content: flex-end;
            gap: 6px;
        }
        .badge {
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 5px 8px;
            color: var(--muted);
            background: rgba(255, 255, 255, 0.03);
            font-size: 12px;
            line-height: 1;
            white-space: nowrap;
        }
        .badge.hot {
            border-color: rgba(246, 183, 60, 0.7);
            color: var(--amber);
            background: rgba(246, 183, 60, 0.12);
        }
        .badge.stop {
            border-color: rgba(255, 92, 122, 0.72);
            color: var(--red);
            background: rgba(255, 92, 122, 0.12);
        }
        .metrics {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
            margin: 14px 0;
        }
        .metric {
            border: 1px solid var(--line);
            background: var(--panel-2);
            border-radius: 8px;
            padding: 12px;
        }
        .metric strong {
            display: block;
            margin-top: 4px;
            font-size: 18px;
        }
        .explain {
            color: #cdd8e6;
            line-height: 1.55;
            margin: 10px 0 16px;
        }
        .band-row {
            display: flex;
            justify-content: space-between;
            gap: 10px;
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 10px;
        }
        .range {
            position: relative;
            height: 18px;
            border-radius: 999px;
            background: linear-gradient(90deg, rgba(66, 211, 146, 0.32), rgba(98, 167, 255, 0.38), rgba(255, 92, 122, 0.3));
            border: 1px solid rgba(255, 255, 255, 0.12);
            overflow: visible;
        }
        .marker {
            position: absolute;
            top: 50%;
            width: 4px;
            height: 30px;
            border-radius: 999px;
            transform: translate(-50%, -50%);
            background: #ffffff;
            box-shadow: 0 0 0 5px rgba(255, 255, 255, 0.14), 0 0 18px rgba(255, 255, 255, 0.55);
        }
        .card.frozen .marker {
            background: var(--amber);
            box-shadow: 0 0 0 5px rgba(246, 183, 60, 0.18), 0 0 18px rgba(246, 183, 60, 0.7);
        }
        .range-note {
            margin-top: 10px;
            color: var(--muted);
            font-size: 13px;
        }
        .risk-grid, .params {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }
        .params {
            margin-top: 14px;
        }
        .empty-state {
            display: grid;
            min-height: 100vh;
            place-items: center;
            text-align: center;
        }
        .empty-card {
            width: min(520px, calc(100vw - 32px));
            padding: 28px;
        }
        .empty-card h1 {
            font-size: 28px;
            margin-bottom: 8px;
        }
        @media (max-width: 860px) {
            .topbar, .grid, .metrics, .risk-grid, .params {
                grid-template-columns: 1fr;
            }
            h1 { font-size: 28px; }
        }
    """

    def _page(body: str) -> str:
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>网格监控面板</title>
  <style>{css}</style>
</head>
<body>
{body}
</body>
</html>"""

    if not live:
        return _page(
            """
<main class="empty-state">
  <section class="card empty-card">
    <h1><span class="alive"><span class="dot dead"></span>引擎未运行</span></h1>
    <p class="muted">暂无 live 快照，无数据可渲染。页面会每 10 秒自动刷新。</p>
  </section>
</main>"""
        )

    cfg = live.get("cfg")
    cfg = cfg if isinstance(cfg, dict) else {}
    mark = _num(live.get("mark"))
    band_low = _num(live.get("band_low"))
    band_high = _num(live.get("band_high"))
    adx_val = _num(live.get("adx"))
    slope_short = _num(live.get("slope_short"))
    slope_long = _num(live.get("slope_long"))
    atr_pct = _num(live.get("atr_pct"))
    frozen = _truthy(live.get("frozen"))
    halted = _truthy(live.get("halted"))
    # 取生效值：OFF 合成的 BOTH 只存在于 effective_blocked_side，
    # blocked_side 仅记 band 突破；旧快照无新字段时回退到后者。
    if "effective_blocked_side" in live:
        blocked_side = live["effective_blocked_side"]
    else:
        blocked_side = live.get("blocked_side")
    mode_raw = live.get("mode")
    mode = mode_raw.value if hasattr(mode_raw, "value") else _raw_text(mode_raw).lower()

    if mode == "neutral":
        mode_title = "震荡 ✓ 适合网格"
    elif mode == "off":
        mode_title = "趋势 · 已停铺"
    else:
        mode_title = "市况 —"

    try:
        regime_text = describe_regime(
            mode if mode in {"neutral", "off"} else "neutral",
            adx_val if adx_val is not None else 0.0,
            slope_short if slope_short is not None else 0.0,
            slope_long if slope_long is not None else 0.0,
            atr_pct if atr_pct is not None else 0.0,
        )
    except (TypeError, ValueError):
        regime_text = "市况数据不完整，等待下一轮快照"

    position_pct = None
    out_of_band = False
    if mark is not None and band_low is not None and band_high is not None and band_high > band_low:
        raw_pct = (mark - band_low) / (band_high - band_low) * 100
        out_of_band = raw_pct < 0 or raw_pct > 100
        position_pct = min(100.0, max(0.0, raw_pct))

    alert = frozen or halted or out_of_band
    card_class = "card trend-card frozen" if alert else "card trend-card"
    marker_left = 50.0 if position_pct is None else position_pct
    if out_of_band:
        range_note = "当前价越界，标记已贴近最近边界"
    elif frozen:
        range_note = "band 已冻结，等待满足恢复条件"
    elif position_pct is None:
        range_note = "价格位置缺少 mark 或 band 数据"
    else:
        range_note = f"当前价位于 band 的 {position_pct:.1f}%"

    pnl = _num(equity.get("pnl_since_start"))
    pnl_class = "muted" if pnl is None else ("up" if pnl >= 0 else "down")
    equity_text = _fmt_number(equity.get("equity"), 2)
    pnl_text = _fmt_signed(equity.get("pnl_since_start"))

    badges = []
    # 以生效封锁方向为准：OFF 模式 frozen=False 但双向禁挂，
    # 只看 frozen 会把停摆显示成"frozen 否"的正常态。
    if blocked_side:
        badges.append(
            f'<span class="badge hot">'
            f'{"frozen 冻结" if frozen else "已封锁"} '
            f'{escape(_raw_text(blocked_side))}</span>'
        )
    else:
        badges.append('<span class="badge">frozen 否</span>')
    if halted:
        badges.append('<span class="badge stop">halted 已停止</span>')
    else:
        badges.append('<span class="badge">halted 否</span>')
    if out_of_band:
        badges.append('<span class="badge hot">越界</span>')

    body = f"""
<main class="shell">
  <section class="topbar" aria-label="顶部状态">
    <div class="stat">
      <div class="label">进程</div>
      <div class="value alive"><span class="dot"></span>存活</div>
    </div>
    <div class="stat">
      <div class="label">权益</div>
      <div class="value">{escape(equity_text)}</div>
    </div>
    <div class="stat">
      <div class="label">自起始盈亏</div>
      <div class="value {pnl_class}">{escape(pnl_text)}</div>
    </div>
    <div class="stat">
      <div class="label">现价</div>
      <div class="value">{escape(_fmt_number(mark, 2))}</div>
    </div>
    <div class="stat">
      <div class="label">数据时间</div>
      <div class="value">{escape(_fmt_ts(live.get("ts")))}</div>
    </div>
  </section>

  <section class="grid">
    <article class="{card_class}">
      <div class="title-row">
        <div>
          <div class="label">趋势卡</div>
          <h1>{escape(mode_title)}</h1>
        </div>
        <div class="badges">{''.join(badges)}</div>
      </div>

      <div class="metrics">
        <div class="metric"><span class="label">ADX</span><strong>{escape(_fmt_number(adx_val, 1))}</strong></div>
        <div class="metric"><span class="label">斜率 short / long</span><strong>{escape(_fmt_pct(slope_short, 3))} / {escape(_fmt_pct(slope_long, 3))}</strong></div>
        <div class="metric"><span class="label">ATR</span><strong>{escape(_fmt_pct(atr_pct, 2))}</strong></div>
      </div>

      <p class="explain">{escape(regime_text)}</p>
      <div class="band-row">
        <span>下界 {escape(_fmt_number(band_low, 2))}</span>
        <span>mark {escape(_fmt_number(mark, 2))}</span>
        <span>上界 {escape(_fmt_number(band_high, 2))}</span>
      </div>
      <div class="range" aria-label="价格位置条">
        <span class="marker" style="left: {marker_left:.2f}%"></span>
      </div>
      <div class="range-note">{escape(range_note)}</div>
    </article>

    <aside class="card risk-card{' frozen' if alert else ''}">
      <div class="title-row">
        <div>
          <div class="label">风险卡</div>
          <h2>持仓与保护</h2>
        </div>
      </div>
      <div class="risk-grid">
        <div class="metric"><span class="label">BTC 持仓</span><strong>{escape(_fmt_number(live.get("inv_btc"), 6))}</strong></div>
        <div class="metric"><span class="label">USD 持仓</span><strong>{escape(_fmt_number(live.get("inv_usd"), 2))}</strong></div>
        <div class="metric"><span class="label">距强平</span><strong>{escape(_fmt_pct(live.get("dist_to_liq_pct"), 2))}</strong></div>
        <div class="metric"><span class="label">库存上限</span><strong>{escape(_fmt_number(cfg.get("max_inv"), 2))}</strong></div>
      </div>
      <div class="params">
        <div class="metric"><span class="label">格距</span><strong>{escape(_fmt_pct(cfg.get("spacing"), 2))}</strong></div>
        <div class="metric"><span class="label">档数</span><strong>{escape(_fmt_number(cfg.get("levels"), 0))}</strong></div>
        <div class="metric"><span class="label">单格</span><strong>{escape(_fmt_number(cfg.get("unit"), 2))}</strong></div>
        <div class="metric"><span class="label">硬止损距</span><strong>{escape(_fmt_pct(cfg.get("hard_stop_dist"), 2))}</strong></div>
        <div class="metric"><span class="label">ADX OFF</span><strong>{escape(_fmt_number(cfg.get("adx_off"), 1))}</strong></div>
        <div class="metric"><span class="label">冻结侧</span><strong>{_html(blocked_side)}</strong></div>
      </div>
    </aside>
  </section>
</main>"""
    return _page(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地网格监控面板")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--live-path",
        type=Path,
        default=PROJECT_ROOT / "data" / "grid_live.json",
    )
    parser.add_argument(
        "--monitor-path",
        type=Path,
        default=PROJECT_ROOT / "data" / "grid_monitor.jsonl",
    )
    args = parser.parse_args()

    GridPanelHandler.live_path = args.live_path
    GridPanelHandler.monitor_path = args.monitor_path
    with http.server.HTTPServer(("localhost", args.port), GridPanelHandler) as server:
        print(f"面板已启动：http://localhost:{args.port}（Ctrl+C 停）", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
