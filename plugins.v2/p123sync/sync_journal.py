"""Durable, per-file checkpoints for the latest and previous sync runs.

No account credentials are stored here. Each transition and its run metadata
are committed together; incomplete runs are reconciled on the next start.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

# Consecutive identical failures before a monitored file stops auto-retrying.
MONITOR_GIVE_UP_FAILURES = 5


class SyncJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._lease_lock = threading.Lock()
        self._lease_file = open(path.with_suffix(path.suffix + '.lock'), 'a+b')
        if self._lease_file.seek(0, os.SEEK_END) == 0:
            self._lease_file.write(b'0')
            self._lease_file.flush()
        self._lease_owned = False
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        with self._conn:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, meta TEXT NOT NULL)')
            self._conn.execute('CREATE TABLE IF NOT EXISTS files (run_id TEXT NOT NULL, file_key TEXT NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_id, file_key))')
            self._conn.execute('CREATE TABLE IF NOT EXISTS current_run (id INTEGER PRIMARY KEY CHECK(id=1), run_id TEXT NOT NULL)')
            self._conn.execute('CREATE TABLE IF NOT EXISTS monitor_files (pair_id TEXT NOT NULL, scope TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, changed_at REAL NOT NULL, present INTEGER NOT NULL DEFAULT 1, synced_size INTEGER, synced_mtime_ns INTEGER, retry_after REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, PRIMARY KEY(pair_id,scope,path))')
            self._conn.execute('CREATE TABLE IF NOT EXISTS md5_cache (path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, md5 TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(path, size, mtime_ns))')

    def acquire(self) -> bool:
        """Nonblocking OS lock; the kernel releases it if the process dies."""
        with self._lease_lock:
            if self._lease_owned:
                return True
            try:
                if os.name == 'nt':
                    import msvcrt
                    self._lease_file.seek(0)
                    msvcrt.locking(self._lease_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                return False
            self._lease_owned = True
            return True

    def is_owned(self) -> bool:
        with self._lease_lock:
            return self._lease_owned

    def release(self):
        with self._lease_lock:
            if not self._lease_owned:
                return
            if os.name == 'nt':
                import msvcrt
                self._lease_file.seek(0)
                msvcrt.locking(self._lease_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lease_file.fileno(), fcntl.LOCK_UN)
            self._lease_owned = False

    def checkpoint(self, meta: Dict, records: Iterable[Dict] = (), replace: bool = False):
        run_id = meta['run_id']
        with self._lock, self._conn:
            self._conn.execute('INSERT OR REPLACE INTO runs(run_id, meta) VALUES (?, ?)',
                               (run_id, json.dumps(meta, ensure_ascii=False)))
            self._conn.execute('INSERT OR REPLACE INTO current_run(id, run_id) VALUES (1, ?)', (run_id,))
            if replace:
                self._conn.execute('DELETE FROM files WHERE run_id = ?', (run_id,))
            for record in records:
                self._conn.execute('INSERT INTO files(run_id, file_key, position, data) VALUES (?, ?, ?, ?) '
                                   'ON CONFLICT(run_id, file_key) DO UPDATE SET data=excluded.data, position=excluded.position',
                                   (run_id, record['key'], record['_position'], json.dumps(record, ensure_ascii=False)))
                if (record.get('state') in ('done', 'instant', 'skipped') and
                        record.get('source_mtime_ns') is not None and record.get('pair_id') and record.get('monitor_scope')):
                    self._conn.execute('''INSERT INTO monitor_files(pair_id,scope,path,size,mtime_ns,changed_at,present,synced_size,synced_mtime_ns)
                        VALUES (?,?,?,?,?,?,1,?,?) ON CONFLICT(pair_id,scope,path) DO UPDATE SET
                        synced_size=excluded.synced_size, synced_mtime_ns=excluded.synced_mtime_ns,
                        retry_after=0, failures=0, last_error=NULL
                        WHERE monitor_files.size=excluded.size AND monitor_files.mtime_ns=excluded.mtime_ns''',
                        (record['pair_id'], record['monitor_scope'], record['path'], record['size'],
                         record['source_mtime_ns'], time.time(), record['size'], record['source_mtime_ns']))

    def observe_files(self, pair_id: str, scope: str, files: Iterable[Dict], now: float, complete: bool):
        """Reconcile a complete directory snapshot or a completed-download set."""
        with self._lock, self._conn:
            existing = ({row[0] for row in self._conn.execute('SELECT path FROM monitor_files WHERE pair_id=? AND scope=?', (pair_id, scope))}
                        if complete else set())
            observed = set()
            for item in files:
                observed.add(item['path'])
                self._conn.execute('''INSERT INTO monitor_files(pair_id,scope,path,size,mtime_ns,changed_at,present)
                    VALUES (?,?,?,?,?,?,1) ON CONFLICT(pair_id,scope,path) DO UPDATE SET
                    changed_at=CASE WHEN present=0 OR size!=excluded.size OR mtime_ns!=excluded.mtime_ns THEN excluded.changed_at ELSE changed_at END,
                    retry_after=CASE WHEN present=0 OR size!=excluded.size OR mtime_ns!=excluded.mtime_ns THEN 0 ELSE retry_after END,
                    failures=CASE WHEN present=0 OR size!=excluded.size OR mtime_ns!=excluded.mtime_ns THEN 0 ELSE failures END,
                    last_error=CASE WHEN present=0 OR size!=excluded.size OR mtime_ns!=excluded.mtime_ns THEN NULL ELSE last_error END,
                    synced_size=CASE WHEN present=0 OR size!=excluded.size OR mtime_ns!=excluded.mtime_ns THEN NULL ELSE synced_size END,
                    synced_mtime_ns=CASE WHEN present=0 OR size!=excluded.size OR mtime_ns!=excluded.mtime_ns THEN NULL ELSE synced_mtime_ns END,
                    size=excluded.size, mtime_ns=excluded.mtime_ns, present=1''',
                    (pair_id, scope, item['path'], item['size'], item['mtime_ns'], now))
            if complete:
                self._conn.executemany('UPDATE monitor_files SET present=0 WHERE pair_id=? AND scope=? AND path=?',
                                       ((pair_id, scope, path) for path in existing - observed))

    def monitor_ready(self, pair_id: str, scope: str, now: float, settle_seconds: int, limit: int = 200) -> list:
        with self._lock:
            rows = self._conn.execute('''SELECT path,size,mtime_ns FROM monitor_files
                WHERE pair_id=? AND scope=? AND present=1 AND changed_at<=? AND retry_after<=?
                  AND (synced_size IS NULL OR synced_size!=size OR synced_mtime_ns!=mtime_ns)
                ORDER BY changed_at,path LIMIT ?''', (pair_id, scope, now-settle_seconds, now, limit)).fetchall()
            return [{'path': row[0], 'size': row[1], 'mtime_ns': row[2]} for row in rows]

    def monitor_fail(self, pair_id: str, scope: str, path: str, size: int, mtime_ns: int, error: str, now: float):
        """Back off a failing file; pause auto-retry after repeated identical failures.

        The counter restarts when the error text changes (a new, possibly
        transient problem), and any size/mtime change in ``observe_files``
        clears it. Five identical failures in a row mean the file cannot be
        synced as-is, so auto-retry pauses until the file or the cloud
        changes; manual runs are never gated by ``retry_after``.
        """
        with self._lock, self._conn:
            row = self._conn.execute('SELECT failures, last_error FROM monitor_files WHERE pair_id=? AND scope=? AND path=? AND size=? AND mtime_ns=?',
                                     (pair_id, scope, path, size, mtime_ns)).fetchone()
            if row:
                message = str(error)[:500]
                if row[1] is not None and row[1] != message:
                    failures = 1
                else:
                    failures = min(row[0] + 1, 10)
                if failures >= MONITOR_GIVE_UP_FAILURES:
                    retry_after = now + 10 ** 9
                    message = f"{message}；已暂停自动重试，文件内容变化后自动恢复，也可手动同步"
                else:
                    retry_after = now + min(60 * 2 ** (failures - 1), 3600)
                self._conn.execute('UPDATE monitor_files SET failures=?, retry_after=?, last_error=? WHERE pair_id=? AND scope=? AND path=?',
                                   (failures, retry_after, message, pair_id, scope, path))

    def monitor_counts(self, pair_id: str, scope: str) -> Dict:
        with self._lock:
            row = self._conn.execute('''SELECT COUNT(*), SUM(CASE WHEN present=1 AND
                (synced_size IS NULL OR synced_size!=size OR synced_mtime_ns!=mtime_ns) THEN 1 ELSE 0 END),
                SUM(CASE WHEN present=0 AND (synced_size IS NULL OR synced_size!=size OR synced_mtime_ns!=mtime_ns) THEN 1 ELSE 0 END),
                SUM(CASE WHEN present=1 AND failures>=? AND
                (synced_size IS NULL OR synced_size!=size OR synced_mtime_ns!=mtime_ns) THEN 1 ELSE 0 END)
                FROM monitor_files WHERE pair_id=? AND scope=?''',
                (MONITOR_GIVE_UP_FAILURES, pair_id, scope)).fetchone()
            return {'observed': row[0] or 0, 'pending': row[1] or 0, 'missing': row[2] or 0,
                    'gave_up': row[3] or 0}

    def md5_lookup(self, path: str, size: int, mtime_ns: int) -> Optional[str]:
        """Cached digest for an unchanged source file (size+mtime_ns must match)."""
        with self._lock:
            row = self._conn.execute('SELECT md5 FROM md5_cache WHERE path=? AND size=? AND mtime_ns=?',
                                     (path, size, mtime_ns)).fetchone()
            return row[0] if row else None

    def md5_store(self, path: str, size: int, mtime_ns: Optional[int], md5: str):
        if mtime_ns is None:
            return
        with self._lock, self._conn:
            self._conn.execute('INSERT OR REPLACE INTO md5_cache(path, size, mtime_ns, md5, updated_at) VALUES (?, ?, ?, ?, ?)',
                               (path, size, mtime_ns, md5, time.time()))

    def load_latest(self) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute('SELECT r.run_id, r.meta FROM runs r JOIN current_run c ON c.run_id=r.run_id WHERE c.id=1').fetchone()
            if not row:
                return None
            snapshot = json.loads(row[1])
            snapshot['files'] = [json.loads(item[0]) for item in self._conn.execute(
                'SELECT data FROM files WHERE run_id=? ORDER BY position', (row[0],))]
            return snapshot

    def history_files_page(self, run_id: str, page: int, page_size: int, active_only: bool) -> Optional[Dict]:
        with self._lock:
            if not self._conn.execute('SELECT 1 FROM runs WHERE run_id=?', (run_id,)).fetchone():
                return None
            predicate = " AND json_extract(data, '$.state') IN ('uploading','failed','cancelled')" if active_only else ''
            total = self._conn.execute('SELECT COUNT(*) FROM files WHERE run_id=?' + predicate, (run_id,)).fetchone()[0]
            page_size = max(1, min(page_size, 100))
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            rows = self._conn.execute('SELECT data FROM files WHERE run_id=?' + predicate +
                                      ' ORDER BY position LIMIT ? OFFSET ?',
                                      (run_id, page_size, (page - 1) * page_size)).fetchall()
            return {'run_id':run_id, 'page':page, 'page_size':page_size, 'total':total,
                    'pages':pages, 'items':[json.loads(row[0]) for row in rows]}

    def close(self):
        self.release()
        with self._lock:
            self._conn.close()
        self._lease_file.close()
