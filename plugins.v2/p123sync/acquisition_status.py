"""PrivateFilm's exact-file view of p123sync's own evidence."""
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, List

from .auto_monitor import scope_for, _value, _file_complete


def status_for_files(files, pairs, passport, enabled, journal, journal_error,
                     snapshot, monitors) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for item in files:
        result = {'id': item.id, 'state': 'unknown', 'progress': None, 'detail': None}
        results.append(result)
        if not enabled or journal_error or not journal:
            result['detail'] = '同步插件未启用或状态记录不可用'
            continue
        raw = item.path.replace('\\', '/')
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts or any(ord(c) < 32 for c in raw):
            result['detail'] = '文件路径无法安全核对'
            continue
        try:
            resolved = path.resolve()
            candidates = []
            for pair in pairs:
                if not pair.enabled:
                    continue
                root = Path(pair.source_path).resolve()
                try:
                    relative = resolved.relative_to(root).as_posix()
                except ValueError:
                    continue
                if relative != '.':
                    candidates.append((pair, relative))
            if not candidates:
                result.update(state='not_monitored', detail='该文件不在同步任务的源目录内')
                continue
            if len(candidates) != 1:
                result['detail'] = '文件匹配多个同步任务，请检查源目录'
                continue
            pair, relative = candidates[0]
            if not resolved.is_file():
                result['detail'] = '源文件当前不可用，无法核对同步记录'
                continue
            stat = resolved.stat()
            scope = scope_for(pair, passport)
            data = journal.acquisition_file(pair.id, scope, relative)
            lookup = snapshot.get('_file_lookup')
            if lookup:
                try:
                    live = lookup(f'{pair.id}:{relative}')
                except KeyError:
                    live = None
            else:
                live = next((f for f in snapshot.get('files', [])
                             if f.get('pair_id') == pair.id and f.get('path') == relative), None)
            if live and (live.get('sync_scope') or live.get('monitor_scope')) != scope:
                live = None
            record = live or data['record']
            if record and (record.get('size'), record.get('source_mtime_ns')) == (stat.st_size, stat.st_mtime_ns):
                state = record.get('state')
                if state in ('done', 'instant', 'skipped'):
                    result.update(state='synced', progress=1.0,
                                  detail=('云端存在同名同大小文件，按插件策略跳过' if state == 'skipped'
                                          else '插件已确认上传完成'))
                    continue
                if state == 'uploading' and snapshot.get('running') and live:
                    percent = record.get('percent')
                    result.update(state='syncing',
                                  progress=max(0.0, min(float(percent) / 100, 1.0))
                                  if isinstance(percent, (int, float)) else None,
                                  detail='正在上传到 123 云盘')
                    continue
                if state == 'pending' and snapshot.get('running') and live:
                    activity = snapshot.get('activity') or {}
                    if activity.get('key') == live.get('key'):
                        stage = activity.get('stage')
                        result.update(state='syncing', detail=('正在计算或核对文件'
                                      if stage in ('hashing', 'checking_existing', 'checking_remote',
                                                   'strict_verification') else '正在准备同步'))
                    else:
                        result.update(state='waiting', detail='文件已进入同步队列')
                    continue
                if state in ('failed', 'cancelled'):
                    result.update(state='failed', detail='同步未完成，请在 123 云盘同步插件中查看并重试')
                    continue
            observed = data['monitor']
            if observed and (observed['size'], observed['mtime_ns']) == (stat.st_size, stat.st_mtime_ns):
                if observed['present'] and observed['failures']:
                    result.update(state='failed', detail='同步失败，插件将重试或等待手动处理')
                    continue
                if observed['present'] and (observed['synced_size'], observed['synced_mtime_ns']) == (stat.st_size, stat.st_mtime_ns):
                    result.update(state='synced', progress=1.0, detail='插件已记录该文件同步完成')
                    continue
            monitor = monitors.get(pair.id, {})
            if monitor.get('state') == 'error':
                result['detail'] = '同步监控异常，请打开插件检查'
                continue
            result.update(state='waiting',
                          detail='等待手动执行同步' if pair.trigger_mode == 'manual'
                          else '等待插件发现并同步该文件')
        except (OSError, ValueError, TypeError, OverflowError):
            result['detail'] = '同步记录暂时无法核对'
    return {'version': 1, 'items': results}


def status_for_torrents(requests, pairs, passport, enabled, journal, journal_error,
                        snapshot, monitors, chain, query_status):
    """Resolve exact selected files through the downloader, never by title."""
    torrents = chain.list_torrents(status=query_status, include_all_tags=True)
    if torrents is None:
        raise RuntimeError('下载器不可用')
    result = []
    for request in requests:
        summary = {'id': request.id, 'state': 'unknown', 'progress': None,
                   'filesTotal': 0, 'filesSynced': 0,
                   'detail': '下载器未提供可核对的文件清单'}
        result.append(summary)
        matching = [t for t in torrents if str(_value(t, 'hash') or '').lower() == request.hash.lower()]
        if len(matching) != 1:
            if len(matching) > 1:
                summary['detail'] = '多个下载器存在相同任务，无法确认文件来源'
            continue
        torrent = matching[0]
        downloader, save_path = _value(torrent, 'downloader'), _value(torrent, 'save_path')
        if not downloader or not save_path or not Path(str(save_path)).is_absolute():
            continue
        try:
            items = chain.torrent_files(tid=_value(torrent, 'hash'), downloader=downloader)
            if not items:
                continue
            paths, unresolved = [], 0
            for item in items:
                selected, priority = _value(item, 'selected'), _value(item, 'priority')
                if selected is False or (selected is None and priority in (0, '0')):
                    continue
                name = str(_value(item, 'name') or '').replace('\\', '/')
                rel = PurePosixPath(name)
                if not name or rel.is_absolute() or '..' in rel.parts or not _file_complete(item):
                    unresolved += 1
                    continue
                local = Path(str(save_path)) / Path(*rel.parts)
                expected = _value(item, 'size')
                if expected is None or not local.is_file() or local.stat().st_size != int(expected):
                    unresolved += 1
                    continue
                paths.append(SimpleNamespace(id=str(len(paths)), path=str(local)))
            checks = status_for_files(paths, pairs, passport, enabled, journal, journal_error,
                                      snapshot, monitors)['items']
            summary['filesTotal'] = len(checks) + unresolved
            summary['filesSynced'] = sum(item['state'] == 'synced' for item in checks)
            if unresolved:
                checks.append({'state': 'unknown', 'progress': None,
                               'detail': '部分已选文件尚未完成或无法核对'})
            if not checks:
                continue
            priority = ('failed', 'syncing', 'waiting', 'unknown', 'not_monitored', 'synced')
            state = next((s for s in priority if any(item['state'] == s for item in checks)), 'unknown')
            current = next(item for item in checks if item['state'] == state)
            detail = current.get('detail')
            if state == 'synced' and any('同名同大小' in (item.get('detail') or '') for item in checks):
                detail = '部分文件按同名同大小策略跳过，未必经过内容校验'
            summary.update(state=state, detail=detail,
                           progress=current.get('progress') if len(checks) == 1 else None)
        except (OSError, ValueError, TypeError, OverflowError):
            summary['detail'] = '下载器文件清单无法核对'
    return result
