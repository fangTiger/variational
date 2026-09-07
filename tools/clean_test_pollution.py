"""一次性排查 JSONL 测试污染；默认只读，--apply 备份后原子重写。"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tempfile


def pollution_reasons(record: object) -> list[str]:
    """递归检查明确测试标记；假价格必须同时伴随权益恰为 1000。"""
    reasons = []
    text = json.dumps(record, ensure_ascii=False)
    for marker in ("未配置调用", "AssertionError"):
        if marker in text:
            reasons.append(f"含测试异常标记：{marker}")
    prices, equities = [], []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                try:
                    number = Decimal(str(child))
                except (InvalidOperation, ValueError):
                    number = None
                if number is not None and not number.is_finite():
                    number = None
                if key in {"execution_price", "quote_mid", "price"}:
                    prices.append(number)
                if key == "equity":
                    equities.append(number)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(record)
    if Decimal("1000") in equities and any(p in (Decimal("4000"), Decimal("4001")) for p in prices):
        reasons.append("价格恰为 4000/4001，且 equity 恰为 1000")
    return reasons


def clean_file(path: Path, *, apply: bool = False) -> int:
    """保留未命中行的原始字节；拒绝覆盖已有备份或并发改变的文件。"""
    original = path.read_bytes()
    kept = []
    count = 0
    for line_number, line in enumerate(original.splitlines(keepends=True), 1):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if line.strip():
                print(f"{path}:{line_number} 非法 JSON，原样保留")
            kept.append(line)
            continue
        reasons = pollution_reasons(record)
        if not reasons:
            kept.append(line)
            continue
        count += 1
        print(f"{path}:{line_number} 将删除：{json.dumps(record, ensure_ascii=False)}")
        print(f"  判定理由：{'；'.join(reasons)}")
    if not apply or not count:
        return count
    backup = path.with_suffix(path.suffix + ".bak")
    if path.is_symlink():
        raise OSError(f"拒绝重写符号链接：{path}")
    before = path.stat()
    if path.read_bytes() != original:
        raise OSError(f"扫描后文件已改变，停止清理：{path}")
    # 独占创建备份，保留首次原始证据。
    with backup.open("xb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(b"".join(kept))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(before.st_mode & 0o777)
        if path.stat().st_mtime_ns != before.st_mtime_ns or path.read_bytes() != original:
            raise OSError(f"备份后文件已改变，停止清理：{path}")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"已删除 {count} 条，原文件备份：{backup}")
    return count


def main(argv=None) -> int:
    """逐文件列出候选记录；只有显式 --apply 才修改。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--apply", action="store_true", help="确认备份并删除命中记录")
    args = parser.parse_args(argv)
    print("执行清理（先备份）" if args.apply else "只读扫描，不删除；确认后使用 --apply")
    failed = False
    count = 0
    for path in sorted(args.data_dir.glob("*.jsonl")):
        try:
            count += clean_file(path, apply=args.apply)
        except OSError as exc:
            print(f"清理失败：{exc}")
            failed = True
    print(f"共识别 {count} 条测试特征记录")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
