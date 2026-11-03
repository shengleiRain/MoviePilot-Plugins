"""MoviePilot V2 plugin: one-way folder sync to 123 cloud drive.

p123sync uploads whole local directory trees to 123云盘 without going through
MoviePilot's recognition/transfer chain, so unrecognized content (AV, misc
files) syncs as-is. The plugin ships a Vue federated AppPage that renders live
task status and per-file upload progress.

Key design:

* the sync engine lives in ``sync_engine.py`` and is driven by one background
  thread with a single-flight lock (manual runs and the cron service all go
  through :meth:`P123Sync._start_run`);
* run snapshots and automatic-monitor observations are checkpointed in the
  plugin data directory and served through the plugin APIs;
* run summaries are persisted through ``save_data`` (latest 20 summaries).
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Body
from pydantic import BaseModel, Field

from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

from .p123_client import P123ClientError, P123SyncClient
from .sync_journal import SyncJournal
from .auto_monitor import discover_downloads, scan_directory, scope_for
from .sync_engine import (
    PHASE_CANCELLED,
    PHASE_ERROR,
    PHASE_FINISHED,
    POLICIES,
    TRIGGER_DIRECTORY,
    TRIGGER_DOWNLOADER,
    TRIGGER_MANUAL,
    TRIGGER_MODES,
    RunState,
    SyncEngine,
    TaskPairConfig,
)

HISTORY_KEY = "history"
HISTORY_LIMIT = 20
PENDING_CONFIG_KEY = "deferred_config_v1"
LOG_PREFIX = "【123同步】"
MONITOR_INTERVAL_SECONDS = 30
MONITOR_BATCH_SIZE = 200

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


class RetryRequest(BaseModel):
    run_id: str
    file_keys: List[str] = Field(default_factory=list)


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
    plugin_desc = "本地目录同步到123云盘：支持手动、目录监控及下载器监控，内容校验、安全覆盖与中断恢复。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.5.0"
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
        self._journal: Optional[SyncJournal] = None
        self._journal_error: Optional[str] = None
        self._external_run_active = False
        self._run_lock = threading.RLock()
        self._run_in_progress = False
        self._pending_config = None
        self._queued_config = None
        self._queue_loaded = False
        self._queue_error = None
        self._schedule_error = None
        self._stop_requested = False
        self._browse_lock = threading.Lock()
        self._browse_client: Optional[P123SyncClient] = None
        self._browse_credentials = None
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._monitor_enabled = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_lock = threading.Lock()
        self._monitor_status: Dict[str, Dict[str, Any]] = {}
        self._download_chain = None

    # ------------------------------------------------------------ lifecycle

    def init_plugin(self, config: dict = None):
        """Apply settings only after any active worker has stopped using them."""
        with self._run_lock:
            if self._run_in_progress:
                self._pending_config = dict(config or {})
                logger.info(f"{LOG_PREFIX}同步运行中，配置将在任务结束后生效")
                return
            self.stop_service()
            if self._journal_error and self._journal is not None:
                try:
                    self._journal.close()
                except Exception:
                    pass
                self._journal = None
            if self._journal is None:
                try:
                    self._journal = SyncJournal(self.get_data_path() / "sync_state.sqlite3")
                    if self._journal.acquire():
                        try:
                            self._state = RunState.restore(self._journal)
                        finally:
                            self._journal.release()
                        self._external_run_active = False
                    else:
                        self._state = RunState.restore(self._journal, recover=False)
                        self._external_run_active = True
                    self._journal_error = None
                except Exception as err:
                    self._journal = None
                    self._journal_error = str(err)
                    logger.error(f"{LOG_PREFIX}无法读取持久状态: {err}")
            if self._queue_error:
                self._queue_loaded = False
            if not self._queue_loaded:
                try:
                    self._queued_config = self.get_data(PENDING_CONFIG_KEY)
                    if self._queued_config is not None and not isinstance(self._queued_config, dict):
                        raise ValueError("待生效配置格式错误")
                    self._queue_loaded = True
                    self._queue_error = None
                except Exception as err:
                    self._queue_error = str(err)
                    logger.error(f"{LOG_PREFIX}无法读取待生效配置: {err}")
            if config is not None:
                try:
                    config = self._migrate_task_ids(config)
                except Exception as err:
                    self._journal_error = str(err)
                    logger.error(f"{LOG_PREFIX}无法安全保存任务标识: {type(err).__name__}")
                self._apply_config_values(config)
            # The host registers jobs after init_plugin returns.
            self._apply_queued_if_safe(refresh_schedule=False)
        logger.info(f"{LOG_PREFIX}插件初始化完成，启用={self._enabled}，任务对={len(self._pairs)}")

    def _migrate_task_ids(self, config: dict) -> dict:
        """Persist stable IDs; reuse an unambiguous journal identity on restart."""
        tasks = config.get('tasks') or []
        if not isinstance(tasks, list):
            raise ValueError('任务配置格式错误')
        result = dict(config)
        copied = [dict(t) if isinstance(t, dict) else t for t in tasks]
        used = {str(t['id']) for t in copied if isinstance(t, dict) and t.get('id')}
        id_count = sum(1 for t in copied if isinstance(t, dict) and t.get('id'))
        if len(used) != id_count:
            raise ValueError('任务标识重复，无法安全恢复同步')
        previous = self._state.snapshot(include_files=False).get('pairs', {})
        changed = False
        for task in copied:
            if not isinstance(task, dict) or task.get('id'):
                continue
            candidate = TaskPairConfig.from_dict(task)
            matches = [pid for pid, old in previous.items() if pid not in used and
                       old.get('source') == candidate.source_path and old.get('target') == candidate.target_path and
                       old.get('policy') == candidate.policy and old.get('exclude_ext') == candidate.exclude_ext]
            if len(matches) > 1:
                raise RuntimeError('无法唯一匹配待恢复任务，请保留原配置并检查同步记录')
            task['id'] = matches[0] if matches else candidate.id
            used.add(task['id'])
            changed = True
        if changed:
            result['tasks'] = copied
            if self.update_config(result) is False:
                raise RuntimeError('无法保存任务标识')
        return result

    def _apply_config_values(self, config: dict):
        self._enabled = bool(config.get("enabled"))
        self._passport = str(config.get("passport") or "")
        self._password = str(config.get("password") or "")
        self._cron = str(config.get("cron") or "").strip()
        self._notify = bool(config.get("notify", True))
        self._pairs = [TaskPairConfig.from_dict(task) for task in (config.get("tasks") or [])
                       if isinstance(task, dict) and (task.get("source_path") or "").strip()]
        self._client = None
        self._monitor_enabled = True
        self._ensure_monitor()

    def _apply_queued_if_safe(self, refresh_schedule: bool = True):
        """Do not invalidate recovery data or another worker's credentials."""
        if self._run_in_progress or self._journal_error:
            return False
        already_owned = bool(self._journal and self._journal.is_owned())
        if not already_owned and not self._claim_journal():
            return False
        try:
            if self._queue_loaded or self._queued_config:
                try:
                    self._queued_config = self.get_data(PENDING_CONFIG_KEY)
                except Exception as err:
                    self._queue_error = type(err).__name__
                    return False
            if not self._queued_config:
                return False
            snap = self._state.snapshot()
            if (snap['phase'] not in ('idle', PHASE_FINISHED) or snap.get('error') or
                    snap['counters'].get('failed') or snap['counters'].get('cancelled') or
                    any(f.get('transfer') for f in snap['files'])):
                return False
            try:
                if self.update_config(self._queued_config) is False:
                    raise RuntimeError("保存配置失败")
                self.stop_service()
                self._apply_config_values(self._queued_config)
                if refresh_schedule:
                    self._refresh_schedule()
                self.del_data(PENDING_CONFIG_KEY)
                self._queued_config = None
                self._queue_error = None
                return True
            except Exception as err:
                self._queue_error = type(err).__name__
                logger.error(f"{LOG_PREFIX}待生效配置保存失败: {type(err).__name__}")
                return False
        finally:
            if not already_owned:
                self._release_journal()

    def _refresh_schedule(self):
        try:
            from app.scheduler import Scheduler
            Scheduler().update_plugin_job(self.__class__.__name__)
            self._schedule_error = None
        except Exception as err:
            self._schedule_error = type(err).__name__
            logger.error(f"{LOG_PREFIX}刷新定时服务失败: {type(err).__name__}")

    def _claim_journal(self) -> bool:
        """Own the cross-instance run lock until the worker is finished."""
        if not self._journal:
            return True
        if not self._journal.acquire():
            self._external_run_active = True
            return False
        try:
            was_external = self._external_run_active
            latest = self._journal.load_latest()
            changed = bool(latest and latest['run_id'] != self._state.run_id)
            if latest and (was_external or changed):
                self._state = RunState.restore(self._journal)
            if was_external or changed:
                current_config = self.get_config()
                if current_config is None:
                    current_config = {}
                if isinstance(current_config, dict):
                    if self._client is not None:
                        self._client.client.close()
                    self._apply_config_values(current_config)
                else:
                    raise RuntimeError("无法读取当前插件配置")
            self._external_run_active = False
            return True
        except Exception as err:
            self._journal.release()
            self._journal_error = str(err)
            return False

    def _release_journal(self):
        if self._journal:
            self._journal.release()

    def _refresh_external_run(self):
        if not self._external_run_active or not self._journal or self._run_in_progress:
            return
        if self._claim_journal():
            try:
                self._apply_queued_if_safe()
            finally:
                self._release_journal()
        else:
            # Read-only while the other worker owns the lock; do not mark it
            # interrupted or alter the journal.
            try:
                self._state = RunState.restore(self._journal, recover=False)
            except Exception as err:
                self._journal_error = str(err)

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
                "path": "/files",
                "endpoint": self.api_files,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "分页读取当前同步文件",
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
                "path": "/retry",
                "endpoint": self.api_retry,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重试最近一次同步的失败文件",
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
                "path": "/discard_pending_config",
                "endpoint": self.api_discard_pending_config,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "取消待生效配置",
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
            {
                "path": "/space",
                "endpoint": self.api_space,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "读取123云盘已用和总空间",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """Register optional full sync and the monitoring health check."""
        services = []
        if self._enabled and self._cron and any(p.enabled and p.trigger_mode == TRIGGER_MANUAL for p in self._pairs):
            services.extend([
                {
                    "id": "P123SyncSchedule",
                    "name": "123云盘定时同步",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self._scheduled_sync,
                    "kwargs": {},
                }
            ])
        if self._enabled and any(p.enabled and p.trigger_mode != TRIGGER_MANUAL for p in self._pairs):
            services.append({"id":"P123SyncMonitorWatchdog", "name":"123云盘同步监控健康检查",
                             "trigger":IntervalTrigger(seconds=60), "func":self._ensure_monitor, "kwargs":{}})
        return services

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """Vue federated frontend with a versioned asset directory."""
        return "vue", "dist/v1_5_0"

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
        """Request cancellation; never close a client still in use."""
        with self._run_lock:
            self._monitor_enabled = False
            self._monitor_stop.set()
            self._monitor_thread = None
            self._stop_requested = True
            self._cancel_event.set()
            worker = self._worker
            running = self._run_in_progress
        with self._monitor_lock:
            self._monitor_status = {}
        if running and worker and worker is not threading.current_thread() and worker.is_alive():
            worker.join(timeout=10)
        with self._run_lock:
            if self._run_in_progress:
                logger.warn(f"{LOG_PREFIX}任务仍在停止，等待当前网络请求结束后释放客户端")
                return
            self._cancel_event = threading.Event()
            self._worker = None
            client = self._client
            self._client = None
            self._stop_requested = False
        with self._browse_lock:
            browse_client = self._browse_client
            self._browse_client = None
            self._browse_credentials = None
            if browse_client is not None:
                browse_client.client.close()
        if client is not None:
            try:
                client.client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ api

    def api_status(self, include_files: bool = True) -> Dict[str, Any]:
        with self._run_lock:
            self._refresh_external_run()
        snapshot = self._state.snapshot(include_files=include_files)
        snapshot["persistence_error"] = self._journal_error
        snapshot["pending_config"] = bool(self._queued_config or self._pending_config)
        snapshot["pending_config_error"] = self._queue_error
        snapshot["schedule_error"] = self._schedule_error
        snapshot["external_run_active"] = self._external_run_active
        snapshot["monitor_stopped"] = bool(self._monitor_enabled and self._enabled and
            any(p.enabled and p.trigger_mode != TRIGGER_MANUAL for p in self._pairs) and
            (not self._monitor_thread or not self._monitor_thread.is_alive()))
        with self._monitor_lock:
            snapshot["monitor"] = {key: dict(value) for key, value in self._monitor_status.items()}
        return snapshot

    def api_files(self, page: int = 1, page_size: int = 20,
                  active_only: bool = False, run_id: Optional[str] = None) -> Dict[str, Any]:
        if run_id and run_id != self._state.run_id:
            result = self._journal.history_files_page(run_id, page, page_size, active_only) if self._journal else None
            return result or {"success": False, "message": "历史文件明细不存在或来自旧版插件"}
        return self._state.files_page(page, page_size, active_only)

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
                    "trigger_mode": pair.trigger_mode,
                    "settle_seconds": pair.settle_seconds,
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

    def api_retry(self, request: RetryRequest) -> Dict[str, Any]:
        with self._run_lock:
            if self._run_in_progress:
                return {"success": False, "message": "已有同步在运行中"}
            if self._journal_error:
                return {"success": False, "message": f"无法保存同步状态：{self._journal_error}"}
            if not self._claim_journal():
                return {"success": False, "message": "上一个插件实例仍在同步或停止，请稍后重试"}
            started = False
            try:
                snap = self._state.snapshot()
                if request.run_id != snap["run_id"]:
                    return {"success": False, "message": "同步记录已变化，请刷新后重试"}
                failures = {f["key"]: f for f in snap["files"] if f.get("state") == "failed"}
                selected = set(request.file_keys) if request.file_keys else set(failures)
                if not selected or not selected.issubset(failures):
                    return {"success": False, "message": "没有可重试的失败文件，请刷新后重试"}
                pair_map = {pair.id: pair for pair in self._pairs if pair.enabled}
                retry_paths: Dict[str, set] = {}
                for key in selected:
                    record = failures[key]
                    pair = pair_map.get(record["pair_id"])
                    old = snap["pairs"].get(record["pair_id"], {})
                    if not pair or any((old.get("source") != pair.source_path,
                                        old.get("target") != pair.target_path,
                                        old.get("policy") != pair.policy,
                                        old.get("exclude_ext") != pair.exclude_ext)):
                        return {"success": False, "message": "任务配置已更改或停用，请恢复原配置后重试"}
                    retry_paths.setdefault(pair.id, set()).add(record["path"])
                pairs = [pair_map[pid] for pid in retry_paths]
                self._run_in_progress = True
                self._cancel_event = threading.Event()
                state = self._state
                try:
                    state.begin_retry(selected)
                except Exception as err:
                    self._run_in_progress = False
                    self._journal_error = str(err)
                    try:
                        if self._journal:
                            self._state = RunState.restore(self._journal)
                    except Exception:
                        pass
                    return {"success": False, "message": f"无法保存重试状态：{err}"}
                self._worker = threading.Thread(target=self._sync_worker,
                                                args=(pairs, state, self._cancel_event, retry_paths),
                                                name="p123sync-retry", daemon=True)
                try:
                    self._worker.start()
                except Exception as err:
                    self._run_in_progress = False
                    try:
                        state.finish(PHASE_ERROR, str(err))
                    except Exception as persist_err:
                        self._journal_error = str(persist_err)
                    return {"success": False, "message": f"无法启动重试：{err}"}
                started = True
                return {"success": True, "message": f"已开始重试 {len(selected)} 个失败文件"}
            finally:
                if not started:
                    self._release_journal()

    def api_cancel(self) -> Dict[str, Any]:
        if self._external_run_active and not self._run_in_progress:
            return {"success": False, "message": "旧实例正在停止，请等待当前网络请求结束"}
        snapshot = self._state.snapshot(include_files=False)
        if not snapshot.get("running"):
            return {"success": False, "message": "当前没有运行中的同步"}
        self._cancel_event.set()
        auto = any(p.trigger_mode != TRIGGER_MANUAL and p.id in snapshot['pairs'] for p in self._pairs)
        return {"success": True, "message": "已请求取消；监控任务的待处理文件将在退避后继续" if auto else "已请求取消"}

    def api_get_config(self) -> Dict[str, Any]:
        if self._queued_config:
            return {**self._queued_config, "pending_config": True}
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
                    "trigger_mode": pair.trigger_mode,
                    "settle_seconds": pair.settle_seconds,
                }
                for pair in self._pairs
            ],
        }

    def _normalize_config(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        config = dict(DEFAULT_CONFIG)
        config.update(raw or {})
        config['cron'] = str(config.get('cron') or '').strip()
        if config['cron']:
            try:
                CronTrigger.from_crontab(config['cron'])
            except Exception as err:
                raise ValueError('定时表达式无效，请填写 5 位 Cron 表达式') from err
        tasks = config.get("tasks") or []
        if not isinstance(tasks, list):
            raise ValueError("同步任务格式不正确")
        normalized = []
        used = set()
        for index, item in enumerate(tasks):
            if not isinstance(item, dict) or not str(item.get("source_path") or "").strip():
                raise ValueError(f"第 {index + 1} 个任务缺少本地源目录")
            if not Path(str(item['source_path']).strip()).is_absolute():
                raise ValueError(f"第 {index + 1} 个任务的本地源目录必须是绝对路径")
            if item.get('trigger_mode', TRIGGER_MANUAL) not in TRIGGER_MODES:
                raise ValueError(f"第 {index + 1} 个任务的同步方式无效")
            try:
                value = item.get('settle_seconds', 120)
                seconds = int(120 if value is None or value == '' else value)
            except (ValueError, TypeError, OverflowError) as err:
                raise ValueError(f"第 {index + 1} 个任务的文件稳定时间无效") from err
            if not 30 <= seconds <= 3600:
                raise ValueError(f"第 {index + 1} 个任务的文件稳定时间须为 30 到 3600 秒")
            pair = TaskPairConfig.from_dict(item)
            if pair.id in used:
                raise ValueError(f"第 {index + 1} 个任务的标识重复")
            used.add(pair.id)
            normalized.append(asdict(pair))
        config["tasks"] = normalized
        return config

    def _queue_config(self, config: Dict[str, Any]):
        self.save_data(PENDING_CONFIG_KEY, config)
        if self.get_data(PENDING_CONFIG_KEY) != config:
            raise RuntimeError("待生效配置持久化校验失败")
        self._queued_config = config
        self._queue_error = None

    def api_save_config(self, request: SaveConfigRequest) -> Dict[str, Any]:
        try:
            config = self._normalize_config(request.config or {})
        except (TypeError, ValueError) as err:
            return {"success": False, "message": str(err)}
        with self._run_lock:
            if self._queue_error:
                return {"success": False, "message": "无法读取待生效配置，请检查插件数据存储"}
            claimed = False
            if not self._run_in_progress:
                if self._journal_error:
                    return {"success": False, "message": "同步状态存储不可用，请修复后重新加载插件"}
                claimed = self._claim_journal()
                if not claimed and self._journal_error:
                    return {"success": False, "message": "同步状态存储不可用，请修复后重新加载插件"}
            try:
                if self._run_in_progress or self._external_run_active or self._queued_config:
                    try:
                        self._queue_config(config)
                    except Exception as err:
                        logger.error(f"{LOG_PREFIX}待生效配置保存失败: {type(err).__name__}")
                        return {"success": False, "message": "无法保存待生效配置，请检查插件数据存储"}
                    if claimed:
                        self._apply_queued_if_safe()
                    if self._queued_config:
                        return {"success": True, "deferred": True,
                                "message": "设置已保存为待生效；当前同步及未解决的失败处理完成后自动应用"}
                    return {"success": True, "schedule_error": bool(self._schedule_error),
                            "message": "设置已保存，但定时服务刷新失败，请重新加载插件" if self._schedule_error else "已保存"}
                pending = [f for f in self._state.snapshot().get("files", []) if f.get("transfer")]
                if pending:
                    new_pairs = {p.id: p for p in (TaskPairConfig.from_dict(item) for item in config.get("tasks", []))}
                    old_pairs = {p.id: p for p in self._pairs}
                    if (config.get("passport", "") != self._passport or config.get("password", "") != self._password or
                            any(new_pairs.get(f["pair_id"]) != old_pairs.get(f["pair_id"]) for f in pending)):
                        return {"success": False, "message": "存在待核对的云盘临时文件，请先重试相关失败文件，再修改账号或对应任务"}
                if self.update_config(config) is False:
                    return {"success": False, "message": "保存配置失败，请重试"}
                self.init_plugin(config)
                self._refresh_schedule()
                return {"success": True, "schedule_error": bool(self._schedule_error),
                        "message": "设置已保存，但定时服务刷新失败，请重新加载插件" if self._schedule_error else "已保存"}
            finally:
                if claimed:
                    self._release_journal()

    def api_discard_pending_config(self) -> Dict[str, Any]:
        with self._run_lock:
            claimed = False
            if not self._run_in_progress:
                claimed = self._claim_journal()
                if not claimed:
                    return {"success": False, "message": "旧实例仍在停止，请稍后再取消待生效设置"}
            try:
                self.del_data(PENDING_CONFIG_KEY)
                self._queued_config = None
                self._pending_config = None
                self._queue_error = None
                return {"success": True, "message": "已取消待生效设置"}
            except Exception:
                return {"success": False, "message": "取消待生效设置失败，请检查插件数据存储"}
            finally:
                if claimed:
                    self._release_journal()

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
        except FileNotFoundError as err:
            return {"success": False, "message": str(err)}
        except Exception:
            return {"success": False, "message": "无法读取云盘目录，请检查账号、网络或稍后重试"}

    def api_test_login(self, request: BrowseRequest = Body(default=None)) -> Dict[str, Any]:
        return self.api_space(request)

    def api_space(self, request: BrowseRequest = Body(default=None)) -> Dict[str, Any]:
        # Verify entered credentials without saving or interrupting a sync.
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
                info = self._browse_client.user_info()
        except Exception:
            return {"success": False, "message": "无法读取云盘信息，请检查账号、密码和网络"}
        return {"success": True, "message": "登录成功", "space": {"used": info['used'], "total": info['total']}}

    # -------------------------------------------------------------- monitor

    def _ensure_monitor(self):
        with self._run_lock:
            if (not self._monitor_enabled or not self._enabled or self._journal is None or self._journal_error or
                    not any(p.enabled and p.trigger_mode != TRIGGER_MANUAL for p in self._pairs)):
                return
            if self._monitor_thread and self._monitor_thread.is_alive() and not self._monitor_stop.is_set():
                return
            stop = threading.Event()
            self._monitor_stop = stop
            self._monitor_thread = threading.Thread(target=self._monitor_loop, args=(stop,),
                                                    name="p123sync-monitor", daemon=True)
            try:
                self._monitor_thread.start()
            except Exception as err:
                self._monitor_thread = None
                logger.error(f"{LOG_PREFIX}监控启动失败，将由健康检查重试: {type(err).__name__}")
                with self._monitor_lock:
                    self._monitor_status['_error'] = {'state':'error', 'message':'监控启动失败，健康检查将重试'}

    def _monitor_loop(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self._monitor_tick(stop)
            except KeyboardInterrupt:
                if stop.is_set():
                    return
            except Exception as err:
                logger.error(f"{LOG_PREFIX}监控检查失败: {type(err).__name__}")
                with self._monitor_lock:
                    self._monitor_status['_error'] = {'state':'error', 'message':'监控检查失败，将自动重试'}
            if stop.wait(MONITOR_INTERVAL_SECONDS):
                return

    def _get_download_monitor(self):
        try:
            from app.chain import ChainBase
            from app.schemas.types import TorrentQueryStatus
        except ImportError as err:
            raise RuntimeError('当前 MoviePilot 版本不支持下载器完成状态查询') from err
        if self._download_chain is None:
            self._download_chain = ChainBase()
        return self._download_chain, TorrentQueryStatus.ALL

    def _monitor_tick(self, stop: threading.Event, now: Optional[float] = None):
        with self._run_lock:
            if stop.is_set() or not self._enabled or self._journal_error or not self._journal:
                return
            if self._external_run_active:
                self._refresh_external_run()
                if self._external_run_active:
                    return
            pairs = [p for p in self._pairs if p.enabled and p.trigger_mode != TRIGGER_MANUAL]
            passport = self._passport
            journal = self._journal
        if not pairs:
            return
        def current_time():
            return time.time() if now is None else now
        observed: Dict[str, Dict[str, Dict]] = {}
        status: Dict[str, Dict[str, Any]] = {}
        downloader_pairs = []
        for pair in pairs:
            if stop.is_set():
                return
            scope = scope_for(pair, passport)
            if pair.trigger_mode == TRIGGER_DIRECTORY:
                try:
                    records = scan_directory(pair, stop.is_set)
                    if stop.is_set():
                        return
                    journal.observe_files(pair.id, scope, records, current_time(), complete=True)
                    observed[pair.id] = {entry['path']: entry for entry in records}
                    status[pair.id] = {'mode':pair.trigger_mode, 'state':'watching',
                                       'message':'周期检查目录，文件稳定后同步'}
                except KeyboardInterrupt:
                    return
                except Exception as err:
                    status[pair.id] = {'mode':pair.trigger_mode, 'state':'error',
                                       'message':f'目录检查失败：{err}'}
            elif pair.trigger_mode == TRIGGER_DOWNLOADER:
                try:
                    if not Path(pair.source_path).is_dir():
                        raise FileNotFoundError('源目录不可用或未映射到 MoviePilot')
                    downloader_pairs.append(pair)
                except Exception as err:
                    status[pair.id] = {'mode':pair.trigger_mode, 'state':'error', 'message':str(err)}
        if downloader_pairs and not stop.is_set():
            try:
                chain, query_status = self._get_download_monitor()
                results, warnings = discover_downloads(downloader_pairs, chain, query_status, stop.is_set)
                if stop.is_set():
                    return
                for pair in downloader_pairs:
                    scope = scope_for(pair, passport)
                    records = results.get(pair.id, [])
                    # Keep previously discovered work durable if a torrent is
                    # removed or a downloader becomes unavailable; it is only
                    # eligible while the current poll confirms completion.
                    journal.observe_files(pair.id, scope, records, current_time(), complete=False)
                    observed[pair.id] = {entry['path']: entry for entry in records}
                    status[pair.id] = {'mode':pair.trigger_mode, 'state':'warning' if warnings else 'watching',
                                       'message':('; '.join(warnings[:2]) if warnings else
                                                  '仅同步下载器确认完成的已选文件' if records else
                                                  '本轮未发现可确认的已完成文件，请检查下载器及路径映射')}
            except KeyboardInterrupt:
                return
            except Exception as err:
                for pair in downloader_pairs:
                    status[pair.id] = {'mode':pair.trigger_mode, 'state':'error',
                                       'message':'下载器检查失败，请检查连接、MoviePilot 版本及路径映射'}
                logger.error(f"{LOG_PREFIX}下载器检查失败: {type(err).__name__}")
        if stop.is_set():
            return
        ready: Dict[str, Dict[str, Dict]] = {}
        for pair in pairs:
            scope = scope_for(pair, passport)
            try:
                counts = journal.monitor_counts(pair.id, scope)
                status.setdefault(pair.id, {'mode':pair.trigger_mode, 'state':'error', 'message':'监控暂不可用'}).update(counts)
                active = observed.get(pair.id, {})
                candidates = journal.monitor_ready(pair.id, scope, current_time(), pair.settle_seconds, MONITOR_BATCH_SIZE)
                matching = {item['path']:item for item in candidates if active.get(item['path']) == item}
                if matching:
                    ready[pair.id] = matching
                elif pair.trigger_mode == TRIGGER_DOWNLOADER and counts['pending'] and pair.id in observed:
                    status[pair.id]['message'] = '待处理文件需等待下载器再次确认完成且稳定'
            except Exception as err:
                status[pair.id] = {'mode':pair.trigger_mode, 'state':'error', 'message':f'监控队列不可用：{err}'}
        with self._monitor_lock:
            self._monitor_status = status
        if stop.is_set() or not ready:
            return
        with self._run_lock:
            if stop.is_set() or self._run_in_progress or self._external_run_active or self._journal_error:
                return
            snapshot = self._state.snapshot()
            # Do not replace unresolved failures with a new automatic run.
            failures = [record for record in snapshot['files'] if record.get('state') == 'failed']
            if failures:
                keys = [record['key'] for record in failures
                        if record.get('pair_id') in ready and record.get('path') in ready[record['pair_id']]]
                if keys:
                    self.api_retry(RetryRequest(run_id=snapshot['run_id'], file_keys=keys))
                else:
                    with self._monitor_lock:
                        for pair_id in ready:
                            status[pair_id]['message'] = '存在未解决的失败文件，自动任务已等待'
                return
            chosen = [pair for pair in pairs if pair.id in ready]
            if chosen:
                self._start_run(chosen, trigger='monitor', selected_paths=ready)

    def _mark_monitor_failures(self, state: RunState, pairs: List[TaskPairConfig],
                               selected_paths: Optional[Dict[str, Dict[str, Dict]]],
                               retry_paths: Optional[Dict[str, set]]):
        if not self._journal:
            return
        try:
            snapshot = state.snapshot()
            by_key = {record['key']:record for record in snapshot['files']}
            now = time.time()
            for pair in pairs:
                if pair.trigger_mode == TRIGGER_MANUAL:
                    continue
                scope = scope_for(pair, self._passport)
                if selected_paths is not None:
                    selected = selected_paths.get(pair.id, {})
                elif retry_paths is not None:
                    selected = {path:by_key.get(f"{pair.id}:{path}", {})
                                for path in retry_paths.get(pair.id, set())}
                else:
                    selected = {record['path']:record for record in snapshot['files']
                                if record.get('pair_id') == pair.id}
                for path, spec in selected.items():
                    record = by_key.get(f"{pair.id}:{path}")
                    if record and record.get('state') not in ('failed', 'cancelled'):
                        continue
                    size = spec.get('size')
                    mtime = spec.get('mtime_ns', spec.get('source_mtime_ns'))
                    if size is not None and mtime is not None:
                        journal_error = (record or {}).get('error') or snapshot.get('error') or '同步未完成'
                        self._journal.monitor_fail(pair.id, scope, path, size, mtime, journal_error, now)
        except Exception as err:
            logger.error(f"{LOG_PREFIX}记录监控重试时间失败: {type(err).__name__}")

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

    def _start_run(self, pairs: List[TaskPairConfig], trigger: str,
                   selected_paths: Optional[Dict[str, Dict[str, Dict]]] = None) -> Tuple[bool, str]:
        """Single-flight launcher for manual runs (own daemon thread)."""
        with self._run_lock:
            if self._run_in_progress:
                return False, "已有同步在运行中"
            if self._journal_error:
                return False, f"无法保存同步状态：{self._journal_error}"
            if not self._claim_journal():
                return False, "上一个插件实例仍在同步或停止，请稍后重试"
            current = {pair.id: pair for pair in self._pairs if pair.enabled}
            if any(current.get(pair.id) != pair for pair in pairs):
                self._release_journal()
                return False, "任务配置已更新，请刷新页面后重试"
            if any(f.get("transfer") for f in self._state.snapshot().get("files", [])):
                self._release_journal()
                return False, "存在待核对的云盘临时文件，请先重试相关失败文件"
            self._run_in_progress = True
            self._cancel_event = threading.Event()
            previous_state = self._state
            self._state = RunState(trigger=trigger, journal=self._journal)
            state = self._state
            cancel_event = self._cancel_event
            try:
                state.begin(pairs)
            except Exception as err:
                self._run_in_progress = False
                self._state = previous_state
                self._journal_error = str(err)
                self._release_journal()
                return False, f"无法保存同步初始状态：{err}"
            try:
                self._worker = threading.Thread(
                    target=self._sync_worker,
                    args=(pairs, state, cancel_event, None, selected_paths),
                    name="p123sync-worker",
                    daemon=True,
                )
                self._worker.start()
            except Exception as err:
                self._run_in_progress = False
                try:
                    state.finish(PHASE_ERROR, str(err))
                except Exception as persist_err:
                    self._journal_error = str(persist_err)
                self._release_journal()
                return False, f"无法启动同步：{err}"
            return True, f"同步已开始（{len(pairs)} 个任务对）"

    def _scheduled_sync(self):
        """Entry used by MoviePilot's scheduler (already a worker thread)."""
        pairs = [p for p in self._pairs if p.enabled and p.trigger_mode == TRIGGER_MANUAL]
        if not pairs:
            return
        with self._run_lock:
            if self._run_in_progress:
                logger.info(f"{LOG_PREFIX}定时触发跳过：已有同步在运行")
                return
            if self._journal_error:
                logger.error(f"{LOG_PREFIX}定时同步无法保存状态：{self._journal_error}")
                return
            if not self._claim_journal():
                logger.info(f"{LOG_PREFIX}其他插件实例正在同步，跳过定时触发")
                return
            pairs = [p for p in self._pairs if p.enabled and p.trigger_mode == TRIGGER_MANUAL]
            if not pairs:
                self._release_journal()
                return
            if any(f.get("transfer") for f in self._state.snapshot().get("files", [])):
                self._release_journal()
                logger.warn(f"{LOG_PREFIX}存在待核对的云盘临时文件，跳过定时同步")
                return
            self._run_in_progress = True
            self._cancel_event = threading.Event()
            self._state = RunState(trigger="schedule", journal=self._journal)
            state = self._state
            cancel_event = self._cancel_event
            self._worker = threading.current_thread()
            try:
                state.begin(pairs)
            except Exception as err:
                self._run_in_progress = False
                self._journal_error = str(err)
                logger.error(f"{LOG_PREFIX}定时同步无法保存初始状态：{err}")
                self._release_journal()
                return
        self._sync_worker(pairs, state, cancel_event)

    def _sync_worker(self, pairs: List[TaskPairConfig], state: RunState,
                     cancel_event: threading.Event, retry_paths: Optional[Dict[str, set]] = None,
                     selected_paths: Optional[Dict[str, Dict[str, Dict]]] = None):
        try:
            client = self._get_client()
            engine = SyncEngine(
                client=client,
                logger=lambda msg: logger.info(f"{LOG_PREFIX}{msg}"),
                cancel_event=cancel_event,
                state=state,
                monitor_scopes={p.id: scope_for(p, self._passport) for p in pairs
                                if p.trigger_mode != TRIGGER_MANUAL},
            )
            engine.run(pairs, retry_paths=retry_paths, already_started=True, selected_paths=selected_paths)
        except Exception as err:  # noqa: BLE001
            try:
                state.finish(PHASE_ERROR, str(err))
            except Exception as persist_err:
                self._journal_error = str(persist_err)
            logger.error(f"{LOG_PREFIX}同步启动失败: {err}")
        finally:
            try:
                if state.snapshot(include_files=False).get("running"):
                    try:
                        state.mark_interrupted()
                        state.finish(PHASE_ERROR, "同步线程意外中断，请重试失败文件或重新运行未完成任务")
                    except Exception as persist_err:
                        self._journal_error = str(persist_err)
                self._mark_monitor_failures(state, pairs, selected_paths, retry_paths)
                self._persist_history(summary=state.summary())
            finally:
                with self._run_lock:
                    self._run_in_progress = False
                    pending_config = self._pending_config
                    self._pending_config = None
                    stop_requested = self._stop_requested
                    try:
                        if pending_config is not None:
                            try:
                                self._queue_config(self._normalize_config(pending_config))
                            except Exception as err:
                                self._queue_error = type(err).__name__
                                self._pending_config = pending_config
                                logger.error(f"{LOG_PREFIX}待生效配置保存失败: {type(err).__name__}")
                        self._apply_queued_if_safe()
                        if stop_requested:
                            self.stop_service()
                    finally:
                        self._release_journal()
        self._notify_result(state)

    def _notify_result(self, state: RunState):
        if not self._notify:
            return
        snapshot = state.snapshot(include_files=False)
        counters = snapshot["counters"]
        phase = snapshot["phase"]
        titles = {
            PHASE_FINISHED: "123云盘同步完成",
            PHASE_CANCELLED: "123云盘同步已取消",
            PHASE_ERROR: "123云盘同步失败",
        }
        title = titles.get(phase)
        if phase == PHASE_FINISHED and counters.get("failed", 0):
            title = "123云盘同步完成（有失败）"
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
