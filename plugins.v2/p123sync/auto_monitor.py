"""Conservative discovery for directory and completed-downloader monitoring.

Monitoring only produces exact local file identities. It never performs cloud
operations or marks a file synced; the durable run journal does that after the
upload has been verified.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Tuple

from .sync_engine import TaskPairConfig, scan_source, should_exclude


def scope_for(pair: TaskPairConfig, passport: str) -> str:
    payload = [passport, pair.id, pair.source_path, pair.target_path, pair.policy,
               sorted(pair.exclude_ext), pair.trigger_mode]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def scan_directory(pair: TaskPairConfig, cancel_check: Callable[[], bool] | None = None) -> List[Dict]:
    if not Path(pair.source_path).is_absolute():
        raise ValueError('监控源目录必须是绝对路径')
    root = Path(pair.source_path).resolve(strict=True)
    records = []
    for item in scan_source(pair.source_path, pair.exclude_ext, cancel_check):
        try:
            resolved = item.abs_path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError(f'监控文件链接指向源目录外：{item.rel_path}')
        except OSError as err:
            raise OSError(f'无法核对监控文件 {item.rel_path}: {err}') from err
        records.append({'path':item.rel_path, 'size':item.size, 'mtime_ns':item.mtime_ns})
    return records


def _value(item: Any, name: str, default=None):
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def _file_complete(item: Any) -> bool:
    selected = _value(item, 'selected')
    progress = _value(item, 'progress')
    priority = _value(item, 'priority')
    if selected is False:
        return False
    if selected is None and progress is not None:
        if priority is None or int(priority) == 0:
            return False
        fraction = float(progress)
        return math.isfinite(fraction) and 1.0 <= fraction <= 1.000001
    if selected is True:
        size = _value(item, 'size')
        completed = _value(item, 'completed')
        return size is not None and completed is not None and int(size) >= 0 and int(completed) >= int(size)
    return False


def discover_downloads(pairs: Iterable[TaskPairConfig], chain: Any, query_status: Any,
                       cancel_check: Callable[[], bool] | None = None) -> Tuple[Dict[str, List[Dict]], List[str]]:
    """Map only confirmed selected torrent files into configured source roots.

    MoviePilot normalizes each torrent save_path to its local path. Never guess
    a mapping or fall back to scanning the whole torrent directory.
    """
    pairs = list(pairs)
    if any(not Path(pair.source_path).is_absolute() for pair in pairs):
        raise ValueError('下载器监控源目录必须是绝对路径')
    roots = {pair.id: Path(pair.source_path).resolve(strict=True) for pair in pairs}
    found: Dict[str, Dict[str, Dict]] = {pair.id:{} for pair in pairs}
    warnings = []
    torrents = chain.list_torrents(status=query_status, include_all_tags=True)
    if torrents is None:
        raise RuntimeError('无法读取下载器的已完成任务')
    for torrent in torrents:
        if cancel_check and cancel_check():
            raise KeyboardInterrupt
        state = str(_value(torrent, 'state') or '').lower()
        torrent_hash = _value(torrent, 'hash')
        downloader = _value(torrent, 'downloader')
        save_path = _value(torrent, 'save_path')
        progress = _value(torrent, 'progress')
        try:
            fraction = float(progress) if progress is not None else float('nan')
        except (TypeError, ValueError):
            warnings.append('部分下载任务完成状态不可用')
            continue
        if (state not in ('completed', 'paused') or not math.isfinite(fraction) or fraction < 100 or
                not torrent_hash or not downloader or not save_path):
            continue
        base = Path(str(save_path))
        if not base.is_absolute():
            warnings.append('下载器保存路径不是容器内绝对路径')
            continue
        content_path = _value(torrent, 'path') or _value(torrent, 'content_path')
        if content_path:
            content = Path(str(content_path))
            if content.is_absolute():
                content = content.resolve()
                if not any(content.is_relative_to(root) or root.is_relative_to(content) for root in roots.values()):
                    continue
        try:
            files = chain.torrent_files(tid=torrent_hash, downloader=downloader)
            if not files:
                warnings.append('部分下载任务的文件清单不可用，稍后重试')
                continue
            for item in files:
                if cancel_check and cancel_check():
                    raise KeyboardInterrupt
                try:
                    if not _file_complete(item):
                        continue
                    name = str(_value(item, 'name') or '').replace('\\', '/')
                    rel = PurePosixPath(name)
                    if not name or rel.is_absolute() or '..' in rel.parts:
                        warnings.append('下载器返回了无法安全映射的文件路径')
                        continue
                    local = (base / Path(*rel.parts)).resolve(strict=True)
                    if not local.is_file():
                        continue
                    stat = local.stat()
                    expected_size = _value(item, 'size')
                    if expected_size is None or stat.st_size != int(expected_size):
                        warnings.append('下载器文件大小与本地文件不一致，稍后重试')
                        continue
                    for pair in pairs:
                        root = roots[pair.id]
                        if not local.is_relative_to(root):
                            continue
                        relative = local.relative_to(root)
                        if any(should_exclude(part, pair.exclude_ext if i == len(relative.parts)-1 else ())
                               for i, part in enumerate(relative.parts)):
                            continue
                        rel_path = relative.as_posix()
                        found[pair.id][rel_path] = {'path':rel_path, 'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns}
                except (OSError, ValueError, TypeError, OverflowError) as err:
                    warnings.append(f'部分下载文件无法核对：{type(err).__name__}')
        except Exception as err:
            warnings.append(f'下载任务文件清单不可用：{type(err).__name__}')
    return {key:list(value.values()) for key,value in found.items()}, list(dict.fromkeys(warnings))
