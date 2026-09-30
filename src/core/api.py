"""Enhanced NetEase Cloud Music API with authentication support."""
import requests
import time
import random
import json
import logging
from typing import Optional, Dict, Any, List
from src.auth import get_login_manager
from src.config import get_settings


class NeteaseAPI:
    """Enhanced NetEase Cloud Music API client with login support."""
    
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.login_manager = get_login_manager()
        self.settings = get_settings()
        
        # API domains for rotation
        self.api_domains = [
            "https://music.163.com/api",
            "https://api.music.163.com",
            "https://music.163.com"
        ]
        self.current_domain = 0
        self.base_url = self.api_domains[self.current_domain]
        
        # Session with cookies from login manager
        self.session = self.login_manager.get_session()
        self.max_retries = 5
        self.retry_delay = 1
        
        # Stats
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'domain_switches': 0,
        }
        
        # Error codes
        self.error_codes = {
            400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden',
            404: 'Not Found', 429: 'Too Many Requests',
            500: 'Internal Server Error', 502: 'Bad Gateway',
            503: 'Service Unavailable', 504: 'Gateway Timeout'
        }
        self.api_error_codes = {
            200: 'Success', 400: 'Param Error', 401: 'Unauthorized',
            403: 'Forbidden', 404: 'Not Found', -460: 'Rate Limited',
            -461: 'Param Error', -462: 'Not Found', -463: 'Already Exists',
            -475: 'Login Required', -478: 'No Permission', -500: 'Server Error'
        }
        
        # Setup logging
        self.logger = logging.getLogger("netease_api")
        if debug:
            self.logger.setLevel(logging.DEBUG)
    
    def _switch_domain(self):
        self.current_domain = (self.current_domain + 1) % len(self.api_domains)
        self.base_url = self.api_domains[self.current_domain]
        self.stats['domain_switches'] += 1
        self._log(f"Switched API domain to: {self.base_url}")
    
    def _get_timestamp(self) -> str:
        return str(int(time.time() * 1000))
    
    def _log(self, message: str, level: str = 'info'):
        if level == 'debug' and not self.debug:
            return
        getattr(self.logger, level)(message)
    
    def _request(self, endpoint: str, params: Optional[Dict] = None, 
                 method: str = 'GET', use_auth: bool = True) -> Optional[Dict]:
        """Enhanced request with authentication support."""
        self.stats['total_requests'] += 1
        params = params or {}
        
        if 'timestamp' not in params:
            params['timestamp'] = self._get_timestamp()
        
        if use_auth and self.login_manager.is_logged_in:
            # Ensure cookies are synced
            pass  # Session already has cookies from login manager
        
        for retry in range(self.max_retries):
            try:
                url = f"{self.base_url}{endpoint}"
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
                    'Referer': 'https://music.163.com/',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json, text/plain, */*',
                }
                
                if method.upper() == 'GET':
                    resp = self.session.get(url, headers=headers, params=params, timeout=(5, 15))
                else:
                    resp = self.session.post(url, headers=headers, data=params, timeout=(5, 15))
                
                resp.raise_for_status()
                result = resp.json()
                
                code = result.get('code')
                if code == 200:
                    self.stats['successful_requests'] += 1
                    return result
                
                # Handle auth errors
                if code in [401, -475] or result.get('needLogin'):
                    self._log("Login required, session may have expired")
                    if not self.login_manager.is_logged_in:
                        return result  # Return error for caller to handle
                    # Try to refresh by checking status
                    continue
                
                if code == 403:
                    self._switch_domain()
                    continue
                
                if code in [429, -460]:
                    delay = (retry + 1) * 5 + random.uniform(1, 3)
                    self._log(f"Rate limited, waiting {delay:.1f}s")
                    time.sleep(delay)
                    continue
                
                error_msg = self.api_error_codes.get(code, f'Unknown error {code}')
                self._log(f"API error: {error_msg}")
                return result
                
            except requests.exceptions.RequestException as e:
                self._log(f"Request error: {e}")
                if retry < self.max_retries - 1:
                    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                        self._switch_domain()
                    delay = self.retry_delay * (2 ** retry) + random.uniform(0.5, 1.5)
                    time.sleep(delay)
            except json.JSONDecodeError:
                self._log("Invalid JSON response")
                return None
        
        self.stats['failed_requests'] += 1
        return None
    
    # Playlist methods
    def get_playlist_info(self, playlist_id: str) -> Optional[Dict]:
        """Get playlist detail info."""
        self._log(f"Fetching playlist info: {playlist_id}")
        
        for endpoint in ["/v3/playlist/detail", "/playlist/detail"]:
            result = self._request(endpoint, {'id': playlist_id})
            if result and result.get('code') == 200:
                playlist = result.get('playlist') or result.get('result')
                if playlist:
                    self._log(f"Got playlist: {playlist.get('name', 'Unknown')}")
                    return playlist
        return None
    
    def get_playlist_songs(self, playlist_id: str) -> Optional[List[Dict]]:
        """Get all songs from playlist."""
        self._log(f"Fetching songs for playlist: {playlist_id}")
        
        playlist = self.get_playlist_info(playlist_id)
        if not playlist:
            return None
        
        tracks = playlist.get('tracks', [])
        track_count = playlist.get('trackCount', len(tracks))
        
        # If tracks incomplete, fetch via trackIds
        if len(tracks) < track_count or (not tracks and 'trackIds' in playlist):
            track_ids = [str(t['id']) for t in playlist.get('trackIds', [])]
            if track_ids:
                return self.get_songs_detail(track_ids)
        
        return tracks
    
    def get_songs_detail(self, song_ids: List[str]) -> Optional[List[Dict]]:
        """Get detailed info for multiple songs (batched)."""
        if isinstance(song_ids, str):
            song_ids = song_ids.split(',')
        
        all_songs = []
        batch_size = 50
        
        for i in range(0, len(song_ids), batch_size):
            batch = song_ids[i:i+batch_size]
            ids_str = ','.join(batch)
            
            # Try different parameter formats
            for params in [
                {'ids': f'[{ids_str}]'},
                {'ids': ids_str},
            ]:
                result = self._request("/song/detail", params)
                if result and result.get('code') == 200:
                    songs = result.get('songs', [])
                    if songs:
                        all_songs.extend(songs)
                        break
            
            time.sleep(random.uniform(0.3, 0.7))
        
        return all_songs if all_songs else None
    
    def get_song_url(self, song_id: str, quality: str = 'standard') -> Optional[str]:
        """Get song download URL with quality fallback."""
        quality_map = {
            'standard': 'standard',
            'higher': 'exhigh', 
            'exhigh': 'exhigh',
            'lossless': 'lossless',
            'hires': 'hires'
        }
        level = quality_map.get(quality, 'standard')
        
        for endpoint in ["/song/url/v1", "/song/url", "/v3/song/url"]:
            result = self._request(endpoint, {'id': song_id, 'level': level})
            if result and result.get('code') == 200:
                data = result.get('data', [])
                if data and isinstance(data, list) and data[0].get('url'):
                    url = data[0]['url']
                    if url and 'http' in url:
                        return url
        
        # Fallback to standard
        if quality != 'standard':
            return self.get_song_url(song_id, 'standard')
        return None
    
    def get_request_stats(self) -> Dict[str, Any]:
        stats = self.stats.copy()
        if stats['total_requests'] > 0:
            stats['success_rate'] = f"{(stats['successful_requests']/stats['total_requests']*100):.1f}%"
        return stats


_api_instance: Optional[NeteaseAPI] = None


def get_api(debug: bool = False) -> NeteaseAPI:
    """Get global API instance."""
    global _api_instance
    if _api_instance is None:
        _api_instance = NeteaseAPI(debug=debug)
    return _api_instance