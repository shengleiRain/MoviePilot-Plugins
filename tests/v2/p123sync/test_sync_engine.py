"""Unit tests for the p123sync engine (pure planning + fake-client runs).

The engine module is loaded standalone (no MoviePilot runtime, no p123client
dependency) via importlib, mirroring the mteamadultsearch test style.
"""

from __future__ import annotations

import importlib.util
import threading
import hashlib
import time
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
    digest: str | None = None

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

    @property
    def md5(self):
        return self.digest


class FakeClient:
    """Duck-typed stand-in for P123SyncClient."""

    def __init__(self, remote: dict | None = None):
        # remote: {"dirpath": [FakeItem, ...]} keyed by remote dir path
        self.remote = remote or {}
        self.next_id = 1000
        self.uploads: list[tuple[str, str, int]] = []   # (dir, name, size)
        self.trashed: list[int] = []
        self.mkdir_calls: list[str] = []
        self.uploaded_sources: list[str] = []
        self.renames: list[tuple[int, str]] = []

    def list_dir(self, parent_file_id: int, cancel_check=None) -> list[FakeItem]:
        return self.remote.get(str(parent_file_id), [])

    def find_files_by_name(self, parent_file_id, name, cancel_check=None):
        return [item for item in self.list_dir(parent_file_id, cancel_check)
                if not item.is_dir and item.name == name]

    def walk(self, root_file_id: int, prefix: str = "", cancel_check=None):
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
        self.remote.setdefault(str(parent_file_id), []).append(FakeItem(self.next_id, name, 1))
        self.remote[str(self.next_id)] = []
        return self.next_id

    def trash(self, file_id: int) -> bool:
        self.trashed.append(file_id)
        for items in self.remote.values():
            items[:] = [item for item in items if item.file_id != file_id]
        return True

    def rename(self, file_id: int, name: str):
        for items in self.remote.values():
            item = next((item for item in items if item.file_id == file_id), None)
            if item:
                if any(other.name == name and other.file_id != file_id for other in items):
                    raise RuntimeError('name conflict')
                item.FileName = name
                self.renames.append((file_id, name))
                return
        raise RuntimeError('not found')

    def _file_md5(self, path, cancel_check=None, on_progress=None):
        if cancel_check and cancel_check():
            raise KeyboardInterrupt
        data = path.read_bytes()
        if on_progress:
            on_progress(len(data))
        return hashlib.md5(data).hexdigest()

    def upload(self, local_path: Path, parent_file_id: int, name: str,
               on_progress=None, cancel_check=None, file_md5=None, on_stage=None):
        size = local_path.stat().st_size
        if on_stage:
            on_stage('checking_instant')
            on_stage('uploading')
        if on_progress:
            on_progress(50.0)
            on_progress(100.0)
        self.uploads.append((str(parent_file_id), name, size))
        self.uploaded_sources.append(local_path.name)
        self.next_id += 1
        self.remote.setdefault(str(parent_file_id), []).append(
            FakeItem(self.next_id, name, 0, size, file_md5 or self._file_md5(local_path)))
        return self.next_id, False


class CountingClient(FakeClient):
    """FakeClient that counts how often local files are actually read for MD5."""

    def __init__(self, remote: dict | None = None):
        super().__init__(remote)
        self.hash_calls = 0

    def _file_md5(self, path, cancel_check=None, on_progress=None):
        self.hash_calls += 1
        return super()._file_md5(path, cancel_check, on_progress)


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
    lf = engine_mod.LocalFile("a.mkv", Path("/x/a.mkv"), 10, md5='a' * 32)
    remote = {"a.mkv": {"fileid": 5, "size": 10, "md5": 'a' * 32}}
    actions = engine_mod.plan_sync([lf], remote, engine_mod.POLICY_SKIP_SAME_SIZE)
    assert actions[0].kind == "skip"

    remote_diff = {"a.mkv": {"fileid": 5, "size": 11}}
    actions = engine_mod.plan_sync([lf], remote_diff, engine_mod.POLICY_SKIP_SAME_SIZE)
    assert actions[0].kind == "overwrite"

    actions = engine_mod.plan_sync([lf], {}, engine_mod.POLICY_SKIP_SAME_SIZE)
    assert actions[0].kind == "upload"


def test_plan_sync_overwrite_and_keep_both():
    lf = engine_mod.LocalFile("a.mkv", Path("/x/a.mkv"), 10, md5='a' * 32)
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
        [lf], {"a.mkv": {"fileid": 5, "size": 10, "md5": 'a' * 32}}, engine_mod.POLICY_KEEP_BOTH)
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
        "2": [FakeItem(503, "a.txt", 0, a_size, hashlib.md5(b'aaa').hexdigest()), FakeItem(500, "Season 1", 1)],
        "500": [FakeItem(501, "e01.mkv", 0, 100, hashlib.md5(b'x' * 100).hexdigest()), FakeItem(502, "e02.mkv", 0, 200, hashlib.md5(b'y' * 200).hexdigest())],
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


def test_skips_are_checkpointed_in_batches_without_hashing(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    remote = {'0':[FakeItem(1,'test',1)], '1':[FakeItem(2,'target',1)], '2':[]}
    for i in range(120):
        name = f'{i:03}.txt'; (src / name).write_bytes(b'x')
        remote['2'].append(FakeItem(1000+i, name, 0, 1))
    class State(engine_mod.RunState):
        def __init__(self): super().__init__(); self.batches=[]
        def mark_skipped_batch(self, pair_id, keys):
            self.batches.append(len(keys))
            return super().mark_skipped_batch(pair_id, keys)
    class Client(FakeClient):
        def _file_md5(self, *args, **kwargs): raise AssertionError('unexpected MD5')
    state=State()
    engine_mod.SyncEngine(Client(remote), lambda _:None, threading.Event(), state).run([make_pair(tmp_path)])
    snapshot=state.snapshot()
    assert state.batches == [50,50,20]
    assert snapshot['counters']['skipped'] == 120 and snapshot['counters']['failed'] == 0
    assert snapshot['bytes_skipped'] == 120 and snapshot['pairs']['t1']['skipped'] == 120


def test_failed_batch_checkpoint_restores_all_in_memory_state(local_tree, monkeypatch):
    pair=make_pair(local_tree.parent)
    state=engine_mod.RunState(); state.begin([pair])
    state.add_files([{'key':f't1:{name}', 'pair_id':'t1', 'path':name, 'size':3,
                      'state':'pending', 'percent':0, 'speed':0, 'error':None} for name in ('a','b')])
    before=state.snapshot()
    monkeypatch.setattr(state, '_checkpoint', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        state.mark_skipped_batch('t1', ['t1:a','t1:b'])
    after=state.snapshot()
    assert after['files'] == before['files'] and after['counters'] == before['counters']
    assert after['bytes_skipped'] == before['bytes_skipped'] and after['pairs'] == before['pairs']


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
                   cancel_check=None, file_md5=None, on_stage=None):
            if local_path.name.endswith("e01.mkv"):
                raise RuntimeError("boom")
            return super().upload(local_path, parent_file_id, name, on_progress,
                                  cancel_check, file_md5, on_stage)

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
                   cancel_check=None, file_md5=None, on_stage=None):
            result = super().upload(local_path, parent_file_id, name,
                                    on_progress, cancel_check, file_md5, on_stage)
            cancel_event.set()   # cancel right after the first upload
            return result

    client = CancelClient(remote={"0": [FakeItem(1, "test", 1)]})
    pair = make_pair(local_tree.parent, exclude_ext=[".nfo"])
    state, summary = run_engine(client, [pair], cancel_event=cancel_event)
    snap = state.snapshot()
    assert summary["phase"] == engine_mod.PHASE_CANCELLED
    assert snap["counters"]["done"] == 0
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
        def upload(self, local_path, parent_file_id, name, on_progress=None, cancel_check=None, file_md5=None, on_stage=None):
            if local_path.name in self.failing:
                raise RuntimeError('temporary error')
            return super().upload(local_path, parent_file_id, name, on_progress, cancel_check, file_md5, on_stage)

    client = RetryClient(remote=_populated_remote())
    pair = make_pair(local_tree.parent, policy=engine_mod.POLICY_OVERWRITE, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    previous_id = state.run_id
    assert state.snapshot()['counters']['failed'] == 2
    client.failing.remove('e01.mkv')
    client.uploads.clear()
    client.uploaded_sources.clear()
    client.trashed.clear()
    path = 'Season 1/e01.mkv'
    state.begin_retry({f't1:{path}'})
    eng = engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state)
    eng.run([pair], retry_paths={'t1': {path}}, already_started=True)
    snapshot = state.snapshot()
    assert snapshot['run_id'] != previous_id and snapshot['trigger'] == 'retry'
    assert snapshot['counters']['failed'] == 1 and snapshot['counters']['done'] == 2
    assert [entry['state'] for entry in snapshot['files'] if entry['path'].endswith('e02.mkv')] == ['failed']
    assert client.uploaded_sources == ['e01.mkv']
    assert client.trashed == [501]  # no successful or unselected files are overwritten


@pytest.mark.parametrize('strict,remote_size,remote_digest,expect_skip', [
    (False, 3, 'different', True),
    (False, 4, None, False),
    (True, 3, hashlib.md5(b'aaa').hexdigest(), True),
    (True, 3, 'different', False),
])
def test_missing_listing_entry_is_reconciled_only_by_current_policy(local_tree, strict, remote_size, remote_digest, expect_skip):
    class Conflict(Exception):
        operation = 'upload_request'
        service_code = 5060
    class Cloud(FakeClient):
        def upload(self, *args, **kwargs):
            raise Conflict('duplicate')
        def find_files_by_name(self, parent_id, name, cancel_check):
            assert parent_id == 2 and name == 'a.txt'
            return [FakeItem(503, name, 0, remote_size, remote_digest)]
    remote = {'0':[FakeItem(1,'test',1)], '1':[FakeItem(2,'target',1)], '2':[]}
    client = Cloud(remote)
    pair = make_pair(local_tree.parent)
    state = engine_mod.RunState()
    state.begin([pair])
    state.add_files([{'key':'t1:a.txt','pair_id':'t1','pair_name':'test','path':'a.txt',
                      'size':3,'state':'pending','percent':0,'speed':0,'error':None}])
    state.mark_file('t1:a.txt','failed',error='old failure')
    state.patch_file('t1:a.txt', transfer={'kind':'upload','target':'a.txt','stage':'a.txt',
                                           'backup':None,'stage_id':None,'size':3,
                                           'md5':hashlib.md5(b'aaa').hexdigest()})
    state.finish('finished')
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _:None, threading.Event(), state, verify_md5=strict).run(
        [pair], retry_paths={'t1':{'a.txt'}}, already_started=True)
    record = state.file_record('t1:a.txt')
    assert record['state'] == ('skipped' if expect_skip else 'failed')
    assert (record['transfer'] is None) == expect_skip
    assert client.trashed == [] and not client.renames


def test_pending_upload_size_recovery_does_not_hash_or_upload(local_tree):
    class Cloud(FakeClient):
        def find_files_by_name(self, parent_id, name, cancel_check=None):
            return [FakeItem(503, name, 0, 3)]
        def _file_md5(self, *args, **kwargs):
            raise AssertionError('size-only recovery must not hash')
        def upload(self, *args, **kwargs):
            raise AssertionError('existing same-size file must not upload')
    client = Cloud({'0':[FakeItem(1,'test',1)], '1':[FakeItem(2,'target',1)], '2':[]})
    pair = make_pair(local_tree.parent)
    state = engine_mod.RunState()
    state.begin([pair])
    state.add_files([{'key':'t1:a.txt','pair_id':'t1','path':'a.txt','size':3,
                      'state':'pending','percent':0,'speed':0,'error':None}])
    state.patch_file('t1:a.txt', transfer={'kind':'upload','target':'a.txt','stage':'a.txt','size':3})
    state.mark_file('t1:a.txt','failed',error='old failure')
    state.finish('finished')
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _:None, threading.Event(), state).run(
        [pair], retry_paths={'t1':{'a.txt'}}, already_started=True)
    assert state.file_record('t1:a.txt')['state'] == 'skipped'
    assert state.file_record('t1:a.txt')['transfer'] is None


def test_unlisted_same_size_file_is_found_before_hash_or_upload(local_tree):
    class Cloud(FakeClient):
        def find_files_by_name(self, parent_id, name, cancel_check=None):
            return [FakeItem(503, name, 0, 3)] if name == 'a.txt' else []
        def _file_md5(self, *args, **kwargs):
            raise AssertionError('same-size file must not be hashed')
        def upload(self, *args, **kwargs):
            raise AssertionError('same-size file must not be uploaded')
    client = Cloud({'0':[FakeItem(1,'test',1)], '1':[FakeItem(2,'target',1)], '2':[]})
    pair = make_pair(local_tree.parent)
    file = local_tree / 'a.txt'
    stat = file.stat()
    selected = {'t1': {'a.txt': {'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns}}}
    state = engine_mod.RunState()
    engine_mod.SyncEngine(client, lambda _:None, threading.Event(), state).run([pair], selected_paths=selected)
    assert state.file_record('t1:a.txt')['state'] == 'skipped'
    assert state.snapshot()['counters']['failed'] == 0


def test_live_hash_activity_shows_pending_file_and_clears_after_completion(tmp_path):
    source = tmp_path / 'src'; source.mkdir(); (source / 'movie.mkv').write_bytes(b'abcdef')
    hash_started, finish_hash = threading.Event(), threading.Event()
    class Cloud(FakeClient):
        def _file_md5(self, path, cancel_check=None, on_progress=None):
            on_progress(2)
            hash_started.set()
            assert finish_hash.wait(5)
            on_progress(6)
            return hashlib.md5(path.read_bytes()).hexdigest()
    class State(engine_mod.RunState):
        def __init__(self): super().__init__(); self.stages=[]
        def set_activity(self, pair_id, path, stage, **kwargs):
            self.stages.append(stage)
            super().set_activity(pair_id, path, stage, **kwargs)
    state = State(); client = Cloud({'0':[FakeItem(1,'test',1)], '1':[FakeItem(2,'target',1)], '2':[]})
    worker = threading.Thread(target=lambda: engine_mod.SyncEngine(client, lambda _:None, threading.Event(), state).run([make_pair(tmp_path)]))
    worker.start()
    try:
        assert hash_started.wait(5)
        snapshot = state.snapshot(include_files=False)
        assert snapshot['phase'] == 'uploading'
        assert snapshot['activity']['stage'] == 'hashing'
        assert snapshot['activity']['path'] == 'movie.mkv'
        assert snapshot['activity']['processed_bytes'] == 2 and snapshot['activity']['total_bytes'] == 6
        assert state.files_page(active_only=True)['items'][0]['path'] == 'movie.mkv'
    finally:
        finish_hash.set(); worker.join(5)
    assert not worker.is_alive()
    assert state.snapshot()['activity'] is None
    assert state.snapshot()['counters']['done'] == 1
    assert 'checking_instant' in state.stages and 'uploading' in state.stages and 'checking_remote' in state.stages


def test_activity_is_cleared_on_failure_and_not_restored_from_journal(tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location('p123sync_activity_journal_test', _PLUGIN_DIR / 'sync_journal.py')
    journal_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = journal_mod; spec.loader.exec_module(journal_mod)
    journal = journal_mod.SyncJournal(tmp_path / 'activity.sqlite3')
    pair = make_pair(tmp_path)
    state = engine_mod.RunState(journal=journal); state.begin([pair])
    state.set_activity('t1', 'movie.mkv', 'hashing', total_bytes=100)
    state.update_activity_bytes('t1', 'movie.mkv', 'hashing', 20)
    state.set_phase('uploading', 'test')  # checkpoint while an activity is visible
    restored = engine_mod.RunState.restore(journal, recover=False)
    assert restored.snapshot()['activity'] is None
    state.finish('error', 'failed')
    assert state.snapshot()['activity'] is None
    journal.close()


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
        def list_dir(self, *args, **kwargs):
            if self.fail_listing:
                raise RuntimeError('listing unavailable')
            return super().list_dir(*args, **kwargs)
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


def test_retry_lists_only_the_selected_parent_directory(local_tree):
    class TrackingClient(FakeClient):
        def __init__(self, remote):
            super().__init__(remote)
            self.listed = []
        def list_dir(self, parent_file_id, cancel_check=None):
            self.listed.append(parent_file_id)
            return super().list_dir(parent_file_id, cancel_check)
        def walk(self, *args, **kwargs):
            raise AssertionError('retry must not walk the whole cloud tree')
    client = TrackingClient(_populated_remote())
    pair = make_pair(local_tree.parent)
    state = engine_mod.RunState()
    state.begin([pair])
    state.add_files([{'key':'t1:Season 1/e01.mkv', 'pair_id':'t1', 'path':'Season 1/e01.mkv',
                      'size':1, 'state':'pending', 'percent':0, 'speed':0, 'error':None}])
    state.mark_file('t1:Season 1/e01.mkv', 'failed', error='retry')
    state.finish('finished')
    state.begin_retry({'t1:Season 1/e01.mkv'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'Season 1/e01.mkv'}}, already_started=True)
    assert state.snapshot()['counters']['failed'] == 0
    assert 2 in client.listed  # target root
    assert all(str(directory) in {'0', '1', '2', '500'} for directory in client.listed)


def test_monitor_lists_only_selected_parent_directory(local_tree):
    class Cloud(FakeClient):
        def walk(self, *args, **kwargs):
            raise AssertionError('monitor batch must not walk the whole cloud tree')
    client = Cloud(_populated_remote())
    pair = make_pair(local_tree.parent)
    file = local_tree / 'Season 1' / 'e01.mkv'
    stat = file.stat()
    selected = {'t1': {'Season 1/e01.mkv': {'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns}}}
    state = engine_mod.RunState()
    engine_mod.SyncEngine(client, lambda _:None, threading.Event(), state).run([pair], selected_paths=selected)
    assert state.snapshot()['counters']['skipped'] == 1
    assert state.snapshot()['counters']['failed'] == 0


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


def test_checkpoint_restores_interrupted_files_without_losing_details(tmp_path):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location('p123sync_journal_test', _PLUGIN_DIR / 'sync_journal.py')
    journal_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = journal_mod
    spec.loader.exec_module(journal_mod)
    journal = journal_mod.SyncJournal(tmp_path / 'state.sqlite3')
    pair = make_pair(tmp_path)
    state = engine_mod.RunState(journal=journal)
    state.begin([pair])
    state.add_files([{'key':'t1:a.txt','pair_id':'t1','path':'a.txt','size':3,
                      'state':'pending','percent':0,'speed':0,'error':None}])
    state.patch_file('t1:a.txt', transfer={'stage_name':'stage-unique','file_id':123})
    state.mark_file('t1:a.txt', 'uploading', percent=20)
    old_id = state.run_id
    journal.close()
    restored_journal = journal_mod.SyncJournal(tmp_path / 'state.sqlite3')
    restored = engine_mod.RunState.restore(restored_journal)
    snap = restored.snapshot()
    assert snap['run_id'] == old_id and snap['phase'] == 'error'
    assert snap['files'][0]['state'] == 'failed'
    assert snap['files'][0]['transfer'] == {'stage_name':'stage-unique','file_id':123}
    assert snap['counters']['failed'] == 1
    restored.begin_retry({'t1:a.txt'})
    restored.finish('cancelled')
    restored_journal.close()
    reopened = journal_mod.SyncJournal(tmp_path / 'state.sqlite3')
    again = engine_mod.RunState.restore(reopened).snapshot()
    assert again['files'][0]['state'] == 'failed' and again['files'][0]['error'] == '重试已取消'
    assert again['files'][0]['transfer']['file_id'] == 123
    reopened.close()


def test_overwrite_upload_failure_never_moves_or_trashes_original(local_tree):
    class FailUpload(FakeClient):
        def upload(self, *args, **kwargs): raise RuntimeError('upload unavailable')
    client = FailUpload(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    assert state.snapshot()['counters']['failed'] == 1
    assert client.trashed == [] and client.renames == []
    assert any(f.file_id == 503 and f.name == 'a.txt' for f in client.remote['2'])


def test_overwrite_failed_promotion_rolls_back_and_retry_reuses_staged_upload(local_tree):
    class FailPromotion(FakeClient):
        fail_once = True
        def rename(self, file_id, name):
            if self.fail_once and file_id > 1000 and name == 'a.txt':
                self.fail_once = False
                raise RuntimeError('rename failed')
            return super().rename(file_id, name)
    client = FailPromotion(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    failed = next(f for f in state.snapshot()['files'] if f['path'] == 'a.txt')
    assert failed['state'] == 'failed' and failed['transfer']
    assert any(f.file_id == 503 and f.name == 'a.txt' for f in client.remote['2'])
    assert not client.trashed and len(client.uploads) == 1
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    snap = state.snapshot()
    assert snap['counters']['failed'] == 0 and len(client.uploads) == 1
    assert client.trashed == [503]
    result = next(f for f in client.remote['2'] if f.name == 'a.txt')
    assert result.file_id != 503 and result.md5 == hashlib.md5(b'aaa').hexdigest()


def test_lost_upload_response_is_reconciled_without_duplicate(local_tree):
    class LostResponse(FakeClient):
        fail_once = True
        def upload(self, *args, **kwargs):
            result = super().upload(*args, **kwargs)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError('response lost')
            return result
    client = LostResponse(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    assert state.snapshot()['counters']['failed'] == 1 and not client.trashed
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    assert state.snapshot()['counters']['failed'] == 0
    assert len(client.uploads) == 1 and client.trashed == [503]


def test_same_size_different_content_size_mode_skips_strict_overwrites():
    local = engine_mod.LocalFile('a.mkv', Path('/unused'), 5, md5='a' * 32)
    remote = {'a.mkv': {'fileid': 1, 'size': 5, 'md5': 'b' * 32}}
    # default size-only mode: same name+size is treated as identical
    assert engine_mod.plan_sync([local], remote, engine_mod.POLICY_SKIP_SAME_SIZE)[0].kind == 'skip'
    # strict mode keeps the MD5-verified semantics
    assert engine_mod.plan_sync([local], remote, engine_mod.POLICY_SKIP_SAME_SIZE, strict_verify=True)[0].kind == 'overwrite'
    assert engine_mod.plan_sync([local], remote, engine_mod.POLICY_KEEP_BOTH, strict_verify=True)[0].kind == 'keep_both'
    assert engine_mod.plan_sync([local], {'a.mkv': {'fileid':1,'size':5}},
                                engine_mod.POLICY_SKIP_SAME_SIZE, strict_verify=True)[0].kind == 'unverified'


def test_scan_reports_inaccessible_subdirectory(monkeypatch, local_tree):
    def broken_walk(root, onerror=None):
        onerror(PermissionError(13, 'denied', str(root / 'secret')))
        yield from ()
    monkeypatch.setattr(engine_mod.os, 'walk', broken_walk)
    with pytest.raises(OSError, match='无法扫描源目录'):
        engine_mod.scan_source(str(local_tree))


def test_crash_after_backup_rename_recovers_from_journal_without_reupload(local_tree, tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location('p123sync_crash_journal_test', _PLUGIN_DIR / 'sync_journal.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    class CrashAfterBackup(FakeClient):
        crash = True
        def rename(self, file_id, name):
            result = super().rename(file_id, name)
            if self.crash and file_id == 503 and name.startswith('p123sync-backup-'):
                self.crash = False
                raise SystemExit('simulated process death')
            return result
    client = CrashAfterBackup(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    journal = module.SyncJournal(tmp_path / 'journal.sqlite3')
    state = engine_mod.RunState(journal=journal)
    with pytest.raises(SystemExit):
        engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run([pair])
    assert not client.trashed and len(client.uploads) == 1
    assert not any(item.name == 'a.txt' for item in client.remote['2'])
    journal.close()
    journal = module.SyncJournal(tmp_path / 'journal.sqlite3')
    restored = engine_mod.RunState.restore(journal)
    record = next(f for f in restored.snapshot()['files'] if f['path'] == 'a.txt')
    assert record['state'] == 'failed' and record['transfer']['stage_id']
    restored.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), restored).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    assert restored.snapshot()['counters']['failed'] == 0
    assert len(client.uploads) == 1 and client.trashed == [503]
    assert any(item.name == 'a.txt' and item.md5 == hashlib.md5(b'aaa').hexdigest() for item in client.remote['2'])
    journal.close()


def test_changed_source_restores_original_without_deleting_staged_copy(local_tree):
    class InterruptRename(FakeClient):
        crash = True
        def rename(self, file_id, name):
            result = super().rename(file_id, name)
            if self.crash and file_id == 503 and name.startswith('p123sync-backup-'):
                self.crash = False
                raise SystemExit('crash')
            return result
    client = InterruptRename(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state = engine_mod.RunState()
    with pytest.raises(SystemExit):
        engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run([pair])
    state.finish('error', 'interrupted')
    (local_tree / 'a.txt').write_bytes(b'new version')
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    snapshot = state.snapshot()
    record = next(f for f in snapshot['files'] if f['path'] == 'a.txt')
    assert record['state'] == 'failed' and record['transfer'] is None
    assert any(item.file_id == 503 and item.name == 'a.txt' for item in client.remote['2'])
    assert any(item.name.startswith('p123sync-stage-') for item in client.remote['2'])
    assert client.trashed == []
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    assert state.snapshot()['counters']['failed'] == 0
    assert any(item.name == 'a.txt' and item.md5 == hashlib.md5(b'new version').hexdigest() for item in client.remote['2'])


def test_missing_source_restores_original_and_retains_stage(local_tree):
    class InterruptRename(FakeClient):
        crash = True
        def rename(self, file_id, name):
            result = super().rename(file_id, name)
            if self.crash and file_id == 503 and name.startswith('p123sync-backup-'):
                self.crash = False
                raise SystemExit('crash')
            return result
    client = InterruptRename(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state = engine_mod.RunState()
    with pytest.raises(SystemExit):
        engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run([pair])
    state.finish('error', 'interrupted')
    (local_tree / 'a.txt').unlink()
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    record = next(f for f in state.snapshot()['files'] if f['path'] == 'a.txt')
    assert record['state'] == 'failed' and record['transfer'] is None
    assert any(item.file_id == 503 and item.name == 'a.txt' for item in client.remote['2'])
    assert any(item.name.startswith('p123sync-stage-') for item in client.remote['2'])
    assert client.trashed == []


def test_progress_counts_active_bytes_and_excludes_instant_transfer():
    state = engine_mod.RunState()
    state.begin([])
    state.add_files([{'key':key, 'path':key, 'size':1000, 'state':'pending', 'percent':0, 'speed':0, 'error':None}
                     for key in ('normal', 'instant')])
    state.mark_file('normal', 'uploading', percent=50, transfer_delta=500)
    snap = state.snapshot()
    assert snap['bytes_inflight'] == 500 and snap['bytes_done'] == 0
    assert snap['bytes_transferred'] == 500 and snap['speed'] > 0
    state.mark_file('normal', 'uploading', percent=100, transfer_delta=500)
    state.mark_file('normal', 'done', percent=100, bytes_done=1000)
    state.mark_file('instant', 'instant', percent=100, bytes_done=1000)
    snap = state.snapshot()
    assert snap['bytes_inflight'] == 0 and snap['bytes_done'] == 2000
    assert snap['bytes_transferred'] == 1000


def test_interrupted_progress_is_recovered_as_failed_with_transfer_bytes(tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location('p123sync_progress_journal_test', _PLUGIN_DIR / 'sync_journal.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    journal = module.SyncJournal(tmp_path / 'progress.sqlite3')
    state = engine_mod.RunState(journal=journal)
    state.begin([])
    state.add_files([{'key':'k','path':'a','size':1000,'state':'pending','percent':0,'speed':0,'error':None}])
    state.mark_file('k', 'uploading', percent=40, transfer_delta=400)
    journal.close()
    journal = module.SyncJournal(tmp_path / 'progress.sqlite3')
    snap = engine_mod.RunState.restore(journal).snapshot()
    assert snap['files'][0]['state'] == 'failed'
    assert snap['bytes_inflight'] == 0 and snap['bytes_transferred'] == 400
    journal.close()


def test_remote_original_change_during_upload_prevents_overwrite(local_tree):
    class ConcurrentChange(FakeClient):
        def upload(self, *args, **kwargs):
            result = super().upload(*args, **kwargs)
            next(item for item in self.remote['2'] if item.file_id == 503).digest = 'f' * 32
            return result
    client = ConcurrentChange(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    assert state.snapshot()['counters']['failed'] == 1
    assert not client.trashed and not client.renames
    assert any(item.file_id == 503 and item.name == 'a.txt' for item in client.remote['2'])
    assert any(item.name.startswith('p123sync-stage-') for item in client.remote['2'])


def test_overwrite_without_original_md5_fails_before_upload(local_tree):
    remote = _populated_remote(a_size=99)
    remote['2'][0].digest = None
    client = FakeClient(remote=remote)
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    assert state.snapshot()['counters']['failed'] == 1
    assert not client.uploads and not client.trashed and not client.renames


def test_lost_promotion_response_is_reconciled_before_old_copy_is_trashed(local_tree):
    class LostPromotionResponse(FakeClient):
        lose = True
        def rename(self, file_id, name):
            result = super().rename(file_id, name)
            if self.lose and file_id > 1000 and name == 'a.txt':
                self.lose = False
                raise RuntimeError('response lost after rename')
            return result
    client = LostPromotionResponse(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    record = next(f for f in state.snapshot()['files'] if f['path'] == 'a.txt')
    assert record['state'] == 'failed' and client.trashed == []
    assert any(item.name == 'a.txt' and item.file_id != 503 for item in client.remote['2'])
    assert any(item.file_id == 503 and item.name.startswith('p123sync-backup-') for item in client.remote['2'])
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run(
        [pair], retry_paths={'t1': {'a.txt'}}, already_started=True)
    assert len(client.uploads) == 1 and client.trashed == [503]
    assert state.snapshot()['counters']['failed'] == 0


def test_size_mode_skips_without_hashing_or_failing_on_missing_remote_md5(local_tree):
    remote = _populated_remote()
    remote['2'][0].digest = None   # a.txt has no usable Etag on the remote
    client = CountingClient(remote=remote)
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    snap = state.snapshot()
    assert snap['counters']['skipped'] == 3 and snap['counters']['failed'] == 0
    assert client.hash_calls == 0 and not client.uploads and not client.trashed


def test_strict_verify_still_fails_on_missing_remote_md5(local_tree):
    remote = _populated_remote()
    remote['2'][0].digest = None
    client = FakeClient(remote=remote)
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state = engine_mod.RunState()
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state,
                          verify_md5=True).run([pair])
    snap = state.snapshot()
    assert snap['counters']['failed'] == 1 and snap['counters']['skipped'] == 2
    assert not client.uploads and not client.trashed


def test_failed_checkpoint_cannot_clear_recovery_metadata():
    class FlakyJournal:
        fail = False
        def checkpoint(self, *args, **kwargs):
            if self.fail:
                self.fail = False
                raise OSError('disk temporarily unavailable')
    journal = FlakyJournal()
    state = engine_mod.RunState(journal=journal)
    state.begin([])
    state.add_files([{'key':'k','path':'a','size':3,'state':'pending','percent':0,'speed':0,'error':None}])
    state.patch_file('k', transfer={'stage':'staged'})
    state.mark_file('k', 'uploading', percent=100, transfer_delta=3)
    journal.fail = True
    with pytest.raises(OSError):
        state.mark_file('k', 'done', percent=100, bytes_done=3, clear_transfer=True)
    snap = state.snapshot()
    assert snap['files'][0]['state'] == 'uploading' and snap['files'][0]['transfer'] == {'stage':'staged'}
    assert snap['counters']['done'] == 0 and snap['bytes_done'] == 0 and snap['bytes_inflight'] == 3
    state.mark_file('k', 'failed', error='checkpoint failed')
    assert state.snapshot()['files'][0]['transfer'] == {'stage':'staged'}


def test_restart_recognizes_fully_checkpointed_last_file_without_reupload(tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location('p123sync_lastfile_journal_test', _PLUGIN_DIR / 'sync_journal.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.txt').write_bytes(b'aaa')
    pair = make_pair(tmp_path, target_path='/')
    class CrashAfterCommit(engine_mod.RunState):
        def pair_update(self, pair_id, **kwargs):
            if kwargs.get('done') == 1:
                raise SystemExit('crash after file commit')
            return super().pair_update(pair_id, **kwargs)
    journal = module.SyncJournal(tmp_path / 'last.sqlite3')
    state = CrashAfterCommit(journal=journal)
    client = FakeClient()
    with pytest.raises(SystemExit):
        engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state).run([pair])
    assert len(client.uploads) == 1
    journal.close()
    journal = module.SyncJournal(tmp_path / 'last.sqlite3')
    snap = engine_mod.RunState.restore(journal).snapshot()
    assert snap['phase'] == 'finished' and snap['counters']['done'] == 1
    assert snap['pairs']['t1']['done'] == 1 and snap['error'] is None
    journal.close()


def test_journal_lock_excludes_other_instances_and_releases(tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location('p123sync_lease_journal_test', _PLUGIN_DIR / 'sync_journal.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    one = module.SyncJournal(tmp_path / 'shared.sqlite3')
    two = module.SyncJournal(tmp_path / 'shared.sqlite3')
    assert one.acquire()
    assert not two.acquire()
    one.release()
    assert two.acquire()
    two.release()
    one.close()
    two.close()


def test_journal_lock_is_shared_across_processes(tmp_path):
    import importlib.util, subprocess, sys
    journal_path = _PLUGIN_DIR / 'sync_journal.py'
    spec = importlib.util.spec_from_file_location('p123sync_process_journal_test', journal_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    data_path = tmp_path / 'shared.sqlite3'
    journal = module.SyncJournal(data_path)
    assert journal.acquire()
    script = ("import importlib.util,sys; from pathlib import Path; "
              "s=importlib.util.spec_from_file_location('childjournal',sys.argv[1]); "
              "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
              "j=m.SyncJournal(Path(sys.argv[2])); print('owned' if j.acquire() else 'busy'); j.close()")
    def probe():
        return subprocess.run([sys.executable, '-c', script, str(journal_path), str(data_path)],
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    assert probe() == 'busy'
    journal.release()
    assert probe() == 'owned'
    journal.close()


def test_unconfirmed_trash_keeps_failure_and_backup(local_tree):
    class IgnoredTrash(FakeClient):
        def trash(self, file_id):
            self.trashed.append(file_id)
            return True  # simulate an upstream success response that did not move it
    client = IgnoredTrash(remote=_populated_remote(a_size=99))
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, _ = run_engine(client, [pair])
    record = next(f for f in state.snapshot()['files'] if f['path'] == 'a.txt')
    assert record['state'] == 'failed' and '回收操作未确认' in record['error']
    assert record['transfer'] and any(item.file_id == 503 for item in client.remote['2'])
    assert any(item.name == 'a.txt' and item.file_id != 503 for item in client.remote['2'])


def test_keep_both_does_not_collide_with_another_local_source(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.txt').write_bytes(b'new content')
    (src / 'a (2).txt').write_bytes(b'another local file')
    cloud = FakeClient(remote={'0':[FakeItem(10,'a.txt',0,3,hashlib.md5(b'old').hexdigest())]})
    pair = make_pair(tmp_path, target_path='/', policy=engine_mod.POLICY_KEEP_BOTH)
    state, _ = run_engine(cloud, [pair])
    assert state.snapshot()['counters']['failed'] == 0
    assert {item.name for item in cloud.remote['0']} == {'a.txt','a (2).txt','a (3).txt'}
    assert not cloud.trashed


def test_monitor_checkpoint_ack_is_atomic_and_requeues_changed_files(tmp_path):
    import importlib.util, sys
    spec=importlib.util.spec_from_file_location('p123sync_monitor_journal_test', _PLUGIN_DIR / 'sync_journal.py')
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
    journal=module.SyncJournal(tmp_path/'monitor.sqlite3')
    file={'path':'a.mkv','size':10,'mtime_ns':100}
    journal.observe_files('p','scope',[file],now=1000,complete=True)
    assert journal.monitor_ready('p','scope',1119,120) == []
    assert journal.monitor_ready('p','scope',1120,120) == [file]
    state=engine_mod.RunState(journal=journal)
    state.begin([])
    state.add_files([{'key':'p:a.mkv','pair_id':'p','path':'a.mkv','size':10,'source_mtime_ns':100,
                      'monitor_scope':'scope','state':'pending','percent':0,'speed':0,'error':None}])
    state.mark_file('p:a.mkv','done',bytes_done=10)
    assert journal.monitor_ready('p','scope',1200,120) == []
    changed={'path':'a.mkv','size':11,'mtime_ns':200}
    journal.observe_files('p','scope',[changed],now=1200,complete=True)
    assert journal.monitor_ready('p','scope',1319,120) == []
    assert journal.monitor_ready('p','scope',1320,120) == [changed]
    journal.close()


def test_monitor_selected_run_uploads_only_matching_stable_files(local_tree):
    client=FakeClient(remote={'0':[FakeItem(1,'test',1)]})
    pair=make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state=engine_mod.RunState()
    selected='Season 1/e01.mkv'
    file=(local_tree / selected).stat()
    engine_mod.SyncEngine(client, lambda _:None, threading.Event(), state).run(
        [pair], selected_paths={'t1':{selected:{'size':file.st_size,'mtime_ns':file.st_mtime_ns}}})
    assert client.uploaded_sources == ['e01.mkv']
    assert state.snapshot()['counters']['total_files'] == 1


def test_monitor_selected_file_changed_after_observation_is_not_uploaded(local_tree):
    client=FakeClient(remote={'0':[FakeItem(1,'test',1)]})
    pair=make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state=engine_mod.RunState()
    engine_mod.SyncEngine(client, lambda _:None, threading.Event(), state).run(
        [pair], selected_paths={'t1':{'a.txt':{'size':999,'mtime_ns':1}}})
    snap=state.snapshot()
    assert snap['counters']['failed'] == 1 and not client.uploads
    assert '源文件已变化' in snap['files'][0]['error']


def test_monitor_queue_persists_backoff_and_never_acknowledges_newer_version(tmp_path):
    import importlib.util, sys
    spec=importlib.util.spec_from_file_location('p123sync_queue_recovery_test', _PLUGIN_DIR/'sync_journal.py')
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
    path=tmp_path/'queue.sqlite3'
    journal=module.SyncJournal(path)
    old={'path':'a','size':3,'mtime_ns':1}
    new={'path':'a','size':4,'mtime_ns':2}
    journal.observe_files('p','scope',[old],now=100,complete=True)
    journal.monitor_fail('p','scope','a',3,1,'failed',now=150)
    assert journal.monitor_ready('p','scope',209,30) == []
    journal.close()
    journal=module.SyncJournal(path)
    assert journal.monitor_ready('p','scope',210,30) == [old]
    journal.observe_files('p','scope',[new],now=220,complete=True)
    state=engine_mod.RunState(journal=journal); state.begin([])
    state.add_files([{'key':'p:a','pair_id':'p','path':'a','size':3,'source_mtime_ns':1,'monitor_scope':'scope',
                      'state':'pending','percent':0,'speed':0,'error':None}])
    state.mark_file('p:a','done',bytes_done=3)
    assert journal.monitor_ready('p','scope',250,30) == [new]
    journal.observe_files('p','scope',[],now=260,complete=True)
    assert journal.monitor_counts('p','scope')['missing'] == 1
    journal.close()


def test_manual_sync_acknowledges_file_before_first_monitor_scan(tmp_path):
    import importlib.util, sys
    spec=importlib.util.spec_from_file_location('p123sync_early_ack_test', _PLUGIN_DIR/'sync_journal.py')
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
    journal=module.SyncJournal(tmp_path/'early.sqlite3')
    state=engine_mod.RunState(journal=journal); state.begin([])
    state.add_files([{'key':'p:a','pair_id':'p','path':'a','size':3,'source_mtime_ns':1,'monitor_scope':'scope',
                      'state':'pending','percent':0,'speed':0,'error':None}])
    state.mark_file('p:a','done',bytes_done=3)
    journal.observe_files('p','scope',[{'path':'a','size':3,'mtime_ns':1}],now=1000,complete=True)
    assert journal.monitor_counts('p','scope')['pending'] == 0
    journal.close()


def test_lost_backup_rename_response_restores_old_name_before_retry(local_tree):
    class LostBackupResponse(FakeClient):
        lose=True
        def rename(self, file_id, name):
            result=super().rename(file_id,name)
            if self.lose and file_id == 503 and name.startswith('p123sync-backup-'):
                self.lose=False
                raise RuntimeError('response lost')
            return result
    client=LostBackupResponse(remote=_populated_remote(a_size=99))
    pair=make_pair(local_tree.parent,exclude_ext=['.nfo'])
    state,_=run_engine(client,[pair])
    assert state.snapshot()['counters']['failed'] == 1 and not client.trashed
    assert any(item.file_id == 503 and item.name == 'a.txt' for item in client.remote['2'])
    state.begin_retry({'t1:a.txt'})
    engine_mod.SyncEngine(client,lambda _:None,threading.Event(),state).run(
        [pair],retry_paths={'t1':{'a.txt'}},already_started=True)
    assert len(client.uploads) == 1 and client.trashed == [503]
    assert state.snapshot()['counters']['failed'] == 0


def test_incorrect_staged_hash_never_moves_or_trashes_old_file(local_tree):
    class BadHash(FakeClient):
        def upload(self,*args,**kwargs):
            result=super().upload(*args,**kwargs)
            parent=str(args[1])
            self.remote[parent][-1].digest = 'f'*32
            return result
    client=BadHash(remote=_populated_remote(a_size=99))
    pair=make_pair(local_tree.parent,exclude_ext=['.nfo'])
    state,_=run_engine(client,[pair])
    assert state.snapshot()['counters']['failed'] == 1
    assert not client.renames and not client.trashed
    assert any(item.file_id == 503 and item.name == 'a.txt' for item in client.remote['2'])


# ------------------------------------------------------------- md5 cache

def _make_journal(tmp_path: Path, name: str):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(f'p123sync_journal_md5_{name.replace(".", "_")}',
                                                  _PLUGIN_DIR / 'sync_journal.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SyncJournal(tmp_path / name)


def test_md5_cache_avoids_rereading_unchanged_files(tmp_path, local_tree):
    journal = _make_journal(tmp_path, 'md5.sqlite3')
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    # strict run #1 hashes every same-size file once and fills the cache
    client = CountingClient(remote=_populated_remote())
    state = engine_mod.RunState(journal=journal)
    engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state,
                          journal=journal, verify_md5=True).run([pair])
    assert client.hash_calls == 3
    assert state.snapshot()['counters']['skipped'] == 3
    # strict run #2 (new process/journal handle, same cache) reads no bytes
    journal.close()
    journal = _make_journal(tmp_path, 'md5.sqlite3')
    client2 = CountingClient(remote=_populated_remote())
    state2 = engine_mod.RunState(journal=journal)
    engine_mod.SyncEngine(client2, lambda _: None, threading.Event(), state2,
                          journal=journal, verify_md5=True).run([pair])
    assert client2.hash_calls == 0
    assert state2.snapshot()['counters']['skipped'] == 3
    journal.close()


def test_md5_cache_invalidated_when_file_changes(tmp_path, local_tree):
    journal = _make_journal(tmp_path, 'md5b.sqlite3')
    client = CountingClient()
    eng = engine_mod.SyncEngine(client, lambda _: None, threading.Event(),
                                engine_mod.RunState(), journal=journal)
    target = local_tree / 'a.txt'
    local1 = engine_mod.LocalFile('a.txt', target, target.stat().st_size,
                                  mtime_ns=target.stat().st_mtime_ns)
    assert eng._hash_source(local1) == hashlib.md5(b'aaa').hexdigest()
    assert client.hash_calls == 1
    # same size+mtime_ns -> served from the cache even for a fresh LocalFile
    local2 = engine_mod.LocalFile('a.txt', target, target.stat().st_size,
                                  mtime_ns=target.stat().st_mtime_ns)
    assert eng._hash_source(local2) == hashlib.md5(b'aaa').hexdigest()
    assert client.hash_calls == 1
    # a content change updates mtime_ns -> re-read and refresh the cache
    target.write_bytes(b'zzz')
    local3 = engine_mod.LocalFile('a.txt', target, target.stat().st_size,
                                  mtime_ns=target.stat().st_mtime_ns)
    assert eng._hash_source(local3) == hashlib.md5(b'zzz').hexdigest()
    assert client.hash_calls == 2
    journal.close()


def test_verify_hashes_runs_parallel_with_progress(local_tree):
    client = CountingClient()
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state = engine_mod.RunState()
    state.begin([pair])
    eng = engine_mod.SyncEngine(client, lambda _: None, threading.Event(),
                                state, verify_md5=True)
    files = []
    for rel in ('a.txt', 'Season 1/e01.mkv', 'Season 1/e02.mkv'):
        path = local_tree / rel
        files.append(engine_mod.LocalFile(rel, path, path.stat().st_size,
                                          mtime_ns=path.stat().st_mtime_ns))
    eng._verify_hashes(pair, files)
    assert client.hash_calls == 3
    assert all(local.md5 for local in files)
    assert state.snapshot()['pairs']['t1']['current'] == '校验 3/3 Season 1/e02.mkv' or \
           state.snapshot()['pairs']['t1']['current'].startswith('校验 3/3')


# ------------------------------------------------------- duplicate remote names

def test_plan_sync_resolves_ambiguous_duplicate_by_md5():
    local = engine_mod.LocalFile('books/x.pdf', Path('x'), 5,
                                 md5=hashlib.md5(b'aaa').hexdigest())
    remote = {'books/x.pdf': {'ambiguous': True, 'candidates': [
        {'fileid': 1, 'size': 5, 'md5': hashlib.md5(b'aaa').hexdigest()},
        {'fileid': 2, 'size': 5, 'md5': hashlib.md5(b'aaa').hexdigest()}]}}
    assert engine_mod.plan_sync([local], remote,
                                engine_mod.POLICY_SKIP_SAME_SIZE)[0].kind == 'skip'
    assert engine_mod.plan_sync([local], remote,
                                engine_mod.POLICY_KEEP_BOTH)[0].kind == 'skip'
    action = engine_mod.plan_sync([local], remote, engine_mod.POLICY_OVERWRITE)[0]
    assert action.kind == 'conflict' and '覆盖' in action.error


def test_plan_sync_duplicate_differs_by_md5_size_mode_skips_strict_conflicts():
    local = engine_mod.LocalFile('books/x.pdf', Path('x'), 5,
                                 md5=hashlib.md5(b'local').hexdigest())
    remote = {'books/x.pdf': {'ambiguous': True, 'candidates': [
        {'fileid': 1, 'size': 5, 'md5': hashlib.md5(b'other1').hexdigest()},
        {'fileid': 2, 'size': 7, 'md5': hashlib.md5(b'other2').hexdigest()}]}}
    # size-only mode: a same-size copy is presumed identical without hashing
    assert engine_mod.plan_sync([local], remote, engine_mod.POLICY_SKIP_SAME_SIZE)[0].kind == 'skip'
    for policy in engine_mod.POLICIES:
        action = engine_mod.plan_sync([local], remote, policy, strict_verify=True)[0]
        assert action.kind == 'conflict'
        assert '同名' in action.error and '云盘' in action.error


def test_plan_sync_duplicate_lacks_digest_size_mode_skips_strict_conflicts():
    local = engine_mod.LocalFile('books/x.pdf', Path('x'), 5, md5=None)
    remote = {'books/x.pdf': {'ambiguous': True,
                              'candidates': [{'fileid': 1, 'size': 5, 'md5': None}]}}
    assert engine_mod.plan_sync([local], remote,
                                engine_mod.POLICY_SKIP_SAME_SIZE)[0].kind == 'skip'
    assert engine_mod.plan_sync([local], remote,
                                engine_mod.POLICY_SKIP_SAME_SIZE, strict_verify=True)[0].kind == 'conflict'


def test_build_remote_map_collects_duplicate_names(local_tree):
    client = FakeClient(remote={
        '0': [FakeItem(1, 'test', 1)],
        '1': [FakeItem(2, 'target', 1)],
        '2': [FakeItem(503, 'a.txt', 0, 3, hashlib.md5(b'aaa').hexdigest()),
              FakeItem(504, 'a.txt', 0, 3, hashlib.md5(b'aaa').hexdigest()),
              FakeItem(500, 'Season 1', 1)],
        '500': [FakeItem(501, 'e01.mkv', 0, 100, hashlib.md5(b'x' * 100).hexdigest())],
    })
    pair = make_pair(local_tree.parent)
    state = engine_mod.RunState()
    eng = engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state)
    remote_map = eng._build_remote_map(pair)
    entry = remote_map['a.txt']
    assert entry.get('ambiguous') is True and len(entry['candidates']) == 2
    assert remote_map['Season 1/e01.mkv'].get('ambiguous') is None


def test_engine_duplicate_identical_copy_skips_and_rest_still_syncs(local_tree):
    client = FakeClient(remote={
        '0': [FakeItem(1, 'test', 1)],
        '1': [FakeItem(2, 'target', 1)],
        '2': [FakeItem(503, 'a.txt', 0, 3, hashlib.md5(b'aaa').hexdigest()),
              FakeItem(504, 'a.txt', 0, 3, hashlib.md5(b'aaa').hexdigest()),
              FakeItem(500, 'Season 1', 1)],
        '500': [FakeItem(501, 'e01.mkv', 0, 100, hashlib.md5(b'x' * 100).hexdigest()),
                FakeItem(502, 'e02.mkv', 0, 200, hashlib.md5(b'y' * 200).hexdigest())],
    })
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    state, summary = run_engine(client, [pair])
    snap = state.snapshot()
    assert summary['phase'] == engine_mod.PHASE_FINISHED
    assert snap['counters']['skipped'] == 3      # duplicated a.txt + e01 + e02
    assert snap['counters']['done'] == 0 and snap['counters']['failed'] == 0
    assert client.uploads == [] and not client.trashed


def test_engine_duplicate_size_mode_skips_by_size_strict_conflicts(local_tree):
    remote = {
        '0': [FakeItem(1, 'test', 1)],
        '1': [FakeItem(2, 'target', 1)],
        '2': [FakeItem(503, 'a.txt', 0, 3, hashlib.md5(b'old-one').hexdigest()),
              FakeItem(504, 'a.txt', 0, 4, hashlib.md5(b'old-two').hexdigest()),
              FakeItem(500, 'Season 1', 1)],
        '500': [FakeItem(501, 'e01.mkv', 0, 100, hashlib.md5(b'x' * 100).hexdigest()),
                FakeItem(502, 'e02.mkv', 0, 200, hashlib.md5(b'y' * 200).hexdigest())],
    }
    pair = make_pair(local_tree.parent, exclude_ext=['.nfo'])
    # size-only mode: the size-3 copy is presumed identical, no failure
    client = FakeClient(remote=remote)
    state, summary = run_engine(client, [pair])
    snap = state.snapshot()
    assert summary['phase'] == engine_mod.PHASE_FINISHED
    assert snap['counters']['failed'] == 0 and snap['counters']['skipped'] == 3
    # strict mode: MD5 mismatch against both copies -> single-file conflict
    client = FakeClient(remote=remote)
    state = engine_mod.RunState()
    summary = engine_mod.SyncEngine(client, lambda _: None, threading.Event(), state,
                                    verify_md5=True).run([pair])
    snap = state.snapshot()
    assert summary['phase'] == engine_mod.PHASE_FINISHED
    assert snap['counters']['failed'] == 1 and snap['counters']['skipped'] == 2
    failed = next(f for f in snap['files'] if f['state'] == 'failed')
    assert failed['path'] == 'a.txt' and '同名' in failed['error']
    assert not client.uploads and not client.trashed


def test_engine_pair_listing_failure_does_not_block_other_pairs(local_tree, tmp_path):
    missing = make_pair(tmp_path, id='bad', name='坏任务对',
                        source_path=str(tmp_path / 'missing'))
    good = make_pair(local_tree.parent, id='good', name='好任务对', exclude_ext=['.nfo'])
    client = FakeClient(remote={'0': [FakeItem(1, 'test', 1)]})
    state, summary = run_engine(client, [missing, good])
    snap = state.snapshot()
    assert snap['pairs']['bad']['phase'] == engine_mod.PHASE_ERROR
    assert snap['pairs']['good']['phase'] == engine_mod.PHASE_FINISHED
    assert snap['counters']['done'] == 3
    assert summary['phase'] == engine_mod.PHASE_ERROR
    assert '坏任务对' in (summary['error'] or '')


def test_monitor_fail_gives_up_after_identical_failures(tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location('p123sync_giveup_test', _PLUGIN_DIR / 'sync_journal.py')
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    journal = module.SyncJournal(tmp_path / 'giveup.sqlite3')
    file = {'path': 'a.pdf', 'size': 3, 'mtime_ns': 1}
    journal.observe_files('p', 'scope', [file], now=1000, complete=True)
    # four identical failures keep the normal 60s->8min backoff
    for offset in (2000, 2100, 2200, 2300):
        journal.monitor_fail('p', 'scope', 'a.pdf', 3, 1, '同名冲突', now=offset)
    assert journal.monitor_counts('p', 'scope')['gave_up'] == 0
    assert journal.monitor_ready('p', 'scope', 3000, 120) == [file]  # backoff expires
    # the fifth identical failure pauses auto-retry instead of delaying again
    journal.monitor_fail('p', 'scope', 'a.pdf', 3, 1, '同名冲突', now=3000)
    assert journal.monitor_counts('p', 'scope')['gave_up'] == 1
    assert journal.monitor_ready('p', 'scope', 3000 + 10 ** 6, 120) == []
    # a different error restarts the escalation instead of staying paused
    journal.monitor_fail('p', 'scope', 'a.pdf', 3, 1, '网络错误', now=3100)
    assert journal.monitor_counts('p', 'scope')['gave_up'] == 0
    assert journal.monitor_ready('p', 'scope', 3300, 120) == [file]
    # a changed file clears the pause via observe_files
    journal.observe_files('p', 'scope', [{'path': 'a.pdf', 'size': 9, 'mtime_ns': 2}], now=3400, complete=True)
    assert journal.monitor_counts('p', 'scope')['gave_up'] == 0
    journal.close()


# ------------------------------------------------------- md5 prefetch pipeline

def test_prefetch_hashes_next_file_during_current_upload(local_tree):
    """Upcoming MD5s are computed by background workers while a file uploads."""
    order: list[tuple[str, str]] = []
    lock = threading.Lock()
    e01_hashed = threading.Event()

    class Cloud(FakeClient):
        def _file_md5(self, path, cancel_check=None, on_progress=None):
            with lock:
                order.append(('hash_start', path.name))
            digest = super()._file_md5(path, cancel_check, on_progress)
            if path.name == 'e01.mkv':
                e01_hashed.set()
            return digest

        def upload(self, local_path, parent_file_id, name, on_progress=None,
                   cancel_check=None, file_md5=None, on_stage=None):
            with lock:
                order.append(('upload_start', name))
            if name == 'a.txt':
                # The first upload must wait for a digest that only a
                # background worker can produce; without the prefetch
                # pipeline this waits out the timeout and fails.
                assert e01_hashed.wait(timeout=10), 'MD5 prefetch did not overlap the upload'
            result = super().upload(local_path, parent_file_id, name, on_progress,
                                    cancel_check, file_md5, on_stage)
            with lock:
                order.append(('upload_end', name))
            return result

    client = Cloud(remote={"0": [FakeItem(1, "test", 1)]})
    state, summary = run_engine(client, [make_pair(local_tree.parent, exclude_ext=[".nfo"])])
    snap = state.snapshot()
    assert summary["phase"] == engine_mod.PHASE_FINISHED
    assert snap["counters"]["done"] == 3 and snap["counters"]["failed"] == 0
    assert order.index(('hash_start', 'e01.mkv')) < order.index(('upload_end', 'a.txt'))


def test_prefetch_wait_reuses_inflight_hash_instead_of_rehashing(local_tree):
    class StaggeredHash(FakeClient):
        def __init__(self, remote=None):
            super().__init__(remote)
            self.hash_calls: list[str] = []

        def _file_md5(self, path, cancel_check=None, on_progress=None):
            self.hash_calls.append(path.name)
            # e01 hashes slower than a.txt uploads, so the executing thread
            # reaches e01 while its background hash is still in flight.
            time.sleep(0.8 if path.name == 'e01.mkv' else 0.02)
            return super()._file_md5(path, cancel_check, on_progress)

    client = StaggeredHash(remote={"0": [FakeItem(1, "test", 1)]})
    state, summary = run_engine(client, [make_pair(local_tree.parent, exclude_ext=[".nfo"])])
    assert summary["phase"] == engine_mod.PHASE_FINISHED
    # Exactly one full read per file: in-flight hashes are awaited, not rerun.
    assert sorted(client.hash_calls) == ["a.txt", "e01.mkv", "e02.mkv"]


def test_prefetch_background_failure_is_reported_by_inline_rerun(local_tree):
    class BrokenHash(FakeClient):
        def __init__(self, remote=None):
            super().__init__(remote)
            self.hash_calls: dict[str, int] = {}

        def _file_md5(self, path, cancel_check=None, on_progress=None):
            self.hash_calls[path.name] = self.hash_calls.get(path.name, 0) + 1
            if path.name == 'e01.mkv':
                raise OSError('simulated read failure')
            return super()._file_md5(path, cancel_check, on_progress)

    client = BrokenHash(remote={"0": [FakeItem(1, "test", 1)]})
    state, summary = run_engine(client, [make_pair(local_tree.parent, exclude_ext=[".nfo"])])
    snap = state.snapshot()
    assert summary["phase"] == engine_mod.PHASE_FINISHED
    assert snap["counters"]["failed"] == 1 and snap["counters"]["done"] == 2
    # The background attempt failed silently; the inline re-run raised it.
    assert client.hash_calls["e01.mkv"] == 2
