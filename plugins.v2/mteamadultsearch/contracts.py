"""Dependency-free M-Team request and MoviePilot download contracts."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict


AV_KEYWORD_RE = re.compile(r"^[A-Z0-9]{2,12}(?:-[A-Z0-9]{1,12})*-[0-9]{2,9}$")
DEFAULT_API_BASE_URL = "https://api.m-team.cc/api"


def normalize_av_keyword(value: str) -> str:
    """Normalize common AV-number separators without changing the identifier."""

    normalized = str(value or "").strip().upper()
    normalized = re.sub(r"[\s._]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized or not AV_KEYWORD_RE.fullmatch(normalized):
        raise ValueError("请输入有效的 AV 番号，例如 PRED-879")
    return normalized


def build_search_payload(keyword: str, page: int, page_size: int) -> Dict[str, Any]:
    """Build the M-Team adult-mode request body."""

    return {
        "keyword": normalize_av_keyword(keyword),
        "mode": "adult",
        "categories": [],
        "pageNumber": int(page),
        "pageSize": int(page_size),
        "visible": 1,
    }


def build_credential_enclosure(
    api_url: str,
    torrent_id: str,
    api_key: str,
    user_agent: str,
    use_proxy: bool,
) -> str:
    """Build MoviePilot's credential-style M-Team download URL."""

    request_descriptor = {
        "method": "post",
        "cookie": False,
        "params": {"id": str(torrent_id)},
        "header": {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "x-api-key": api_key,
        },
        "proxy": bool(use_proxy),
        "result": "data",
    }
    encoded = base64.b64encode(
        json.dumps(request_descriptor, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")
    ).decode("ascii")
    return f"[{encoded}]{api_url.rstrip('/')}/torrent/genDlToken"

