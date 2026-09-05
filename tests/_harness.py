"""Host-stub harness for loading the plugin outside a MoviePilot runtime.

The plugin imports host namespaces (``app.plugins``, ``app.sdk.*`` /
``app.core.*`` …) that only exist inside MoviePilot. The harness installs
minimal stub modules into ``sys.modules`` per generation and then imports the
plugin package under a unique module name, so endpoint logic can be tested
with fake HTTP, site, directory and download-chain collaborators.

The same stub classes are reused across generations and tests; per-test state
lives on the classes and is reset by :func:`reset`.
"""

from __future__ import annotations

import enum
import importlib.util
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]

API_BASE_URL = "https://api.m-team.cc/api"


class HTTPException(Exception):
    """FastAPI-compatible minimal exception for tests."""

    def __init__(self, status_code: int, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class MediaType(enum.Enum):
    MOVIE = "电影"


class NotificationType(enum.Enum):
    Plugin = "插件消息"


MessageType = NotificationType


class _FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class RequestUtils:
    """Records upstream calls and replays queued fake responses."""

    calls: list = []
    responses: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def post_res(self, url=None, json=None):
        cls = type(self)
        cls.calls.append({"url": url, "json": json})
        if cls.responses:
            return cls.responses.pop(0)
        return _FakeResponse(200, {"code": 0, "data": {"data": [], "total": 0}})


class SitesHelper:
    SITE = {
        "id": 1,
        "name": "M-Team",
        "domain": "https://m-team.cc/",
        "parser": "mTorrent",
        "apikey": "site-api-key",
        "ua": "SiteUA",
        "proxy": 0,
    }

    def get_indexers(self):
        return [dict(self.SITE), {"id": 2, "name": "Other", "parser": "Torznab"}]


class DownloadChain:
    calls: list = []
    next_download_id = "dl-hash-1"

    def download_single(self, context, **kwargs):
        cls = type(self)
        cls.calls.append({"context": context, **kwargs})
        return cls.next_download_id


DOWNLOAD_DIRS = [
    SimpleNamespace(
        download_path="/downloads/av",
        storage="local",
        name="AV 目录",
        media_type=None,
        media_category=None,
    )
]
ALLOWED_SAVE_PATHS = {"/downloads/av", "local:/downloads/av"}


def validate_download_save_path(save_path: str) -> str:
    if save_path in ALLOWED_SAVE_PATHS:
        return "/downloads/av"
    raise ValueError("保存路径不在允许的下载目录范围内")


class DirectoryHelper:
    def get_download_dirs(self):
        return list(DOWNLOAD_DIRS)

    def validate_download_save_path(self, save_path: str) -> str:
        return validate_download_save_path(save_path)


class TorrentInfo:
    def __init__(self):
        self.data = None

    def from_dict(self, data):
        self.data = data


class MediaInfo:
    def __init__(self):
        self.data = None

    def from_dict(self, data):
        self.data = data


class MetaInfo:
    def __init__(self, title=None, subtitle=None):
        self.title = title
        self.subtitle = subtitle


class Context:
    def __init__(self, meta_info=None, media_info=None, torrent_info=None):
        self.meta_info = meta_info
        self.media_info = media_info
        self.torrent_info = torrent_info


class _StubPluginBase:
    """In-memory stand-in for ``app.plugins._PluginBase``."""

    def __init__(self):
        self._data_store = {}
        self.messages = []

    def save_data(self, key, value, plugin_id=None):
        self._data_store[key] = value

    def get_data(self, key=None, plugin_id=None):
        return self._data_store.get(key)

    def post_message(
        self,
        channel=None,
        mtype=None,
        title=None,
        text=None,
        image=None,
        link=None,
        userid=None,
        **kwargs,
    ):
        self.messages.append(
            {
                "mtype": getattr(mtype, "value", mtype),
                "title": title,
                "text": text,
            }
        )


MODULE_MAPS = {
    "v2": {
        "app": {},
        "app.plugins": {"_PluginBase": _StubPluginBase},
        "app.chain": {},
        "app.chain.download": {"DownloadChain": DownloadChain},
        "app.core": {},
        "app.core.context": {
            "Context": Context,
            "MediaInfo": MediaInfo,
            "TorrentInfo": TorrentInfo,
        },
        "app.core.metainfo": {"MetaInfo": MetaInfo},
        "app.helper": {},
        "app.helper.directory": {
            "DirectoryHelper": DirectoryHelper,
            "validate_download_save_path": validate_download_save_path,
        },
        "app.helper.sites": {"SitesHelper": SitesHelper},
        "app.utils": {},
        "app.utils.http": {"RequestUtils": RequestUtils},
        "app.schemas": {},
        "app.schemas.types": {
            "MediaType": MediaType,
            "NotificationType": NotificationType,
        },
    },
    "v3": {
        "app": {},
        "app.plugins": {"_PluginBase": _StubPluginBase},
        "app.chain": {},
        "app.chain.download": {"DownloadChain": DownloadChain},
        "app.sdk": {},
        "app.sdk.media": {
            "Context": Context,
            "MediaInfo": MediaInfo,
            "MetaInfo": MetaInfo,
            "TorrentInfo": TorrentInfo,
        },
        "app.sdk.network": {"RequestUtils": RequestUtils, "SitesHelper": SitesHelper},
        "app.application": {},
        "app.application.directory": {
            "DirectoryHelper": DirectoryHelper,
            "validate_download_save_path": validate_download_save_path,
        },
        "app.schemas": {},
        "app.schemas.types": {"MediaType": MediaType, "MessageType": MessageType},
    },
}

_PLUGIN_CACHE: dict = {}


def _install(generation: str) -> None:
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.") or n == "fastapi"]:
        del sys.modules[name]
    fastapi = types.ModuleType("fastapi")
    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi
    for name, attrs in MODULE_MAPS[generation].items():
        module = types.ModuleType(name)
        for attr, value in attrs.items():
            setattr(module, attr, value)
        sys.modules[name] = module


def load_plugin(generation: str):
    """Import the plugin package for a generation with host stubs installed."""

    if generation not in _PLUGIN_CACHE:
        _install(generation)
        plugin_dir = REPO_ROOT / f"plugins.{generation}" / "mteamadultsearch"
        spec = importlib.util.spec_from_file_location(
            f"_hosted_mteamadultsearch_{generation}",
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _PLUGIN_CACHE[generation] = module
    else:
        _install(generation)
    return _PLUGIN_CACHE[generation]


def reset(module) -> None:
    """Clear cross-test state on stub classes and the plugin class."""

    RequestUtils.calls.clear()
    RequestUtils.responses.clear()
    DownloadChain.calls.clear()
    DownloadChain.next_download_id = "dl-hash-1"
    plugin_class = module.MTeamAdultSearch
    plugin_class._sessions.clear()
    plugin_class._search_cache.clear()
    plugin_class._last_upstream_at = 0.0
    # Keep the upstream throttle from sleeping between test calls.
    module.MIN_REQUEST_INTERVAL_SECONDS = 0.0


def mteam_body(rows, total=None):
    return {
        "code": 0,
        "data": {"data": rows, "total": len(rows) if total is None else total},
    }


def make_row(
    torrent_id,
    name=None,
    size=1024,
    seeders=1,
    discount="",
    created="2026-01-01T00:00:00",
):
    return {
        "id": str(torrent_id),
        "name": name or f"torrent-{torrent_id}",
        "smallDescr": "descr",
        "size": size,
        "createdDate": created,
        "status": {
            "seeders": seeders,
            "leechers": 1,
            "timesCompleted": 2,
            "discount": discount,
        },
        "labelsNew": ["label"],
    }


def make_plugin(module, config=None):
    instance = module.MTeamAdultSearch()
    instance.init_plugin({"enabled": True, **(config or {})})
    return instance


def expire_session(module, search_id, seconds=15 * 60 + 60):
    module.MTeamAdultSearch._sessions[search_id].created_at = time.time() - seconds
