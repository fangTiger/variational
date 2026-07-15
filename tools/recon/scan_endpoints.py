"""扫描已下载的前端 chunk，提取 Variational Omni 私有 API 的关键信息。

输出：API 基址、认证头、端点清单、是否存在钱包签名原语。
用于验证 FINDINGS.md 中的结论，并在前端改版后快速复查。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

CHUNKS = Path(__file__).parent / "captured" / "chunks"

# 感兴趣的端点前缀
ENDPOINT_RE = re.compile(
    r'"(/(?:auth|orders|quotes|positions|trades|points|portfolio|'
    r'referrals|funding|metadata|v1|profile)[A-Za-z0-9_/-]*)"'
)
# 钱包签名原语（若命中说明下单需要签名）
SIGN_RE = re.compile(
    r"signTypedData|personal_sign|eth_sign|signMessage|sign_order|_signTransaction"
)
BASE_RE = re.compile(r'm_="([^"]*)"')
AUTH_HEADER_RE = re.compile(r'"(x-omni-auth|vr-connected-address)"')


def _read_all() -> str:
    """读取全部 chunk 内容拼接为一个字符串。"""
    if not CHUNKS.exists():
        raise SystemExit("未找到 captured/chunks，请先运行 fetch_bundle.py")
    return "\n".join(
        f.read_text(encoding="utf-8", errors="replace") for f in CHUNKS.glob("*.js")
    )


def main() -> None:
    blob = _read_all()

    print("=== API 基址 (m_) ===")
    for m in sorted(set(BASE_RE.findall(blob))):
        print(f"  {m!r}")

    print("\n=== 认证/固定头 ===")
    for h in sorted(set(AUTH_HEADER_RE.findall(blob))):
        print(f"  {h}")

    print("\n=== 端点清单 ===")
    endpoints = Counter(ENDPOINT_RE.findall(blob))
    for ep in sorted(endpoints):
        print(f"  {ep}")
    print(f"  共 {len(endpoints)} 个端点")

    # 只在“包含下单/报价端点”的 chunk 里查签名原语。
    # 全量代码里的签名来自 WalletConnect 登录/存款模块，与下单无关，会误报。
    print("\n=== 下单流程是否含钱包签名原语（仅扫下单/报价所在 chunk）===")
    order_ep = re.compile(r"/orders/(?:new|cancel|tpsl|close_all)|/quotes/accept")
    order_chunks = [
        f for f in CHUNKS.glob("*.js")
        if order_ep.search(f.read_text(encoding="utf-8", errors="replace"))
    ]
    total_hits: Counter = Counter()
    for f in order_chunks:
        total_hits.update(SIGN_RE.findall(f.read_text(encoding="utf-8", errors="replace")))
    print(f"  相关 chunk：{', '.join(f.name for f in order_chunks) or '无'}")
    if total_hits:
        print(f"  ⚠️ 命中 {dict(total_hits)} —— 下单可能需要签名，请复查")
    else:
        print("  ✅ 下单/报价 chunk 内无签名原语，下单靠会话 Cookie 授权")


if __name__ == "__main__":
    main()
