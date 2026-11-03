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
MAX_TRACKED_FILES = 5000


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

    @classmethod
    def from_dict(cls, data: Dict) -> "TaskPairConfig":
        exclude_raw = data.get("exclude_ext") or []
        if isinstance(exclude_raw, str):
            exclude_raw = [part.strip() for part in exclude_raw.split(",") if part.strip()]
        exts = [part.lower() if part.startswith(".") else f".{part.lower()}" for part in exclude_raw]
        policy = data.get("policy") or POLICY_SKIP_SAME_SIZE
        if policy not in POLICIES:
            policy = POLICY_SKIP_SAME_SIZE
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            name=str(data.get("name") or data.get("source_path") or ""),
            source_path=_normalize_source_path(str(data.get("source_path") or "")),
            target_path="/" + str(data.get("target_path") or "/").strip("/"),
            policy=policy,
            exclude_ext=exts,
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class LocalFile:
    rel_path: str
    abs_path: Path
    size: int


@dataclass
class SyncAction:
    kind: str                 # upload | skip | overwrite | keep_both
    local: LocalFile
    remote_fileid: Optional[int] = None
    remote_size: Optional[int] = None
    instant: bool = False


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


def scan_source(source_path: str, exclude_ext: Iterable[str] = ()) -> List[LocalFile]:
    """Walk the local source tree and return all syncable files (sorted)."""
    root = Path(source_path)
    if not root.is_dir():
        raise FileNotFoundError(f"源目录不存在或不是目录: {source_path}")
    files: List[LocalFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_exclude(d)]
        for filename in filenames:
            if should_exclude(filename, exclude_ext):
                continue
            abs_path = Path(dirpath) / filename
            try:
                size = abs_path.stat().st_size
            except OSError:
                continue
            rel = normalize_rel_path(abs_path.relative_to(root).as_posix())
            files.append(LocalFile(rel_path=rel, abs_path=abs_path, size=size))
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
        }
        if policy == POLICY_OVERWRITE or (policy == POLICY_SKIP_SAME_SIZE and not same_size):
            actions.append(SyncAction(kind="overwrite", local=local, **remote_args))
        elif policy == POLICY_KEEP_BOTH and not same_size:
            actions.append(SyncAction(kind="keep_both", local=local, **remote_args))
        else:
            actions.append(SyncAction(kind="skip", local=local, **remote_args))
    return actions


# ------------------------------------------------------------------- state

class RunState:
    """Thread-safe, JSON-friendly snapshot of one sync run."""

    def __init__(self, run_id: Optional[str] = None, trigger: str = "manual"):
        self._lock = threading.Lock()
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
        self.speed = 0.0
        self.files: List[Dict] = []
        self.pairs: Dict[str, Dict] = {}
        self._speed_window: List[Tuple[float, int]] = []

    # -- engine-thread mutations ---------------------------------------------

    def begin(self, pairs: List[TaskPairConfig]):
        with self._lock:
            self.phase = PHASE_SCANNING
            self.started_at = time.time()
            self.finished_at = None
            self.error = None
            self.counters = {state: 0 for state in COUNTERED_STATES}
            self.counters.update({"total_files": 0, "total_bytes": 0})
            self.bytes_done = 0
            self.bytes_skipped = 0
            self.speed = 0.0
            self._speed_window = []
            self.files = []
            self.pairs = {
                pair.id: {
                    "id": pair.id, "name": pair.name,
                    "source": pair.source_path, "target": pair.target_path,
                    "phase": PHASE_IDLE, "current": "",
                    "total": 0, "done": 0, "instant": 0, "skipped": 0, "failed": 0,
                }
                for pair in pairs
            }

    def set_phase(self, phase: str, detail: str = ""):
        with self._lock:
            self.phase = phase
            self.phase_detail = detail

    def pair_update(self, pair_id: str, **kwargs):
        with self._lock:
            pair = self.pairs.get(pair_id)
            if pair:
                pair.update(kwargs)

    def add_files(self, records: List[Dict]):
        with self._lock:
            for record in records:
                self.counters["total_files"] += 1
                self.counters["total_bytes"] += record.get("size") or 0
                self.counters[FILE_PENDING] += 1
                if len(self.files) < MAX_TRACKED_FILES:
                    self.files.append(record)

    def _transition(self, record: Dict, state: str, percent: Optional[float],
                    speed: Optional[float], error: Optional[str]):
        old = record.get("state")
        if old != state:
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
                  bytes_done: Optional[int] = None):
        with self._lock:
            record = next((f for f in reversed(self.files) if f["key"] == rel_key), None)
            if record is None:
                return
            self._transition(record, state, percent, speed, error)
            if bytes_done:
                self.bytes_done += bytes_done
                self._speed_window.append((time.time(), bytes_done))

    def add_skipped_bytes(self, size: int):
        with self._lock:
            self.bytes_skipped += size

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
            self.phase = phase
            self.error = error
            self.finished_at = time.time()
            for record in self.files:
                if record.get("state") in (FILE_PENDING, FILE_UPLOADING):
                    self._transition(record, FILE_CANCELLED, None, None, None)

    # -- reads ---------------------------------------------------------------

    def snapshot(self) -> Dict:
        with self._lock:
            elapsed = (self.finished_at or time.time()) - (self.started_at or time.time())
            remaining = max(
                self.counters["total_bytes"] - self.bytes_done - self.bytes_skipped, 0)
            eta = remaining / self.speed if self.speed > 0 else None
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
                "speed": self.speed,
                "eta": round(eta, 1) if eta else None,
                "error": self.error,
                "running": self.phase in RUNNING_PHASES,
                "pairs": {pid: dict(pair) for pid, pair in self.pairs.items()},
                "files": [dict(f) for f in self.files],
            }

    def summary(self) -> Dict:
        snap = self.snapshot()
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
    ):
        self.client = client
        self.log = logger
        self.cancel_event = cancel_event
        self.state = state
        self._tick_interval = tick_interval
        self._dir_cache: Dict[str, int] = {}

    # ---------------------------------------------------------------- public

    def run(self, pairs: List[TaskPairConfig]) -> Dict:
        """Blocking run over all pairs. Returns the run summary."""
        self.state.begin(pairs)
        self.log(f"同步开始 run={self.state.run_id}，共 {len(pairs)} 个任务对")
        try:
            for pair in pairs:
                if self.cancel_event.is_set():
                    break
                self._run_pair(pair)
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
            found = None
            for item in self.client.list_dir(current_id):
                if item.is_dir and item.name == segment:
                    found = item.file_id
                    break
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
        for rel_path, item in self.client.walk(root_id):
            listed += 1
            if listed % 200 == 0:
                self.state.pair_update(
                    pair.id, phase=PHASE_LISTING, current=f"已列举 {listed} 个远端文件")
                if self._cancelled():
                    raise KeyboardInterrupt
            remote_map[normalize_rel_path(rel_path)] = {
                "fileid": item.file_id,
                "size": item.size,
            }
        return remote_map

    # ----------------------------------------------------------------- pair

    def _run_pair(self, pair: TaskPairConfig):
        self.state.set_phase(PHASE_SCANNING, pair.name)
        self.state.pair_update(pair.id, phase=PHASE_SCANNING, current=pair.source_path)
        local_files = scan_source(pair.source_path, pair.exclude_ext)
        self.log(f"[{pair.name}] 本地扫描完成: {len(local_files)} 个文件")

        self.state.set_phase(PHASE_LISTING, pair.name)
        self.state.pair_update(pair.id, phase=PHASE_LISTING, current=pair.target_path)
        remote_map = self._build_remote_map(pair)
        self.log(f"[{pair.name}] 远端列举完成: {len(remote_map)} 个文件")

        actions = plan_sync(local_files, remote_map, pair.policy)
        self.state.add_files([
            {
                "key": f"{pair.id}:{action.local.rel_path}",
                "pair_id": pair.id,
                "pair_name": pair.name,
                "path": action.local.rel_path,
                "size": action.local.size,
                "state": FILE_PENDING,
                "percent": 0.0,
                "speed": 0.0,
                "error": None,
            }
            for action in actions
        ])
        self.state.pair_update(pair.id, phase=PHASE_UPLOADING, total=len(actions), current="")

        done = instant = skipped = failed = 0
        for action in actions:
            if self._cancelled():
                break
            key = f"{pair.id}:{action.local.rel_path}"
            self.state.pair_update(pair.id, current=action.local.rel_path)
            try:
                if action.kind == "skip":
                    skipped += 1
                    self.state.mark_file(key, FILE_SKIPPED, percent=100.0)
                    self.state.add_skipped_bytes(action.local.size)
                    self.state.pair_update(pair.id, skipped=skipped)
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

    def _execute_action(self, pair: TaskPairConfig, action: SyncAction,
                        key: str, remote_map: Dict[str, Dict]):
        local = action.local
        rel_dir = str(PurePosixPath(local.rel_path).parent)
        if action.kind == "overwrite" and action.remote_fileid:
            self.client.trash(action.remote_fileid)
            self.log(f"[{pair.name}] 覆盖旧文件(进回收站): {local.rel_path}")
        target_name = local.abs_path.name
        if action.kind == "keep_both":
            target_name = unique_keep_both_name(
                target_name,
                lambda name: join_rel(rel_dir, name) in remote_map,
            )
            self.log(f"[{pair.name}] 保留两者，新名称: {target_name}")
        parent_id = self._ensure_remote_dir(pair.target_path, rel_dir)
        self.state.mark_file(key, FILE_UPLOADING)

        last_time = [time.time()]
        last_bytes = [0]

        def on_progress(percent: float):
            now = time.time()
            if now - last_time[0] >= self._tick_interval or percent >= 100.0:
                sent = int(local.size * percent / 100.0)
                delta_bytes = max(sent - last_bytes[0], 0)
                delta_time = max(now - last_time[0], 1e-6)
                self.state.mark_file(
                    key, FILE_UPLOADING, percent=percent, speed=delta_bytes / delta_time)
                self.state.tick_speed()
                last_time[0] = now
                last_bytes[0] = sent

        file_id, instant = self.client.upload(
            local.abs_path, parent_id, target_name,
            on_progress=on_progress, cancel_check=self._cancelled,
        )
        action.instant = instant
        self.state.mark_file(
            key, FILE_INSTANT if instant else FILE_DONE,
            percent=100.0, bytes_done=local.size,
        )
