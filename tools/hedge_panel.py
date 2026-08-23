"""定时定量对冲实时持仓的本地只读网页面板。"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STALE_AFTER_SECONDS = Decimal("120")
GOOD_EXPOSURE_LIMIT = Decimal("0.0001")
WARN_EXPOSURE_LIMIT = Decimal("0.001")


@dataclass(frozen=True)
class InstanceConfig:
    """一套定时定量对冲实例的只读数据路径。"""

    key: str
    name: str
    primary_exchange: str
    hedge_exchange: str
    heartbeat_path: Path
    state_path: Path
    lock_path: Path


@dataclass(frozen=True)
class InstanceSnapshot:
    """一次页面渲染所使用的实例快照。"""

    config: InstanceConfig
    heartbeat: dict
    state: dict
    pid: int | None
    running: bool

    @property
    def data(self) -> dict:
        """以心跳优先合并持久化状态。"""
        combined = dict(self.state)
        combined.update(self.heartbeat)
        return combined


DEFAULT_INSTANCES = (
    InstanceConfig(
        key="lighter_entropy",
        name="实例 A · Lighter × Entropy",
        primary_exchange="Lighter",
        hedge_exchange="Entropy",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume.jsonl",
        state_path=PROJECT_ROOT / "data" / "timed_volume" / "state.json",
        lock_path=PROJECT_ROOT / "data" / "timed_volume" / "state.json.lock",
    ),
    InstanceConfig(
        key="variational_entropy",
        name="实例 B · Variational × Entropy",
        primary_exchange="Variational",
        hedge_exchange="Entropy",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_var.jsonl",
        state_path=PROJECT_ROOT / "data" / "timed_volume_var" / "state.json",
        lock_path=PROJECT_ROOT / "data" / "timed_volume_var" / "state.json.lock",
    ),
)


def _load_json(text: str) -> object:
    """解析 JSON，并将带小数的数值直接保留为 Decimal。"""
    return json.loads(text, parse_float=Decimal)


def read_json_object(path: Path | str) -> dict:
    """读取 JSON 对象；文件缺失、损坏或结构异常时返回空字典。"""
    try:
        value = _load_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_last_jsonl(path: Path | str) -> dict:
    """读取 JSONL 的最后一个非空行；不可读时返回空字典。"""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            value = _load_json(line)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def pid_is_alive(pid: int) -> bool:
    """检查 PID 是否存活；无权限探测时按存活处理。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_process_status(lock_path: Path | str) -> tuple[int | None, bool]:
    """从锁文件读取 PID，并以进程实际存活状态为准。"""
    payload = read_json_object(lock_path)
    raw_pid = payload.get("pid")
    if isinstance(raw_pid, bool):
        return None, False
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError, OverflowError):
        return None, False
    return pid, pid_is_alive(pid)


def collect_instance(config: InstanceConfig) -> InstanceSnapshot:
    """只读采集一套实例的心跳、状态和锁文件。"""
    pid, running = read_process_status(config.lock_path)
    return InstanceSnapshot(
        config=config,
        heartbeat=read_last_jsonl(config.heartbeat_path),
        state=read_json_object(config.state_path),
        pid=pid,
        running=running,
    )


def _to_decimal(value: object) -> Decimal | None:
    """安全转换普通数值，拒绝布尔值与非有限数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _truthy(value: object) -> bool:
    """兼容 JSON 布尔值与常见文本布尔值。"""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _decimal_text(value: object) -> str:
    """以十进制定点形式显示数值，不经过浮点数。"""
    parsed = _to_decimal(value)
    if parsed is None:
        return "—"
    if parsed == 0:
        return "0"
    return format(parsed, "f")


def _local_time(value: object) -> str:
    """按进程本地时区格式化 Unix 时间戳。"""
    timestamp = _to_decimal(value)
    if timestamp is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


def _countdown(due_at: object, now: Decimal) -> str:
    """把到期时间转换为中文倒计时。"""
    due = _to_decimal(due_at)
    if due is None:
        return "—"
    remaining = due - now
    if remaining <= 0:
        return "已到期"
    total_minutes = int(remaining / Decimal("60"))
    if total_minutes == 0:
        return "不足1分"
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分"


def exposure_class(value: object) -> str:
    """按净敞口绝对值返回绿、黄、红三级样式。"""
    exposure = _to_decimal(value)
    if exposure is None:
        return "exposure-missing"
    magnitude = abs(exposure)
    if magnitude < GOOD_EXPOSURE_LIMIT:
        return "exposure-good"
    if magnitude < WARN_EXPOSURE_LIMIT:
        return "exposure-warn"
    return "exposure-bad"


def _field(data: dict, primary: str, *fallbacks: str) -> object:
    """返回首个存在且非空的字段。"""
    for key in (primary, *fallbacks):
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _text(value: object) -> str:
    """把动态内容转换为可安全嵌入 HTML 的文本。"""
    if value is None or value == "":
        return "—"
    return escape(str(value), quote=True)


def _direction(value: object) -> str:
    """把策略方向转换为中文。"""
    normalized = str(value).strip().lower() if value is not None else ""
    return {"long": "多", "short": "空"}.get(normalized, _text(value))


def _action(value: object) -> str:
    """把常见心跳动作转换为中文，同时保留未知动作。"""
    normalized = str(value).strip().lower() if value is not None else ""
    labels = {
        "opened": "已开仓",
        "closed": "已平仓",
        "opening": "开仓中",
        "closing": "平仓中",
        "waiting": "等待中",
        "skipped": "已跳过",
        "idle": "空闲",
    }
    return labels.get(normalized, _text(value))


def _freshness(heartbeat: dict, now: Decimal) -> tuple[str, str, bool]:
    """返回数据时刻、相对时间文本及是否过期。"""
    timestamp = _to_decimal(heartbeat.get("ts"))
    if timestamp is None:
        return "—", "暂无心跳数据", False
    age = max(Decimal("0"), now - timestamp)
    seconds = int(age)
    return _local_time(timestamp), f"{seconds} 秒前", age > STALE_AFTER_SECONDS


def _render_leg(
    role: str,
    exchange: object,
    size: object,
    notional: object,
) -> str:
    """渲染单腿持仓：方向、数量与折合美元。

    只显示数量的话看不出这条腿到底是多还是空、值多少钱，
    对冲是否成立要靠心算两个带符号小数，实际用起来很吃力。
    """
    parsed = _to_decimal(size)
    if parsed is None:
        return (
            f'    <div class="leg">\n'
            f"      <span>{_text(role)} · {_text(exchange)}</span>\n"
            f'      <strong class="mono">—</strong>\n'
            f"    </div>"
        )

    if parsed > 0:
        side, side_class = "多", "leg-long"
    elif parsed < 0:
        side, side_class = "空", "leg-short"
    else:
        side, side_class = "空仓", "leg-flat"

    # 名义额是策略给这一轮定的单边美元数，两腿共用，用它折算即可，
    # 避免面板进程为了取价而去连交易所。
    value = _to_decimal(notional)
    usd = f"≈ {'-' if parsed < 0 else ''}${value:,.0f}" if value is not None and parsed != 0 else ""

    return (
        f'    <div class="leg">\n'
        f"      <span>{_text(role)} · {_text(exchange)}</span>\n"
        f'      <strong class="mono {side_class}">{_text(side)} {_text(_decimal_text(size))}</strong>\n'
        f'      <em class="leg-usd">{_text(usd)}</em>\n'
        f"    </div>"
    )


def _render_offset(primary: object, hedge: object) -> str:
    """渲染两腿是否互相抵消的一句话结论。"""
    a, b = _to_decimal(primary), _to_decimal(hedge)
    if a is None or b is None:
        return ""
    if a == 0 and b == 0:
        return '  <p class="offset offset-flat">两腿均为空仓</p>'
    if (a > 0) == (b > 0):
        return (
            '  <p class="offset offset-bad">⚠ 两腿方向相同，未形成对冲</p>'
        )
    return '  <p class="offset offset-ok">两腿方向相反，对冲成立</p>'


def _render_warnings(value: object) -> str:
    """仅在存在告警时渲染告警列表。"""
    if isinstance(value, (list, tuple)):
        warnings = [item for item in value if item not in (None, "")]
    elif value not in (None, ""):
        warnings = [value]
    else:
        warnings = []
    if not warnings:
        return ""
    items = "".join(f"<li>{_text(item)}</li>" for item in warnings)
    return f'<section class="warnings"><h3>告警</h3><ul>{items}</ul></section>'


def _render_instance(snapshot: InstanceSnapshot, now: Decimal) -> str:
    """渲染单个实例卡片。"""
    data = snapshot.data
    interlocked = _truthy(data.get("hedge_interlock_active"))
    card_class = "instance-card interlocked" if interlocked else "instance-card"
    time_text, age_text, stale = _freshness(snapshot.heartbeat, now)
    freshness_class = "data-time stale" if stale else "data-time"
    stale_warning = "<strong>⚠ 数据可能已过期</strong>" if stale else ""

    if snapshot.running:
        status_class = "status running"
        status_text = f"运行中 · PID {snapshot.pid}"
    elif snapshot.pid is not None:
        status_class = "status stopped"
        status_text = f"未运行 · PID {snapshot.pid} 已退出"
    else:
        status_class = "status stopped"
        status_text = "未运行"

    direction = _field(data, "direction", "current_direction", "last_direction")
    round_index = _field(data, "round_index")
    notional = _field(data, "notional_usd", "current_notional_usd")
    due_at = _field(data, "due_at")
    net_exposure = _field(data, "net_exposure")
    net_class = exposure_class(net_exposure)
    interlock_reason = _field(data, "hedge_interlock_reason")

    if interlocked:
        interlock_html = (
            '<section class="interlock-alert">'
            '<strong>⛔ 互锁已激活</strong>'
            f'<span>{_text(interlock_reason)}</span>'
            "</section>"
        )
    else:
        availability = data.get("hedge_available")
        if availability is False:
            interlock_html = (
                '<section class="interlock-note unavailable">对冲侧不可用，互锁未激活</section>'
            )
        else:
            interlock_html = '<section class="interlock-note">互锁未激活</section>'

    return f"""
<article class="{card_class}" data-instance="{_text(snapshot.config.key)}">
  <div class="card-top">
    <div class="data-block">
      <div class="{freshness_class}">数据时间：<span class="mono">{time_text}</span>（{age_text}） {stale_warning}</div>
    </div>
    <span class="{status_class}">{_text(status_text)}</span>
  </div>
  <div class="title-row">
    <div>
      <p class="eyebrow">定时定量对冲</p>
      <h2>{_text(snapshot.config.name)}</h2>
    </div>
    <div class="round">第 <span class="mono">{_text(round_index)}</span> 轮</div>
  </div>

  <section class="net-block">
    <span>净敞口</span>
    <strong class="net-value mono {net_class}">{_text(_decimal_text(net_exposure))}</strong>
    <small>绝对值越接近 0 越好</small>
  </section>

  <section class="facts">
    <div><span>当前动作</span><strong>{_action(data.get("action"))}</strong></div>
    <div><span>方向</span><strong>{_direction(direction)}</strong></div>
    <div><span>本轮名义额</span><strong class="mono">{_text(_decimal_text(notional))} USD</strong></div>
    <div><span>距平仓</span><strong class="mono countdown">{_text(_countdown(due_at, now))}</strong></div>
  </section>

  <section class="legs" aria-label="两腿持仓">
{_render_leg("主腿", snapshot.config.primary_exchange, data.get("primary_size"), notional)}
{_render_leg("对冲腿", snapshot.config.hedge_exchange, data.get("hedge_size"), notional)}
  </section>
  {_render_offset(data.get("primary_size"), data.get("hedge_size"))}

  {interlock_html}
  {_render_warnings(data.get("warnings"))}
</article>"""


def build_page(
    *,
    instances: Iterable[InstanceConfig] = DEFAULT_INSTANCES,
    now: Decimal | int | str | None = None,
) -> str:
    """采集全部实例并渲染自包含深色 HTML。"""
    current = _to_decimal(now)
    if current is None:
        current = Decimal(str(time.time()))
    snapshots = tuple(collect_instance(config) for config in instances)

    exposures = [
        parsed
        for snapshot in snapshots
        if (parsed := _to_decimal(snapshot.data.get("net_exposure"))) is not None
    ]
    total_exposure = sum(exposures, Decimal("0")) if exposures else None
    total_text = _decimal_text(total_exposure)
    total_class = exposure_class(total_exposure)
    any_interlocked = any(
        _truthy(snapshot.data.get("hedge_interlock_active")) for snapshot in snapshots
    )
    interlock_summary_class = "summary-danger" if any_interlocked else "summary-ok"
    interlock_summary = "有互锁" if any_interlocked else "无互锁"
    cards = "".join(_render_instance(snapshot, current) for snapshot in snapshots)
    rendered_at = datetime.fromtimestamp(int(current)).strftime("%Y-%m-%d %H:%M:%S")

    css = """
      :root {
        color-scheme: dark;
        --bg: #080b10;
        --panel: #111821;
        --panel-2: #17212d;
        --line: #293748;
        --text: #edf4ff;
        --muted: #92a3b6;
        --green: #42d392;
        --yellow: #f6c453;
        --red: #ff5573;
        --blue: #63a8ff;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background: radial-gradient(circle at 50% -20%, #182536 0, var(--bg) 46%);
        color: var(--text);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .mono, .net-value {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        font-variant-numeric: tabular-nums;
      }
      .shell { width: min(1180px, calc(100vw - 28px)); margin: 0 auto; padding: 28px 0 42px; }
      .page-head { margin-bottom: 16px; }
      h1, h2, h3, p { margin: 0; }
      h1 { font-size: clamp(26px, 4vw, 38px); line-height: 1.1; }
      h2 { margin-top: 5px; font-size: clamp(19px, 2.2vw, 25px); }
      .rendered-at { margin-top: 10px; color: var(--muted); }
      .manual-note { margin-top: 5px; color: var(--blue); font-size: 13px; }
      .overview {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
        margin: 18px 0;
      }
      .summary-item, .instance-card {
        background: rgba(17, 24, 33, 0.96);
        border: 1px solid var(--line);
        border-radius: 10px;
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
      }
      .summary-item { padding: 14px 16px; }
      .summary-item span { display: block; color: var(--muted); font-size: 12px; }
      .summary-item strong { display: block; margin-top: 5px; font-size: 22px; }
      .summary-ok { color: var(--green); }
      .summary-danger { color: var(--red); }
      .cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
      .instance-card { min-width: 0; padding: 18px; }
      .instance-card.interlocked {
        border: 2px solid var(--red);
        box-shadow: 0 0 0 2px rgba(255, 85, 115, 0.18), 0 18px 52px rgba(255, 85, 115, 0.18);
      }
      .card-top, .title-row { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
      .card-top { min-height: 38px; margin-bottom: 13px; }
      .data-time { color: var(--text); font-size: 14px; font-weight: 700; }
      .data-time.stale { color: var(--red); }
      .data-time.stale strong { display: block; margin-top: 4px; }
      .status, .round {
        flex: 0 0 auto;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 6px 9px;
        font-size: 12px;
        white-space: nowrap;
      }
      .status.running { color: var(--green); border-color: rgba(66, 211, 146, 0.45); }
      .status.stopped { color: var(--muted); }
      .eyebrow { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
      .round { color: var(--blue); }
      .net-block {
        display: flex;
        flex-direction: column;
        align-items: center;
        margin: 18px 0 14px;
        padding: 18px 12px;
        background: var(--panel-2);
        border: 1px solid var(--line);
        border-radius: 9px;
      }
      .net-block > span, .net-block small { color: var(--muted); }
      .net-value {
        max-width: 100%;
        margin: 4px 0;
        font-size: clamp(38px, 7vw, 58px);
        line-height: 1.08;
        overflow-wrap: anywhere;
      }
      .exposure-good { color: var(--green); }
      .exposure-warn { color: var(--yellow); }
      .exposure-bad { color: var(--red); }
      .exposure-missing { color: var(--muted); }
      .facts, .legs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
      .facts > div, .leg {
        min-width: 0;
        padding: 11px;
        background: var(--panel-2);
        border: 1px solid var(--line);
        border-radius: 8px;
      }
      .facts span, .leg span { display: block; margin-bottom: 5px; color: var(--muted); font-size: 12px; }
      .facts strong, .leg strong { overflow-wrap: anywhere; }
      .legs { margin-top: 9px; }
      .leg-long { color: var(--green); }
      .leg-short { color: var(--red); }
      .leg-flat { color: var(--muted); }
      .leg-usd { display: block; margin-top: 3px; font-style: normal; font-size: 12px; color: var(--muted); }
      .offset { margin: 9px 0 0; font-size: 13px; }
      .offset-ok { color: var(--green); }
      .offset-bad { color: var(--red); font-weight: 600; }
      .offset-flat { color: var(--muted); }
      .leg strong { font-size: 21px; }
      .interlock-note, .interlock-alert {
        display: flex;
        flex-wrap: wrap;
        gap: 7px 12px;
        margin-top: 12px;
        padding: 10px 12px;
        border-radius: 8px;
      }
      .interlock-note { color: var(--green); background: rgba(66, 211, 146, 0.08); }
      .interlock-note.unavailable { color: var(--yellow); background: rgba(246, 196, 83, 0.1); }
      .interlock-alert { color: #ffdce3; background: rgba(255, 85, 115, 0.18); border: 1px solid var(--red); }
      .warnings { margin-top: 12px; padding: 12px; color: var(--yellow); background: rgba(246, 196, 83, 0.09); border-radius: 8px; }
      .warnings h3 { font-size: 14px; }
      .warnings ul { margin: 7px 0 0; padding-left: 20px; }
      .warnings li + li { margin-top: 5px; }
      @media (max-width: 820px) {
        .cards { grid-template-columns: 1fr; }
      }
      @media (max-width: 520px) {
        .shell { width: min(100% - 20px, 1180px); padding-top: 18px; }
        .overview, .facts, .legs { grid-template-columns: 1fr; }
        .card-top, .title-row { flex-direction: column; }
        .status { white-space: normal; }
        .instance-card { padding: 14px; }
      }
    """

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>定时定量对冲持仓面板</title>
  <style>{css}</style>
</head>
<body>
  <main class="shell">
    <header class="page-head">
      <h1>定时定量对冲 · 实时持仓</h1>
      <p class="rendered-at">页面渲染时间：<span class="mono">{rendered_at}</span>（本地时间）</p>
      <p class="manual-note">本页不会自动刷新，请按 F5 获取最新数据。</p>
    </header>
    <section class="overview" aria-label="总览">
      <div class="summary-item">
        <span>两个实例净敞口合计</span>
        <strong class="mono {total_class}">{_text(total_text)}</strong>
      </div>
      <div class="summary-item">
        <span>互锁总览</span>
        <strong class="{interlock_summary_class}">{interlock_summary}</strong>
      </div>
    </section>
    <section class="cards">{cards}</section>
  </main>
</body>
</html>"""


def render_html(
    instances: Iterable[InstanceConfig] = DEFAULT_INSTANCES,
    *,
    now: Decimal | int | str | None = None,
) -> str:
    """提供与其他本地面板一致的 HTML 渲染入口。"""
    return build_page(instances=instances, now=now)


class HedgePanelHandler(http.server.BaseHTTPRequestHandler):
    """定时定量对冲面板 HTTP handler。"""

    instances = DEFAULT_INSTANCES

    def do_GET(self) -> None:
        """只响应面板首页。"""
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        body = build_page(instances=self.instances).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        """关闭默认的英文访问日志。"""
        return


def main() -> None:
    """启动仅监听本机的只读面板。"""
    parser = argparse.ArgumentParser(description="启动定时定量对冲持仓面板")
    parser.add_argument("--port", type=int, default=8787, help="本地监听端口")
    args = parser.parse_args()

    with http.server.HTTPServer(("localhost", args.port), HedgePanelHandler) as server:
        print(f"对冲面板已启动：http://localhost:{args.port}（Ctrl+C 停止）", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
