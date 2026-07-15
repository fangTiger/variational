"""下载 Variational Omni 网页端 SvelteKit 打包代码。

用途：抓取入口 JS 及其递归引用的全部 chunk，供 scan_endpoints.py 分析私有接口。
产物写入 tools/recon/captured/，该目录已在 .gitignore 中忽略。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://omni.variational.io"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CAPTURED = Path(__file__).parent / "captured"
CHUNKS = CAPTURED / "chunks"

# 匹配前端里引用的 chunk 文件名
CHUNK_RE = re.compile(r"chunks/([A-Za-z0-9_-]+\.js)")
ENTRY_RE = re.compile(r"/_app/immutable/entry/[A-Za-z0-9_.-]+\.js")


def _get(url: str) -> str:
    """发起 GET 请求，返回文本内容。"""
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _save(name: str, text: str) -> None:
    """保存内容到 captured 目录。"""
    path = CHUNKS / name
    path.write_text(text, encoding="utf-8")


def main() -> None:
    CHUNKS.mkdir(parents=True, exist_ok=True)

    # 1. 抓首页 HTML，找入口 JS
    print("抓取首页 HTML …")
    html = _get(f"{BASE}/")
    (CAPTURED / "omni_index.html").write_text(html, encoding="utf-8")
    entries = sorted(set(ENTRY_RE.findall(html)))
    print(f"发现入口 JS {len(entries)} 个")

    seen: set[str] = set()
    for entry in entries:
        text = _get(f"{BASE}{entry}")
        _save(Path(entry).name, text)

    # 2. 递归下载所有被引用的 chunk，直到收敛
    for round_no in range(1, 8):
        refs: set[str] = set()
        for js in CHUNKS.glob("*.js"):
            refs.update(CHUNK_RE.findall(js.read_text(encoding="utf-8", errors="replace")))
        new = 0
        for name in sorted(refs):
            if name in seen or (CHUNKS / name).exists():
                seen.add(name)
                continue
            try:
                _save(name, _get(f"{BASE}/_app/immutable/chunks/{name}"))
                new += 1
            except Exception as exc:  # 单个失败不阻断整体
                print(f"  ! 下载 {name} 失败: {exc}")
            seen.add(name)
            time.sleep(0.05)
        total = len(list(CHUNKS.glob("*.js")))
        print(f"第 {round_no} 轮：新增 {new}，累计 {total}")
        if new == 0:
            break

    total_bytes = sum(f.stat().st_size for f in CHUNKS.glob("*.js"))
    print(f"完成，chunk 总大小 {total_bytes / 1024:.0f} KB")


if __name__ == "__main__":
    main()
