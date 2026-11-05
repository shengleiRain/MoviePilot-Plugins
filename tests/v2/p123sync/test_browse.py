"""Directory browsing contracts with isolated host and 123 client fakes."""
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def plugin_module(monkeypatch):
    class Scheduler:
        calls = []
        def update_plugin_job(self, plugin_id):
            self.calls.append(plugin_id)
    modules = {
        'apscheduler': {}, 'apscheduler.triggers': {},
        'apscheduler.triggers.cron': {'CronTrigger': object},
        'apscheduler.triggers.interval': {'IntervalTrigger': lambda **kwargs: kwargs},
        'fastapi': {'Body': lambda default=None: default},
        'app': {}, 'app.log': {'logger': SimpleNamespace(info=lambda *a: None, error=lambda *a: None, warn=lambda *a: None)},
        'app.plugins': {'_PluginBase': object}, 'app.schemas': {},
        'app.scheduler': {'Scheduler': Scheduler},
        'app.schemas.types': {'NotificationType': SimpleNamespace(Plugin='plugin')},
        'p123client': {'P123Client': object, 'check_response': lambda x: None},
    }
    for name, attrs in modules.items():
        stub = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(stub, key, value)
        monkeypatch.setitem(sys.modules, name, stub)
    root = Path(__file__).resolve().parents[3] / 'plugins.v2' / 'p123sync'
    spec = importlib.util.spec_from_file_location('_p123sync_browse_test', root / '__init__.py', submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_local_lists_only_folders_and_rejects_missing(plugin_module, tmp_path):
    (tmp_path / 'Movies').mkdir()
    (tmp_path / 'file.txt').write_text('x')
    plugin = plugin_module.P123Sync()
    result = plugin.api_browse_local(plugin_module.BrowseRequest(path=str(tmp_path)))
    assert result['success'] and result['folders'] == ['Movies']
    assert not plugin.api_browse_local(plugin_module.BrowseRequest(path=str(tmp_path / 'missing')))['success']
    assert not plugin.api_browse_local(plugin_module.BrowseRequest(path='relative'))['success']


def test_remote_browsing_and_login_use_isolated_cached_session(plugin_module, monkeypatch):
    clients = []
    class Client:
        def __init__(self, passport, password):
            self.credentials = (passport, password)
            self.client = self
            self.closed = False
            self.calls = []
            clients.append(self)
        def close(self): self.closed = True
        def login_ok(self): return True, 'ok'
        def user_info(self): return {'used': 10, 'total': 100, 'available': 90}
        def list_dir(self, parent):
            self.calls.append(parent)
            return {0: [SimpleNamespace(name='电影', is_dir=True, file_id=7),
                        SimpleNamespace(name='file', is_dir=False, file_id=2)],
                    7: [SimpleNamespace(name='动作', is_dir=True, file_id=8)]}[parent]
    monkeypatch.setattr(plugin_module, 'P123SyncClient', Client)
    plugin = plugin_module.P123Sync()
    sync_client = Client('sync', 'secret')
    plugin._client = sync_client
    request = plugin_module.BrowseRequest(path='/电影', passport='user', password='secret')
    assert plugin.api_browse_remote(request) == {'success': True, 'path': '/电影', 'folders': ['动作']}
    assert clients[1].calls == [0, 7]
    assert plugin.api_test_login(request)['space'] == {'used': 10, 'total': 100}
    assert len(clients) == 2 and not sync_client.closed
    assert not plugin.api_browse_remote(plugin_module.BrowseRequest(path='/missing', passport='user', password='secret'))['success']
    assert not plugin.api_browse_remote(plugin_module.BrowseRequest(path='/../x', passport='user', password='secret'))['success']
    assert not plugin.api_browse_remote(plugin_module.BrowseRequest())['success']
    plugin.api_browse_remote(plugin_module.BrowseRequest(passport='other', password='secret'))
    assert clients[1].closed and not sync_client.closed


def test_root_source_is_preserved(plugin_module):
    pair = plugin_module.TaskPairConfig.from_dict({'source_path': '/', 'target_path': '/'})
    assert pair.source_path == '/'


def test_versioned_frontend_and_dashboard_registration(plugin_module):
    plugin = plugin_module.P123Sync()
    assert plugin.plugin_version == '1.7.2'
    assert plugin.get_render_mode() == ('vue', 'dist/v1_7_2')
    assert plugin.get_dashboard() == ({'cols': 12, 'md': 6}, {'border': False}, None)


def test_versioned_federated_asset_references_exist(plugin_module):
    import re
    plugin_dir = Path(__file__).resolve().parents[3] / 'plugins.v2' / 'p123sync'
    asset_dir = plugin_dir / plugin_module.P123Sync().get_render_mode()[1]
    entry = asset_dir / 'remoteEntry.js'
    assert entry.is_file()
    for asset in asset_dir.glob('*.js'):
        content = asset.read_text(encoding='utf-8')
        for filename in re.findall(r'["\'](?:\./)?((?:__federation_|_plugin-vue_|utils-)[^"\']+\.(?:js|css))["\']', content):
            assert (asset_dir / filename).is_file(), f'{asset.name}: missing {filename}'


def test_retry_endpoint_keeps_other_failures_and_rejects_stale_config(plugin_module, monkeypatch, tmp_path):
    started = []
    class FakeThread:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def start(self): started.append(self.kwargs)
    monkeypatch.setattr(plugin_module.threading, 'Thread', FakeThread)
    plugin = plugin_module.P123Sync()
    pair = plugin_module.TaskPairConfig(id='p1', name='Test', source_path=str(tmp_path),
                                        target_path='/backup', policy='overwrite')
    plugin._pairs = [pair]
    plugin._state.begin([pair])
    plugin._state.add_files([
        {'key':f'p1:{name}', 'pair_id':'p1', 'path':name, 'size':1,
         'state':'pending', 'percent':0, 'speed':0, 'error':None}
        for name in ('a.mkv', 'b.mkv')
    ])
    plugin._state.mark_file('p1:a.mkv', 'failed', error='a failed')
    plugin._state.mark_file('p1:b.mkv', 'failed', error='b failed')
    plugin._state.pair_update('p1', failed=2, total=2)
    plugin._state.finish(plugin_module.PHASE_FINISHED)
    original_id = plugin._state.run_id
    result = plugin.api_retry(plugin_module.RetryRequest(run_id=original_id, file_keys=['p1:a.mkv']))
    assert result['success'] and len(started) == 1
    snap = plugin._state.snapshot()
    assert snap['run_id'] != original_id and snap['running']
    assert {f['key']: f['state'] for f in snap['files']} == {'p1:a.mkv':'pending', 'p1:b.mkv':'failed'}
    assert not plugin.api_retry(plugin_module.RetryRequest(run_id=snap['run_id']))['success']
    # Simulate a completed worker and an intervening configuration edit.
    plugin._run_in_progress = False
    plugin._state.finish(plugin_module.PHASE_ERROR, 'stopped')
    assert not plugin.api_retry(plugin_module.RetryRequest(run_id=original_id))['success']
    pair.target_path = '/changed'
    result = plugin.api_retry(plugin_module.RetryRequest(run_id=plugin._state.run_id))
    assert not result['success'] and '配置已更改' in result['message']


def test_single_flight_starts_before_worker_runs(plugin_module, monkeypatch, tmp_path):
    class FakeThread:
        def __init__(self, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(plugin_module.threading, 'Thread', FakeThread)
    plugin = plugin_module.P123Sync()
    pair = plugin_module.TaskPairConfig(id='p', name='Test', source_path=str(tmp_path), target_path='/')
    plugin._pairs = [pair]
    assert plugin._start_run([pair], 'manual')[0]
    assert plugin._state.snapshot()['running']
    assert not plugin._start_run([pair], 'manual')[0]


def test_running_settings_are_queued_and_worker_client_is_not_closed(plugin_module, monkeypatch, tmp_path):
    from types import SimpleNamespace
    plugin = plugin_module.P123Sync()
    plugin._run_in_progress = True
    plugin._passport = 'old-account'
    updates = []
    plugin.update_config = lambda config: updates.append(config)
    stored = {}
    plugin.save_data = lambda key, value: stored.__setitem__(key, value)
    plugin.get_data = lambda key: stored.get(key)
    plugin.del_data = lambda key: stored.pop(key, None)
    response = plugin.api_save_config(plugin_module.SaveConfigRequest(config={'passport':'new-account'}))
    assert response['success'] and response['deferred']
    assert stored[plugin_module.PENDING_CONFIG_KEY]['passport'] == 'new-account'
    assert updates == [] and plugin._passport == 'old-account'
    plugin.init_plugin({'passport':'new-account'})
    assert plugin._passport == 'old-account' and plugin._pending_config['passport'] == 'new-account'
    closed = []
    plugin._client = SimpleNamespace(client=SimpleNamespace(close=lambda: closed.append(True)))
    class BusyWorker:
        def is_alive(self): return True
        def join(self, timeout): pass
    plugin._worker = BusyWorker()
    monkeypatch.setattr(plugin_module.logger, 'warn', lambda *_: None, raising=False)
    plugin.stop_service()
    assert plugin._cancel_event.is_set() and not closed and plugin._client is not None


def test_pending_recovery_blocks_changing_account_and_starting_new_run(plugin_module, tmp_path):
    plugin = plugin_module.P123Sync()
    pair = plugin_module.TaskPairConfig(id='p1', name='Test', source_path=str(tmp_path), target_path='/backup')
    plugin._pairs = [pair]
    plugin._passport, plugin._password = 'old', 'secret'
    plugin._state.begin([pair])
    plugin._state.add_files([{'key':'p1:a','pair_id':'p1','path':'a','size':1,'state':'pending','percent':0,'speed':0,'error':None}])
    plugin._state.patch_file('p1:a', transfer={'stage':'temporary'})
    plugin._state.mark_file('p1:a', 'failed', error='uncertain')
    plugin._state.finish('finished')
    assert not plugin._start_run([pair], 'manual')[0]
    plugin.update_config = lambda config: (_ for _ in ()).throw(AssertionError('must not save'))
    response = plugin.api_save_config(plugin_module.SaveConfigRequest(config={'passport':'different','password':'secret','tasks':[]}))
    assert not response['success'] and '待核对' in response['message']


def test_client_rejects_incomplete_pagination(plugin_module, monkeypatch):
    client = plugin_module.P123SyncClient('account', 'secret', page_interval=0)
    monkeypatch.setattr(client, '_checked', lambda *_, **__: {'data': {'InfoList': [], 'Next': '', 'Total': 2}})
    with pytest.raises(plugin_module.P123ClientError, match='分页中断'):
        client.list_dir(0)
    monkeypatch.setattr(client, '_checked', lambda *_, **__: {'data': {'InfoList': []}})
    with pytest.raises(plugin_module.P123ClientError, match='缺少列表或总数'):
        client.list_dir(0)
    monkeypatch.setattr(client, '_checked', lambda *_, **__: {'data': {'InfoList': [], 'Next': '', 'Total': 0}})
    assert client.list_dir(0) == []


def test_page_based_directory_listing_collects_all_508_entries(plugin_module, monkeypatch):
    client = plugin_module.P123SyncClient('account', 'secret', page_interval=0)
    calls = []
    def checked(method, path, *, params):
        assert method == 'request' and path == 'file/list/new' and params['driveId'] == 0
        page = params['Page']; calls.append(page)
        start = (page-1)*100
        return {'data': {'InfoList': [{'FileId':i, 'FileName':str(i), 'Type':0} for i in range(start, min(start+100,508))],
                         'Total':508, 'Next':-1 if page == 6 else ''}}
    monkeypatch.setattr(client, '_checked', checked)
    assert len(client.list_dir(7)) == 508 and calls == [1,2,3,4,5,6]


def test_page_based_listing_rejects_duplicate_ids_and_changed_totals(plugin_module, monkeypatch):
    client = plugin_module.P123SyncClient('account', 'secret', page_interval=0)
    monkeypatch.setattr(client, '_checked', lambda *_, **__: {'data': {
        'InfoList':[{'FileId':1,'FileName':'a'},{'FileId':1,'FileName':'a'}], 'Total':2}})
    with pytest.raises(plugin_module.P123ClientError, match='分页重复'):
        client.list_dir(7)
    def changed(_method, _path, *, params):
        page=params['Page']
        return {'data': {'InfoList':[{'FileId':page,'FileName':str(page)}], 'Total':2 if page==1 else 3}}
    monkeypatch.setattr(client, '_checked', changed)
    with pytest.raises(plugin_module.P123ClientError, match='列举期间发生变化'):
        client.list_dir(7)


def test_directory_rate_limit_retries_and_cancellation(plugin_module, monkeypatch):
    client = plugin_module.P123SyncClient('account', 'secret', page_interval=0)
    calls, waits = [], []
    def checked(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise plugin_module.P123ClientError('throttled', service_code=100011, operation='request')
        return {'data': {'InfoList':[], 'Total':0, 'Next':''}}
    monkeypatch.setattr(client, '_checked', checked)
    monkeypatch.setattr(client, '_wait', lambda seconds, cancel=None: waits.append(seconds))
    assert client.list_dir(7) == [] and len(calls) == 3
    assert 2 in waits and 4 in waits
    with pytest.raises(KeyboardInterrupt):
        client.list_dir(7, cancel_check=lambda: True)


def test_client_instant_upload_does_not_count_as_network_transfer(plugin_module, monkeypatch, tmp_path):
    client = plugin_module.P123SyncClient('account', 'secret', page_interval=0)
    sample = tmp_path / 'a.mkv'
    sample.write_bytes(b'content')
    requests = []
    def checked(method, payload):
        requests.append((method, payload))
        return {'data': {'Reuse':True, 'Info':{'FileId':123}}}
    monkeypatch.setattr(client, '_checked', checked)
    progress, stages = [], []
    assert client.upload(sample, 0, 'a.mkv', on_progress=progress.append, on_stage=stages.append) == (123, True)
    assert requests[0][1]['duplicate'] == 0
    assert progress == []
    assert stages == ['checking_instant']


def test_md5_reports_real_bytes_read(plugin_module, tmp_path):
    import hashlib
    client = plugin_module.P123SyncClient('account', 'secret')
    sample = tmp_path / 'sample.bin'; content = b'a' * (1024 * 1024 + 7); sample.write_bytes(content)
    progress = []
    assert client._file_md5(sample, on_progress=progress.append) == hashlib.md5(content).hexdigest()
    assert progress and progress[-1] == len(content)
    assert progress == sorted(progress) and all(0 < count <= len(content) for count in progress)


def test_upload_reports_request_transfer_and_finalization_stages(plugin_module, monkeypatch, tmp_path):
    client = plugin_module.P123SyncClient('account', 'secret')
    sample = tmp_path / 'sample.bin'; sample.write_bytes(b'abcde')
    def checked(name, payload):
        if name == 'upload_request': return {'data': {'Reuse':False, 'SliceSize':2}}
        if name == 'upload_prepare': return {'data': {'presignedUrls': {str(payload['partNumberStart']):'url'}}}
        if name == 'upload_complete': return {'data': {'file_info': {'FileId':123}}}
        raise AssertionError(name)
    monkeypatch.setattr(client, '_checked', checked)
    monkeypatch.setattr(client, '_put', lambda url, data: None)
    stages, progress = [], []
    assert client.upload(sample, 0, 'sample.bin', file_md5='digest', on_stage=stages.append,
                         on_progress=progress.append) == (123, False)
    assert stages[0] == 'checking_instant' and stages[-1] == 'completing_upload'
    assert stages.count('preparing_upload') == stages.count('uploading') == 3
    assert progress == [40.0, 80.0, 100.0]


def test_cloud_error_keeps_numeric_code_without_exposing_response(plugin_module, monkeypatch):
    client = plugin_module.P123SyncClient('account', 'secret')
    client.client = SimpleNamespace(call=lambda *args, **kwargs: {
        'code': 429, 'message': 'private secret', 'data': {'token': 'private token'}})
    client_module = sys.modules[plugin_module.P123SyncClient.__module__]
    monkeypatch.setattr(client_module, 'check_response',
                        lambda resp: (_ for _ in ()).throw(OSError(str(resp))))
    with pytest.raises(plugin_module.P123ClientError) as error:
        client._checked('upload_request')
    assert '云盘错误码 429' in str(error.value)
    assert error.value.service_code == 429 and error.value.operation == 'upload_request'
    assert 'private' not in str(error.value)


def test_cloud_name_search_filters_parent_and_follows_pages(plugin_module, monkeypatch):
    client = plugin_module.P123SyncClient('account', 'secret', page_interval=0)
    calls = []
    def checked(_name, _path, *, params):
        calls.append(params)
        if params['Page'] == 1:
            return {'data': {'InfoList': [{'FileId':1,'FileName':'a','Type':0,'Size':3,'ParentFileId':99}], 'Next':'', 'Total':3}}
        return {'data': {'InfoList': [{'FileId':2,'FileName':'a','Type':0,'Size':3,'ParentFileId':7},
                                      {'FileId':3,'FileName':'a-folder','Type':0,'Size':3,'ParentFileId':7}], 'Next':-1, 'Total':3}}
    monkeypatch.setattr(client, '_checked', checked)
    assert [item.file_id for item in client.find_files_by_name(7, 'a')] == [2]
    assert len(calls) == 2


def test_client_retry_wait_is_cancellable(plugin_module, monkeypatch):
    client = plugin_module.P123SyncClient('account', 'secret')
    monkeypatch.setattr(client, '_put', lambda *_: (_ for _ in ()).throw(OSError('network')))
    calls = []
    def cancelled():
        calls.append(1)
        return len(calls) >= 2
    with pytest.raises(KeyboardInterrupt):
        client._put_with_retry({'data': {'presignedUrls': {'1':'url'}}}, {}, '1', b'x', cancelled)


def test_deferred_settings_survive_restart_and_wait_for_recovery(plugin_module, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module.logger, 'warn', lambda *_: None, raising=False)
    monkeypatch.setattr(plugin_module.logger, 'error', lambda *_: None, raising=False)
    stored = {}
    applied = []
    def setup(plugin):
        plugin.get_data_path = lambda: tmp_path
        plugin.save_data = lambda key, value: stored.__setitem__(key, value)
        plugin.get_data = lambda key: stored.get(key)
        plugin.del_data = lambda key: stored.pop(key, None)
        plugin.update_config = lambda config: applied.append(config) or True
        return plugin
    original = {'enabled':True, 'passport':'old', 'password':'secret', 'tasks':[
        {'id':'p1', 'name':'Test', 'source_path':str(tmp_path), 'target_path':'/old'}]}
    desired = {'enabled':True, 'passport':'new', 'password':'secret', 'tasks':[
        {'id':'p1', 'name':'Test', 'source_path':str(tmp_path), 'target_path':'/new'}]}
    plugin = setup(plugin_module.P123Sync())
    plugin.init_plugin(original)
    plugin._run_in_progress = True
    plugin._state.begin(plugin._pairs)
    plugin._state.add_files([{'key':'p1:a','pair_id':'p1','path':'a','size':1,'state':'pending','percent':0,'speed':0,'error':None}])
    queued = plugin.api_save_config(plugin_module.SaveConfigRequest(config=desired))
    assert queued['success'] and queued['deferred'] and plugin._passport == 'old'
    assert plugin.api_get_config()['passport'] == 'new'
    plugin._journal.close()

    resumed = setup(plugin_module.P123Sync())
    resumed.init_plugin(original)
    assert resumed._passport == 'old' and resumed._state.snapshot()['phase'] == 'error'
    assert resumed.api_get_config()['pending_config'] and applied == []
    resumed._state.begin_retry({'p1:a'})
    resumed._state.mark_file('p1:a', 'done', percent=100, bytes_done=1)
    # The original interrupted-task warning is intentionally retained until the
    # affected task is reconciled; do not apply changed destinations prematurely.
    resumed._state.finish('finished')
    assert not resumed._apply_queued_if_safe() and resumed._passport == 'old'
    # A clean new run can resolve the task-level interruption.
    resumed._state.begin(resumed._pairs)
    resumed._state.finish('finished')
    assert resumed._apply_queued_if_safe()
    assert resumed._passport == 'new' and applied[-1]['tasks'][0]['target_path'] == '/new'
    assert plugin_module.PENDING_CONFIG_KEY not in stored
    resumed._journal.close()


def test_new_task_ids_are_persisted_when_saving(plugin_module, monkeypatch, tmp_path):
    plugin = plugin_module.P123Sync()
    saved = []
    plugin.update_config = lambda config: saved.append(config) or True
    plugin.init_plugin = lambda config: None
    result = plugin.api_save_config(plugin_module.SaveConfigRequest(config={
        'tasks':[{'source_path':str(tmp_path), 'target_path':'/movies'}]}))
    assert result['success'] and saved[0]['tasks'][0]['id']


def test_queued_config_applies_only_after_clean_completion(plugin_module, tmp_path):
    from types import SimpleNamespace
    plugin = plugin_module.P123Sync()
    data, applied, closed = {}, [], []
    plugin.save_data = lambda key, value: data.__setitem__(key, value)
    plugin.get_data = lambda key: data.get(key)
    plugin.del_data = lambda key: data.pop(key, None)
    plugin.update_config = lambda config: applied.append(config) or True
    plugin._client = SimpleNamespace(client=SimpleNamespace(close=lambda: closed.append(True)))
    plugin._run_in_progress = True
    old = plugin_module.TaskPairConfig(id='p1', name='Test', source_path=str(tmp_path), target_path='/old')
    plugin._pairs = [old]
    plugin._state.begin([old])
    result = plugin.api_save_config(plugin_module.SaveConfigRequest(config={
        'passport':'new', 'password':'secret', 'tasks':[{'id':'p1','source_path':str(tmp_path),'target_path':'/new'}]}))
    assert result['deferred'] and not closed and not applied
    plugin._state.finish('finished')
    plugin._run_in_progress = False
    assert plugin._apply_queued_if_safe()
    assert closed and applied[-1]['tasks'][0]['target_path'] == '/new'
    assert plugin.api_get_config()['passport'] == 'new' and not data


def test_reverting_queued_settings_clears_queue_but_preserves_recovery(plugin_module, tmp_path):
    plugin = plugin_module.P123Sync()
    data = {}
    plugin.save_data = lambda key, value: data.__setitem__(key, value)
    plugin.get_data = lambda key: data.get(key)
    plugin.del_data = lambda key: data.pop(key, None)
    plugin.update_config = lambda config: pytest.fail('active config must not be rewritten')
    pair = plugin_module.TaskPairConfig(id='p1', name='Test', source_path=str(tmp_path), target_path='/old')
    plugin._pairs = [pair]
    plugin._passport, plugin._password = 'old', 'secret'
    active = plugin._active_config()
    plugin._state.begin([pair])
    plugin._state.add_files([{'key':'p1:a', 'pair_id':'p1', 'path':'a', 'size':1,
                              'state':'pending', 'percent':0, 'speed':0, 'error':None}])
    transfer = {'kind':'upload', 'target':'a', 'stage':'a'}
    plugin._state.patch_file('p1:a', transfer=transfer)
    plugin._state.mark_file('p1:a', 'failed', error='uncertain')
    plugin._state.finish('finished')
    plugin._queue_config({**active, 'verify_md5':True})
    response = plugin.api_save_config(plugin_module.SaveConfigRequest(config=active))
    assert response['success'] and not response.get('deferred')
    assert plugin_module.PENDING_CONFIG_KEY not in data
    assert not plugin.api_get_config().get('pending_config')
    assert plugin._state.file_record('p1:a')['transfer'] == transfer
    assert plugin._state.file_record('p1:a')['state'] == 'failed'


def test_reverting_queue_reports_storage_failure(plugin_module):
    plugin = plugin_module.P123Sync()
    data = {}
    plugin.save_data = lambda key, value: data.__setitem__(key, value)
    plugin.get_data = lambda key: data.get(key)
    plugin.del_data = lambda key: None
    plugin._queue_config({**plugin._active_config(), 'verify_md5':True})
    result = plugin.api_save_config(plugin_module.SaveConfigRequest(config=plugin._active_config()))
    assert not result['success'] and '处理失败' in result['message']
    assert plugin._queue_error and plugin._queued_config is not None


def test_legacy_queued_settings_equal_to_active_are_cleared(plugin_module):
    plugin = plugin_module.P123Sync()
    data = {}
    plugin.save_data = lambda key, value: data.__setitem__(key, value)
    plugin.get_data = lambda key: data.get(key)
    plugin.del_data = lambda key: data.pop(key, None)
    legacy = plugin._active_config()
    legacy.pop('verify_md5')
    plugin._queue_config(legacy)
    assert plugin._apply_queued_if_safe()
    assert plugin._queued_config is None and not data


def test_deferred_config_storage_failure_does_not_report_saved(plugin_module):
    plugin = plugin_module.P123Sync()
    plugin._run_in_progress = True
    plugin.save_data = lambda *_: (_ for _ in ()).throw(OSError('storage unavailable'))
    result = plugin.api_save_config(plugin_module.SaveConfigRequest(config={'enabled':True}))
    assert not result['success'] and '无法保存待生效' in result['message']
    assert plugin._queued_config is None


def test_persistence_failure_blocks_start_without_losing_previous_state(plugin_module, tmp_path):
    class BrokenJournal:
        def checkpoint(self, *args, **kwargs): raise OSError('disk full')
        def acquire(self): return True
        def load_latest(self): return None
        def release(self): pass
    plugin = plugin_module.P123Sync()
    previous = plugin._state
    plugin._journal = BrokenJournal()
    pair = plugin_module.TaskPairConfig(id='p1', name='Test', source_path=str(tmp_path), target_path='/')
    plugin._pairs = [pair]
    ok, message = plugin._start_run([pair], 'manual')
    assert not ok and '无法保存同步初始状态' in message
    assert plugin._state is previous and not plugin._run_in_progress
    assert plugin._journal_error == 'disk full' and plugin._client is None


def test_missing_legacy_task_id_reuses_unique_recovery_identity(plugin_module, tmp_path):
    plugin = plugin_module.P123Sync()
    pair = plugin_module.TaskPairConfig(id='original-id', name='Test', source_path=str(tmp_path), target_path='/backup')
    plugin._state.begin([pair])
    saved = []
    plugin.update_config = lambda config: saved.append(config) or True
    migrated = plugin._migrate_task_ids({'tasks':[{'name':'Test','source_path':str(tmp_path),'target_path':'/backup'}]})
    assert migrated['tasks'][0]['id'] == 'original-id'
    assert saved[0]['tasks'][0]['id'] == 'original-id'


def test_ambiguous_legacy_task_id_is_not_guessed(plugin_module, tmp_path):
    plugin = plugin_module.P123Sync()
    pairs = [plugin_module.TaskPairConfig(id=pid, name='Test', source_path=str(tmp_path), target_path='/backup') for pid in ('one','two')]
    plugin._state.begin(pairs)
    plugin.update_config = lambda config: (_ for _ in ()).throw(AssertionError('must not guess'))
    with pytest.raises(RuntimeError, match='无法唯一匹配'):
        plugin._migrate_task_ids({'tasks':[{'source_path':str(tmp_path),'target_path':'/backup'}]})


def test_reload_does_not_recover_or_start_while_previous_worker_owns_lock(plugin_module, monkeypatch, tmp_path):
    data, applied = {}, []
    def setup():
        plugin = plugin_module.P123Sync()
        plugin.get_data_path = lambda: tmp_path
        plugin.save_data = lambda key, value: data.__setitem__(key, value)
        plugin.get_data = lambda key: data.get(key)
        plugin.del_data = lambda key: data.pop(key, None)
        plugin.update_config = lambda config: applied.append(config) or True
        plugin.get_config = lambda: applied[-1] if applied else original
        return plugin
    original = {'enabled':True,'passport':'old','password':'secret',
                'tasks':[{'id':'p1','name':'Test','source_path':str(tmp_path),'target_path':'/old'}]}
    old = setup()
    old.init_plugin(original)
    assert old._journal.acquire()
    old._run_in_progress = True
    old._state.begin(old._pairs)
    newer = setup()
    newer.init_plugin(original)
    assert newer._external_run_active and newer._state.snapshot()['phase'] == 'scanning'
    assert not newer._start_run(newer._pairs, 'manual')[0]
    assert not newer.api_cancel()['success']
    queued = newer.api_save_config(plugin_module.SaveConfigRequest(config={**original,'passport':'new'}))
    assert queued['success'] and queued['deferred'] and newer._passport == 'old'
    old._state.pair_update('p1', phase='finished')
    old._state.finish('finished')
    old._run_in_progress = False
    old._journal.release()
    status = newer.api_status(include_files=False)
    assert not status['external_run_active'] and status['phase'] == 'finished'
    assert newer._passport == 'new' and data == {}
    old._journal.close()
    newer._journal.close()


def test_plugin_worker_persists_and_restores_completed_sync(plugin_module, tmp_path):
    import hashlib
    from types import SimpleNamespace
    src = tmp_path / 'source'
    src.mkdir()
    (src / 'a.txt').write_bytes(b'abc')
    data = {}
    class Cloud:
        def __init__(self): self.items = []; self.uploads = 0
        def list_dir(self, parent_id, cancel_check=None): return list(self.items)
        def find_files_by_name(self, parent_id, name, cancel_check=None): return [x for x in self.items if x.name == name]
        def walk(self, parent_id, prefix='', cancel_check=None):
            for item in self.items: yield item.name, item
        def _file_md5(self, path, cancel_check=None, on_progress=None): return hashlib.md5(path.read_bytes()).hexdigest()
        def upload(self, path, parent_id, name, on_progress=None, cancel_check=None, file_md5=None, on_stage=None):
            self.uploads += 1
            if on_progress: on_progress(100)
            self.items.append(SimpleNamespace(name=name, file_id=41, size=path.stat().st_size, md5=file_md5, is_dir=False))
            return 41, False
    cloud = Cloud()
    def setup():
        plugin = plugin_module.P123Sync()
        plugin.get_data_path = lambda: tmp_path / 'plugin-data'
        plugin.save_data = lambda key, value: data.__setitem__(key, value)
        plugin.get_data = lambda key: data.get(key)
        plugin.del_data = lambda key: data.pop(key, None)
        plugin.update_config = lambda config: True
        plugin.get_config = lambda: config
        return plugin
    config = {'enabled':True,'notify':False,'passport':'account','password':'secret',
              'tasks':[{'id':'p1','name':'Test','source_path':str(src),'target_path':'/'}]}
    plugin = setup()
    plugin.init_plugin(config)
    plugin._get_client = lambda: cloud
    assert plugin.api_run(plugin_module.RunRequest())['success']
    plugin._worker.join(timeout=10)
    assert not plugin._worker.is_alive()
    assert plugin.api_status()['counters']['done'] == 1 and cloud.uploads == 1
    assert not plugin._journal.is_owned()
    plugin._journal.close()
    resumed = setup()
    resumed.init_plugin(config)
    snap = resumed.api_status()
    assert snap['phase'] == 'finished' and snap['files'][0]['path'] == 'a.txt'
    assert snap['counters']['done'] == 1 and cloud.uploads == 1
    resumed._journal.close()


def test_upload_put_http_failure_is_not_reported_as_success(plugin_module):
    client = plugin_module.P123SyncClient('account', 'secret')
    class Response:
        def raise_for_status(self): raise OSError('HTTP 503')
    client.client = SimpleNamespace(call=lambda *args, **kwargs: Response())
    with pytest.raises(OSError, match='HTTP 503'):
        client._put('https://example.test/upload', b'data')


def test_release_zip_contains_only_current_plugin_sources_and_assets(tmp_path):
    import subprocess, sys, zipfile
    root = Path(__file__).resolve().parents[3]
    output = tmp_path / 'p123sync_v1.7.2.zip'
    subprocess.run([sys.executable, str(root / '.github/scripts/package_p123sync_release.py'), str(output)],
                   cwd=root, check=True, capture_output=True, text=True)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert 'sync_journal.py' in names and 'auto_monitor.py' in names and 'dist/v1_7_2/remoteEntry.js' in names
        assert all(not name.startswith(('dist/assets/', 'dist/v1_4_0/', 'dist/v1_5_0/', 'dist/v1_6_0/')) for name in names)
        assert not any('__pycache__' in name or name.endswith('.sqlite3') for name in names)
        assert archive.testzip() is None


def test_duplicate_task_ids_are_rejected_without_persisting(plugin_module, tmp_path):
    plugin = plugin_module.P123Sync()
    saved = []
    plugin.update_config = lambda config: saved.append(config)
    item = {'id':'same', 'source_path':str(tmp_path), 'target_path':'/'}
    result = plugin.api_save_config(plugin_module.SaveConfigRequest(config={'tasks':[item, dict(item)]}))
    assert not result['success'] and '标识重复' in result['message'] and not saved


def test_downloader_monitor_only_discovers_completed_selected_mapped_files(plugin_module, tmp_path):
    from types import SimpleNamespace
    from collections import namedtuple
    monitor = sys.modules[plugin_module.__name__ + '.auto_monitor']
    root = tmp_path / 'downloads'
    (root / 'Movie').mkdir(parents=True)
    (root / 'Movie' / 'ready.mkv').write_bytes(b'abc')
    (root / 'Movie' / 'partial.mkv').write_bytes(b'abc')
    (root / 'Movie' / 'unselected.mkv').write_bytes(b'abc')
    (root / 'Movie' / 'tr-ready.mkv').write_bytes(b'xyz')
    other = tmp_path / 'outside.mkv'
    other.write_bytes(b'abc')
    pair = plugin_module.TaskPairConfig(id='p',name='Test',source_path=str(root),target_path='/',trigger_mode='downloader')
    torrent = SimpleNamespace(state='completed', progress=100, hash='hash1', downloader='qb', save_path=str(root))
    paused = SimpleNamespace(state='paused', progress=100, hash='hash2', downloader='tr', save_path=str(root))
    incomplete = SimpleNamespace(state='downloading', progress=50, hash='hash3', downloader='qb', save_path=str(root))
    TrFile = namedtuple('TrFile', 'name size completed priority selected')
    class Chain:
        def list_torrents(self, **kwargs):
            assert kwargs['include_all_tags'] and kwargs['status'] == 'all'
            return [torrent, paused, incomplete]
        def torrent_files(self, tid, downloader):
            if tid == 'hash2':
                return [TrFile('Movie/tr-ready.mkv',3,3,0,True)]
            return [
                {'name':'Movie/ready.mkv','size':3,'progress':1,'priority':1},
                {'name':'Movie/partial.mkv','size':3,'progress':0.5,'priority':1},
                {'name':'Movie/unselected.mkv','size':3,'progress':1,'priority':0},
                {'name':'../outside.mkv','size':3,'progress':1,'priority':1},
                {'name':'Movie/not-there.mkv','size':3,'progress':1,'priority':1},
            ]
    results, warnings = monitor.discover_downloads([pair], Chain(), 'all')
    assert {item['path'] for item in results['p']} == {'Movie/ready.mkv','Movie/tr-ready.mkv'}
    assert warnings


def test_directory_monitor_waits_for_stability_and_acknowledges_exact_files(plugin_module, tmp_path):
    import hashlib, threading, time
    from types import SimpleNamespace
    monitor = sys.modules[plugin_module.__name__ + '.auto_monitor']
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'ready.mkv').write_bytes(b'abc')
    data = {}
    class Cloud:
        def __init__(self): self.items=[]; self.uploads=[]
        def list_dir(self, parent_id, cancel_check=None): return list(self.items)
        def find_files_by_name(self, parent_id, name, cancel_check=None): return [x for x in self.items if x.name == name]
        def walk(self, parent_id, prefix='', cancel_check=None):
            for item in self.items: yield item.name,item
        def _file_md5(self, path, cancel_check=None, on_progress=None): return hashlib.md5(path.read_bytes()).hexdigest()
        def upload(self,path,parent_id,name,on_progress=None,cancel_check=None,file_md5=None,on_stage=None):
            self.uploads.append(name)
            if on_progress: on_progress(100)
            self.items.append(SimpleNamespace(name=name,file_id=101,size=path.stat().st_size,md5=file_md5,is_dir=False))
            return 101,False
    cloud=Cloud()
    plugin=plugin_module.P123Sync()
    plugin.get_data_path=lambda:tmp_path/'plugin-data'
    plugin.save_data=lambda key,value:data.__setitem__(key,value)
    plugin.get_data=lambda key:data.get(key)
    plugin.del_data=lambda key:data.pop(key,None)
    plugin.update_config=lambda config: True
    plugin._ensure_monitor=lambda:None
    config={'enabled':True,'notify':False,'passport':'account','password':'secret',
            'tasks':[{'id':'p','name':'Test','source_path':str(source),'target_path':'/',
                      'trigger_mode':'directory','settle_seconds':30}]}
    plugin.init_plugin(config)
    plugin._get_client=lambda:cloud
    stop=threading.Event()
    now=time.time()
    plugin._monitor_tick(stop,now=now)
    assert not cloud.uploads and plugin._monitor_status['p']['pending'] == 1
    plugin._monitor_tick(stop,now=now+31)
    plugin._worker.join(timeout=10)
    assert cloud.uploads == ['ready.mkv']
    scope=monitor.scope_for(plugin._pairs[0], plugin._passport)
    assert plugin._journal.monitor_counts('p',scope)['pending'] == 0
    plugin._monitor_tick(stop,now=now+62)
    assert cloud.uploads == ['ready.mkv']
    plugin._journal.close()


def test_monitor_modes_default_to_manual_and_validate_values(plugin_module, tmp_path):
    old=plugin_module.TaskPairConfig.from_dict({'source_path':str(tmp_path),'target_path':'/'})
    assert old.trigger_mode == 'manual'
    plugin=plugin_module.P123Sync()
    result=plugin.api_save_config(plugin_module.SaveConfigRequest(config={'tasks':[{'source_path':str(tmp_path),'trigger_mode':'unknown'}]}))
    assert not result['success'] and '同步方式无效' in result['message']
    result=plugin.api_save_config(plugin_module.SaveConfigRequest(config={'tasks':[{'source_path':str(tmp_path),'trigger_mode':'directory','settle_seconds':5}]}))
    assert not result['success'] and '30 到 3600' in result['message']


def test_capacity_includes_permanent_and_temporary_space(plugin_module, monkeypatch):
    client=plugin_module.P123SyncClient('account','secret')
    monkeypatch.setattr(client, '_checked', lambda *_: {'data':{'SpacePermanent':100,'SpaceTemp':25,'SpaceUsed':40}})
    assert client.user_info() == {'total':125,'used':40,'available':85}
    monkeypatch.setattr(client, '_checked', lambda *_: {'data':{'SpaceUsed':40}})
    with pytest.raises(plugin_module.P123ClientError, match='未返回完整'):
        client.user_info()


def test_monitor_retries_lost_upload_response_after_backoff_without_duplicate(plugin_module, tmp_path):
    import hashlib, threading, time
    from types import SimpleNamespace
    monitor = sys.modules[plugin_module.__name__ + '.auto_monitor']
    source=tmp_path/'source'; source.mkdir(); (source/'a.mkv').write_bytes(b'abc')
    data={}
    class Cloud:
        def __init__(self): self.items=[]; self.uploads=0
        def list_dir(self,parent_id,cancel_check=None): return list(self.items)
        def find_files_by_name(self,parent_id,name,cancel_check=None): return [x for x in self.items if x.name==name]
        def walk(self,parent_id,prefix='',cancel_check=None):
            for item in self.items: yield item.name,item
        def _file_md5(self,path,cancel_check=None,on_progress=None): return hashlib.md5(path.read_bytes()).hexdigest()
        def upload(self,path,parent_id,name,on_progress=None,cancel_check=None,file_md5=None,on_stage=None):
            self.uploads += 1
            self.items.append(SimpleNamespace(name=name,file_id=201,size=path.stat().st_size,md5=file_md5,is_dir=False))
            raise RuntimeError('response lost')
    cloud=Cloud(); plugin=plugin_module.P123Sync()
    plugin.get_data_path=lambda:tmp_path/'plugin-data'
    plugin.save_data=lambda key,value:data.__setitem__(key,value)
    plugin.get_data=lambda key:data.get(key)
    plugin.del_data=lambda key:data.pop(key,None)
    plugin.update_config=lambda config:True
    plugin._ensure_monitor=lambda:None
    config={'enabled':True,'notify':False,'passport':'account','password':'secret',
            'tasks':[{'id':'p','name':'Test','source_path':str(source),'target_path':'/',
                      'trigger_mode':'directory','settle_seconds':30}]}
    plugin.init_plugin(config); plugin._get_client=lambda:cloud
    stop=threading.Event(); now=time.time()
    plugin._monitor_tick(stop,now=now)
    plugin._monitor_tick(stop,now=now+31)
    plugin._worker.join(timeout=10)
    assert cloud.uploads == 1 and plugin._state.snapshot()['counters']['failed'] == 1
    plugin._monitor_tick(stop,now=now+32)
    assert cloud.uploads == 1
    plugin._monitor_tick(stop,now=now+100)
    plugin._worker.join(timeout=10)
    snap=plugin._state.snapshot()
    assert cloud.uploads == 1 and snap['counters']['failed'] == 0
    scope=monitor.scope_for(plugin._pairs[0],plugin._passport)
    assert plugin._journal.monitor_counts('p',scope)['pending'] == 0
    plugin._journal.close()


def test_cron_does_not_full_scan_automatic_monitor_tasks(plugin_module, monkeypatch, tmp_path):
    plugin=plugin_module.P123Sync()
    plugin._enabled=True; plugin._cron='0 3 * * *'
    plugin._pairs=[
        plugin_module.TaskPairConfig(id='manual',name='Manual',source_path=str(tmp_path),target_path='/',trigger_mode='manual'),
        plugin_module.TaskPairConfig(id='down',name='Downloader',source_path=str(tmp_path),target_path='/',trigger_mode='downloader'),
        plugin_module.TaskPairConfig(id='dir',name='Directory',source_path=str(tmp_path),target_path='/',trigger_mode='directory'),
    ]
    monkeypatch.setattr(plugin_module, 'CronTrigger', SimpleNamespace(from_crontab=lambda text:text))
    captured=[]
    plugin._sync_worker=lambda pairs, state, stop: captured.extend(p.id for p in pairs)
    services=plugin.get_service()
    assert {item['id'] for item in services} == {'P123SyncSchedule','P123SyncMonitorWatchdog'}
    plugin._scheduled_sync()
    assert captured == ['manual']


def test_downloader_monitor_queues_and_syncs_only_confirmed_completed_files(plugin_module, tmp_path):
    import hashlib, threading, time
    source=tmp_path/'downloads'; source.mkdir()
    (source/'complete.mkv').write_bytes(b'abc')
    (source/'partial.mkv').write_bytes(b'xyz')
    task=SimpleNamespace(state='completed',progress=100,hash='hash1',downloader='qb',save_path=str(source))
    class Chain:
        def list_torrents(self, **kwargs): return [task]
        def torrent_files(self, tid, downloader):
            return [{'name':'complete.mkv','size':3,'progress':1,'priority':1},
                    {'name':'partial.mkv','size':3,'progress':0.5,'priority':1}]
    class Cloud:
        def __init__(self): self.items=[]; self.uploads=[]
        def list_dir(self,parent_id,cancel_check=None): return list(self.items)
        def find_files_by_name(self,parent_id,name,cancel_check=None): return [x for x in self.items if x.name == name]
        def walk(self,parent_id,prefix='',cancel_check=None):
            for item in self.items: yield item.name,item
        def _file_md5(self,path,cancel_check=None,on_progress=None): return hashlib.md5(path.read_bytes()).hexdigest()
        def upload(self,path,parent_id,name,on_progress=None,cancel_check=None,file_md5=None,on_stage=None):
            self.uploads.append(name)
            if on_progress: on_progress(100)
            self.items.append(SimpleNamespace(name=name,file_id=8,size=3,md5=file_md5,is_dir=False))
            return 8,False
    data={}; cloud=Cloud(); plugin=plugin_module.P123Sync()
    plugin.get_data_path=lambda:tmp_path/'plugin-data'
    plugin.save_data=lambda key,value:data.__setitem__(key,value)
    plugin.get_data=lambda key:data.get(key)
    plugin.del_data=lambda key:data.pop(key,None)
    plugin.update_config=lambda config:True
    plugin._ensure_monitor=lambda:None
    plugin.init_plugin({'enabled':True,'notify':False,'passport':'account','password':'secret','tasks':[
        {'id':'p','source_path':str(source),'target_path':'/','trigger_mode':'downloader','settle_seconds':30}]})
    plugin._get_client=lambda:cloud
    plugin._get_download_monitor=lambda:(Chain(),'all')
    now=time.time(); stop=threading.Event()
    plugin._monitor_tick(stop,now=now)
    plugin._monitor_tick(stop,now=now+31)
    plugin._worker.join(timeout=10)
    assert cloud.uploads == ['complete.mkv']
    assert plugin._state.snapshot()['counters']['total_files'] == 1
    plugin._journal.close()


def test_removed_downloader_task_stays_queued_without_guessing(plugin_module, tmp_path):
    import threading, time
    monitor=sys.modules[plugin_module.__name__ + '.auto_monitor']
    source=tmp_path/'downloads'; source.mkdir(); path=source/'a.mkv'; path.write_bytes(b'abc')
    data={}; plugin=plugin_module.P123Sync()
    plugin.get_data_path=lambda:tmp_path/'plugin-data'
    plugin.save_data=lambda key,value:data.__setitem__(key,value)
    plugin.get_data=lambda key:data.get(key)
    plugin.del_data=lambda key:data.pop(key,None)
    plugin.update_config=lambda config:True
    plugin._ensure_monitor=lambda:None
    plugin.init_plugin({'enabled':True,'notify':False,'passport':'account','password':'secret','tasks':[
        {'id':'p','source_path':str(source),'target_path':'/','trigger_mode':'downloader','settle_seconds':30}]})
    pair=plugin._pairs[0]; scope=monitor.scope_for(pair,'account'); stat=path.stat(); now=time.time()
    plugin._journal.observe_files('p',scope,[{'path':'a.mkv','size':stat.st_size,'mtime_ns':stat.st_mtime_ns}],now-90,complete=False)
    class EmptyChain:
        def list_torrents(self, **kwargs): return []
    plugin._get_download_monitor=lambda:(EmptyChain(),'all')
    plugin._monitor_tick(threading.Event(),now=now)
    assert plugin._worker is None and plugin._journal.monitor_counts('p',scope)['pending'] == 1
    assert '再次确认' in plugin._monitor_status['p']['message']
    plugin._journal.close()


def test_historical_file_details_survive_new_runs(plugin_module, tmp_path):
    plugin = plugin_module.P123Sync()
    journal = plugin_module.SyncJournal(tmp_path/'history.sqlite3')
    plugin._journal = journal
    prior = plugin_module.RunState(journal=journal)
    prior.begin([])
    prior.add_files([{'key':'p:a','path':'a','size':1,'state':'pending','percent':0,'speed':0,'error':None}])
    prior.mark_file('p:a','failed',error='old failure')
    prior.finish('finished')
    prior_id=prior.run_id
    plugin._state=plugin_module.RunState(journal=journal)
    plugin._state.begin([])
    plugin._state.finish('finished')
    result=plugin.api_files(page=1,page_size=20,active_only=True,run_id=prior_id)
    assert result['run_id'] == prior_id and result['total'] == 1
    assert result['items'][0]['error'] == 'old failure'
    assert plugin.api_files(run_id='unknown')['success'] is False
    journal.close()


def test_cloud_errors_do_not_expose_credentials_or_signed_urls(plugin_module, monkeypatch):
    client=plugin_module.P123SyncClient('account','secret')
    client.client=SimpleNamespace(call=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('secret https://example.test/?token=private')))
    with pytest.raises(plugin_module.P123ClientError) as caught:
        client._checked('fs_list', {})
    assert 'secret' not in str(caught.value) and 'token=private' not in str(caught.value)


def test_downloader_monitor_never_accepts_nan_or_unknown_completion(plugin_module, tmp_path):
    monitor=sys.modules[plugin_module.__name__ + '.auto_monitor']
    path=tmp_path/'file.mkv'; path.write_bytes(b'abc')
    pair=plugin_module.TaskPairConfig(id='p',name='Test',source_path=str(tmp_path),target_path='/',trigger_mode='downloader')
    bad=SimpleNamespace(state='completed',progress=float('nan'),hash='h',downloader='qb',save_path=str(tmp_path))
    class Chain:
        def list_torrents(self,**kwargs): return [bad]
        def torrent_files(self,**kwargs): raise AssertionError('must not inspect unknown completion')
    result,_=monitor.discover_downloads([pair],Chain(),'all')
    assert result['p'] == []
    assert not monitor._file_complete({'name':'file.mkv','size':3,'progress':float('nan'),'priority':1})
    assert not monitor._file_complete({'name':'file.mkv','size':3,'progress':1})


def test_directory_monitor_retains_changes_while_sync_is_busy(plugin_module, tmp_path):
    import threading, time
    source=tmp_path/'source'; source.mkdir(); item=source/'a.mkv'; item.write_bytes(b'one')
    data={}; plugin=plugin_module.P123Sync()
    plugin.get_data_path=lambda:tmp_path/'plugin-data'
    plugin.save_data=lambda key,value:data.__setitem__(key,value)
    plugin.get_data=lambda key:data.get(key)
    plugin.del_data=lambda key:data.pop(key,None)
    plugin.update_config=lambda config:True
    plugin._ensure_monitor=lambda:None
    plugin.init_plugin({'enabled':True,'notify':False,'passport':'account','password':'secret','tasks':[
        {'id':'p','source_path':str(source),'target_path':'/','trigger_mode':'directory','settle_seconds':30}]})
    started=[]
    plugin._start_run=lambda pairs, trigger, selected_paths=None: started.append(selected_paths) or (True,'started')
    stop=threading.Event(); now=time.time()
    plugin._run_in_progress=True
    plugin._monitor_tick(stop,now=now)
    item.write_bytes(b'new content')
    plugin._monitor_tick(stop,now=now+31)
    assert started == []
    plugin._run_in_progress=False
    plugin._monitor_tick(stop,now=now+40)
    assert started == []
    plugin._monitor_tick(stop,now=now+62)
    assert len(started) == 1 and started[0]['p']['a.mkv']['size'] == len(b'new content')
    plugin._journal.close()


def test_relative_monitor_source_is_rejected(plugin_module):
    plugin=plugin_module.P123Sync()
    result=plugin.api_save_config(plugin_module.SaveConfigRequest(config={'tasks':[{'source_path':'relative/path','trigger_mode':'directory'}]}))
    assert not result['success'] and '绝对路径' in result['message']


def test_unexpected_worker_exit_is_durable_and_monitor_retry_reconciles(plugin_module, tmp_path):
    import hashlib, threading, time
    source=tmp_path/'source'; source.mkdir(); (source/'a.mkv').write_bytes(b'abc')
    data={}
    class Cloud:
        def __init__(self): self.items=[]; self.uploads=0
        def list_dir(self,parent_id,cancel_check=None): return list(self.items)
        def find_files_by_name(self,parent_id,name,cancel_check=None): return [x for x in self.items if x.name==name]
        def walk(self,parent_id,prefix='',cancel_check=None):
            for item in self.items: yield item.name,item
        def _file_md5(self,path,cancel_check=None,on_progress=None): return hashlib.md5(path.read_bytes()).hexdigest()
        def upload(self,path,parent_id,name,on_progress=None,cancel_check=None,file_md5=None,on_stage=None):
            self.uploads+=1
            self.items.append(SimpleNamespace(name=name,file_id=201,size=3,md5=file_md5,is_dir=False))
            raise SystemExit('simulated worker death')
    cloud=Cloud(); plugin=plugin_module.P123Sync()
    plugin.get_data_path=lambda:tmp_path/'plugin-data'
    plugin.save_data=lambda key,value:data.__setitem__(key,value)
    plugin.get_data=lambda key:data.get(key)
    plugin.del_data=lambda key:data.pop(key,None)
    plugin.update_config=lambda config:True
    plugin.get_config=lambda:config
    plugin._ensure_monitor=lambda:None
    config={'enabled':True,'notify':False,'passport':'account','password':'secret',
            'tasks':[{'id':'p','source_path':str(source),'target_path':'/','trigger_mode':'directory','settle_seconds':30}]}
    plugin.init_plugin(config); plugin._get_client=lambda:cloud
    pair=plugin._pairs[0]; monitor=sys.modules[plugin_module.__name__+'.auto_monitor']; scope=monitor.scope_for(pair,'account')
    stat=(source/'a.mkv').stat(); now=time.time()
    spec={'path':'a.mkv','size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
    plugin._journal.observe_files('p',scope,[spec],now-31,complete=True)
    assert plugin._journal.acquire()
    plugin._run_in_progress=True
    plugin._state=plugin_module.RunState(trigger='monitor',journal=plugin._journal)
    plugin._state.begin([pair])
    with pytest.raises(SystemExit):
        plugin._sync_worker([pair],plugin._state,threading.Event(),None,{'p':{'a.mkv':spec}})
    assert not plugin._journal.is_owned() and plugin._state.snapshot()['counters']['failed'] == 1
    plugin._monitor_tick(threading.Event(),now=now+100)
    plugin._worker.join(timeout=10)
    snap=plugin._state.snapshot()
    assert cloud.uploads == 1 and snap['counters']['failed'] == 0 and snap['phase'] == 'finished'
    assert plugin._journal.monitor_counts('p',scope)['pending'] == 0
    plugin._journal.close()
