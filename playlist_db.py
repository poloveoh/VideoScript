"""Durable Playlist membership and FIFO state; job contents stay in JobStore."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class PlaylistRecord:
    id: int
    playlist_id: str
    url: str
    title: str
    enabled: bool
    removed: bool
    created_at: float
    last_sync_started_at: float | None
    last_sync_success_at: float | None
    last_error: str
    video_count: int


@dataclass(frozen=True)
class QueueItem:
    position: int
    job_id: str
    video_id: str
    state: str
    retry_count: int
    error: str
    created_at: float
    started_at: float | None
    finished_at: float | None


class PlaylistDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError(f'Unsupported database version: {version}')
            if version == 0:
                connection.execute('''CREATE TABLE playlists (
                    id INTEGER PRIMARY KEY, playlist_id TEXT NOT NULL UNIQUE,
                    url TEXT NOT NULL, title TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    removed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    last_sync_started_at REAL, last_sync_success_at REAL,
                    last_error TEXT NOT NULL DEFAULT '',
                    video_count INTEGER NOT NULL DEFAULT 0)''')
                connection.execute('''CREATE TABLE settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
                connection.execute('''CREATE TABLE videos (
                    video_id TEXT PRIMARY KEY, url TEXT NOT NULL,
                    title TEXT NOT NULL, duration REAL, availability TEXT NOT NULL,
                    first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL)''')
                connection.execute('''CREATE TABLE playlist_videos (
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id),
                    video_id TEXT NOT NULL REFERENCES videos(video_id),
                    position INTEGER NOT NULL, present INTEGER NOT NULL,
                    first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
                    removed_at REAL, completed_at_removal REAL,
                    PRIMARY KEY (playlist_id, video_id))''')
                connection.execute('''CREATE TABLE playlist_jobs (
                    video_id TEXT PRIMARY KEY REFERENCES videos(video_id),
                    job_id TEXT NOT NULL UNIQUE, method TEXT NOT NULL,
                    creation_reason TEXT NOT NULL DEFAULT 'discovery',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE (job_id, video_id))''')
                connection.execute('''CREATE TABLE queue_items (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE, video_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'waiting'
                        CHECK (state IN ('waiting', 'processing', 'interrupted', 'completed', 'failed')),
                    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 1),
                    error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    started_at REAL, finished_at REAL,
                    FOREIGN KEY (job_id, video_id) REFERENCES playlist_jobs(job_id, video_id))''')
                connection.execute('CREATE INDEX queue_state_position ON queue_items(state, position, created_at)')
                connection.execute('PRAGMA user_version = 1')
            if version in (0, 1):
                # Job links survive queue cleanup, so retry history belongs here.
                connection.execute('''ALTER TABLE playlist_jobs ADD COLUMN
                    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 1)''')
                connection.execute('''UPDATE playlist_jobs SET retry_count=COALESCE((
                    SELECT retry_count FROM queue_items q
                    WHERE q.job_id=playlist_jobs.job_id), 0)''')
                connection.execute('PRAGMA user_version = 2')

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('PRAGMA busy_timeout = 5000')
            connection.execute('PRAGMA journal_mode = WAL')
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self):
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _playlist(row):
        values = dict(row)
        values['enabled'] = bool(values['enabled'])
        values['removed'] = bool(values['removed'])
        return PlaylistRecord(**values)

    def add_or_reactivate_playlist(self, playlist_id, url, title):
        with self._write() as connection:
            connection.execute('''INSERT INTO playlists
                (playlist_id, url, title, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                url=excluded.url, title=excluded.title, enabled=1, removed=0''',
                               (playlist_id, url, title, time.time()))
            return self._playlist(connection.execute(
                'SELECT * FROM playlists WHERE playlist_id=?', (playlist_id,)
            ).fetchone())

    def list_playlists(self):
        with self._connect() as connection:
            return [self._playlist(row) for row in connection.execute(
                'SELECT * FROM playlists WHERE removed=0 ORDER BY id')]

    def set_playlist_enabled(self, id, enabled):
        with self._write() as connection:
            connection.execute('UPDATE playlists SET enabled=? WHERE id=?',
                               (bool(enabled), id))

    def soft_remove_playlist(self, id):
        with self._write() as connection:
            connection.execute('UPDATE playlists SET enabled=0, removed=1 WHERE id=?', (id,))

    def set_auto_sync_paused(self, paused):
        with self._write() as connection:
            connection.execute('''INSERT INTO settings VALUES ('auto_sync_paused', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                               ('1' if paused else '0',))

    def auto_sync_paused(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='auto_sync_paused'"
            ).fetchone()
            return row is not None and row['value'] == '1'

    def apply_snapshot(self, playlist_id, videos, synced_at):
        """Apply one successful snapshot, retaining absent memberships and jobs.

        playlist_id is the internal PlaylistRecord.id. Video mappings contain
        video_id (or yt-dlp's id), with optional url/title/duration/availability.
        Return unique video IDs in observed order.
        """
        seen = set()
        ordered = []
        with self._write() as connection:
            if connection.execute('SELECT id FROM playlists WHERE id=?', (playlist_id,)).fetchone() is None:
                raise ValueError('Unknown playlist')
            connection.execute('''UPDATE playlist_videos SET present=0,
                removed_at=COALESCE(removed_at, ?),
                completed_at_removal=COALESCE(completed_at_removal, (
                    SELECT q.finished_at FROM queue_items q
                    WHERE q.video_id=playlist_videos.video_id AND q.state='completed'))
                WHERE playlist_id=? AND present=1''', (synced_at, playlist_id))
            for video in videos:
                video_id = video['video_id'] if 'video_id' in video else video['id']
                if video_id in seen:
                    continue
                seen.add(video_id)
                position = len(ordered)
                ordered.append(video_id)
                connection.execute('''INSERT INTO videos VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET url=excluded.url,
                    title=excluded.title, duration=excluded.duration,
                    availability=excluded.availability, last_seen_at=excluded.last_seen_at''',
                    (video_id, video.get('url', f'https://www.youtube.com/watch?v={video_id}'),
                     video.get('title', ''), video.get('duration'),
                     video.get('availability', 'unknown'), synced_at, synced_at))
                connection.execute('''INSERT INTO playlist_videos
                    (playlist_id, video_id, position, present, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                    position=excluded.position, present=1, last_seen_at=excluded.last_seen_at,
                    removed_at=NULL, completed_at_removal=NULL''',
                    (playlist_id, video_id, position, synced_at, synced_at))
            connection.execute('''UPDATE playlists SET last_sync_started_at=?,
                last_sync_success_at=?, last_error='', video_count=? WHERE id=?''',
                (synced_at, synced_at, len(ordered), playlist_id))
        return ordered

    def record_sync_failure(self, playlist_id, message, at):
        with self._write() as connection:
            connection.execute('''UPDATE playlists SET last_sync_started_at=?,
                last_error=? WHERE id=?''', (at, message, playlist_id))

    def link_job(self, video_id, job_id, method):
        """Link once globally; repeated linking of the same job updates its method."""
        now = time.time()
        with self._write() as connection:
            existing = connection.execute(
                'SELECT job_id FROM playlist_jobs WHERE video_id=?', (video_id,)
            ).fetchone()
            if existing is not None and existing['job_id'] != job_id:
                raise sqlite3.IntegrityError('Video already linked to another job')
            connection.execute('''INSERT INTO playlist_jobs
                (video_id, job_id, method, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET method=excluded.method,
                updated_at=excluded.updated_at''', (video_id, job_id, method, now, now))

    def find_job_id(self, video_id):
        with self._connect() as connection:
            row = connection.execute('SELECT job_id FROM playlist_jobs WHERE video_id=?',
                                     (video_id,)).fetchone()
            return row['job_id'] if row else None

    def enqueue(self, job_id, video_id, *, completed=False):
        """Append once; reconciled completions are persisted without runnable work."""
        with self._write() as connection:
            row = connection.execute('SELECT * FROM queue_items WHERE job_id=?',
                                     (job_id,)).fetchone()
            if row is not None:
                if row['video_id'] != video_id:
                    raise sqlite3.IntegrityError('Job already queued for another video')
                return QueueItem(**dict(row))
            history = connection.execute(
                'SELECT retry_count FROM playlist_jobs WHERE job_id=?', (job_id,)
            ).fetchone()
            now = time.time()
            connection.execute('''INSERT INTO queue_items
                (job_id, video_id, created_at, retry_count, state, finished_at)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (job_id, video_id, now, history['retry_count'] if history else 0,
                 'completed' if completed else 'waiting', now if completed else None))
            return QueueItem(**dict(connection.execute(
                'SELECT * FROM queue_items WHERE job_id=?', (job_id,)).fetchone()))

    def remove_unreferenced_waiting(self):
        with self._write() as connection:
            items = connection.execute('''SELECT job_id FROM queue_items q
                WHERE state='waiting' AND NOT EXISTS (
                    SELECT 1 FROM playlist_videos pv JOIN playlists p ON p.id=pv.playlist_id
                    WHERE pv.video_id=q.video_id AND pv.present=1
                        AND p.enabled=1 AND p.removed=0) ORDER BY position''').fetchall()
            connection.executemany('DELETE FROM queue_items WHERE job_id=?',
                                   [(row['job_id'],) for row in items])
            return [row['job_id'] for row in items]

    def claim_next(self):
        with self._write() as connection:
            row = connection.execute('''SELECT * FROM queue_items WHERE state='waiting'
                ORDER BY position, created_at LIMIT 1''').fetchone()
            if row is None:
                return None
            connection.execute('''UPDATE queue_items SET state='processing', started_at=?,
                finished_at=NULL WHERE job_id=?''', (time.time(), row['job_id']))
            return QueueItem(**dict(connection.execute(
                'SELECT * FROM queue_items WHERE job_id=?', (row['job_id'],)).fetchone()))

    @staticmethod
    def _finish(connection, job_id, state, error):
        row = connection.execute('SELECT * FROM queue_items WHERE job_id=?',
                                 (job_id,)).fetchone()
        if row is None or row['state'] != 'processing':
            return
        retry = state in ('failed', 'interrupted') and row['retry_count'] == 0
        connection.execute('UPDATE playlist_jobs SET retry_count=? WHERE job_id=?',
                           (int(retry) + row['retry_count'], job_id))
        connection.execute('''UPDATE queue_items SET state=?, retry_count=?,
            error=?, finished_at=? WHERE job_id=?''',
            ('waiting' if retry else state, int(retry) + row['retry_count'],
             error, None if retry else time.time(), job_id))

    def finish_queue_item(self, job_id, state, error=''):
        """One failed/interrupted attempt retries in place; the second is terminal."""
        if state not in ('completed', 'failed', 'interrupted'):
            raise ValueError('Expected terminal queue state')
        with self._write() as connection:
            self._finish(connection, job_id, state, error)

    def recover_stale_processing(self, active_job_ids):
        active = set(active_job_ids)
        with self._write() as connection:
            stale = [row['job_id'] for row in connection.execute(
                "SELECT job_id FROM queue_items WHERE state='processing' ORDER BY position"
            ) if row['job_id'] not in active]
            for job_id in stale:
                self._finish(connection, job_id, 'interrupted', 'Processing interrupted during restart')
            return stale
