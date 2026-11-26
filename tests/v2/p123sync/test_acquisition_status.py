"""The PrivateFilm view must use exact, current, scoped file evidence."""
import importlib
from pathlib import Path

from test_browse import plugin_module


def test_exact_file_scoped_progress_and_changed_source(plugin_module, tmp_path):
    mod = importlib.import_module(plugin_module.__name__ + '.acquisition_status')
    folder = tmp_path / 'source'
    folder.mkdir()
    file = folder / 'film.mkv'
    file.write_bytes(b'film')
    pair = plugin_module.TaskPairConfig(id='one', name='Movies', source_path=str(folder),
                                       target_path='/Movies', trigger_mode='manual')
    scope = plugin_module.scope_for(pair, 'account')
    journal = plugin_module.SyncJournal(tmp_path / 'journal.sqlite')
    stat = file.stat()
    record = dict(key='one:film.mkv', pair_id='one', path='film.mkv', _position=0,
                  size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
                  sync_scope=scope, state='done', percent=100)
    journal.checkpoint({'run_id': '1'}, [record])
    item = plugin_module.AcquisitionFile(id='task', path=str(file))
    def query(snapshot=None, account='account', pairs=None):
        return mod.status_for_files([item], pairs or [pair], account, True, journal, None,
                                    snapshot or {'files': [], 'running': False}, {})['items'][0]
    assert query()['state'] == 'synced'
    assert query(account='another')['state'] == 'waiting'
    active = {**record, 'state': 'uploading', 'percent': 42}
    result = query({'files': [active], 'running': True})
    assert result['state'] == 'syncing' and result['progress'] == 0.42
    result = query({'_file_lookup': lambda key: active, 'running': True})
    assert result['state'] == 'syncing' and result['progress'] == 0.42
    pending = {**record, 'state': 'pending'}
    result = query({'files': [pending], 'running': True})
    assert result['state'] == 'waiting'
    file.write_bytes(b'changed file')
    assert query()['state'] == 'waiting'
    journal.close()


def test_unmonitored_and_ambiguous_paths_are_never_inferred_as_synced(plugin_module, tmp_path):
    mod = importlib.import_module(plugin_module.__name__ + '.acquisition_status')
    folder = tmp_path / 'media'
    folder.mkdir()
    file = folder / 'film.mkv'
    file.write_bytes(b'x')
    pair = plugin_module.TaskPairConfig(id='one', name='Movies', source_path=str(folder),
                                       target_path='/Movies')
    second = plugin_module.TaskPairConfig(id='two', name='More', source_path=str(folder),
                                         target_path='/Other')
    journal = plugin_module.SyncJournal(tmp_path / 'journal.sqlite')
    items = [
        plugin_module.AcquisitionFile(id='ambiguous', path=str(file)),
        plugin_module.AcquisitionFile(id='other', path=str(tmp_path / 'elsewhere' / 'film.mkv')),
        plugin_module.AcquisitionFile(id='traversal', path=str(folder / '..' / 'film.mkv')),
    ]
    result = mod.status_for_files(items, [pair, second], 'account', True, journal, None,
                                  {'files': [], 'running': False}, {})
    assert [item['state'] for item in result['items']] == ['unknown', 'not_monitored', 'unknown']
    journal.close()


def test_torrent_file_list_tracks_raw_sync_without_assuming_partial_files_complete(plugin_module, tmp_path):
    mod = importlib.import_module(plugin_module.__name__ + '.acquisition_status')
    folder = tmp_path / 'downloads'
    folder.mkdir()
    file = folder / 'film.mkv'
    file.write_bytes(b'film')
    pair = plugin_module.TaskPairConfig(id='one', name='Raw', source_path=str(folder),
                                       target_path='/Raw', trigger_mode='manual')
    journal = plugin_module.SyncJournal(tmp_path / 'journal.sqlite')
    stat = file.stat()
    journal.checkpoint({'run_id': 'one'}, [dict(
        key='one:film.mkv', pair_id='one', path='film.mkv', _position=0,
        size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
        sync_scope=plugin_module.scope_for(pair, 'account'), state='done')])
    class Chain:
        files = [{'name': 'film.mkv', 'selected': True, 'size': 4, 'completed': 4}]
        def list_torrents(self, **kwargs):
            return [{'hash': 'ABC', 'downloader': 'qb', 'save_path': str(folder)}]
        def torrent_files(self, **kwargs):
            return self.files
    chain = Chain()
    req = plugin_module.AcquisitionTorrent(id='task', hash='abc')
    def query():
        return mod.status_for_torrents([req], [pair], 'account', True, journal, None,
                                       {'files': [], 'running': False}, {}, chain, 'all')[0]
    assert query()['state'] == 'synced'
    assert query()['filesTotal'] == query()['filesSynced'] == 1
    chain.files.append({'name': 'episode2.mkv', 'selected': True, 'size': 10, 'completed': 3})
    assert query()['state'] == 'unknown'
    assert query()['filesTotal'] == 2 and query()['filesSynced'] == 1
    journal.close()
