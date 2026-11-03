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


def test_retry_only_selected_failure_preserves_other_results(local_tree):
    class RetryClient(FakeClient):
        failing = {'e01.mkv', 'e02.mkv'}
        def upload(self, local_path, parent_file_id, name, on_progress=None, cancel_check=None):
            if name in self.failing:
                raise RuntimeError('temporary error')
            return super().upload(local_path, parent_file_id, name, on_progress, cancel_check)

    client = RetryClient(remote=_populated_remote())
    pair = make_pair(local_tree.parent, policy=engine_mod.POLICY_OVERWRITE, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    previous_id = state.run_id
    assert state.snapshot()['counters']['failed'] == 2
    client.failing.remove('e01.mkv')
    client.uploads.clear()
    client.trashed.clear()
    path = 'Season 1/e01.mkv'
    state.begin_retry({f't1:{path}'})
    eng = engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state)
    eng.run([pair], retry_paths={'t1': {path}}, already_started=True)
    snapshot = state.snapshot()
    assert snapshot['run_id'] != previous_id and snapshot['trigger'] == 'retry'
    assert snapshot['counters']['failed'] == 1 and snapshot['counters']['done'] == 2
    assert [entry['state'] for entry in snapshot['files'] if entry['path'].endswith('e02.mkv')] == ['failed']
    assert [name for _, name, _ in client.uploads] == ['e01.mkv']
    assert client.trashed == [501]  # no successful or unselected files are overwritten


def test_retry_missing_source_stays_failed(local_tree):
    class FailingClient(FakeClient):
        def upload(self, *args, **kwargs):
            raise RuntimeError('failed')
    client = FailingClient(remote=_populated_remote())
    pair = make_pair(local_tree.parent, policy=engine_mod.POLICY_OVERWRITE, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    file_path = local_tree / 'a.txt'
    file_path.unlink()
    state.begin_retry({'t1:a.txt'})
    client.trashed.clear()
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    failure = next(f for f in state.snapshot()['files'] if f['path'] == 'a.txt')
    assert failure['state'] == 'failed' and '源文件不存在' in failure['error']
    assert client.trashed == []


def test_pagination_tracks_files_beyond_previous_limit():
    state = engine_mod.RunState()
    state.begin([])
    state.add_files([{'key': f'k{i}', 'path': f'{i}.mkv', 'size': 1,
                      'state': 'pending', 'percent': 0, 'speed': 0, 'error': None}
                     for i in range(5002)])
    state.mark_file('k5001', 'failed', error='failed')
    assert state.snapshot(include_files=False)['files'] == []
    assert state.files_page(251, 20)['items'][-1]['key'] == 'k5001'
    filtered = state.files_page(1, 20, active_only=True)
    assert filtered['total'] == 1 and filtered['items'][0]['key'] == 'k5001'
    assert state.snapshot()['counters']['failed'] == 1


def test_retry_listing_failure_preserves_retryable_records(local_tree):
    class BrokenClient(FakeClient):
        fail_listing = False
        def upload(self, *args, **kwargs):
            raise RuntimeError('upload failed')
        def walk(self, *args, **kwargs):
            if self.fail_listing:
                raise RuntimeError('listing unavailable')
            yield from super().walk(*args, **kwargs)
    client = BrokenClient(remote=_populated_remote())
    pair = make_pair(local_tree.parent, policy=engine_mod.POLICY_OVERWRITE, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    assert state.snapshot()['counters']['failed'] == 3
    client.fail_listing = True
    selected = {'t1:a.txt'}
    state.begin_retry(selected)
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    snapshot = state.snapshot()
    assert snapshot['phase'] == 'error'
    assert snapshot['counters']['failed'] == 3
    assert snapshot['pairs']['t1']['failed'] == 3
    record = next(f for f in snapshot['files'] if f['key'] == 't1:a.txt')
    assert record['state'] == 'failed' and 'listing unavailable' in record['error']


def test_retry_keeps_original_run_error_visible_and_cancelled_retry_is_retryable(local_tree):
    pair = make_pair(local_tree.parent)
    state = engine_mod.RunState()
    state.begin([pair])
    state.add_files([{'key':'t1:a.txt', 'pair_id':'t1', 'path':'a.txt', 'size':3,
                      'state':'pending', 'percent':0, 'speed':0, 'error':None}])
    state.mark_file('t1:a.txt', 'failed', error='upload error')
    state.pair_update('t1', failed=1, total=1)
    state.finish('error', 'another task could not be listed')
    state.begin_retry({'t1:a.txt'})
    state.mark_file('t1:a.txt', 'done', percent=100, bytes_done=3)
    state.finish('finished')
    assert state.snapshot()['phase'] == 'error'
    assert state.snapshot()['error'] == 'another task could not be listed'

    state2 = engine_mod.RunState()
    state2.begin([pair])
    state2.add_files([{'key':'t1:a.txt', 'pair_id':'t1', 'path':'a.txt', 'size':3,
                       'state':'pending', 'percent':0, 'speed':0, 'error':None}])
    state2.mark_file('t1:a.txt', 'failed', error='upload error')
    state2.pair_update('t1', failed=1, total=1)
    state2.finish('finished')
    state2.begin_retry({'t1:a.txt'})
    state2.finish('cancelled')
    snap = state2.snapshot()
    assert snap['phase'] == 'cancelled' and snap['counters']['failed'] == 1
    assert snap['files'][0]['state'] == 'failed' and snap['files'][0]['error'] == '重试已取消'
