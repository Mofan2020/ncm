"""GUI backend bridge for pywebview."""
import os
import sys
import json
import threading
import time
from typing import Optional, Dict, Any, List
from pathlib import Path

# Core modules
from src.config import get_settings
from src.i18n import get_i18n, t
from src.auth import get_login_manager, LoginStatus
from src.core import get_api
from src.core.downloader import SongDownloader, DownloadStats


class GuiBridge:
    """Bridge between Python backend and HTML frontend."""
    
    def __init__(self):
        self.settings = get_settings()
        self.i18n = get_i18n()
        self.login_manager = get_login_manager()
        self.api = get_api(debug=self.settings.debug)
        self.downloader: Optional[SongDownloader] = None
        self.current_playlist = None
        self.playlist_songs = []
        self.selected_songs = set()
        self.download_stats = DownloadStats()
        self._window = None
        self._callbacks = {}
        
        # Register login callbacks
        self.login_manager.on('status_change', self._on_login_status_change)
        self.login_manager.on('qrcode_update', self._on_qrcode_update)
        self.login_manager.on('login_success', self._on_login_success)
        self.login_manager.on('login_failed', self._on_login_failed)
    
    def set_window(self, window):
        self._window = window
    
    def _call_js(self, func: str, *args):
        """Call JavaScript function in frontend."""
        if self._window:
            try:
                args_json = json.dumps(args)
                self._window.evaluate_js(f"{func}({args_json})")
            except Exception as e:
                print(f"JS call error: {e}")
    
    # === Settings API ===
    def get_settings(self) -> Dict[str, Any]:
        """Get all settings."""
        return {
            'download': {
                'quality': self.settings.download.quality,
                'max_concurrent': self.settings.download.max_concurrent,
                'overwrite': self.settings.download.overwrite,
                'download_dir': self.settings.download.download_dir or str(Path.home() / 'Music' / 'Downloads'),
                'skip_url_check': self.settings.download.skip_url_check,
            },
            'ui': {
                'language': self.settings.ui.language,
                'theme': self.settings.ui.theme,
                'window_width': self.settings.ui.window_width,
                'window_height': self.settings.ui.window_height,
            },
            'auth': {
                'remember_login': self.settings.auth.remember_login,
                'auto_login': self.settings.auth.auto_login,
            },
            'debug': self.settings.debug,
        }
    
    def update_settings(self, category: str, data: Dict[str, Any]) -> bool:
        """Update settings."""
        try:
            if category == 'download':
                self.settings.update_download(**data)
            elif category == 'ui':
                self.settings.update_ui(**data)
                if 'language' in data:
                    self.i18n.set_language(data['language'])
            elif category == 'auth':
                self.settings.update_auth(**data)
            elif category == 'debug':
                self.settings.debug = data.get('debug', False)
                self.api = get_api(debug=self.settings.debug)
            return True
        except Exception as e:
            print(f"Update settings error: {e}")
            return False
    
    def choose_directory(self) -> Optional[str]:
        """Open folder picker dialog."""
        if self._window:
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,  # type: ignore
                directory=self.settings.download.download_dir or str(Path.home())
            )
            if result:
                return result[0]
        return None
    
    # === Language API ===
    def get_languages(self) -> Dict[str, str]:
        return self.i18n.available_languages
    
    def set_language(self, lang: str) -> bool:
        result = self.i18n.set_language(lang)
        if result:
            self._call_js('onLanguageChanged', self.get_translations())
        return result
    
    def get_translations(self) -> Dict[str, str]:
        """Get all translations for current language (flattened)."""
        translations = self.i18n.get_all_translations(self.i18n.current_language)
        # Flatten nested dict
        flat = {}
        def flatten(d, prefix=''):
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    flatten(v, key)
                else:
                    flat[key] = v
        flatten(translations)
        return flat
    
    # === Login API ===
    def get_login_status(self) -> Dict[str, Any]:
        return {
            'status': self.login_manager.status.value,
            'is_logged_in': self.login_manager.is_logged_in,
            'user': {
                'user_id': self.login_manager.user_info.user_id if self.login_manager.user_info else '',
                'nickname': self.login_manager.user_info.nickname if self.login_manager.user_info else '',
                'avatar_url': self.login_manager.user_info.avatar_url if self.login_manager.user_info else '',
                'vip_type': self.login_manager.user_info.vip_type if self.login_manager.user_info else 0,
            } if self.login_manager.user_info else None
        }
    
    def login_qrcode(self) -> bool:
        return self.login_manager.login_qrcode()
    
    def refresh_qrcode(self) -> bool:
        return self.login_manager.refresh_qrcode()
    
    def cancel_login(self):
        self.login_manager.cancel_login()
    
    def send_phone_code(self, phone: str, ctcode: str = "86") -> bool:
        return self.login_manager.send_phone_code(phone, ctcode)
    
    def login_phone(self, phone: str, code: str, ctcode: str = "86") -> bool:
        return self.login_manager.login_phone(phone, code, ctcode)
    
    def login_cookie(self, cookie: str) -> bool:
        return self.login_manager.login_cookie(cookie)
    
    def logout(self):
        self.login_manager.logout()
    
    def _on_login_status_change(self, status: LoginStatus):
        self._call_js('onLoginStatusChange', {'status': status.value})
    
    def _on_qrcode_update(self, qrcode_url: str):
        # Generate QR code image
        img_data = self.login_manager.get_qrcode_image(qrcode_url)
        if img_data:
            import base64
            b64 = base64.b64encode(img_data).decode()
            self._call_js('onQrcodeUpdate', {'url': qrcode_url, 'image': f'data:image/png;base64,{b64}'})
        else:
            self._call_js('onQrcodeUpdate', {'url': qrcode_url, 'image': None})
    
    def _on_login_success(self, user_info):
        self._call_js('onLoginSuccess', {
            'user_id': user_info.user_id,
            'nickname': user_info.nickname,
            'avatar_url': user_info.avatar_url,
            'vip_type': user_info.vip_type,
        })
    
    def _on_login_failed(self, error: str):
        self._call_js('onLoginFailed', {'error': error})
    
    # === Playlist API ===
    def fetch_playlist(self, playlist_input: str) -> Dict[str, Any]:
        """Fetch playlist info and songs."""
        # Extract playlist ID from URL
        playlist_id = self._extract_playlist_id(playlist_input)
        if not playlist_id:
            return {'success': False, 'error': t('messages.api_error', message='Invalid playlist ID/URL')}
        
        self._call_js('onStatusUpdate', {'status': 'fetching_playlist', 'message': t('status.fetching_playlist')})
        
        playlist_info = self.api.get_playlist_info(playlist_id)
        if not playlist_info:
            return {'success': False, 'error': t('status.no_playlist')}
        
        self._call_js('onStatusUpdate', {'status': 'fetching_songs', 'message': t('status.fetching_songs')})
        
        songs = self.api.get_playlist_songs(playlist_id)
        if not songs:
            return {'success': False, 'error': 'Failed to get songs'}
        
        self.current_playlist = playlist_info
        self.playlist_songs = songs
        self.selected_songs = set()
        
        # Format for frontend
        tracks = []
        for i, song in enumerate(songs):
            artists_data = song.get('artists') or song.get('ar', [])
            artists = ", ".join([a.get('name', 'Unknown') for a in artists_data])
            tracks.append({
                'index': i,
                'id': str(song.get('id', '')),
                'name': song.get('name', 'Unknown'),
                'artists': artists,
                'album': song.get('al', {}).get('name', '') if song.get('al') else '',
                'duration': song.get('dt', 0),
                'fee': song.get('fee', 0),  # 0=free, 1=VIP, etc.
                'privilege': song.get('privilege', {}),
            })
        
        return {
            'success': True,
            'playlist': {
                'id': playlist_info.get('id', ''),
                'name': playlist_info.get('name', 'Unknown'),
                'creator': playlist_info.get('creator', {}).get('nickname', 'Unknown'),
                'cover_url': playlist_info.get('coverImgUrl', ''),
                'track_count': playlist_info.get('trackCount', len(tracks)),
                'play_count': playlist_info.get('playCount', 0),
                'description': playlist_info.get('description', ''),
            },
            'tracks': tracks
        }
    
    def _extract_playlist_id(self, input_str: str) -> Optional[str]:
        import re
        if input_str.isdigit():
            return input_str
        patterns = [
            r'id=(\d+)',
            r'playlist/(\d+)',
            r'play/\d+/(\d+)',
            r'show\?id=(\d+)',
        ]
        for p in patterns:
            m = re.search(p, input_str)
            if m:
                return m.group(1)
        return None
    
    def select_songs(self, indices: List[int]) -> Dict[str, Any]:
        self.selected_songs = set(indices)
        return {'selected_count': len(self.selected_songs), 'total': len(self.playlist_songs)}
    
    def select_all_songs(self) -> Dict[str, Any]:
        self.selected_songs = set(range(len(self.playlist_songs)))
        return {'selected_count': len(self.selected_songs), 'total': len(self.playlist_songs)}
    
    def deselect_all_songs(self) -> Dict[str, Any]:
        self.selected_songs = set()
        return {'selected_count': 0, 'total': len(self.playlist_songs)}
    
    # === Download API ===
    def start_download(self, options: Dict[str, Any]) -> Dict[str, Any]:
        if not self.playlist_songs:
            return {'success': False, 'error': t('status.no_playlist')}
        if not self.selected_songs:
            return {'success': False, 'error': t('status.no_songs_selected')}
        
        # Check if login required for VIP songs
        need_login = False
        for idx in self.selected_songs:
            if idx < len(self.playlist_songs):
                song = self.playlist_songs[idx]
                if song.get('fee', 0) > 0 and not self.login_manager.is_logged_in:
                    need_login = True
                    break
        
        if need_login:
            return {'success': False, 'error': t('status.login_required'), 'need_login': True}
        
        # Prepare download
        songs_to_download = [self.playlist_songs[i] for i in self.selected_songs if i < len(self.playlist_songs)]
        
        download_dir = options.get('download_dir') or self.settings.download.download_dir
        if not download_dir:
            download_dir = str(Path.home() / 'Music' / 'Downloads')
        
        quality = options.get('quality', self.settings.download.quality)
        overwrite = options.get('overwrite', self.settings.download.overwrite)
        max_concurrent = options.get('max_concurrent', self.settings.download.max_concurrent)
        
        self.downloader = SongDownloader(
            download_dir=download_dir,
            quality=quality,
            overwrite=overwrite,
            max_concurrent=max_concurrent
        )
        
        # Set callbacks
        self.downloader.set_progress_callback(self._on_download_progress)
        self.downloader.set_stats_callback(self._on_download_stats)
        
        # Start download in background thread
        def download_thread():
            stats = self.downloader.batch_download(songs_to_download, self.api)
            self.download_stats = stats
            self._call_js('onDownloadComplete', {
                'success': True,
                'stats': {
                    'total': stats.total,
                    'success': stats.success,
                    'failed': stats.failed,
                    'skipped': stats.skipped,
                    'time_elapsed': stats.time_elapsed,
                }
            })
        
        threading.Thread(target=download_thread, daemon=True).start()
        
        return {'success': True}
    
    def pause_download(self):
        if self.downloader:
            self.downloader.pause()
    
    def resume_download(self):
        if self.downloader:
            self.downloader.resume()
    
    def cancel_download(self):
        if self.downloader:
            self.downloader.cancel()
    
    def _on_download_progress(self, task):
        self._call_js('onDownloadProgress', {
            'song_id': task.song_id,
            'name': task.song_name,
            'artists': task.artists,
            'status': task.status,
            'progress': task.progress,
            'speed': task.speed,
            'downloaded_size': task.downloaded_size,
            'total_size': task.total_size,
            'error': task.error,
        })
    
    def _on_download_stats(self, stats: DownloadStats):
        self._call_js('onDownloadStats', {
            'total': stats.total,
            'success': stats.success,
            'failed': stats.failed,
            'skipped': stats.skipped,
            'time_elapsed': stats.time_elapsed,
        })
    
    def open_download_folder(self):
        if self.downloader:
            import subprocess
            import platform
            path = str(self.downloader.download_dir)
            if platform.system() == 'Darwin':
                subprocess.run(['open', path])
            elif platform.system() == 'Windows':
                subprocess.run(['explorer', path])
            else:
                subprocess.run(['xdg-open', path])
    
    # === Utility ===
    def get_api_stats(self) -> Dict[str, Any]:
        return self.api.get_request_stats()
    
    def minimize_window(self):
        if self._window:
            self._window.minimize()
    
    def maximize_window(self):
        if self._window:
            self._window.maximize()
    
    def close_window(self):
        if self._window:
            self._window.destroy()


import webview