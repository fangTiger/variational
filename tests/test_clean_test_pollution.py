"""清理只识别明确特征，默认不写入。"""
import json

import pytest

from tools.clean_test_pollution import main, pollution_reasons


@pytest.mark.parametrize("record", [
    {"阻断原因": ["XAU 费率失败：AssertionError: 未配置调用"]},
    {"error": "AssertionError"},
    {"before": {"equity": "1000"}, "close_legs": [{"execution_price": "4001", "quote_mid": "4000"}]},
])
def test_identifies_nested_test_records(record):
    assert pollution_reasons(record)


@pytest.mark.parametrize("record", [
    {"execution_price": "4001", "quote_mid": "4000", "equity": "12345"},
    {"execution_price": "4001.01", "equity": "1000"},
    {"equity": "1000", "quantity": "4000"},
    {"error": "交易所超时"},
])
def test_preserves_real_records(record):
    assert pollution_reasons(record) == []


def test_default_readonly_apply_backs_up_and_preserves_bytes(tmp_path, capsys):
    path = tmp_path / "history.jsonl"
    real = b'{"execution_price": "4567.12", "equity": "1000"}\r\n'
    dirty = json.dumps({"error": "未配置调用"}, ensure_ascii=False).encode() + b"\n"
    # 非法 JSON 不可擅自删除，原始字节与无末尾换行均需保留。
    original = real + dirty + b"invalid-json"
    path.write_bytes(original)
    before = path.stat().st_mtime_ns
    assert main(["--data-dir", str(tmp_path)]) == 0
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before
    assert not path.with_suffix(".jsonl.bak").exists()
    output = capsys.readouterr().out
    assert "未配置调用" in output and "history.jsonl:2" in output
    assert main(["--data-dir", str(tmp_path), "--apply"]) == 0
    assert path.read_bytes() == real + b"invalid-json"
    assert path.with_suffix(".jsonl.bak").read_bytes() == original
    # 第二次没有候选记录时不重写、不覆盖备份。
    main(["--data-dir", str(tmp_path), "--apply"])
    assert path.with_suffix(".jsonl.bak").read_bytes() == original


def test_existing_backup_is_not_overwritten(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"error": "AssertionError"}\n')
    backup = path.with_suffix(".jsonl.bak")
    backup.write_bytes(b"previous backup")
    assert main(["--data-dir", str(tmp_path), "--apply"]) == 1
    assert "AssertionError" in path.read_text()
    assert backup.read_bytes() == b"previous backup"
