"""Synchronize Playlist membership with durable, globally deduplicated jobs."""
from dataclasses import asdict, dataclass
import time
import uuid

from pipeline import extract_youtube_video_id
from playlist_sync import fetch_playlist


@dataclass
class SyncSummary:
    playlist_id: int
    created: int = 0
    linked: int = 0
    error: str = ''


class PlaylistService:
    def __init__(self, db, store, fetcher=fetch_playlist):
        self.db = db
        self.store = store
        self.fetcher = fetcher

    @staticmethod
    def _new_job(video, playlist_id, job_id=None):
        return {
            'id': job_id or uuid.uuid4().hex,
            'title': video.title, 'source': 'youtube',
            'url': f'https://www.youtube.com/watch?v={video.video_id}',
            'youtube_video_id': video.video_id,
            'created': time.time(), 'status': 'waiting', 'segments': [],
            'error': '', 'edited_text': None, 'output_naming': 'title',
            'model': 'medium', 'discovery_source': 'playlist',
            'media_managed': True, 'auto_cleanup_media': True,
            'discovery_playlist_id': playlist_id,
            'discovery_marker': f'youtube:{video.video_id}',
        }

    def _sync_video(self, video, playlist_id, summary):
        job_id = self.db.find_job_id(video.video_id)
        if job_id is not None:
            try:
                job = self.store.load(job_id)
            except FileNotFoundError:
                # The immutable link is authoritative even if job.json vanished.
                item = self.db.enqueue(job_id, video.video_id)
                if item.state == 'completed':
                    return
                job = self._new_job(video, playlist_id, job_id)
                self.store.save(job)
        else:
            job = self.store.find_youtube_job(video.video_id)
            if job is None:
                job = self._new_job(video, playlist_id)
                self.store.save(job)
                summary.created += 1
            self.db.link_job(video.video_id, job['id'], 'discovery')
            summary.linked += 1
        if job.get('status') == 'complete':
            self.db.enqueue(job['id'], video.video_id, completed=True)
        elif job.get('status') == 'waiting':
            self.db.enqueue(job['id'], video.video_id)

    def sync_playlist(self, playlist_id: int) -> SyncSummary:
        summary = SyncSummary(playlist_id)
        playlist = next((item for item in self.db.list_playlists()
                         if item.id == playlist_id and item.enabled), None)
        if playlist is None:
            return summary
        started = time.time()
        try:
            snapshot = self.fetcher(playlist.url)
            videos = []
            seen = set()
            for video in snapshot.videos:
                canonical = f'https://www.youtube.com/watch?v={video.video_id}'
                if (extract_youtube_video_id(canonical) != video.video_id
                        or video.video_id in seen):
                    continue
                seen.add(video.video_id)
                videos.append(video)
            # Filesystem writes only begin after the snapshot transaction commits.
            self.db.apply_snapshot(playlist_id, [asdict(video) for video in videos], started)
            for video in videos:
                self.store.reconcile_youtube(
                    video.video_id,
                    lambda: self._sync_video(video, playlist_id, summary),
                )
            self.db.remove_unreferenced_waiting()
        except Exception as exc:
            summary.error = str(exc)
            self.db.record_sync_failure(playlist_id, summary.error, started)
        return summary

    def sync_enabled(self) -> list[SyncSummary]:
        summaries = [self.sync_playlist(playlist.id)
                     for playlist in self.db.list_playlists() if playlist.enabled]
        self.db.remove_unreferenced_waiting()
        return summaries
