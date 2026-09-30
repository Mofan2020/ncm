"""Enhanced downloader with concurrent downloads support."""
import os
import sys
import time
import random
import shutil
import threading
import concurrent.futures
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from src.config import get_settings
from src.i18n import t


@dataclass
class DownloadTask:
    """Represents a single download task."""
    song_id: str
    song_name: str
    artists: str
    quality: str
    url: Optional[str] = None
    filepath: Optional[str] = None
    status: str = "waiting"  # waiting, downloading, completed, failed, skipped, paused
    progress: float = 0.0
    speed: float = 0.0
    downloaded_size: int = 0
    total_size: int = 0
    error: Optional[str] = None
    start_time: float = 0
    retries: int = 0


@dataclass
class DownloadStats:
    """Download session statistics."""
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    failed_songs: List[Dict] = field(default_factory=list)
    time_elapsed: float = 0


class SongDownloader:
    """Enhanced downloader with concurrent downloads."""
    
    def __init__(self, download_dir: str, quality: str = 'standard', 
                 overwrite: bool = False, max_concurrent: int = 2):
        self.download_dir = Path(download_dir)
        self.quality = quality
        self.overwrite = overwrite
        self.max_concurrent = max(1, min(max_concurrent, 3))
        
        self.chunk_size = 16384
        self.timeout = 60
        self.max_retries = 3
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
            'Referer': 'https://music.163.com/',
            'Accept-Encoding': 'identity',
            'Accept': '*/*',
            'Range': 'bytes=0-',
        }
        
        self._ensure_download_dir()
        
        # Thread safety
        self._lock = threading.RLock()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Not paused by default
        self._cancelled = False
        
        # Callbacks
        self._progress_callback: Optional[Callable] = None
        self._task_complete_callback: Optional[Callable] = None
        self._stats_callback: Optional[Callable] = None
        
        # Active tasks
        self._tasks: Dict[str, DownloadTask] = {}
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
    
    def _ensure_download_dir(self):
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            test_file = self.download_dir / '.test_write'
            test_file.write_text('test')
            test_file.unlink()
        except Exception as e:
            fallback = Path.cwd() / 'downloads'
            fallback.mkdir(exist_ok=True)
            self.download_dir = fallback
            print(f"Using fallback dir: {fallback}")
    
    def _sanitize_filename(self, filename: str) -> str:
        if not filename:
            return "Unknown"
        invalid = ['/', ':', '\\', '*', '?', '"', '<', '>', '|', '\0']
        for c in invalid:
            filename = filename.replace(c, '-')
        filename = ' '.join(filename.split())
        max_len = 200
        if len(filename) > max_len:
            name, ext = os.path.splitext(filename)
            filename = name[:max_len - len(ext)] + ext
        return filename
    
    def _get_file_extension(self, headers: Dict) -> str:
        ext = ".mp3"
        content_type = headers.get('Content-Type', '')
        if 'audio/flac' in content_type:
            ext = ".flac"
        elif 'audio/wav' in content_type:
            ext = ".wav"
        elif 'audio/ogg' in content_type:
            ext = ".ogg"
        
        content_disp = headers.get('Content-Disposition', '')
        if 'filename=' in content_disp:
            try:
                fname = content_disp.split('filename=')[1].strip('"')
                from urllib.parse import unquote
                fname = unquote(fname)
                _, fext = os.path.splitext(fname)
                if fext:
                    ext = fext
            except Exception:
                pass
        return ext
    
    # Callback setters
    def set_progress_callback(self, callback: Callable[[DownloadTask], None]):
        self._progress_callback = callback
    
    def set_task_complete_callback(self, callback: Callable[[DownloadTask], None]):
        self._task_complete_callback = callback
    
    def set_stats_callback(self, callback: Callable[[DownloadStats], None]):
        self._stats_callback = callback
    
    def pause(self):
        """Pause all downloads."""
        self._pause_event.clear()
    
    def resume(self):
        """Resume paused downloads."""
        self._pause_event.set()
    
    def cancel(self):
        """Cancel all downloads."""
        self._cancelled = True
        self._pause_event.set()  # Unblock any waiting threads
    
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()
    
    def is_cancelled(self) -> bool:
        return self._cancelled
    
    def download_single(self, task: DownloadTask, api_client=None) -> bool:
        """Download a single song."""
        if not task.url:
            task.status = "failed"
            task.error = "No URL provided"
            return False
        
        task.status = "downloading"
        task.start_time = time.time()
        self._notify_progress(task)
        
        temp_file = None
        for retry in range(self.max_retries):
            if self._cancelled:
                task.status = "failed"
                task.error = "Cancelled"
                return False
            
            # Wait if paused
            self._pause_event.wait()
            
            try:
                # HEAD request for file info
                head_resp = requests.head(task.url, headers=self.headers, timeout=self.timeout, allow_redirects=True)
                head_resp.raise_for_status()
                
                ext = self._get_file_extension(head_resp.headers)
                filename = self._sanitize_filename(f"{task.artists} - {task.song_name}{ext}")
                filepath = self.download_dir / filename
                
                # Check existing file
                if filepath.exists() and not self.overwrite:
                    task.status = "skipped"
                    task.filepath = str(filepath)
                    self._notify_complete(task)
                    return True
                
                temp_file = filepath.with_suffix(filepath.suffix + '.tmp')
                total_size = int(head_resp.headers.get('content-length', 0))
                task.total_size = total_size
                
                with requests.get(task.url, headers=self.headers, stream=True, 
                                 timeout=self.timeout, allow_redirects=True) as resp:
                    resp.raise_for_status()
                    
                    downloaded = 0
                    last_update = time.time()
                    
                    with open(temp_file, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=self.chunk_size):
                            if self._cancelled:
                                raise Exception("Cancelled")
                            self._pause_event.wait()  # Block if paused
                            
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                task.downloaded_size = downloaded
                                task.progress = (downloaded / total_size * 100) if total_size > 0 else 0
                                
                                now = time.time()
                                if now - last_update >= 0.5:
                                    elapsed = now - task.start_time
                                    task.speed = downloaded / elapsed / 1024 if elapsed > 0 else 0
                                    last_update = now
                                    self._notify_progress(task)
                
                # Verify file
                if total_size > 0 and temp_file.stat().st_size != total_size:
                    raise Exception(f"Incomplete download: {temp_file.stat().st_size}/{total_size}")
                
                # Move temp to final
                with self._lock:
                    if filepath.exists():
                        filepath.unlink()
                    shutil.move(str(temp_file), str(filepath))
                
                task.filepath = str(filepath)
                task.status = "completed"
                task.progress = 100.0
                self._notify_progress(task)
                self._notify_complete(task)
                return True
                
            except Exception as e:
                task.error = str(e)
                task.retries = retry + 1
                if retry < self.max_retries - 1:
                    time.sleep((2 ** retry) + random.uniform(0.5, 1.5))
                    # Try to re-fetch URL if 403
                    if '403' in str(e) and api_client:
                        new_url = api_client.get_song_url(task.song_id, task.quality)
                        if new_url:
                            task.url = new_url
                else:
                    task.status = "failed"
                    self._notify_complete(task)
                    return False
            finally:
                if temp_file and temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
        
        return False
    
    def _notify_progress(self, task: DownloadTask):
        if self._progress_callback:
            try:
                self._progress_callback(task)
            except Exception:
                pass
    
    def _notify_complete(self, task: DownloadTask):
        if self._task_complete_callback:
            try:
                self._task_complete_callback(task)
            except Exception:
                pass
    
    def _notify_stats(self, stats: DownloadStats):
        if self._stats_callback:
            try:
                self._stats_callback(stats)
            except Exception:
                pass
    
    def batch_download(self, songs_info: List[Dict], api_client=None) -> DownloadStats:
        """Download multiple songs concurrently."""
        if not songs_info:
            return DownloadStats()
        
        self._cancelled = False
        self._pause_event.set()
        
        # Create tasks
        tasks = []
        for song in songs_info:
            song_id = str(song.get('id', ''))
            song_name = song.get('name', 'Unknown')
            artists_data = song.get('artists') or song.get('ar', [])
            artists = ", ".join([a.get('name', 'Unknown') for a in artists_data])
            
            # Get URL with quality fallback
            url = None
            qualities = [self.quality] if self.quality == 'standard' else [self.quality, 'standard']
            for q in qualities:
                if api_client:
                    url = api_client.get_song_url(song_id, q)
                    if url:
                        break
            
            task = DownloadTask(
                song_id=song_id,
                song_name=song_name,
                artists=artists,
                quality=self.quality,
                url=url
            )
            tasks.append(task)
            with self._lock:
                self._tasks[song_id] = task
        
        stats = DownloadStats(total=len(tasks))
        start_time = time.time()
        
        def update_stats():
            with self._lock:
                stats.success = sum(1 for t in self._tasks.values() if t.status == "completed")
                stats.failed = sum(1 for t in self._tasks.values() if t.status == "failed")
                stats.skipped = sum(1 for t in self._tasks.values() if t.status == "skipped")
                stats.time_elapsed = time.time() - start_time
                stats.failed_songs = [
                    {'name': t.song_name, 'artists': t.artists, 'id': t.song_id, 'error': t.error}
                    for t in self._tasks.values() if t.status == "failed"
                ]
            self._notify_stats(stats)
        
        def on_task_complete(task: DownloadTask):
            update_stats()
        
        self.set_task_complete_callback(on_task_complete)
        
        # Execute with thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            self._executor = executor
            futures = {executor.submit(self.download_single, task, api_client): task for task in tasks}
            
            for future in concurrent.futures.as_completed(futures):
                if self._cancelled:
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception as e:
                    task = futures[future]
                    task.status = "failed"
                    task.error = str(e)
        
        self._executor = None
        update_stats()
        
        # Save failed list
        if stats.failed_songs:
            fail_log = self.download_dir / 'failed_downloads.txt'
            try:
                with open(fail_log, 'w', encoding='utf-8') as f:
                    f.write(f"Failed downloads - {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                    for song in stats.failed_songs:
                        f.write(f"{song['artists']} - {song['name']} (ID: {song['id']})\n")
            except Exception:
                pass
        
        return stats


import requests