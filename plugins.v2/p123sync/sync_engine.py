"""One-way folder sync engine for p123sync.

The engine is split into two layers:

* pure planning helpers (:func:`scan_source`, :func:`plan_sync`, ...) that
  never touch the network and are covered by unit tests;
* :class:`SyncEngine` which executes a plan against a duck-typed client and
  keeps a JSON-serializable :class:`RunState` for the plugin API.

File naming between layers uses POSIX-style relative paths
(``Season 1/xx.mkv``) so local and remote trees compare directly.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

if TYPE_CHECKING:  # keep the engine importable without p123client installed
    from .p123_client import P123SyncClient

# conflict policies
POLICY_SKIP_SAME_SIZE = "skip_same_size"
POLICY_OVERWRITE = "overwrite"
POLICY_KEEP_BOTH = "keep_both"
POLICIES = (POLICY_SKIP_SAME_SIZE, POLICY_OVERWRITE, POLICY_KEEP_BOTH)
TRIGGER_MANUAL = "manual"
TRIGGER_DIRECTORY = "directory"
TRIGGER_DOWNLOADER = "downloader"
TRIGGER_MODES = (TRIGGER_MANUAL, TRIGGER_DIRECTORY, TRIGGER_DOWNLOADER)

# run phases
PHASE_IDLE = "idle"
PHASE_SCANNING = "scanning"
PHASE_LISTING = "listing"
PHASE_UPLOADING = "uploading"
PHASE_FINISHED = "finished"
PHASE_CANCELLED = "cancelled"
PHASE_ERROR = "error"

RUNNING_PHASES = (PHASE_SCANNING, PHASE_LISTING, PHASE_UPLOADING)

# file states (counter keys match these names)
FILE_PENDING = "pending"
FILE_UPLOADING = "uploading"
FILE_DONE = "done"          # uploaded this run
FILE_INSTANT = "instant"    # instant upload (秒传) this run
FILE_SKIPPED = "skipped"    # already up to date
FILE_FAILED = "failed"
FILE_CANCELLED = "cancelled"
COUNTERED_STATES = (FILE_PENDING, FILE_UPLOADING, FILE_DONE, FILE_INSTANT,
                    FILE_SKIPPED, FILE_FAILED, FILE_CANCELLED)

TEMP_SUFFIXES = (".tmp", ".temp", ".part", ".!qb", ".downloading")
HIDDEN_PREFIXES = (".", "~$")
JUNK_NAMES = ("thumbs.db", "desktop.ini")


def _normalize_source_path(path: str) -> str:
    trimmed = path.rstrip("/")
    if path and not trimmed:
        return "/"
    if path.endswith("/") and len(trimmed) == 2 and trimmed[1] == ":":
        return trimmed + "/"
    return trimmed


@dataclass
class TaskPairConfig:
    """One configured source->target pair."""

    id: str
    name: str
    source_path: str
    target_path: str
    policy: str = POLICY_SKIP_SAME_SIZE
    exclude_ext: List[str] = field(default_factory=list)
    enabled: bool = True
    trigger_mode: str = TRIGGER_MANUAL
    settle_seconds: int = 120

    @classmethod
    def from_dict(cls, data: Dict) -> "TaskPairConfig":
        exclude_raw = data.get("exclude_ext") or []
        if isinstance(exclude_raw, str):
            exclude_raw = [part.strip() for part in exclude_raw.split(",") if part.strip()]
        exts = [part.lower() if part.startswith(".") else f".{part.lower()}" for part in exclude_raw]
        policy = data.get("policy") or POLICY_SKIP_SAME_SIZE
        if policy not in POLICIES:
            policy = POLICY_SKIP_SAME_SIZE
        trigger_mode = data.get("trigger_mode") or TRIGGER_MANUAL
        if trigger_mode not in TRIGGER_MODES:
            trigger_mode = TRIGGER_MANUAL
        try:
            settle_seconds = max(30, min(int(data.get("settle_seconds") or 120), 3600))
        except (TypeError, ValueError, OverflowError):
            settle_seconds = 120
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            name=str(data.get("name") or data.get("source_path") or ""),
            source_path=_normalize_source_path(str(data.get("source_path") or "")),
            target_path="/" + str(data.get("target_path") or "/").strip("/"),
            policy=policy,
            exclude_ext=exts,
            enabled=bool(data.get("enabled", True)),
            trigger_mode=trigger_mode,
            settle_seconds=settle_seconds,
        )


@dataclass
class LocalFile:
    rel_path: str
    abs_path: Path
    size: int
    md5: Optional[str] = None
    mtime_ns: Optional[int] = None


@dataclass
class SyncAction:
    kind: str                 # upload | skip | overwrite | keep_both
    local: LocalFile
    remote_fileid: Optional[int] = None
    remote_size: Optional[int] = None
    remote_md5: Optional[str] = None
    instant: bool = False
    error: Optional[str] = None


# --------------------------------------------------------------------- pure

def normalize_rel_path(rel: str) -> str:
    return str(PurePosixPath(rel))


def join_rel(rel_dir: str, name: str) -> str:
    if rel_dir in ("", "."):
        return name
    return f"{rel_dir}/{name}"


def should_exclude(name: str, exclude_ext: Iterable[str] = ()) -> bool:
    lowered = name.lower()
    if lowered.endswith(TEMP_SUFFIXES):
        return True
    if any(name.startswith(prefix) for prefix in HIDDEN_PREFIXES):
        return True
    if lowered in JUNK_NAMES:
        return True
    return any(lowered.endswith(ext.lower()) for ext in exclude_ext)


def scan_source(source_path: str, exclude_ext: Iterable[str] = (),
                cancel_check: Optional[Callable[[], bool]] = None) -> List[LocalFile]:
    """Walk the local source tree and return all syncable files (sorted)."""
    root = Path(source_path)
    if not root.is_dir():
        raise FileNotFoundError(f"源目录不存在或不是目录: {source_path}")
    files: List[LocalFile] = []
    def on_error(err):
        raise OSError(f"无法扫描源目录 {err.filename or root}: {err}") from err

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        if cancel_check and cancel_check():
            raise KeyboardInterrupt
        dirnames[:] = [d for d in dirnames if not should_exclude(d)]
        for filename in filenames:
            if should_exclude(filename, exclude_ext):
                continue
            abs_path = Path(dirpath) / filename
            try:
                stat = abs_path.stat()
            except OSError as err:
                raise OSError(f"无法读取源文件 {abs_path}: {err}") from err
            rel = normalize_rel_path(abs_path.relative_to(root).as_posix())
            files.append(LocalFile(rel_path=rel, abs_path=abs_path, size=stat.st_size, mtime_ns=stat.st_mtime_ns))
    files.sort(key=lambda f: f.rel_path)
    return files


def unique_keep_both_name(name: str, taken: Callable[[str], bool]) -> str:
    """Return ``name`` suffixed with `` (n)`` until it is not taken."""
    if not taken(name):
        return name
    stem = PurePosixPath(name).stem
    suffix = PurePosixPath(name).suffix
    index = 1
    while True:
        index += 1
        candidate = f"{stem} ({index}){suffix}"
        if not taken(candidate):
            return candidate


def plan_sync(
    local_files: List[LocalFile],
    remote_map: Dict[str, Dict],
    policy: str,
) -> List[SyncAction]:
    """Build the action list for one pair.

    ``remote_map`` maps relative posix path -> ``{"fileid": int, "size": int}``.
    """
    actions: List[SyncAction] = []
    for local in local_files:
        remote = remote_map.get(local.rel_path)
        if remote is None:
            actions.append(SyncAction(kind="upload", local=local))
            continue
        same_size = int(remote.get("size") or 0) == local.size
        remote_args = {
            "remote_fileid": int(remote["fileid"]),
            "remote_size": int(remote.get("size") or 0),
            "remote_md5": remote.get("md5"),
        }
        if policy != POLICY_OVERWRITE and same_size:
            remote_md5 = remote.get("md5")
            if not remote_md5 or not local.md5:
                actions.append(SyncAction(kind="unverified", local=local,
                                          error="远端未提供可用的文件校验值，无法确认内容是否一致", **remote_args))
            elif local.md5.lower() == remote_md5.lower():
                actions.append(SyncAction(kind="skip", local=local, **remote_args))
            elif policy == POLICY_KEEP_BOTH:
                actions.append(SyncAction(kind="keep_both", local=local, **remote_args))
            else:
                actions.append(SyncAction(kind="overwrite", local=local, **remote_args))
        elif policy == POLICY_OVERWRITE or (policy == POLICY_SKIP_SAME_SIZE and not same_size):
            actions.append(SyncAction(kind="overwrite", local=local, **remote_args))
        elif policy == POLICY_KEEP_BOTH and not same_size:
            actions.append(SyncAction(kind="keep_both", local=local, **remote_args))
        else:
            actions.append(SyncAction(kind="skip", local=local, **remote_args))
    return actions


# ------------------------------------------------------------------- state

class RunState:
    """Thread-safe, JSON-friendly snapshot of one sync run."""

    def __init__(self, run_id: Optional[str] = None, trigger: str = "manual", journal=None):
        self._lock = threading.Lock()
        self._journal = journal
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.trigger = trigger
        self.phase = PHASE_IDLE
        self.phase_detail = ""
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.counters = {state: 0 for state in COUNTERED_STATES}
        self.counters.update({"total_files": 0, "total_bytes": 0})
        self.bytes_done = 0
        self.bytes_skipped = 0
        self.bytes_inflight = 0
        self.bytes_transferred = 0
        self.speed = 0.0
        self.files: List[Dict] = []
        self._file_index: Dict[str, Dict] = {}
        self.pairs: Dict[str, Dict] = {}
        self._speed_window: List[Tuple[float, int]] = []
        self._retry_prior_error: Optional[str] = None
        self._interrupted_recoverable = False

    def _checkpoint(self, records=(), replace=False):
        """Called with the state lock held; failures stop the sync safely."""
        if self._journal:
            self._journal.checkpoint(self._snapshot_unlocked(False), records, replace)

    @classmethod
    def restore(cls, journal, recover: bool = True):
        snapshot = journal.load_latest()
        if not snapshot:
            return cls(journal=journal)
        state = cls(run_id=snapshot['run_id'], trigger=snapshot.get('trigger', 'manual'), journal=journal)
        for key in ('phase', 'phase_detail', 'started_at', 'finished_at', 'error',
                    'counters', 'bytes_done', 'bytes_skipped', 'bytes_inflight',
                    'bytes_transferred', 'pairs', '_retry_prior_error', '_interrupted_recoverable'):
            if key in snapshot:
                setattr(state, key, snapshot[key])
        state.files = snapshot.get('files', [])
        state._file_index = {f['key']: f for f in state.files}
        # A process can stop between a file transition and the subsequent pair
        # summary update. Rebuild aggregate counters from the durable records.
        state.counters = {name: 0 for name in COUNTERED_STATES}
        state.counters.update(total_files=len(state.files), total_bytes=0)
        state.bytes_done = state.bytes_skipped = state.bytes_inflight = 0
        for pair in state.pairs.values():
            pair.update(total=0, done=0, instant=0, skipped=0, failed=0)
        for record in state.files:
            size = record.get('size') or 0
            status = record.get('state')
            state.counters['total_bytes'] += size
            if status in state.counters:
                state.counters[status] += 1
            if status in (FILE_DONE, FILE_INSTANT):
                state.bytes_done += size
            elif status == FILE_SKIPPED:
                state.bytes_skipped += size
            elif status == FILE_UPLOADING:
                state.bytes_inflight += record.get('inflight_bytes', 0)
            pair = state.pairs.get(record.get('pair_id'))
            if pair:
                pair['total'] += 1
                if status in ('done', 'instant', 'skipped', 'failed'):
                    pair[status] += 1
        if recover and state.phase in RUNNING_PHASES:
            for pair in state.pairs.values():
                if (pair.get('phase') == PHASE_UPLOADING and
                        pair['total'] == pair['done'] + pair['instant'] + pair['skipped']):
                    pair.update(phase=PHASE_FINISHED, current='')
            if (state.pairs and all(p.get('phase') == PHASE_FINISHED for p in state.pairs.values()) and
                    not any(state.counters.get(k) for k in (FILE_PENDING, FILE_UPLOADING, FILE_FAILED, FILE_CANCELLED))):
                state.finish(PHASE_FINISHED)
            else:
                state._interrupted_recoverable = True
                state.finish(PHASE_ERROR, '上次同步被中断；可重试失败文件，重试前会重新核对云端')
        return state

    # -- engine-thread mutations ---------------------------------------------

    def begin(self, pairs: List[TaskPairConfig]):
        with self._lock:
            self.phase = PHASE_SCANNING
            self.started_at = time.time()
            self.finished_at = None
            self.error = None
            self._retry_prior_error = None
            self._interrupted_recoverable = False
            self.counters = {state: 0 for state in COUNTERED_STATES}
            self.counters.update({"total_files": 0, "total_bytes": 0})
            self.bytes_done = 0
            self.bytes_skipped = 0
            self.bytes_inflight = 0
            self.bytes_transferred = 0
            self.speed = 0.0
            self._speed_window = []
            self.files = []
            self._file_index = {}
            self.pairs = {
                pair.id: {
                    "id": pair.id, "name": pair.name,
                    "source": pair.source_path, "target": pair.target_path,
                    "policy": pair.policy, "exclude_ext": list(pair.exclude_ext),
                    "phase": PHASE_IDLE, "current": "",
                    "total": 0, "done": 0, "instant": 0, "skipped": 0, "failed": 0,
                }
                for pair in pairs
            }
            self._checkpoint(replace=True)

    def set_phase(self, phase: str, detail: str = ""):
        with self._lock:
            self.phase = phase
            self.phase_detail = detail
            self._checkpoint()

    def pair_update(self, pair_id: str, **kwargs):
        with self._lock:
            pair = self.pairs.get(pair_id)
            if pair:
                pair.update(kwargs)
                self._checkpoint()

    def add_files(self, records: List[Dict]):
        with self._lock:
            for record in records:
                record['_position'] = len(self.files)
                self.counters["total_files"] += 1
                self.counters["total_bytes"] += record.get("size") or 0
                self.counters[FILE_PENDING] += 1
                self.files.append(record)
                self._file_index[record["key"]] = record
            self._checkpoint(records)

    def begin_retry(self, keys: Iterable[str]):
        """Requeue only selected failures while retaining the other records."""
        with self._lock:
            self._retry_prior_error = self.error if self.phase in (PHASE_ERROR, PHASE_CANCELLED) else None
            self.run_id = uuid.uuid4().hex[:12]
            self.trigger = "retry"
            self.phase = PHASE_SCANNING
            self.phase_detail = "重试失败文件"
            self.started_at = time.time()
            self.finished_at = None
            self.error = None
            self.speed = 0.0
            self._speed_window = []
            for key in keys:
                record = self._file_index[key]
                self._transition(record, FILE_PENDING, 0.0, 0.0, None)
                record["error"] = None
                pair = self.pairs.get(record["pair_id"])
                if pair:
                    pair["failed"] = max(pair["failed"] - 1, 0)
            self._checkpoint(self.files, replace=True)

    def mark_interrupted(self):
        with self._lock:
            self._interrupted_recoverable = True

    def _transition(self, record: Dict, state: str, percent: Optional[float],
                    speed: Optional[float], error: Optional[str]):
        old = record.get("state")
        if old != state:
            if old == FILE_UPLOADING:
                self.bytes_inflight = max(self.bytes_inflight - record.get("inflight_bytes", 0), 0)
                record["inflight_bytes"] = 0
            if old in COUNTERED_STATES:
                self.counters[old] = max(self.counters.get(old, 0) - 1, 0)
            if state in COUNTERED_STATES:
                self.counters[state] = self.counters.get(state, 0) + 1
            record["state"] = state
        if percent is not None:
            record["percent"] = round(percent, 1)
        if speed is not None:
            record["speed"] = round(speed, 1)
        if error is not None:
            record["error"] = error

    def mark_file(self, rel_key: str, state: str, *, percent: Optional[float] = None,
                  speed: Optional[float] = None, error: Optional[str] = None,
                  bytes_done: Optional[int] = None, transfer_delta: int = 0,
                  clear_transfer: bool = False):
        with self._lock:
            record = self._file_index.get(rel_key)
            if record is None:
                return
            old_record = dict(record)
            old_counters = dict(self.counters)
            old_totals = (self.bytes_done, self.bytes_inflight, self.bytes_transferred, list(self._speed_window))
            try:
                self._transition(record, state, percent, speed, error)
                if state == FILE_UPLOADING and percent is not None:
                    current = min(record["size"], max(0, int(record["size"] * percent / 100)))
                    self.bytes_inflight += current - record.get("inflight_bytes", 0)
                    record["inflight_bytes"] = current
                if transfer_delta > 0:
                    self.bytes_transferred += transfer_delta
                    self._speed_window.append((time.time(), transfer_delta))
                if bytes_done:
                    self.bytes_done += bytes_done
                if clear_transfer:
                    record["transfer"] = None
                self._checkpoint([record])
            except Exception:
                record.clear()
                record.update(old_record)
                self.counters = old_counters
                self.bytes_done, self.bytes_inflight, self.bytes_transferred, self._speed_window = old_totals
                raise

    def patch_file(self, key: str, **values):
        with self._lock:
            record = self._file_index[key]
            old_record = dict(record)
            try:
                record.update(values)
                self._checkpoint([record])
            except Exception:
                record.clear()
                record.update(old_record)
                raise

    def file_record(self, key: str) -> Dict:
        with self._lock:
            return dict(self._file_index[key])

    def add_skipped_bytes(self, size: int):
        with self._lock:
            self.bytes_skipped += size
            self._checkpoint()

    def update_retry_file_size(self, key: str, size: int, mtime_ns: Optional[int] = None,
                               monitor_scope: Optional[str] = None):
        with self._lock:
            record = self._file_index[key]
            self.counters["total_bytes"] += size - record["size"]
            record["size"] = size
            record["source_mtime_ns"] = mtime_ns
            if monitor_scope is not None:
                record["monitor_scope"] = monitor_scope
            self._checkpoint([record])

    def tick_speed(self):
        """Recompute overall speed from a 10s sliding window."""
        now = time.time()
        with self._lock:
            window = [entry for entry in self._speed_window if now - entry[0] <= 10]
            self._speed_window = window[-100:]
            if not window:
                self.speed = 0.0
                return
            span = max(now - window[0][0], 1e-6)
            total = sum(size for _, size in window)
            self.speed = round(total / span, 1)

    def finish(self, phase: str, error: Optional[str] = None):
        with self._lock:
            if (phase == PHASE_FINISHED and self._interrupted_recoverable and
                    all(p.get('phase') == PHASE_FINISHED for p in self.pairs.values()) and
                    not any(self.counters.get(k) for k in (FILE_PENDING, FILE_UPLOADING, FILE_FAILED, FILE_CANCELLED))):
                self._retry_prior_error = None
                self._interrupted_recoverable = False
            if self._retry_prior_error:
                if phase == PHASE_FINISHED:
                    phase, error = PHASE_ERROR, self._retry_prior_error
                elif phase == PHASE_CANCELLED:
                    error = self._retry_prior_error
                elif phase == PHASE_ERROR and error and error != self._retry_prior_error:
                    error = f"原任务错误：{self._retry_prior_error}；重试错误：{error}"
            self.phase = phase
            self.error = error
            self.finished_at = time.time()
            for record in self.files:
                if record.get("state") in (FILE_PENDING, FILE_UPLOADING):
                    retry_cancelled = phase == PHASE_CANCELLED and self.trigger == "retry"
                    interrupted_transfer = phase == PHASE_CANCELLED and bool(record.get("transfer"))
                    target = FILE_FAILED if phase == PHASE_ERROR or retry_cancelled or interrupted_transfer else FILE_CANCELLED
                    reason = ("重试已取消" if retry_cancelled else
                              "已取消，重试时将核对云盘临时文件" if interrupted_transfer else
                              error if phase == PHASE_ERROR else None)
                    self._transition(record, target, None, 0.0, reason)
                    if target == FILE_FAILED and record.get("pair_id") in self.pairs:
                        self.pairs[record["pair_id"]]["failed"] += 1
            self._checkpoint(self.files)

    # -- reads ---------------------------------------------------------------

    def snapshot(self, include_files: bool = True) -> Dict:
        with self._lock:
            return self._snapshot_unlocked(include_files)

    def _snapshot_unlocked(self, include_files: bool) -> Dict:
        elapsed = (self.finished_at or time.time()) - (self.started_at or time.time())
        remaining = max(self.counters["total_bytes"] - self.bytes_done - self.bytes_skipped - self.bytes_inflight, 0)
        now = time.time()
        window = [(ts, size) for ts, size in self._speed_window if now - ts <= 10]
        speed = sum(size for _, size in window) / max(now - window[0][0], 1.0) if window else 0.0
        eta = remaining / speed if speed > 0 else None
        return {
                "run_id": self.run_id,
                "trigger": self.trigger,
                "phase": self.phase,
                "phase_detail": self.phase_detail,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": round(elapsed, 1),
                "counters": dict(self.counters),
                "bytes_done": self.bytes_done,
                "bytes_skipped": self.bytes_skipped,
                "bytes_inflight": self.bytes_inflight,
                "bytes_transferred": self.bytes_transferred,
                "speed": round(speed, 1),
                "eta": round(eta, 1) if eta else None,
                "error": self.error,
                "running": self.phase in RUNNING_PHASES,
                "_retry_prior_error": self._retry_prior_error,
                "_interrupted_recoverable": self._interrupted_recoverable,
                "pairs": {pid: dict(pair) for pid, pair in self.pairs.items()},
                "files": [dict(f) for f in self.files] if include_files else [],
        }

    def files_page(self, page: int = 1, page_size: int = 20, active_only: bool = False) -> Dict:
        with self._lock:
            page = max(1, page)
            page_size = max(1, min(page_size, 100))
            items = [f for f in self.files if not active_only or
                     f.get("state") in (FILE_UPLOADING, FILE_FAILED, FILE_CANCELLED)]
            total = len(items)
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            start = (page - 1) * page_size
            return {"run_id": self.run_id, "page": page, "page_size": page_size,
                    "total": total, "pages": pages,
                    "items": [dict(f) for f in items[start:start + page_size]]}

    def summary(self) -> Dict:
        snap = self.snapshot(include_files=False)
        snap["files"] = []
        snap["pairs"] = {
            pid: {k: pair[k] for k in ("name", "source", "target", "phase",
                                       "total", "done", "instant", "skipped", "failed")}
            for pid, pair in snap["pairs"].items()
        }
        return snap


# ------------------------------------------------------------------ engine

class SyncEngine:
    """Executes one sync run for a list of pairs against one client."""

    def __init__(
        self,
        client: "P123SyncClient",
        logger: Callable[[str], None],
        cancel_event: threading.Event,
        state: RunState,
        tick_interval: float = 1.0,
        monitor_scopes: Optional[Dict[str, str]] = None,
    ):
        self.client = client
        self.log = logger
        self.cancel_event = cancel_event
        self.state = state
        self._tick_interval = tick_interval
        self._dir_cache: Dict[str, int] = {}
        self._reserved_paths = set()
        self._monitor_scopes = monitor_scopes or {}

    # ---------------------------------------------------------------- public

    def run(self, pairs: List[TaskPairConfig], retry_paths: Optional[Dict[str, set]] = None,
            already_started: bool = False, selected_paths: Optional[Dict[str, Dict[str, Dict]]] = None) -> Dict:
        """Blocking run over all pairs. Returns the run summary."""
        if not already_started:
            self.state.begin(pairs)
        self.log(f"同步开始 run={self.state.run_id}，共 {len(pairs)} 个任务对")
        try:
            for pair in pairs:
                if self.cancel_event.is_set():
                    break
                self._run_pair(pair, None if retry_paths is None else retry_paths[pair.id],
                               None if selected_paths is None else selected_paths[pair.id])
            if self.cancel_event.is_set():
                self.state.finish(PHASE_CANCELLED)
                self.log("同步已取消")
            else:
                self.state.finish(PHASE_FINISHED)
                self.log("同步完成")
        except KeyboardInterrupt:
            self.state.finish(PHASE_CANCELLED)
            self.log("同步已取消")
        except Exception as err:  # noqa: BLE001 - guard for the run thread
            self.state.finish(PHASE_ERROR, str(err))
            self.log(f"同步失败: {err}")
        return self.state.summary()

    # --------------------------------------------------------------- helpers

    def _cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def _resolve_remote_dir(self, target_path: str, create: bool = True) -> int:
        """Resolve (and optionally create) a remote dir, starting from root 0."""
        normalized = "/" + target_path.strip("/")
        cache_key = normalized.rstrip("/") or "/"
        if cache_key in self._dir_cache:
            return self._dir_cache[cache_key]
        current_id = 0
        visited: List[str] = []
        for segment in [seg for seg in normalized.split("/") if seg]:
            visited.append(segment)
            partial = "/" + "/".join(visited)
            if partial in self._dir_cache:
                current_id = self._dir_cache[partial]
                continue
            matches = [item for item in self.client.list_dir(current_id, cancel_check=self._cancelled)
                       if item.is_dir and item.name == segment]
            if len(matches) > 1:
                raise RuntimeError(f"云盘存在多个同名目录，无法安全选择: {partial}")
            found = matches[0].file_id if matches else None
            if found is None:
                if not create:
                    raise FileNotFoundError(f"远端目录不存在: {partial}")
                found = self.client.mkdir(current_id, segment)
                self.log(f"创建远端目录: {partial}")
            self._dir_cache[partial] = found
            current_id = found
        self._dir_cache[cache_key] = current_id
        return current_id

    def _ensure_remote_dir(self, target_root: str, rel_dir: str) -> int:
        if rel_dir in ("", "."):
            return self._resolve_remote_dir(target_root)
        return self._resolve_remote_dir(f"{target_root.rstrip('/')}/{rel_dir}")

    def _build_remote_map(self, pair: TaskPairConfig) -> Dict[str, Dict]:
        root_id = self._resolve_remote_dir(pair.target_path, create=True)
        remote_map: Dict[str, Dict] = {}
        listed = 0
        for rel_path, item in self.client.walk(root_id, cancel_check=self._cancelled):
            listed += 1
            if listed % 200 == 0:
                self.state.pair_update(
                    pair.id, phase=PHASE_LISTING, current=f"已列举 {listed} 个远端文件")
                if self._cancelled():
                    raise KeyboardInterrupt
            normalized = normalize_rel_path(rel_path)
            if normalized in remote_map:
                raise RuntimeError(f"云盘存在多个同名文件，无法安全比对: {normalized}")
            remote_map[normalized] = {
                "fileid": item.file_id,
                "size": item.size,
                "md5": getattr(item, "md5", None),
            }
        return remote_map

    # ----------------------------------------------------------------- pair

    def _run_pair(self, pair: TaskPairConfig, retry_paths: Optional[set] = None,
                  selected_paths: Optional[Dict[str, Dict]] = None):
        self.state.set_phase(PHASE_SCANNING, pair.name)
        self.state.pair_update(pair.id, phase=PHASE_SCANNING, current=pair.source_path)
        local_files = scan_source(pair.source_path, pair.exclude_ext, self._cancelled)
        self._reserved_paths = {f.rel_path for f in local_files}
        previous = self.state.snapshot(include_files=False)["pairs"].get(pair.id, {})
        done = previous.get("done", 0) if retry_paths is not None else 0
        instant = previous.get("instant", 0) if retry_paths is not None else 0
        skipped = previous.get("skipped", 0) if retry_paths is not None else 0
        failed = previous.get("failed", 0) if retry_paths is not None else 0
        if retry_paths is not None:
            local_files = [f for f in local_files if f.rel_path in retry_paths]
            for local in local_files:
                self.state.update_retry_file_size(f"{pair.id}:{local.rel_path}", local.size, local.mtime_ns,
                                                  self._monitor_scopes.get(pair.id))
            missing = retry_paths - {f.rel_path for f in local_files}
            for rel_path in sorted(missing):
                key = f"{pair.id}:{rel_path}"
                record = self.state.file_record(key)
                reason = "源文件不存在、无法读取或已被排除"
                if record.get("transfer"):
                    try:
                        rel_dir = str(PurePosixPath(rel_path).parent)
                        remote_dir = pair.target_path if rel_dir in ("", ".") else f"{pair.target_path.rstrip('/')}/{rel_dir}"
                        parent_id = self._resolve_remote_dir(remote_dir, create=False)
                        self._recover_unusable_source(parent_id, key, record["transfer"])
                        reason += f"；未删除云盘文件（此前临时名称：{record['transfer'].get('stage')}）"
                    except Exception as err:
                        reason += f"；云盘状态尚未恢复：{err}"
                self.state.mark_file(f"{pair.id}:{rel_path}", FILE_FAILED,
                                     error=reason)
                failed += 1
            self.state.pair_update(pair.id, failed=failed)
            if not local_files:
                self.state.pair_update(pair.id, phase=PHASE_FINISHED, current="")
                return
        elif selected_paths is not None:
            matched = {f.rel_path: f for f in local_files if f.rel_path in selected_paths}
            invalid = {path: spec for path, spec in selected_paths.items()
                       if path not in matched or matched[path].size != spec['size'] or
                       matched[path].mtime_ns != spec['mtime_ns']}
            local_files = [item for path, item in matched.items() if path not in invalid]
            if invalid:
                records = [{"key": f"{pair.id}:{path}", "pair_id": pair.id, "pair_name": pair.name,
                            "path": path, "size": spec['size'], "source_mtime_ns": spec['mtime_ns'],
                            "monitor_scope": self._monitor_scopes.get(pair.id), "state": FILE_PENDING,
                            "percent": 0.0, "speed": 0.0, "error": None}
                           for path, spec in invalid.items()]
                self.state.add_files(records)
                for record in records:
                    self.state.mark_file(record['key'], FILE_FAILED, error="监控发现后源文件已变化或不可用，将重新观察")
                failed = len(records)
                self.state.pair_update(pair.id, failed=failed, total=len(selected_paths))
            if not local_files:
                self.state.pair_update(pair.id, phase=PHASE_FINISHED, current="")
                return
        self.log(f"[{pair.name}] 本地扫描完成: {len(local_files)} 个文件")

        self.state.set_phase(PHASE_LISTING, pair.name)
        self.state.pair_update(pair.id, phase=PHASE_LISTING, current=pair.target_path)
        remote_map = self._build_remote_map(pair)
        self.log(f"[{pair.name}] 远端列举完成: {len(remote_map)} 个文件")

        for local in local_files:
            remote = remote_map.get(local.rel_path)
            if (pair.policy != POLICY_OVERWRITE and remote and remote.get("size") == local.size
                    and remote.get("md5")):
                self.state.pair_update(pair.id, current=f"校验 {local.rel_path}")
                local.md5 = self._hash_source(local)

        actions = plan_sync(local_files, remote_map, pair.policy)
        if retry_paths is None:
            self.state.add_files([
                {
                    "key": f"{pair.id}:{action.local.rel_path}",
                    "pair_id": pair.id,
                    "pair_name": pair.name,
                    "path": action.local.rel_path,
                    "size": action.local.size,
                    "source_mtime_ns": action.local.mtime_ns,
                    "monitor_scope": self._monitor_scopes.get(pair.id),
                    "state": FILE_PENDING,
                    "percent": 0.0,
                    "speed": 0.0,
                    "error": None,
                }
                for action in actions
            ])
        self.state.pair_update(pair.id, phase=PHASE_UPLOADING,
                               total=(previous.get("total", 0) if retry_paths is not None else
                                      len(selected_paths) if selected_paths is not None else len(actions)),
                               current="")

        for action in actions:
            if self._cancelled():
                break
            key = f"{pair.id}:{action.local.rel_path}"
            self.state.pair_update(pair.id, current=action.local.rel_path)
            try:
                pending_transfer = self.state.file_record(key).get("transfer")
                if pending_transfer:
                    self._execute_action(pair, action, key, remote_map)
                    if action.instant:
                        instant += 1
                        self.state.pair_update(pair.id, instant=instant)
                    else:
                        done += 1
                        self.state.pair_update(pair.id, done=done)
                elif action.kind == "skip":
                    self._hash_source(action.local)
                    skipped += 1
                    self.state.mark_file(key, FILE_SKIPPED, percent=100.0)
                    self.state.add_skipped_bytes(action.local.size)
                    self.state.pair_update(pair.id, skipped=skipped)
                elif action.kind == "unverified":
                    raise RuntimeError(action.error)
                else:
                    self._execute_action(pair, action, key, remote_map)
                    if action.instant:
                        instant += 1
                        self.state.pair_update(pair.id, instant=instant)
                    else:
                        done += 1
                        self.state.pair_update(pair.id, done=done)
            except KeyboardInterrupt:
                raise
            except Exception as err:  # noqa: BLE001 - per-file isolation
                failed += 1
                self.state.mark_file(key, FILE_FAILED, error=str(err))
                self.state.pair_update(pair.id, failed=failed)
                self.log(f"[{pair.name}] 失败 {action.local.rel_path}: {err}")
        self.state.pair_update(pair.id, phase=PHASE_FINISHED, current="")

    def _hash_source(self, local: LocalFile) -> str:
        if self._cancelled():
            raise KeyboardInterrupt
        before = local.abs_path.stat()
        if before.st_size != local.size or (local.mtime_ns is not None and before.st_mtime_ns != local.mtime_ns):
            raise RuntimeError(f"源文件在扫描后发生变化: {local.rel_path}")
        digest = local.md5 or self.client._file_md5(local.abs_path, self._cancelled)
        after = local.abs_path.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise RuntimeError(f"源文件在校验期间发生变化: {local.rel_path}")
        local.md5 = digest
        return digest

    @staticmethod
    def _find_name(items, name):
        found = [item for item in items if not item.is_dir and item.name == name]
        if len(found) > 1:
            raise RuntimeError(f"云盘存在多个同名文件，无法安全处理: {name}")
        return found[0] if found else None

    @staticmethod
    def _matches(item, expected_md5, expected_size, file_id=None):
        return bool(item and item.size == expected_size and
                    getattr(item, "md5", None) == expected_md5 and
                    (file_id is None or item.file_id == file_id))

    def _recover_unusable_source(self, parent_id: int, key: str, transfer: Dict):
        """Preserve every remote copy if the local source has disappeared/changed."""
        if transfer.get("kind") != "overwrite":
            self.state.patch_file(key, transfer=None)
            return
        target, stage, backup = transfer['target'], transfer['stage'], transfer['backup']
        original_id = transfer['original_id']
        items = self.client.list_dir(parent_id, cancel_check=self._cancelled)
        target_item = self._find_name(items, target)
        backup_item = self._find_name(items, backup)
        stage_item = self._find_name(items, stage)
        if target_item and target_item.file_id == original_id:
            self.state.patch_file(key, transfer=None)
            return
        promoted = bool(target_item and target_item.file_id != original_id and
                        self._matches(target_item, transfer['md5'], transfer['size'], transfer.get('stage_id')))
        if backup_item and backup_item.file_id == original_id:
            if target_item:
                if not promoted or stage_item:
                    raise RuntimeError(f"目标目录存在冲突，请保留并检查 {target}、{stage} 和 {backup}")
                self.client.rename(target_item.file_id, stage)
                items = self.client.list_dir(parent_id, cancel_check=self._cancelled)
                if self._find_name(items, target) or not self._matches(
                        self._find_name(items, stage), transfer['md5'], transfer['size'], target_item.file_id):
                    raise RuntimeError(f"新文件状态未确认，请保留 {stage} 和 {backup}")
            self.client.rename(original_id, target)
            items = self.client.list_dir(parent_id, cancel_check=self._cancelled)
            restored = self._find_name(items, target)
            if not restored or restored.file_id != original_id:
                raise RuntimeError(f"旧文件恢复未确认，请保留 {stage} 和 {backup}")
            self.state.patch_file(key, transfer=None)
            return
        if promoted:
            # The verified new version is already present; an old copy may
            # already have been moved to the recycle bin before interruption.
            self.state.patch_file(key, transfer=None)
            return
        raise RuntimeError(f"旧文件状态无法确认，请保留 {target}、{stage} 和 {backup}")

    def _execute_action(self, pair: TaskPairConfig, action: SyncAction,
                        key: str, remote_map: Dict[str, Dict]):
        """Upload and reconcile by ID/hash; never discard the old copy first."""
        local = action.local
        rel_dir = str(PurePosixPath(local.rel_path).parent)
        parent_id = self._ensure_remote_dir(pair.target_path, rel_dir)
        digest = self._hash_source(local)
        transfer = self.state.file_record(key).get("transfer")
        if transfer:
            transfer = dict(transfer)
            if transfer.get("md5") != digest or transfer.get("size") != local.size:
                self._recover_unusable_source(parent_id, key, transfer)
                raise RuntimeError(f"源文件已变化，未删除云盘文件；可再次重试（此前临时名称：{transfer.get('stage')}）")
        else:
            target = local.abs_path.name
            if action.kind == "keep_both":
                target = unique_keep_both_name(
                    target, lambda name: join_rel(rel_dir, name) in remote_map or
                    join_rel(rel_dir, name) in self._reserved_paths)
            if action.kind == "overwrite" and not action.remote_md5:
                raise RuntimeError("远端原文件缺少可用校验值，无法安全覆盖")
            suffix = uuid.uuid4().hex
            transfer = {"kind": "overwrite" if action.kind == "overwrite" else "upload",
                        "target": target, "md5": digest, "size": local.size,
                        "original_id": action.remote_fileid if action.kind == "overwrite" else None,
                        "original_md5": action.remote_md5 if action.kind == "overwrite" else None,
                        "original_size": action.remote_size if action.kind == "overwrite" else None,
                        "stage": f"p123sync-stage-{suffix}.tmp" if action.kind == "overwrite" else target,
                        "backup": f"p123sync-backup-{suffix}.bak" if action.kind == "overwrite" else None,
                        "stage_id": None}
            self.state.patch_file(key, transfer=transfer)

        target, stage, backup = transfer["target"], transfer["stage"], transfer.get("backup")
        expected_id = transfer.get("stage_id")
        items = self.client.list_dir(parent_id, cancel_check=self._cancelled)
        target_item = self._find_name(items, target)
        stage_item = self._find_name(items, stage)
        backup_item = self._find_name(items, backup) if backup else None

        if transfer["kind"] == "overwrite":
            original_id = transfer.get("original_id")
            if not original_id:
                raise RuntimeError("缺少原文件标识，无法安全覆盖")
            promoted = bool(target_item and target_item.file_id != original_id and
                            self._matches(target_item, digest, local.size, expected_id))
            if promoted:
                stage_item = target_item
            elif target_item and target_item.file_id != original_id:
                raise RuntimeError("云盘目标文件已变化，未覆盖任何文件")
        else:
            original_id = None
            promoted = False

        if not promoted:
            if stage_item:
                if not self._matches(stage_item, digest, local.size, expected_id):
                    raise RuntimeError(f"云盘临时文件无法校验，已保留: {stage}")
                file_id = stage_item.file_id
                action.instant = False
            else:
                self.state.mark_file(key, FILE_UPLOADING)
                last_time = [time.time()]
                last_bytes = [0]

                def on_progress(percent: float):
                    now = time.time()
                    sent = min(local.size, int(local.size * percent / 100.0))
                    delta_bytes = max(sent - last_bytes[0], 0)
                    delta_time = max(now - last_time[0], 1e-6)
                    self.state.mark_file(key, FILE_UPLOADING, percent=percent,
                                         speed=delta_bytes / delta_time, transfer_delta=delta_bytes)
                    last_time[0] = now
                    last_bytes[0] = sent

                file_id, action.instant = self.client.upload(
                    local.abs_path, parent_id, stage, on_progress=on_progress,
                    cancel_check=self._cancelled, file_md5=digest)
                transfer['stage_id'] = file_id
                self.state.patch_file(key, transfer=transfer)
                stage_item = self._find_name(self.client.list_dir(parent_id, cancel_check=self._cancelled), stage)
                if not self._matches(stage_item, digest, local.size, file_id):
                    raise RuntimeError(f"上传结果尚未通过云盘校验，已保留临时文件: {stage}")
            transfer['stage_id'] = file_id
            self.state.patch_file(key, transfer=transfer)
            # Hash and size were captured before transfer; a changed local file
            # must never replace the previous remote version.
            self._hash_source(local)

        if transfer["kind"] == "overwrite":
            items = self.client.list_dir(parent_id, cancel_check=self._cancelled)
            target_item = self._find_name(items, target)
            backup_item = self._find_name(items, backup)
            stage_item = self._find_name(items, stage)
            promoted = bool(target_item and target_item.file_id != original_id and
                            self._matches(target_item, digest, local.size, transfer['stage_id']))
            if not promoted:
                if target_item and target_item.file_id != original_id:
                    raise RuntimeError("云盘目标文件已变化，已保留新旧文件")
                if target_item:
                    if not self._matches(target_item, transfer.get('original_md5'),
                                         transfer.get('original_size'), original_id):
                        raise RuntimeError("云盘原文件在同步期间已变化，未覆盖任何文件")
                    if backup_item:
                        raise RuntimeError(f"备份文件名冲突，未覆盖目标: {backup}")
                    if self._cancelled():
                        raise KeyboardInterrupt
                    try:
                        self.client.rename(original_id, backup)
                        items = self.client.list_dir(parent_id, cancel_check=self._cancelled)
                    except (Exception, KeyboardInterrupt) as err:
                        # If the rename happened but its response was lost,
                        # restore the original name only with duplicate=0.
                        try:
                            self.client.rename(original_id, target)
                        except Exception:
                            pass
                        if isinstance(err, KeyboardInterrupt):
                            raise
                        raise RuntimeError(f"旧文件状态未确认，临时文件为 {stage}，旧文件可能位于 {backup}") from err
                    backup_item = self._find_name(items, backup)
                    target_item = self._find_name(items, target)
                    stage_item = self._find_name(items, stage)
                if (not self._matches(backup_item, transfer.get('original_md5'),
                                      transfer.get('original_size'), original_id) or target_item):
                    raise RuntimeError(f"旧文件状态无法确认，已保留临时文件: {stage}")
                if not self._matches(stage_item, digest, local.size, transfer['stage_id']):
                    raise RuntimeError(f"新文件状态无法确认，旧文件保留为: {backup}")
                if self._cancelled():
                    try:
                        self.client.rename(original_id, target)
                    except Exception:
                        pass
                    raise KeyboardInterrupt
                try:
                    self.client.rename(transfer['stage_id'], target)
                except Exception:
                    # Duplicate=0 protects another writer even when the rename
                    # result is unknown. If rollback fails, preserve both names.
                    try:
                        self.client.rename(original_id, target)
                    except Exception:
                        pass
                    raise RuntimeError(f"替换未确认，新文件保留为 {stage}，旧文件可能位于 {backup}")
                items = self.client.list_dir(parent_id, cancel_check=self._cancelled)
                target_item = self._find_name(items, target)
                backup_item = self._find_name(items, backup)
                if not self._matches(target_item, digest, local.size, transfer['stage_id']):
                    raise RuntimeError(f"替换结果尚未通过校验，旧文件保留为: {backup}")
            if backup_item:
                if not self._matches(backup_item, transfer.get('original_md5'),
                                     transfer.get('original_size'), original_id):
                    raise RuntimeError(f"备份文件已变化或无法校验，未删除: {backup}")
                if self._cancelled():
                    raise KeyboardInterrupt
                self.client.trash(original_id)
                remaining = self._find_name(self.client.list_dir(parent_id, cancel_check=self._cancelled), backup)
                if remaining:
                    raise RuntimeError(f"旧文件回收操作未确认，备份仍位于: {backup}")

        self._hash_source(local)
        remote_map[join_rel(rel_dir, target)] = {
            "fileid": transfer['stage_id'], "size": local.size, "md5": digest,
        }
        self.state.mark_file(key, FILE_INSTANT if action.instant else FILE_DONE,
                             percent=100.0, bytes_done=local.size, clear_transfer=True)
