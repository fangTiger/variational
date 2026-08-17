"""归因 CLI、每小时调度与运维文档测试。"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import pnl_attribution


ROOT = Path(__file__).resolve().parent.parent
PLIST = ROOT / "deploy" / "com.variational.pnl-attribution.plist"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _configure_run(tmp_path, monkeypatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(pnl_attribution, "_ROOT", tmp_path)
    monkeypatch.setattr(pnl_attribution, "_DATA", data)
    monkeypatch.setattr(pnl_attribution, "_DB", data / "grid.db")
    monkeypatch.setattr(pnl_attribution, "_RESULT", data / "attribution.json")
    monkeypatch.setattr(
        pnl_attribution,
        "_START",
        data / "attribution_start.json",
    )
    monkeypatch.setattr(pnl_attribution, "load_dotenv", lambda *_a, **_kw: None)
    return data


def test_run_filters_one_window_and_executes_real_identity_check(
    tmp_path,
    monkeypatch,
) -> None:
    """残差必须来自 Task 5 恒等式，不能因漏传未实现盈亏而固定为零。"""
    data = _configure_run(tmp_path, monkeypatch)
    (data / "attribution_start.json").write_text(
        json.dumps({"start_ts": 1000.0}),
        encoding="utf-8",
    )
    _write_jsonl(
        data / "fills.jsonl",
        [
            {"fill_id": "old-b", "ts": 800, "level": 1, "side": "BUY", "price": 90, "qty": 1, "engine_run_id": "run-700"},
            {"fill_id": "old-s", "ts": 900, "level": 1, "side": "SELL", "price": 100, "qty": 1, "engine_run_id": "run-700"},
            {"fill_id": "new-b", "ts": 1100, "level": 2, "side": "BUY", "price": 100, "qty": 1, "engine_run_id": "run-1000"},
            {"fill_id": "new-s", "ts": 1200, "level": 2, "side": "SELL", "price": 110, "qty": 1, "engine_run_id": "run-1000"},
        ],
    )
    _write_jsonl(
        data / "grid_monitor.jsonl",
        [
            {"ts": 900, "equity": 90, "inv_usd": 0, "price": 90, "mode": "neutral"},
            {"ts": 1001, "equity": 100, "inv_usd": 5, "price": 100, "mode": "neutral"},
            {"ts": 1300, "equity": 106, "inv_usd": -5, "price": 106, "mode": "neutral"},
        ],
    )
    funding_calls = []

    def fake_funding(*, market, limit):
        funding_calls.append({"market": market, "limit": limit})
        return [
            {"funding_id": "old", "ts": 950, "fee": 50},
            {"funding_id": "current", "ts": 1150, "fee": 1},
            {"funding_id": "future", "ts": 1500, "fee": 50},
        ]

    monkeypatch.setattr(pnl_attribution, "fetch_funding", fake_funding)
    monkeypatch.setattr(
        pnl_attribution,
        "fetch_grid_account_snapshot",
        lambda: {"ts": 1400.0, "equity": 107.0, "unrealised_pnl": -1.0},
    )

    result = pnl_attribution.run()

    assert funding_calls == [{"market": "BTC-USD", "limit": 500}]
    assert result["observation_start_ts"] == pytest.approx(1001.0)
    assert result["observation_end_ts"] == pytest.approx(1400.0)
    assert result["loops_count"] == 1
    assert result["grid_pnl"] == pytest.approx(10.0)
    assert result["funding_total"] == pytest.approx(1.0)
    assert result["unrealised_change"] == pytest.approx(-1.0)
    assert result["residual"] == pytest.approx(-3.0)
    assert result["has_gap"] is True
    assert result["identity_checked"] is True
    assert result["unrealised_start_assumed_zero"] is True
    assert json.loads((data / "attribution.json").read_text(encoding="utf-8")) == result


def test_observation_start_falls_back_to_earliest_engine_run(tmp_path) -> None:
    fills = [
        {"ts": 1200.0, "engine_run_id": "run-1000"},
        {"ts": 2200.0, "engine_run_id": "run-2000"},
    ]

    assert pnl_attribution._observation_start_ts(
        fills,
        tmp_path / "missing.json",
    ) == pytest.approx(1000.0)


def test_grid_account_snapshot_explicitly_uses_grid_credentials(monkeypatch) -> None:
    prefixes = []

    class FakeClient:
        async def get_balance(self):
            return SimpleNamespace(
                equity=107,
                unrealised_pnl=-1,
                updated_time=1_400_000_000_000,
            )

        async def close(self):
            return None

    class FakeExtendedClient:
        @classmethod
        def from_env(cls, *, prefix):
            prefixes.append(prefix)
            return FakeClient()

    monkeypatch.setattr(pnl_attribution, "ExtendedClient", FakeExtendedClient)

    result = pnl_attribution.fetch_grid_account_snapshot()

    assert prefixes == ["X10_GRID"]
    assert result == {
        "ts": 1_400_000_000.0,
        "equity": 107.0,
        "unrealised_pnl": -1.0,
    }


def test_missing_account_snapshot_does_not_fake_zero_residual(
    tmp_path,
    monkeypatch,
) -> None:
    data = _configure_run(tmp_path, monkeypatch)
    _write_jsonl(
        data / "fills.jsonl",
        [
            {"fill_id": "b", "ts": 1100, "level": 1, "side": "BUY", "price": 100, "qty": 1, "engine_run_id": "run-1000"},
        ],
    )
    _write_jsonl(
        data / "grid_monitor.jsonl",
        [{"ts": 1001, "equity": 100, "inv_usd": 5, "price": 100, "mode": "neutral"}],
    )
    monkeypatch.setattr(pnl_attribution, "fetch_funding", lambda **_kw: [])
    monkeypatch.setattr(pnl_attribution, "fetch_grid_account_snapshot", lambda: None)

    result = pnl_attribution.run()

    assert result["identity_checked"] is False
    assert result["residual"] is None
    assert result["decided"] is False
    assert "账户快照" in result["reason"]


def test_pnl_attribution_plist_runs_hourly_from_repository() -> None:
    """plist 只保存在仓库，人工安装前必须满足路径和每小时调度约束。"""
    assert PLIST.exists()
    with PLIST.open("rb") as stream:
        config = plistlib.load(stream)

    assert config["Label"] == "com.variational.pnl-attribution"
    assert config["StartInterval"] == 3600
    assert config["RunAtLoad"] is True
    assert config["WorkingDirectory"] == str(ROOT)
    assert config["EnvironmentVariables"]["PYTHONPATH"] == str(ROOT)
    assert config["ProgramArguments"] == [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "tools.pnl_attribution",
    ]
    assert config["StandardOutPath"] == str(ROOT / "logs/pnl-attribution.log")
    assert config["StandardErrorPath"] == str(
        ROOT / "logs/pnl-attribution.err.log"
    )


def test_readme_documents_attribution_service_and_result() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "com.variational.pnl-attribution" in readme
    assert "deploy/com.variational.pnl-attribution.plist" in readme
    assert "python -m tools.pnl_attribution" in readme
    assert "data/attribution.json" in readme
