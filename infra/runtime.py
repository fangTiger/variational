"""运行时环境准备。"""

from __future__ import annotations

import os


def ensure_ssl_cert() -> None:
    """确保 SSL_CERT_FILE 指向可用 CA，避免 macOS 下证书校验失败。

    x10 SDK 底层用 aiohttp，在连接时读取 CA。若系统未配好证书，
    用 certifi 提供的 CA 兜底。
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass
