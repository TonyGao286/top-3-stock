"""
为经 requests / urllib3 发起的访问补充浏览器常见请求头。

说明：
- 东财等站点对「无 User-Agent / 数据中心 UA」的流量更易风控；
- **无法保证**绕过 IP 级黑名单（GitHub Actions、云机房出口常被整段限制）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCHED = False


def ensure_request_identity() -> None:
    """对 ``requests.Session.request`` 做一次性增强（幂等）。"""
    global _PATCHED
    if _PATCHED:
        return
    import requests

    _ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    _defaults = {
        "User-Agent": _ua,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }

    _orig = requests.sessions.Session.request

    def _patched(  # type: ignore[no-untyped-def]
        self,
        method: str,
        url: str,
        **kwargs,
    ):
        headers = dict(kwargs.pop("headers", None) or {})
        for k, v in _defaults.items():
            headers.setdefault(k, v)
        u = str(url).lower()
        if "eastmoney.com" in u:
            headers.setdefault("Referer", "https://quote.eastmoney.com/")
        return _orig(self, method, url, headers=headers, **kwargs)

    requests.sessions.Session.request = _patched  # type: ignore[assignment]
    _PATCHED = True
    logger.debug("已安装 requests 浏览器级默认请求头（akshare/东财 等）")
