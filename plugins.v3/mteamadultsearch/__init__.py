"""MoviePilot V3 plugin for isolated M-Team adult-number searches.

The plugin intentionally owns a separate API instead of overriding the global
indexer module. This keeps MoviePilot's normal M-Team movie/TV search unchanged
while allowing M-Team's ``mode=adult`` request contract for AV numbers.

This V3 copy mirrors ``plugins.v2/mteamadultsearch`` and only swaps host
imports for the V3 SDK namespaces (``app.sdk.media`` / ``app.sdk.network`` /
``app.application.directory``).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.application.directory import DirectoryHelper, validate_download_save_path
from app.chain.download import DownloadChain
from app.plugins import _PluginBase
from app.sdk.media import Context, MediaInfo, MetaInfo, TorrentInfo
from app.sdk.network import RequestUtils, SitesHelper
from app.schemas.types import MediaType, MessageType
from .contracts import (
    DEFAULT_API_BASE_URL,
    build_credential_enclosure,
    build_search_payload,
    normalize_search_keyword,
)


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100
MAX_PAGE = 500
MAX_PAGES_PER_REQUEST = 5
SESSION_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 40
SEARCH_CACHE_TTL_SECONDS = 120
SEARCH_CACHE_MAX_ENTRIES = 20
MIN_REQUEST_INTERVAL_SECONDS = 1.0
HISTORY_KEY = "submission_history"
HISTORY_LIMIT = 50
HISTORY_PAGE_SIZE = 5
SORT_OPTIONS = ("site", "seeders", "size", "time", "free_first")


class PluginApiError(Exception):
    """Plugin API failure mapped to a stable HTTP status and error code."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class SearchRequest(BaseModel):
    """Number search input exposed by the plugin API."""

    keyword: str = Field(..., min_length=1, max_length=100)
    page: int = Field(default=1, ge=1, le=MAX_PAGE)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    max_pages: int = Field(default=1, ge=1, le=MAX_PAGES_PER_REQUEST)
    sort: str = Field(default="site")
    free_only: bool = Field(default=False)


class SearchCandidate(BaseModel):
    """Safe projection returned to the caller; no M-Team download URL."""

    id: str
    title: str
    site_name: str
    size_bytes: Optional[int] = None
    seeders: Optional[int] = None
    peers: Optional[int] = None
    published_at: Optional[str] = None
    category: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    download_factor: Optional[float] = None
    upload_factor: Optional[float] = None
    detail_url: Optional[str] = None
    is_free: bool = False


class SearchResponse(BaseModel):
    """Search result envelope for PrivateFilm and other clients."""

    search_id: str
    keyword: str
    page: int
    page_size: int
    total: Optional[int] = None
    items: List[SearchCandidate]
    is_av_number: bool = False


class SubmitRequest(BaseModel):
    """Opaque candidate reference used for a download submission."""

    search_id: str = Field(..., min_length=16, max_length=64)
    candidate_id: str = Field(..., min_length=16, max_length=64)


class SubmitResponse(BaseModel):
    """Download submission result."""

    submitted: bool
    download_id: Optional[str] = None
    save_path: Optional[str] = None
    title: Optional[str] = None


class PathResponse(BaseModel):
    """A MoviePilot-configured download path."""

    name: str
    save_path: str
    media_type: Optional[str] = None
    media_category: Optional[str] = None


@dataclass
class _StoredCandidate:
    """Raw row and the site context needed only at submit time."""

    raw: Dict[str, Any]
    site: Dict[str, Any]
    keyword: str


@dataclass
class _SearchSession:
    """Short-lived in-memory search session."""

    created_at: float
    keyword: str
    candidates: Dict[str, _StoredCandidate]


class MTeamAdultSearch(_PluginBase):
    """Search M-Team's adult catalog without touching global MoviePilot search."""

    plugin_name = "M-Team 成人区番号搜索"
    plugin_desc = "独立调用 M-Team 成人区 API，支持 AV 番号搜索和 MoviePilot 下载。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "2.1.0"
    plugin_author = "PrivateFilm"
    author_url = "https://github.com/shengleirain"
    plugin_config_prefix = "mteamadultsearch_"
    plugin_order = 80
    auth_level = 2

    _enabled = False
    _config: Dict[str, Any] = {}
    _notify = True
    _sessions: Dict[str, _SearchSession] = {}
    _search_cache: Dict[str, Dict[str, Any]] = {}
    _last_upstream_at = 0.0
    _lock = threading.RLock()
    _save_path_error: Optional[str] = None

    def init_plugin(self, config: dict = None):
        """Load repeatable plugin configuration and clear stale sessions."""

        self._config = dict(config or {})
        self._enabled = bool(self._config.get("enabled", False))
        self._notify = bool(self._config.get("notify", True))
        self._save_path_error = None
        configured_path = self._string(self._config.get("save_path"))
        if configured_path:
            try:
                self._save_path()
            except Exception as error:
                self._save_path_error = self._message_of(error)
        with self._lock:
            self._sessions.clear()
            self._search_cache.clear()

    def get_state(self) -> bool:
        """Return whether the plugin is enabled."""

        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """This plugin is driven by API calls from PrivateFilm."""

        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """Expose isolated search, submit, and configured-path endpoints."""

        return [
            {
                "path": "/search",
                "endpoint": self.search,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "搜索 M-Team 成人区番号",
                "response_model": SearchResponse,
            },
            {
                "path": "/submit",
                "endpoint": self.submit,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "提交 M-Team 成人资源到 MoviePilot",
                "response_model": SubmitResponse,
            },
            {
                "path": "/paths",
                "endpoint": self.paths,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询 MoviePilot 下载路径",
                "response_model": List[PathResponse],
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Render the standard V3 configuration form on a responsive grid."""

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "提交成功后推送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout_seconds",
                                            "label": "请求超时（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_base_url",
                                            "label": "M-Team API 地址",
                                            "placeholder": DEFAULT_API_BASE_URL,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "save_path",
                                            "label": "AV 下载目录",
                                            "items": self._download_path_options(),
                                            "item-title": "title",
                                            "item-value": "value",
                                            "clearable": True,
                                            "hint": "只显示 MoviePilot 已配置的下载目录；清空表示使用默认目录",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "api_base_url": DEFAULT_API_BASE_URL,
            "save_path": "",
            "timeout_seconds": 30,
            "notify": True,
        }

    def get_page(self) -> List[dict]:
        """Show the plugin status, bound site, download directory and history."""

        status = "已启用" if self._enabled else "未启用"
        site_text = "未找到 M-Team 站点"
        try:
            site = self._resolve_site()
            site_name = self._string(site.get("name")) or "M-Team"
            site_text = f"已绑定站点：{site_name}"
        except Exception as error:
            site_text = self._message_of(error)
        path = self._string(self._config.get("save_path")) or "MoviePilot 默认下载目录"

        pages = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": (
                        "本插件只提供独立的 M-Team 成人区番号 API，不重载全局索引器；"
                        "MoviePilot 原有的普通电影/电视剧搜索保持不变。"
                    ),
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": f"状态：{status}；{site_text}；AV 下载目录：{path}",
                },
            },
        ]
        if self._save_path_error:
            pages.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": (
                            f"下载目录配置无效：{self._save_path_error}。"
                            "请在 MoviePilot 下载目录中先配置并重新选择。"
                        ),
                    },
                }
            )
        records = self._load_history()
        summary = "最近提交：暂无" if not records else "最近提交："
        pages.append(
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": summary},
            }
        )
        for record in records[:HISTORY_PAGE_SIZE]:
            if not isinstance(record, dict):
                continue
            saved_at = self._string(record.get("time")) or "-"
            title = self._string(record.get("title")) or "未命名资源"
            download_id = self._string(record.get("download_id")) or "-"
            pages.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "success",
                        "variant": "tonal",
                        "density": "compact",
                        "text": f"{saved_at}｜{title}｜任务 {download_id}",
                    },
                }
            )
        return pages

    def stop_service(self):
        """Release short-lived in-memory sessions when the plugin stops."""

        with self._lock:
            self._sessions.clear()
            self._search_cache.clear()
        self._enabled = False

    def search(self, request: SearchRequest) -> SearchResponse:
        """Search M-Team adult resources and store opaque candidates server-side."""

        try:
            return self._search(request)
        except PluginApiError as error:
            raise self._to_http_error(error) from error

    def submit(self, request: SubmitRequest) -> SubmitResponse:
        """Submit a stored M-Team candidate through MoviePilot's DownloadChain."""

        try:
            return self._submit(request)
        except PluginApiError as error:
            raise self._to_http_error(error) from error

    def paths(self) -> List[PathResponse]:
        """Return configured MoviePilot paths without exposing arbitrary paths."""

        try:
            self._require_enabled()
        except PluginApiError as error:
            raise self._to_http_error(error) from error
        result: List[PathResponse] = []
        for entry in DirectoryHelper().get_download_dirs() or []:
            path = self._string(getattr(entry, "download_path", None))
            if not path:
                continue
            storage = self._string(getattr(entry, "storage", None)) or "local"
            name = self._string(getattr(entry, "name", None)) or path
            result.append(
                PathResponse(
                    name=name,
                    save_path=f"{storage}:{path}",
                    media_type=self._string(getattr(entry, "media_type", None)),
                    media_category=self._string(
                        getattr(entry, "media_category", None)
                    ),
                )
            )
        return result

    def _search(self, request: SearchRequest) -> SearchResponse:
        """Search implementation shared by the API endpoint."""

        self._require_enabled()
        if request.sort not in SORT_OPTIONS:
            raise PluginApiError(
                400,
                "invalid_sort",
                f"sort 必须是 {'/'.join(SORT_OPTIONS)} 之一",
            )
        try:
            keyword, is_av_number = normalize_search_keyword(request.keyword)
        except ValueError as error:
            raise PluginApiError(400, "invalid_keyword", str(error)) from error

        site = self._resolve_site()
        cache_key = (keyword, request.page, request.page_size, request.max_pages)
        entry = self._cache_get(cache_key)
        if entry is not None:
            rows: List[Dict[str, Any]] = entry["rows"]
            total: Optional[int] = entry["total"]
            site = entry["site"]
        else:
            try:
                rows, total = self._fetch_rows(
                    keyword=keyword,
                    page=request.page,
                    page_size=request.page_size,
                    max_pages=request.max_pages,
                    site=site,
                )
            except ValueError as error:
                raise PluginApiError(400, "invalid_api_url", str(error)) from error
            self._cache_put(cache_key, rows, total, site)

        rows = self._sorted_rows(rows, request.sort, request.free_only)

        session_id = uuid4().hex
        safe_items: List[SearchCandidate] = []
        stored: Dict[str, _StoredCandidate] = {}
        for row in rows:
            torrent_id = self._string(row.get("id"))
            title = self._string(row.get("name"))
            if not torrent_id or not title:
                continue
            candidate_id = uuid4().hex
            stored[candidate_id] = _StoredCandidate(
                raw=dict(row),
                site=dict(site),
                keyword=keyword,
            )
            safe_items.append(self._safe_candidate(candidate_id, row, site))

        with self._lock:
            self._purge_sessions()
            self._sessions[session_id] = _SearchSession(
                created_at=time.time(),
                keyword=keyword,
                candidates=stored,
            )

        return SearchResponse(
            search_id=session_id,
            keyword=keyword,
            page=request.page,
            page_size=request.page_size,
            total=total,
            items=safe_items,
            is_av_number=is_av_number,
        )

    def _submit(self, request: SubmitRequest) -> SubmitResponse:
        """Submit implementation shared by the API endpoint."""

        self._require_enabled()
        try:
            save_path = self._save_path()
        except ValueError as error:
            raise PluginApiError(400, "invalid_save_path", str(error)) from error
        stored = self._take_candidate(request.search_id, request.candidate_id)
        site = stored.site
        raw = stored.raw
        torrent_id = self._string(raw.get("id"))
        title = self._string(raw.get("name"))
        if not torrent_id or not title:
            raise PluginApiError(400, "invalid_candidate", "M-Team 候选缺少必要字段")

        api_key = self._site_api_key(site)
        user_agent = self._site_user_agent(site)
        try:
            api_url = self._api_base_url()
        except ValueError as error:
            raise PluginApiError(400, "invalid_api_url", str(error)) from error
        torrent_dict = self._torrent_dict(
            raw=raw,
            site=site,
            enclosure=build_credential_enclosure(
                api_url=api_url,
                torrent_id=torrent_id,
                api_key=api_key,
                user_agent=user_agent,
                use_proxy=bool(site.get("proxy")),
            ),
        )

        torrent = TorrentInfo()
        torrent.from_dict(torrent_dict)
        metainfo = MetaInfo(title=title, subtitle=torrent_dict.get("description"))
        mediainfo = MediaInfo()
        mediainfo.from_dict({"title": title})
        context = Context(
            meta_info=metainfo,
            media_info=mediainfo,
            torrent_info=torrent,
        )
        download_id = DownloadChain().download_single(
            context=context,
            username=self.plugin_name,
            save_path=save_path,
            source="MTeamAdultSearch",
        )
        if not download_id:
            raise PluginApiError(502, "download_failed", "MoviePilot 未能创建下载任务")
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "keyword": stored.keyword,
            "title": title,
            "download_id": str(download_id),
            "save_path": save_path or "",
        }
        self._record_history(record)
        self._notify_submit(record)
        return SubmitResponse(
            submitted=True,
            download_id=str(download_id),
            save_path=save_path,
            title=title,
        )

    def _fetch_rows(
        self,
        keyword: str,
        page: int,
        page_size: int,
        max_pages: int,
        site: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Fetch up to ``max_pages`` pages and merge them without duplicates."""

        rows: List[Dict[str, Any]] = []
        seen: set = set()
        total: Optional[int] = None
        url = f"{self._api_base_url()}/torrent/search"
        for index in range(max_pages):
            payload = build_search_payload(keyword, page + index, page_size)
            self._throttle()
            body = self._post_json(url, payload, site)
            page_rows, page_total = self._parse_search_response(body)
            if total is None:
                total = page_total
            for row in page_rows:
                torrent_id = self._string(row.get("id"))
                if not torrent_id or torrent_id in seen:
                    continue
                seen.add(torrent_id)
                rows.append(row)
            if len(page_rows) < page_size:
                break
            if total is not None and len(rows) >= total:
                break
        return rows, total

    def _sorted_rows(
        self,
        rows: List[Dict[str, Any]],
        sort: str,
        free_only: bool,
    ) -> List[Dict[str, Any]]:
        """Apply the free filter and requested ordering to a page copy."""

        rows = list(rows)
        if free_only:
            rows = [row for row in rows if self._row_is_free(row)]
        if sort == "seeders":
            rows.sort(
                key=lambda row: self._int_value(
                    (row.get("status") or {}).get("seeders")
                )
                or 0,
                reverse=True,
            )
        elif sort == "size":
            rows.sort(
                key=lambda row: self._int_value(row.get("size")) or 0,
                reverse=True,
            )
        elif sort == "time":
            rows.sort(
                key=lambda row: self._string(row.get("createdDate")) or "",
                reverse=True,
            )
        elif sort == "free_first":
            rows.sort(key=lambda row: 0 if self._row_is_free(row) else 1)
        return rows

    def _row_is_free(self, row: Dict[str, Any]) -> bool:
        status = row.get("status") if isinstance(row.get("status"), dict) else {}
        return self._discount_factor(status.get("discount")) == 0.0

    def _cache_get(self, key: Tuple[str, int, int, int]) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._search_cache.get(key)
            if not entry:
                return None
            if time.time() > entry["expires_at"]:
                self._search_cache.pop(key, None)
                return None
            return entry

    def _cache_put(
        self,
        key: Tuple[str, int, int, int],
        rows: List[Dict[str, Any]],
        total: Optional[int],
        site: Dict[str, Any],
    ) -> None:
        with self._lock:
            now = time.time()
            expired = [
                item for item, entry in self._search_cache.items()
                if now > entry["expires_at"]
            ]
            for item in expired:
                self._search_cache.pop(item, None)
            while len(self._search_cache) >= SEARCH_CACHE_MAX_ENTRIES:
                oldest = min(
                    self._search_cache.items(),
                    key=lambda pair: pair[1]["created_at"],
                )[0]
                self._search_cache.pop(oldest, None)
            self._search_cache[key] = {
                "created_at": now,
                "expires_at": now + SEARCH_CACHE_TTL_SECONDS,
                "rows": rows,
                "total": total,
                "site": site,
            }

    def _throttle(self) -> None:
        """Keep a minimum interval between upstream M-Team requests."""

        with self._lock:
            wait = self._last_upstream_at + MIN_REQUEST_INTERVAL_SECONDS - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_upstream_at = time.time()

    def _record_history(self, record: Dict[str, Any]) -> None:
        """Persist the newest-first submission history, never failing submits."""

        records = self._load_history()
        records.insert(0, record)
        del records[HISTORY_LIMIT:]
        try:
            self.save_data(HISTORY_KEY, records)
        except Exception:
            pass

    def _load_history(self) -> List[Dict[str, Any]]:
        try:
            records = self.get_data(HISTORY_KEY)
        except Exception:
            return []
        if not isinstance(records, list):
            return []
        return [record for record in records if isinstance(record, dict)]

    def _notify_submit(self, record: Dict[str, Any]) -> None:
        """Push a message-channel notification; failures never block submits."""

        if not self._notify:
            return
        text = f"{record['title']}\n下载 ID：{record['download_id']}"
        if record.get("save_path"):
            text += f"\n保存目录：{record['save_path']}"
        try:
            self.post_message(
                mtype=MessageType.Plugin,
                title="M-Team 下载任务已提交",
                text=text,
            )
        except Exception:
            pass

    def _download_path_options(self) -> List[Dict[str, str]]:
        """Build form options from MoviePilot's configured download roots."""

        options: List[Dict[str, str]] = [
            {"title": "使用 MoviePilot 默认下载目录", "value": ""}
        ]
        try:
            for entry in DirectoryHelper().get_download_dirs() or []:
                path = self._string(getattr(entry, "download_path", None))
                if not path:
                    continue
                storage = self._string(getattr(entry, "storage", None)) or "local"
                value = path if storage == "local" else f"{storage}:{path}"
                name = self._string(getattr(entry, "name", None)) or path
                options.append({"title": f"{name}（{value}）", "value": value})
        except Exception:
            # The form must still render when the host has no directory
            # configuration yet; submit-time validation remains authoritative.
            pass

        current = self._string(self._config.get("save_path"))
        if current and not any(item["value"] == current for item in options):
            options.append({"title": f"当前配置（{current}）", "value": current})
        return options

    def _resolve_site(self) -> Dict[str, Any]:
        """Find the bound M-Team mTorrent indexer and its API credentials."""

        indexers = SitesHelper().get_indexers() or []
        candidates = [
            item
            for item in indexers
            if str(item.get("parser") or "") == "mTorrent"
        ]
        mteam_candidates = [
            item
            for item in candidates
            if "m-team" in (
                f"{item.get('name', '')} {item.get('domain', '')}"
            ).lower()
        ]
        candidates = mteam_candidates or candidates
        if not candidates:
            raise PluginApiError(
                400,
                "site_not_configured",
                "未找到 M-Team 站点：请先在 MoviePilot 站点中配置 mTorrent 解析器",
            )
        site = dict(candidates[0])
        if not self._site_api_key(site):
            raise PluginApiError(
                400,
                "site_not_configured",
                "M-Team 站点未配置 API Access Token",
            )
        return site

    def _post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        site: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST JSON through MoviePilot's V3 SDK HTTP helper."""

        response = RequestUtils(
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": self._site_user_agent(site),
                "x-api-key": self._site_api_key(site),
            },
            proxies=None,
            referer=self._site_referer(site),
            timeout=self._timeout_seconds(),
        ).post_res(url=url, json=payload)
        if response is None:
            raise PluginApiError(502, "upstream_error", "M-Team 搜索请求无法连接")
        if response.status_code < 200 or response.status_code >= 300:
            raise PluginApiError(
                502,
                "upstream_error",
                f"M-Team 搜索返回 HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except Exception as error:
            raise PluginApiError(
                502, "upstream_error", "M-Team 返回格式无效"
            ) from error
        if not isinstance(body, dict):
            raise PluginApiError(502, "upstream_error", "M-Team 返回格式无效")
        return body

    def _parse_search_response(
        self,
        body: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Validate M-Team's response envelope and extract the result page."""

        code = body.get("code")
        if code not in (None, 0, "0", 200, "200"):
            message = self._string(body.get("message")) or "M-Team 搜索失败"
            raise PluginApiError(502, "upstream_error", message)
        data = body.get("data")
        if not isinstance(data, dict):
            return [], 0
        rows = data.get("data")
        if not isinstance(rows, list):
            return [], self._int_value(data.get("total"))
        valid_rows = [row for row in rows if isinstance(row, dict)]
        return valid_rows, self._int_value(data.get("total"))

    def _safe_candidate(
        self,
        candidate_id: str,
        raw: Dict[str, Any],
        site: Dict[str, Any],
    ) -> SearchCandidate:
        """Project a row without returning credential-bearing download data."""

        status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
        labels = raw.get("labelsNew")
        if not isinstance(labels, list):
            labels = []
        download_factor = self._discount_factor(status.get("discount"))
        return SearchCandidate(
            id=candidate_id,
            title=self._string(raw.get("name")) or "未命名资源",
            site_name=self._string(site.get("name")) or "M-Team",
            size_bytes=self._int_value(raw.get("size")),
            seeders=self._int_value(status.get("seeders")),
            peers=self._int_value(status.get("leechers")),
            published_at=self._string(raw.get("createdDate")),
            category=self._string(raw.get("category")),
            labels=[str(label) for label in labels if str(label).strip()],
            download_factor=download_factor,
            upload_factor=self._upload_factor(status.get("discount")),
            detail_url=self._detail_url(site, raw.get("id")),
            is_free=download_factor == 0.0,
        )

    def _torrent_dict(
        self,
        raw: Dict[str, Any],
        site: Dict[str, Any],
        enclosure: str,
    ) -> Dict[str, Any]:
        """Build the TorrentInfo-compatible input consumed by DownloadChain."""

        status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
        return {
            "title": self._string(raw.get("name")),
            "description": self._string(raw.get("smallDescr")),
            "enclosure": enclosure,
            "pubdate": self._string(raw.get("createdDate")),
            "size": self._int_value(raw.get("size")) or 0,
            "seeders": self._int_value(status.get("seeders")) or 0,
            "peers": self._int_value(status.get("leechers")) or 0,
            "grabs": self._int_value(status.get("timesCompleted")) or 0,
            "downloadvolumefactor": self._discount_factor(
                status.get("discount")
            ),
            "uploadvolumefactor": self._upload_factor(status.get("discount")),
            "page_url": self._detail_url(site, raw.get("id")),
            "site": site.get("id"),
            "site_name": self._string(site.get("name")),
            "site_cookie": self._string(site.get("cookie")),
            "site_ua": self._site_user_agent(site),
            "site_proxy": bool(site.get("proxy")),
            "category": MediaType.MOVIE.value,
        }

    def _take_candidate(
        self,
        search_id: str,
        candidate_id: str,
    ) -> _StoredCandidate:
        """Consume an opaque candidate exactly once after TTL validation."""

        with self._lock:
            self._purge_sessions()
            session = self._sessions.get(search_id)
            if not session:
                raise PluginApiError(410, "session_expired", "搜索会话不存在或已过期")
            candidate = session.candidates.pop(candidate_id, None)
            if not candidate:
                raise PluginApiError(404, "candidate_not_found", "搜索候选不存在或已提交")
            if not session.candidates:
                self._sessions.pop(search_id, None)
            return candidate

    def _purge_sessions(self) -> None:
        """Bound memory and remove expired candidate sessions."""

        now = time.time()
        expired = [
            key
            for key, session in self._sessions.items()
            if now - session.created_at > SESSION_TTL_SECONDS
        ]
        for key in expired:
            self._sessions.pop(key, None)
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.items(), key=lambda item: item[1].created_at)[0]
            self._sessions.pop(oldest, None)

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise PluginApiError(409, "plugin_disabled", "M-Team 成人区番号搜索插件未启用")

    @staticmethod
    def _to_http_error(error: PluginApiError) -> HTTPException:
        return HTTPException(
            status_code=error.status_code,
            detail=f"[{error.code}] {error.message}",
        )

    @staticmethod
    def _message_of(error: Exception) -> str:
        return str(getattr(error, "message", None) or error)

    def _api_base_url(self) -> str:
        value = self._string(self._config.get("api_base_url")) or DEFAULT_API_BASE_URL
        value = value.rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("M-Team API 地址必须是 http:// 或 https://")
        if not parsed.path.rstrip("/").endswith("/api"):
            value = f"{value}/api"
        return value.rstrip("/")

    def _save_path(self) -> Optional[str]:
        value = self._string(self._config.get("save_path"))
        if not value:
            return None
        return validate_download_save_path(value)

    def _timeout_seconds(self) -> int:
        value = self._int_value(self._config.get("timeout_seconds")) or 30
        return min(max(value, 5), 120)

    def _site_api_key(self, site: Dict[str, Any]) -> str:
        return self._string(site.get("apikey")) or self._string(site.get("api_key")) or ""

    def _site_user_agent(self, site: Dict[str, Any]) -> str:
        return self._string(site.get("ua")) or "MoviePilot-MTeamAdultSearch/1.0"

    def _site_referer(self, site: Dict[str, Any]) -> str:
        domain = self._string(site.get("domain")) or "https://m-team.cc/"
        return domain.rstrip("/") + "/browse"

    def _detail_url(self, site: Dict[str, Any], torrent_id: Any) -> Optional[str]:
        value = self._string(torrent_id)
        domain = self._string(site.get("domain"))
        if not value or not domain:
            return None
        return domain.rstrip("/") + f"/detail/{value}"

    @staticmethod
    def _discount_factor(value: Any) -> float:
        return {
            "FREE": 0.0,
            "PERCENT_50": 0.5,
            "PERCENT_70": 0.3,
            "_2X_FREE": 0.0,
            "_2X_PERCENT_50": 0.5,
        }.get(str(value or ""), 1.0)

    @staticmethod
    def _upload_factor(value: Any) -> float:
        return {
            "_2X": 2.0,
            "_2X_FREE": 2.0,
            "_2X_PERCENT_50": 2.0,
        }.get(str(value or ""), 1.0)

    @staticmethod
    def _string(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _int_value(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(float(value))
        except (TypeError, ValueError):
            return None
