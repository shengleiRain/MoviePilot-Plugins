"""Unit tests for the p123sync engine (pure planning + fake-client runs).

The engine module is loaded standalone (no MoviePilot runtime, no p123client
dependency) via importlib, mirroring the mteamadultsearch test style.
"""

from __future__ import annotations

import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v2" / "p123sync"


def _load_engine():
    import sys
    spec = importlib.util.spec_from_file_location(
        "p123sync_sync_engine_test", _PLUGIN_DIR / "sync_engine.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses needs the module registered
    spec.loader.exec_module(module)
    return module


engine_mod = _load_engine()


# ---------------------------------------------------------------- fakes

@dataclass
class FakeItem:
    FileId: int
    FileName: str
    Type: int          # 1 = dir
    Size: int = 0

    @property
    def file_id(self) -> int:
        return self.FileId

    @property
    def name(self) -> str:
        return self.FileName

    @property
    def is_dir(self) -> bool:
        return self.Type == 1

    @property
    def size(self) -> int:
        return self.Size


class FakeClient:
    """Duck-typed stand-in for P123SyncClient."""

    def __init__(self, remote: dict | None = None):
        # remote: {"dirpath": [FakeItem, ...]} keyed by remote dir path
        self.remote = remote or {}
        self.next_id = 1000
        self.uploads: list[tuple[str, str, int]] = []   # (dir, name, size)
        self.trashed: list[int] = []
        self.mkdir_calls: list[str] = []

    def list_dir(self, parent_file_id: int) -> list[FakeItem]:
        return self.remote.get(str(parent_file_id), [])

    def walk(self, root_file_id: int, prefix: str = ""):
        def walk_dir(dir_id: str, rel: str):
            for item in self.remote.get(dir_id, []):
                path = f"{rel}{item.name}"
                if item.is_dir:
                    yield from walk_dir(str(item.FileId), f"{path}/")
                else:
                    yield path, item
        yield from walk_dir(str(root_file_id), prefix)

    def mkdir(self, parent_file_id: int, name: str) -> int:
        self.next_id += 1
        self.mkdir_calls.append(name)
        return self.next_id

    def trash(self, file_id: int) -> bool:
        self.trashed.append(file_id)
        return True

    def upload(self, local_path: Path, parent_file_id: int, name: str,
               on_progress=None, cancel_check=None):
        size = local_path.stat().st_size
        if on_progress:
            on_progress(50.0)
            on_progress(100.0)
        self.uploads.append((str(parent_file_id), name, size))
        self.next_id += 1
        return self.next_id, False


def make_pair(tmp_path: Path, **overrides) -> "engine_mod.TaskPairConfig":
    defaults = dict(
        id="t1",
        name="测试对",
        source_path=str(tmp_path / "src"),
        target_path="/test/target",
        policy=engine_mod.POLICY_SKIP_SAME_SIZE,
    )
    defaults.update(overrides)
    return engine_mod.TaskPairConfig(**defaults)


@pytest.fixture()
def local_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "Season 1").mkdir(parents=True)
    (src / "a.txt").write_text("aaa")
    (src / "b.nfo").write_text("meta")
    (src / "Season 1" / "e01.mkv").write_text("x" * 100)
    (src / "Season 1" / "e02.mkv").write_text("y" * 200)
    (src / ".hidden").write_text("nope")
    (src / "c.part").write_text("partial")
    return src


# ------------------------------------------------------------ pure helpers

def test_should_exclude_filters(tmp_path):
    assert engine_mod.should_exclude("movie.part")
    assert engine_mod.should_exclude(".DS_Store")
    assert engine_mod.should_exclude("Thumbs.db")
    assert engine_mod.should_exclude("x.NFO", (".nfo",))
    assert not engine_mod.should_exclude("movie.mkv")


def test_scan_source_sorted_and_filtered(local_tree):
    files = engine_mod.scan_source(str(local_tree), (".nfo",))
    rels = [f.rel_path for f in files]
    assert rels == ["Season 1/e01.mkv", "Season 1/e02.mkv", "a.txt"]


def test_unique_keep_both_name_suffixes():
    taken = {"movie.mkv", "movie (2).mkv"}
    name = engine_mod.unique_keep_both_name("movie.mkv", lambda n: n in taken)
    assert name == "movie (3).mkv"
    assert engine_mod.unique_keep_both_name("free.mkv", lambda n: False) == "free.mkv"


def test_plan_sync_default_policy():
    lf = engine_mod.LocalFile("a.mkv", Path("/x/a.mkv"), 10)
    remote = {"a.mkv": {"fileid": 5, "size": 10}}
    actions = engine_mod.plan_sync([lf], remote, engine_mod.POLICY_SKIP_SAME_SIZE)
    assert actions[0].kind == "skip"

    remote_diff = {"a.mkv": {"fileid": 5, "size": 11}}
    actions = engine_mod.plan_sync([lf], remote_diff, engine_mod.POLICY_SKIP_SAME_SIZE)
    assert actions[0].kind == "overwrite"

    actions = engine_mod.plan_sync([lf], {}, engine_mod.POLICY_SKIP_SAME_SIZE)
    assert actions[0].kind == "upload"


def test_plan_sync_overwrite_and_keep_both():
    lf = engine_mod.LocalFile("a.mkv", Path("/x/a.mkv"), 10)
    remote_diff = {"a.mkv": {"fileid": 5, "size": 11}}
    actions = engine_mod.plan_sync([lf], remote_diff, engine_mod.POLICY_OVERWRITE)
    assert actions[0].kind == "overwrite"
    # same size under overwrite still overwrites
    actions = engine_mod.plan_sync(
        [lf], {"a.mkv": {"fileid": 5, "size": 10}}, engine_mod.POLICY_OVERWRITE)
    assert actions[0].kind == "overwrite"
    # keep_both: diff size -> keep_both; same size -> skip
    actions = engine_mod.plan_sync([lf], remote_diff, engine_mod.POLICY_KEEP_BOTH)
    assert actions[0].kind == "keep_both"
    actions = engine_mod.plan_sync(
        [lf], {"a.mkv": {"fileid": 5, "size": 10}}, engine_mod.POLICY_KEEP_BOTH)
    assert actions[0].kind == "skip"


def test_task_pair_config_normalization():
    cfg = engine_mod.TaskPairConfig.from_dict({
        "source_path": "/mp/movies/",
        "target_path": "test/movies/",
        "policy": "bogus",
        "exclude_ext": ".nfo, txt",
    })
    assert cfg.source_path == "/mp/movies"
    assert cfg.target_path == "/test/movies"
    assert cfg.policy == engine_mod.POLICY_SKIP_SAME_SIZE
    assert cfg.exclude_ext == [".nfo", ".txt"]
    assert cfg.id and cfg.enabled


# ------------------------------------------------------------- engine runs

def run_engine(client, pairs, cancel_event=None):
    state = engine_mod.RunState()
    cancel_event = cancel_event or threading.Event()
    eng = engine_mod.SyncEngine(
        client=client, logger=lambda _m: None,
        cancel_event=cancel_event, state=state,
    )
    summary = eng.run(pairs)
    return state, summary


def test_engine_first_run_uploads_everything(local_tree):
    client = FakeClient(remote={"0": [FakeItem(1, "test", 1)]})
    state, summary = run_engine(client, [make_pair(local_tree.parent, exclude_ext=[".nfo"])])
    snap = state.snapshot()
    assert summary["phase"] == engine_mod.PHASE_FINISHED
    assert snap["counters"]["done"] == 3          # a.txt + 2 mkv
    assert snap["counters"]["skipped"] == 0
    assert len(client.uploads) == 3
    # nested dir was created remotely
    assert "Season 1" in client.mkdir_calls
    assert snap["counters"]["pending"] == 0 and snap["counters"]["uploading"] == 0


def _populated_remote(a_size: int = 3) -> dict:
    """Remote tree /test/target with a.txt + Season 1/e01,e02 matching local."""
    return {
        "0": [FakeItem(1, "test", 1)],
        "1": [FakeItem(2, "target", 1)],
        "2": [FakeItem(503, "a.txt", 0, a_size), FakeItem(500, "Season 1", 1)],
        "500": [FakeItem(501, "e01.mkv", 0, 100), FakeItem(502, "e02.mkv", 0, 200)],
    }


def test_engine_second_run_skips_same_size(local_tree):
    client = FakeClient(remote=_populated_remote())
    pair = make_pair(local_tree.parent, exclude_ext=[".nfo"])
    state, summary = run_engine(client, [pair])
    snap = state.snapshot()
    assert snap["counters"]["skipped"] == 3
    assert snap["counters"]["done"] == 0
    assert client.uploads == []
    assert snap["bytes_skipped"] == 303


def test_engine_overwrite_trashes_old(local_tree):
    client = FakeClient(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, policy=engine_mod.POLICY_SKIP_SAME_SIZE,
                     exclude_ext=[".nfo"])
    state, summary = run_engine(client, [pair])
    snap = state.snapshot()
    assert snap["counters"]["skipped"] == 2   # e01/e02 same size
    assert snap["counters"]["done"] == 1      # a.txt uploaded
    assert client.trashed == [503]


def test_engine_keep_both_renames(local_tree):
    client = FakeClient(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, policy=engine_mod.POLICY_KEEP_BOTH,
                     exclude_ext=[".nfo"])
    state, summary = run_engine(client, [pair])
    names = sorted(name for _, name, _ in client.uploads)
    # e01/e02 same size -> skipped; only the changed a.txt uploads as "a (2).txt"
    assert names == ["a (2).txt"]
    assert client.trashed == []
    snap = state.snapshot()
    assert snap["counters"]["skipped"] == 2


def test_engine_failure_isolated_per_file(local_tree):
    class FailingClient(FakeClient):
        def upload(self, local_path, parent_file_id, name, on_progress=None,
                   cancel_check=None):
            if name.endswith("e01.mkv"):
                raise RuntimeError("boom")
            return super().upload(local_path, parent_file_id, name, on_progress,
                                  cancel_check)

    client = FailingClient(remote={"0": [FakeItem(1, "test", 1)]})
    pair = make_pair(local_tree.parent, exclude_ext=[".nfo"])
    state, summary = run_engine(client, [pair])
    snap = state.snapshot()
    assert snap["counters"]["failed"] == 1
    assert snap["counters"]["done"] == 2
    failed = [f for f in snap["files"] if f["state"] == "failed"]
    assert failed and failed[0]["error"] == "boom"


def test_engine_cancel_between_files(local_tree):
    cancel_event = threading.Event()

    class CancelClient(FakeClient):
        def upload(self, local_path, parent_file_id, name, on_progress=None,
                   cancel_check=None):
            result = super().upload(local_path, parent_file_id, name,
                                    on_progress, cancel_check)
            cancel_event.set()   # cancel right after the first upload
            return result

    client = CancelClient(remote={"0": [FakeItem(1, "test", 1)]})
    pair = make_pair(local_tree.parent, exclude_ext=[".nfo"])
    state, summary = run_engine(client, [pair], cancel_event=cancel_event)
    snap = state.snapshot()
    assert summary["phase"] == engine_mod.PHASE_CANCELLED
    assert snap["counters"]["done"] == 1
    assert snap["counters"]["cancelled"] >= 1
    assert not snap["running"]


def test_run_state_counter_transitions():
    state = engine_mod.RunState()
    state.begin([])
    state.add_files([
        {"key": "k1", "path": "a", "size": 10, "state": engine_mod.FILE_PENDING,
         "percent": 0.0, "speed": 0.0, "error": None},
    ])
    snap = state.snapshot()
    assert snap["counters"]["pending"] == 1 and snap["counters"]["total_files"] == 1
    state.mark_file("k1", engine_mod.FILE_UPLOADING)
    state.mark_file("k1", engine_mod.FILE_UPLOADING, percent=42.0, speed=100.0)
    snap = state.snapshot()
    assert snap["counters"]["uploading"] == 1
    assert snap["files"][0]["percent"] == 42.0
    state.mark_file("k1", engine_mod.FILE_DONE, percent=100.0, bytes_done=10)
    snap = state.snapshot()
    assert snap["counters"]["uploading"] == 0
    assert snap["counters"]["done"] == 1
    assert snap["bytes_done"] == 10
