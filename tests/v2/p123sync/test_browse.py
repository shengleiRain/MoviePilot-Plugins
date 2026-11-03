"""Directory browsing contracts with isolated host and 123 client fakes."""
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def plugin_module(monkeypatch):
    modules = {
        'apscheduler': {}, 'apscheduler.triggers': {},
        'apscheduler.triggers.cron': {'CronTrigger': object},
        'fastapi': {'Body': lambda default=None: default},
        'app': {}, 'app.log': {'logger': SimpleNamespace(info=lambda *a: None)},
        'app.plugins': {'_PluginBase': object}, 'app.schemas': {},
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
    assert plugin.api_test_login(request)['success']
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
    assert plugin.plugin_version == '1.4.0'
    assert plugin.get_render_mode() == ('vue', 'dist/v1_4_0')
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
    assert plugin._start_run([pair], 'manual')[0]
    assert plugin._state.snapshot()['running']
    assert not plugin._start_run([pair], 'manual')[0]
