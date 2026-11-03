"""MoviePilot V2 plugin: one-way folder sync to 123 cloud drive.

p123sync uploads whole local directory trees to 123云盘 without going through
MoviePilot's recognition/transfer chain, so unrecognized content (AV, misc
files) syncs as-is. The plugin ships a Vue federated AppPage that renders live
task status and per-file upload progress.

Key design:

* the sync engine lives in ``sync_engine.py`` and is driven by one background
  thread with a single-flight lock (manual runs and the cron service all go
  through :meth:`P123Sync._start_run`);
* run snapshots are kept in memory (:class:`RunState`) and served to the
  frontend via ``GET /api/v1/plugin/P123Sync/status`` every couple of seconds;
* run summaries are persisted through ``save_data`` (latest 20 runs).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body
from pydantic import BaseModel, Field

from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

from .p123_client import P123ClientError, P123SyncClient
from .sync_engine import (
    PHASE_CANCELLED,
    PHASE_ERROR,
    PHASE_FINISHED,
    POLICIES,
    RunState,
    SyncEngine,
    TaskPairConfig,
)

HISTORY_KEY = "history"
HISTORY_LIMIT = 20
LOG_PREFIX = "【123同步】"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "passport": "",
    "password": "",
    "cron": "",
    "notify": True,
    "tasks": [],
}


class RunRequest(BaseModel):
    """Body for POST /run."""

    task_ids: List[str] = Field(default_factory=list)


class SaveConfigRequest(BaseModel):
    """Body for POST /save_config."""

    config: Dict[str, Any]


class BrowseRequest(BaseModel):
    path: str = "/"
    passport: str = ""
    password: str = ""


class P123Sync(_PluginBase):
    """One-way 123 cloud drive folder sync with a visual AppPage."""

    plugin_name = "123云盘同步"
    plugin_desc = "本地目录单向同步到123云盘：免识别整树复制、同名策略可配、秒传/分片上传，侧栏页面实时查看任务与进度。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.3.0"
    plugin_author = "PrivateFilm"
    author_url = "https://github.com/shengleiRain"
    plugin_config_prefix = "p123sync_"
    plugin_order = 30
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._passport = ""
        self._password = ""
        self._cron = ""
        self._notify = True
        self._pairs: List[TaskPairConfig] = []
        self._client: Optional[P123SyncClient] = None
        self._state = RunState(trigger="manual")
        self._run_lock = threading.Lock()
        self._browse_lock = threading.Lock()
        self._browse_client: Optional[P123SyncClient] = None
        self._browse_credentials = None
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle

    def init_plugin(self, config: dict = None):
        """(Re)initialize from stored config; stops any in-flight run first."""
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled"))
            self._passport = str(config.get("passport") or "")
            self._password = str(config.get("password") or "")
            self._cron = str(config.get("cron") or "").strip()
            self._notify = bool(config.get("notify", True))
            raw_tasks = config.get("tasks") or []
            self._pairs = [
                TaskPairConfig.from_dict(task)
                for task in raw_tasks
                if isinstance(task, dict) and (task.get("source_path") or "").strip()
            ]
            self._client = None  # force lazy rebuild with new credentials
        logger.info(f"{LOG_PREFIX}插件初始化完成，启用={self._enabled}，任务对={len(self._pairs)}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """Plugin APIs consumed by the federated frontend (auth=bear)."""
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "当前/最近一次同步状态与逐文件进度",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "最近同步记录",
            },
            {
                "path": "/tasks",
                "endpoint": self.api_tasks,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "已配置的同步任务对",
            },
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即同步（可选指定任务对）",
            },
            {
                "path": "/cancel",
                "endpoint": self.api_cancel,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "取消当前同步",
            },
            {
                "path": "/get_config",
                "endpoint": self.api_get_config,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "读取插件配置",
            },
            {
                "path": "/save_config",
                "endpoint": self.api_save_config,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "保存插件配置",
            },
            {
                "path": "/browse_local",
                "endpoint": self.api_browse_local,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "浏览容器内本地目录",
            },
            {
                "path": "/browse_remote",
                "endpoint": self.api_browse_remote,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "浏览123云盘目录",
            },
            {
                "path": "/test_login",
                "endpoint": self.api_test_login,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "验证123云盘账号登录",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """Register the cron job with MoviePilot's scheduler when configured."""
        if self._enabled and self._cron and len(self._pairs) > 0:
            return [
                {
                    "id": "P123SyncSchedule",
                    "name": "123云盘定时同步",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self._scheduled_sync,
                    "kwargs": {},
                }
            ]
        return []

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """Vue federated frontend with a versioned asset directory."""
        return "vue", "dist/v1_3_0"

    def get_dashboard(self, key: str = "", user_agent: str = None) -> Tuple[dict, dict, None]:
        """Register the federated Dashboard with the MoviePilot host."""
        return {"cols": 12, "md": 6}, {"border": False}, None

    def get_page(self) -> Optional[List[dict]]:
        """Vue mode renders its own pages."""
        return None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Form UI is provided by the federated Config component."""
        return [], dict(DEFAULT_CONFIG)

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        return [
            {
                "plugin_id": "P123Sync",
                "nav_key": "main",
                "title": "123云盘同步",
                "icon": "mdi-cloud-sync-outline",
                "section": "organize",
                "order": 20,
            }
        ]

    def stop_service(self):
        """Cancel an in-flight run and release the client."""
        self._cancel_event.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=10)
        self._cancel_event = threading.Event()
        self._worker = None
        with self._browse_lock:
            browse_client = self._browse_client
            self._browse_client = None
            self._browse_credentials = None
            if browse_client is not None:
                browse_client.client.close()
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ api

    def api_status(self) -> Dict[str, Any]:
        return self._state.snapshot()

    def api_history(self) -> Dict[str, Any]:
        return {"list": self._load_history()}

    def api_tasks(self) -> Dict[str, Any]:
        return {
            "tasks": [
                {
                    "id": pair.id,
                    "name": pair.name,
                    "source_path": pair.source_path,
                    "target_path": pair.target_path,
                    "policy": pair.policy,
                    "exclude_ext": pair.exclude_ext,
                    "enabled": pair.enabled,
                }
                for pair in self._pairs
            ],
            "policies": list(POLICIES),
        }

    def api_run(self, request: RunRequest = Body(default=None)) -> Dict[str, Any]:
        pairs = [p for p in self._pairs if p.enabled]
        if request and request.task_ids:
            pairs = [p for p in pairs if p.id in request.task_ids]
        if not pairs:
            return {"success": False, "message": "没有可执行的同步任务对"}
        ok, message = self._start_run(pairs, trigger="manual")
        return {"success": ok, "message": message}

    def api_cancel(self) -> Dict[str, Any]:
        if not self._state.snapshot().get("running"):
            return {"success": False, "message": "当前没有运行中的同步"}
        self._cancel_event.set()
        return {"success": True, "message": "已请求取消"}

    def api_get_config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "passport": self._passport,
            "password": self._password,
            "cron": self._cron,
            "notify": self._notify,
            "tasks": [
                {
                    "id": pair.id,
                    "name": pair.name,
                    "source_path": pair.source_path,
                    "target_path": pair.target_path,
                    "policy": pair.policy,
                    "exclude_ext": pair.exclude_ext,
                    "enabled": pair.enabled,
                }
                for pair in self._pairs
            ],
        }

    def api_save_config(self, request: SaveConfigRequest) -> Dict[str, Any]:
        config = dict(DEFAULT_CONFIG)
        config.update(request.config or {})
        self.update_config(config)
        self.init_plugin(config)
        return {"success": True}

    def api_browse_local(self, request: BrowseRequest) -> Dict[str, Any]:
        try:
            path = Path(request.path or "/")
            if not path.is_absolute():
                raise ValueError("请输入绝对路径")
            path = path.resolve(strict=True)
            if not path.is_dir():
                raise ValueError("路径不是目录")
            folders = sorted((entry.name for entry in path.iterdir() if entry.is_dir()),
                             key=str.casefold)
            return {"success": True, "path": path.as_posix(), "folders": folders}
        except (OSError, ValueError, RuntimeError) as err:
            return {"success": False, "message": f"无法读取本地目录：{err}"}

    def api_browse_remote(self, request: BrowseRequest) -> Dict[str, Any]:
        if not request.passport or not request.password:
            return {"success": False, "message": "请先填写123云盘账号和密码"}
        segments = [segment for segment in request.path.split("/") if segment]
        if any(segment in (".", "..") for segment in segments):
            return {"success": False, "message": "云盘路径不能包含 . 或 .."}
        try:
            # Use a separate, serialized session so browsing never closes or
            # races the client used by an active synchronization.
            with self._browse_lock:
                credentials = (request.passport, request.password)
                if self._browse_credentials != credentials:
                    if self._browse_client is not None:
                        self._browse_client.client.close()
                    self._browse_client = P123SyncClient(*credentials)
                    self._browse_credentials = credentials
                client = self._browse_client
                current_id = 0
                for segment in segments:
                    folder = next((item for item in client.list_dir(current_id)
                                   if item.is_dir and item.name == segment), None)
                    if folder is None:
                        raise FileNotFoundError(f"云盘目录不存在：{segment}")
                    current_id = folder.file_id
                folders = sorted((item.name for item in client.list_dir(current_id)
                                  if item.is_dir), key=str.casefold)
            return {"success": True, "path": "/" + "/".join(segments), "folders": folders}
        except Exception as err:  # surface login, network, and listing errors
            return {"success": False, "message": f"无法读取云盘目录：{err}"}

    def api_test_login(self, request: BrowseRequest = Body(default=None)) -> Dict[str, Any]:
        # Verify the entered credentials without saving settings or interrupting a run.
        credentials = (request.passport, request.password) if request else (self._passport, self._password)
        if not all(credentials):
            return {"success": False, "message": "请先填写123云盘账号和密码"}
        try:
            with self._browse_lock:
                if self._browse_credentials != credentials:
                    if self._browse_client is not None:
                        self._browse_client.client.close()
                    self._browse_client = P123SyncClient(*credentials)
                    self._browse_credentials = credentials
                ok, message = self._browse_client.login_ok()
        except Exception as err:
            return {"success": False, "message": str(err)}
        return {"success": ok, "message": message}

    # --------------------------------------------------------------- runner

    def _get_client(self) -> P123SyncClient:
        if self._client is None:
            if not self._passport or not self._password:
                raise P123ClientError("未配置123云盘账号或密码")
            self._client = P123SyncClient(
                self._passport,
                self._password,
                logger=lambda msg: logger.info(f"{LOG_PREFIX}{msg}"),
            )
        return self._client

    def _start_run(self, pairs: List[TaskPairConfig], trigger: str) -> Tuple[bool, str]:
        """Single-flight launcher for manual runs (own daemon thread)."""
        with self._run_lock:
            if self._state.snapshot().get("running"):
                return False, "已有同步在运行中"
            self._cancel_event = threading.Event()
            self._state = RunState(trigger=trigger)
            state = self._state
            cancel_event = self._cancel_event
            self._worker = threading.Thread(
                target=self._sync_worker,
                args=(pairs, state, cancel_event),
                name="p123sync-worker",
                daemon=True,
            )
            self._worker.start()
            return True, f"同步已开始（{len(pairs)} 个任务对）"

    def _scheduled_sync(self):
        """Entry used by MoviePilot's scheduler (already a worker thread)."""
        pairs = [p for p in self._pairs if p.enabled]
        if not pairs:
            return
        with self._run_lock:
            if self._state.snapshot().get("running"):
                logger.info(f"{LOG_PREFIX}定时触发跳过：已有同步在运行")
                return
            self._cancel_event = threading.Event()
            self._state = RunState(trigger="schedule")
        self._sync_worker(pairs, self._state, self._cancel_event)

    def _sync_worker(self, pairs: List[TaskPairConfig], state: RunState,
                     cancel_event: threading.Event):
        try:
            client = self._get_client()
        except Exception as err:  # noqa: BLE001
            state.begin(pairs)
            state.finish(PHASE_ERROR, str(err))
            logger.error(f"{LOG_PREFIX}客户端初始化失败: {err}")
            return
        engine = SyncEngine(
            client=client,
            logger=lambda msg: logger.info(f"{LOG_PREFIX}{msg}"),
            cancel_event=cancel_event,
            state=state,
        )
        try:
            summary = engine.run(pairs)
        finally:
            self._persist_history(summary=state.summary())
        self._notify_result(state)

    def _notify_result(self, state: RunState):
        if not self._notify:
            return
        counters = state.snapshot()["counters"]
        phase = state.snapshot()["phase"]
        titles = {
            PHASE_FINISHED: "123云盘同步完成",
            PHASE_CANCELLED: "123云盘同步已取消",
            PHASE_ERROR: "123云盘同步失败",
        }
        title = titles.get(phase)
        if not title:
            return
        text = (
            f"上传 {counters.get('done', 0)}，秒传 {counters.get('instant', 0)}，"
            f"跳过 {counters.get('skipped', 0)}，失败 {counters.get('failed', 0)}"
        )
        if state.error:
            text += f"\n错误：{state.error}"
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception:
            pass

    # ------------------------------------------------------------- history

    def _load_history(self) -> List[Dict]:
        data = self.get_data(HISTORY_KEY) or []
        return data[-HISTORY_LIMIT:][::-1]

    def _persist_history(self, summary: Dict):
        try:
            history = self.get_data(HISTORY_KEY) or []
            history.append(summary)
            self.save_data(HISTORY_KEY, history[-HISTORY_LIMIT:])
        except Exception as err:
            logger.warn(f"{LOG_PREFIX}保存历史失败: {err}")
