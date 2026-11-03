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
