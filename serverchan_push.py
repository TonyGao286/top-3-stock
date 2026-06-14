"""Server酱（方糖）消息推送封装。"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

DEFAULT_API_TEMPLATE = "https://sctapi.ftqq.com/{sendkey}.send"


def get_sendkey() -> str:
    key = (os.environ.get("SERVERCHAN_SENDKEY") or os.environ.get("SCKEY") or "").strip()
    if not key:
        raise ValueError(
            "未配置 Server酱 SendKey。请设置环境变量 SERVERCHAN_SENDKEY，"
            "或在项目根目录 .env 中写入 SERVERCHAN_SENDKEY=你的SendKey"
        )
    return key


def build_api_url(sendkey: str | None = None) -> str:
    sendkey = (sendkey or get_sendkey()).strip()
    tpl = (os.environ.get("SERVERCHAN_API_URL") or DEFAULT_API_TEMPLATE).strip()
    if "{sendkey}" in tpl:
        return tpl.format(sendkey=sendkey)
    return tpl.rstrip("/") + f"/{sendkey}.send"


def send_serverchan(
    title: str,
    desp: str,
    *,
    sendkey: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
  向 Server酱 发送消息。

  :returns: 接口 JSON（含 code、message 等）
  :raises: ValueError / requests.RequestException / RuntimeError
  """
    url = build_api_url(sendkey)
    payload = {"title": title[:256], "desp": desp[:65536]}
    logger.info("Server酱推送：%s", title)
    resp = requests.post(url, data=payload, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Server酱返回非 JSON：{resp.text[:500]}") from exc
    code = data.get("code")
    if code not in (0, "0", None):
        raise RuntimeError(f"Server酱推送失败：{data}")
    return data


def send_serverchan_get_fallback(
    title: str,
    desp: str,
    *,
    sendkey: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST 失败时尝试 GET（部分网络环境仅允许 GET）。"""
    sendkey = (sendkey or get_sendkey()).strip()
    url = (
        build_api_url(sendkey)
        + f"?title={quote(title[:256])}&desp={quote(desp[:65536])}"
    )
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") not in (0, "0", None):
        raise RuntimeError(f"Server酱推送失败：{data}")
    return data
