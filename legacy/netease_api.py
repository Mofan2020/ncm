#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网易云音乐API封装
提供与网易云音乐开放API交互的功能，包含完善的错误处理和用户交互
"""

import requests
import time
import random
import json
import logging
from urllib.parse import urlencode

# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("netease_api.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("netease_api")

class NeteaseAPI:
    """网易云音乐API客户端 - 增强版"""
    
    def __init__(self, debug=False):
        """初始化API客户端
        
        Args:
            debug: 是否启用调试模式
        """
        # 网易云音乐的多个API域名，用于轮询避免被限制
        self.api_domains = [
            "https://music.163.com/api",
            "https://api.music.163.com",
            "https://music.163.com"
        ]
        self.current_domain = 0
        self.base_url = self.api_domains[self.current_domain]
        
        # 增强的请求头
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15',
            'Referer': 'https://music.163.com/',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache'
        }
        
        self.cookies = {}
        self.max_retries = 5  # 增加最大重试次数
        self.retry_delay = 1  # 秒
        self.debug = debug
        
        # 统计信息
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'domain_switches': 0,
            'cookie_updates': 0
        }
        
        # 错误码映射
        self.error_codes = {
            400: '请求参数错误',
            401: '未授权，请登录',
            403: '禁止访问',
            404: '请求的资源不存在',
            429: '请求过于频繁，请稍后再试',
            500: '服务器内部错误',
            502: '网关错误',
            503: '服务不可用',
            504: '网关超时'
        }
        
        # API错误码
        self.api_error_codes = {
            200: '成功',
            400: '参数错误',
            401: '未授权',
            403: '禁止访问',
            404: '资源不存在',
            -460: '请求过于频繁',
            -461: '参数错误',
            -462: '资源不存在',
            -463: '资源已存在',
            -475: '请登录',
            -478: '无权操作',
            -500: '服务器内部错误'
        }
        
        # 获取游客cookie
        self._get_anonymous_cookie()
    
    def _switch_domain(self):
        """切换API域名，避免被限制"""
        self.current_domain = (self.current_domain + 1) % len(self.api_domains)
        self.base_url = self.api_domains[self.current_domain]
        self.stats['domain_switches'] += 1
        self._log(f"🔄 已切换API域名至: {self.base_url}", 'info')
    
    def _get_timestamp(self):
        """获取时间戳"""
        return str(int(time.time() * 1000))
        
    def _log(self, message, level='info'):
        """日志记录函数
        
        Args:
            message: 日志消息
            level: 日志级别
        """
        if level == 'debug' and not self.debug:
            return
            
        if level == 'info':
            print(message)
            logger.info(message)
        elif level == 'warning':
            print(f"⚠️ {message}")
            logger.warning(message)
        elif level == 'error':
            print(f"❌ {message}")
            logger.error(message)
        elif level == 'debug':
            logger.debug(message)
    
    def _get_random_str(self, length=16):
        """生成随机字符串"""
        chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        return ''.join(random.choice(chars) for _ in range(length))
    
    def _get_anonymous_cookie(self):
        """获取游客cookie"""
        try:
            self._log("🎯 正在获取游客cookie...", 'info')
            
            # 尝试多种获取Cookie的方式
            methods = [
                # 官方游客注册接口
                {'url': f"{self.base_url}/register/anonimous", 'params': {'timestamp': self._get_timestamp()}},
                # 备用接口 - 直接访问主页获取Cookie
                {'url': 'https://music.163.com/', 'params': {}},
                # 备用接口 - 访问其他公开接口获取Cookie
                {'url': 'https://music.163.com/api/homepage/block/page', 'params': {'timestamp': self._get_timestamp()}}
            ]
            
            cookie_found = False
            for method in methods:
                try:
                    response = requests.get(
                        method['url'], 
                        headers=self.headers, 
                        params=method['params'],
                        timeout=15
                    )
                    response.raise_for_status()
                    
                    # 从响应头获取cookie
                    if 'Set-Cookie' in response.headers:
                        cookies = response.headers['Set-Cookie'].split(';')
                        for cookie in cookies:
                            if '=' in cookie:
                                key_value = cookie.split('=', 1)
                                if len(key_value) == 2:
                                    key, value = key_value
                                    self.cookies[key.strip()] = value.strip()
                                    self.stats['cookie_updates'] += 1
                        cookie_found = True
                        break
                    
                    # 从响应中提取csrf token等信息
                    try:
                        data = response.json()
                        if data.get('code') == 200:
                            self._log("✅ 游客cookie获取成功", 'info')
                            return
                    except json.JSONDecodeError:
                        pass
                        
                except Exception as e:
                    self._log(f"⚠️ Cookie获取方法失败: {str(e)}", 'debug')
                    continue
            
            if cookie_found:
                self._log(f"✅ 成功获取Cookie，共 {len(self.cookies)} 个项", 'info')
            else:
                self._log("⚠️ 未获取到Cookie，将尝试无Cookie访问", 'warning')
                
        except Exception as e:
            self._log(f"❌ 获取游客cookie失败: {str(e)}", 'error')
            # 即使获取失败也继续，使用空cookie尝试
    
    def _request(self, endpoint, params=None, method='GET'):
        """通用请求方法，带有增强的重试机制和错误处理"""
        self.stats['total_requests'] += 1
        
        if params is None:
            params = {}
        
        # 添加时间戳参数
        if 'timestamp' not in params:
            params['timestamp'] = self._get_timestamp()
        
        # 准备请求参数
        request_params = params.copy()
        
        # 重试机制
        for retry in range(self.max_retries):
            try:
                url = f"{self.base_url}{endpoint}"
                self._log(f"📡 发送请求: {method} {url}", 'debug')
                
                # 复制headers以避免修改原始值
                headers = self.headers.copy()
                
                # 如果有cookie，添加到请求头
                if self.cookies:
                    headers['Cookie'] = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
                    self._log(f"🍪 使用Cookie进行请求", 'debug')
                
                # 设置超时和重试参数
                session = requests.Session()
                retry_adapter = requests.adapters.HTTPAdapter(
                    max_retries=3,
                    pool_connections=10,
                    pool_maxsize=10
                )
                session.mount('http://', retry_adapter)
                session.mount('https://', retry_adapter)
                
                if method.upper() == 'GET':
                    response = session.get(
                        url, 
                        headers=headers, 
                        params=request_params,
                        timeout=(5, 15),  # 连接超时5秒，读取超时15秒
                        verify=True
                    )
                else:
                    response = session.post(
                        url, 
                        headers=headers, 
                        data=request_params,
                        timeout=(5, 15),
                        verify=True
                    )
                
                # 检查响应状态码
                response.raise_for_status()
                
                # 更新Cookie
                if response.cookies:
                    new_cookies = response.cookies.get_dict()
                    if new_cookies:
                        self.cookies.update(new_cookies)
                        self.stats['cookie_updates'] += 1
                        self._log(f"🍪 更新Cookie，新增 {len(new_cookies)} 个项", 'debug')
                
                # 解析JSON响应
                try:
                    result = response.json()
                    
                    # 检查业务逻辑错误
                    code = result.get('code')
                    
                    # 成功情况
                    if code == 200 or code == 301:
                        self.stats['successful_requests'] += 1
                        self._log(f"✅ 请求成功: {url}", 'debug')
                        return result
                    
                    # 需要登录的情况
                    elif code in [401, -475] or ('needLogin' in result and result['needLogin']):
                        self._log(f"⚠️ 请求需要登录，尝试重新获取Cookie", 'warning')
                        self._get_anonymous_cookie()
                        continue
                    
                    # 权限错误
                    elif code == 403:
                        self._log(f"❌ 请求被拒绝 (错误码: {code})", 'error')
                        self._switch_domain()
                        continue
                    
                    # 请求频繁
                    elif code in [429, -460]:
                        error_msg = self.api_error_codes.get(code, f'未知错误码 {code}')
                        self._log(f"⚠️ {error_msg}，将等待更长时间后重试", 'warning')
                        # 特殊处理请求频繁的情况
                        if retry < self.max_retries - 1:
                            delay = (retry + 1) * 5 + random.uniform(1, 3)
                            self._log(f"⏱️ {delay:.1f}秒后重试...", 'info')
                            time.sleep(delay)
                        continue
                    
                    # 其他API错误
                    else:
                        error_msg = self.api_error_codes.get(code, f'未知错误码 {code}')
                        self._log(f"❌ API返回错误: {error_msg}", 'error')
                        self._log(f"   完整响应: {result}", 'debug')
                        return result
                        
                except json.JSONDecodeError:
                    self._log(f"❌ 响应不是有效的JSON格式: {response.text[:200]}...", 'error')
                    return None
                    
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response else '未知'
                error_msg = self.error_codes.get(status_code, f'未知HTTP错误 {status_code}')
                self._log(f"❌ HTTP错误: {error_msg}", 'error')
                
                # 重试逻辑
                if retry < self.max_retries - 1:
                    # 特定错误码时切换域名
                    if status_code in [403, 429, 502, 503, 504]:
                        self._switch_domain()
                    
                    # 指数退避策略
                    delay = self.retry_delay * (2 ** retry) + random.uniform(0.5, 1.5)
                    self._log(f"⏱️ {delay:.1f}秒后重试 (第{retry+1}/{self.max_retries}次)...", 'info')
                    time.sleep(delay)
                    
            except requests.exceptions.Timeout as e:
                self._log(f"❌ 请求超时: {str(e)}", 'error')
                
                if retry < self.max_retries - 1:
                    # 超时错误时切换域名
                    self._switch_domain()
                    
                    delay = self.retry_delay * (2 ** retry) + random.uniform(0.5, 1.5)
                    self._log(f"⏱️ {delay:.1f}秒后重试 (第{retry+1}/{self.max_retries}次)...", 'info')
                    time.sleep(delay)
                    
            except requests.exceptions.ConnectionError as e:
                self._log(f"❌ 连接错误: {str(e)}", 'error')
                
                if retry < self.max_retries - 1:
                    # 连接错误时切换域名
                    self._switch_domain()
                    
                    delay = self.retry_delay * (2 ** retry) + random.uniform(0.5, 1.5)
                    self._log(f"⏱️ {delay:.1f}秒后重试 (第{retry+1}/{self.max_retries}次)...", 'info')
                    time.sleep(delay)
                    
            except requests.exceptions.SSLError as e:
                self._log(f"❌ SSL错误: {str(e)}", 'error')
                
                if retry < self.max_retries - 1:
                    self._switch_domain()
                    delay = self.retry_delay * (2 ** retry) + random.uniform(0.5, 1.5)
                    self._log(f"⏱️ {delay:.1f}秒后重试 (第{retry+1}/{self.max_retries}次)...", 'info')
                    time.sleep(delay)
                    
            except Exception as e:
                self._log(f"❌ 请求过程中发生未知错误: {str(e)}", 'error')
                if retry < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** retry) + random.uniform(0.5, 1.5)
                    self._log(f"⏱️ {delay:.1f}秒后重试 (第{retry+1}/{self.max_retries}次)...", 'info')
                    time.sleep(delay)
        
        # 所有重试都失败
        self.stats['failed_requests'] += 1
        self._log(f"❌ 所有重试均失败，放弃请求", 'error')
        return None
    
    def get_playlist_info(self, playlist_id):
        """获取歌单信息
        
        Args:
            playlist_id: 歌单ID
            
        Returns:
            dict: 歌单信息
        """
        self._log(f"🎵 获取歌单 {playlist_id} 信息...", 'info')
        
        # 验证歌单ID格式
        try:
            playlist_id = str(int(playlist_id))  # 确保是数字ID
        except (ValueError, TypeError):
            self._log(f"❌ 无效的歌单ID: {playlist_id}", 'error')
            return None
        
        # 尝试多种获取歌单信息的方法
        methods = [
            # 方法1: 使用v3接口，兼容性更好
            {"endpoint": "/v3/playlist/detail", "params": {'id': playlist_id}},
            # 方法2: 使用旧版接口
            {"endpoint": "/playlist/detail", "params": {'id': playlist_id}},
            # 方法3: 备用参数格式
            {"endpoint": "/v3/playlist/detail", "params": {'id': playlist_id, 'n': 100000}}
        ]
        
        for method in methods:
            result = self._request(method["endpoint"], method["params"])
            
            if result:
                # 处理不同格式的响应
                if result.get('code') == 200:
                    if 'playlist' in result:
                        playlist = result.get('playlist', {})
                        name = playlist.get('name', '未知歌单')
                        # 优先使用trackCount字段，这是API返回的完整歌曲数量
                        track_count = playlist.get('trackCount', len(playlist.get('tracks', [])))
                        self._log(f"✅ 歌单信息获取成功: {name} ({track_count}首歌曲)", 'info')
                        return playlist
                    elif 'result' in result and 'name' in result['result']:
                        # 某些接口可能返回不同格式
                        self._log(f"✅ 歌单信息获取成功: {result['result'].get('name', '未知歌单')}", 'info')
                        return result['result']
                else:
                    error_msg = result.get('msg', self.api_error_codes.get(result.get('code'), '未知错误'))
                    self._log(f"⚠️ 获取歌单失败: {error_msg}", 'warning')
        
        self._log(f"❌ 所有尝试都失败，无法获取歌单信息", 'error')
        return None
    
    def get_playlist_songs(self, playlist_id):
        """获取歌单歌曲列表
        
        Args:
            playlist_id: 歌单ID
            
        Returns:
            list: 歌曲列表
        """
        self._log(f"🎵 获取歌单 {playlist_id} 歌曲列表...", 'info')
        
        # 方法1: 尝试直接从歌单详情获取歌曲
        playlist = self.get_playlist_info(playlist_id)
        
        if playlist:
            # 从歌单信息中提取歌曲列表
            tracks = playlist.get('tracks', [])
            
            # 获取实际歌曲总数
            total_count = playlist.get('trackCount', len(tracks))
            self._log(f"📊 歌单应有{total_count}首歌曲，当前tracks中有{len(tracks)}首", 'info')
            
            # 如果tracks中的歌曲数量少于总数，或者tracks为空但有trackIds，尝试获取完整歌曲信息
            if len(tracks) < total_count or (not tracks and 'trackIds' in playlist):
                track_ids = [str(track['id']) for track in playlist['trackIds']]
                if track_ids:
                    self._log(f"📋 发现{len(track_ids)}首歌曲，尝试获取详细信息...", 'info')
                    # 分批获取歌曲详情，避免请求过长
                    batch_size = 50
                    all_tracks = []
                    
                    for i in range(0, len(track_ids), batch_size):
                        batch_ids = track_ids[i:i+batch_size]
                        self._log(f"🔄 正在获取第{i+1}-{min(i+batch_size, len(track_ids))}首歌曲信息...", 'debug')
                        tracks_batch = self.get_songs_detail(",".join(batch_ids))
                        if tracks_batch:
                            all_tracks.extend(tracks_batch)
                            self._log(f"✅ 成功获取{len(tracks_batch)}首歌曲信息", 'debug')
                        else:
                            self._log(f"⚠️ 获取这批歌曲失败，尝试下一批", 'warning')
                        
                        # 避免请求过快
                        time.sleep(random.uniform(0.3, 0.7))
                    
                    tracks = all_tracks
            
            if tracks:
                self._log(f"✅ 成功获取{len(tracks)}首歌曲", 'info')
                return tracks
        
        # 方法2: 如果上面的方法失败，尝试直接调用歌曲列表接口
        self._log("🔄 尝试使用备用方法获取歌曲列表...", 'info')
        endpoint = "/v3/song/detail"
        params = {
            'id': playlist_id,
            'ids': playlist_id  # 尝试不同参数名
        }
        
        result = self._request(endpoint, params)
        
        if result and result.get('code') == 200:
            tracks = result.get('songs', [])
            if tracks:
                self._log(f"✅ 备用方法成功获取{len(tracks)}首歌曲", 'info')
                return tracks
        
        self._log(f"❌ 无法获取歌单歌曲列表", 'error')
        return None
    
    def get_songs_detail(self, song_ids):
        """获取多首歌曲的详细信息
        
        Args:
            song_ids: 歌曲ID列表或逗号分隔的字符串
            
        Returns:
            list: 歌曲详情列表
        """
        # 标准化song_ids参数
        if isinstance(song_ids, list):
            song_ids = ",".join(map(str, song_ids))
        
        # 限制单次请求的歌曲数量，避免参数过长导致API错误
        song_id_list = song_ids.split(',')
        if len(song_id_list) > 50:
            self._log(f"📋 歌曲数量过多({len(song_id_list)}首)，将分批处理", 'info')
            all_songs = []
            # 分批处理，每批最多30首
            for i in range(0, len(song_id_list), 30):
                batch_ids = ",".join(song_id_list[i:i+30])
                self._log(f"🔄 获取第{i+1}-{min(i+30, len(song_id_list))}首歌曲详情...", 'debug')
                batch_songs = self._get_songs_detail_batch(batch_ids)
                if batch_songs:
                    all_songs.extend(batch_songs)
                # 避免请求过快
                time.sleep(random.uniform(0.3, 0.7))
            return all_songs
        
        return self._get_songs_detail_batch(song_ids)
    
    def _get_songs_detail_batch(self, song_ids):
        """获取一批歌曲的详细信息（内部方法）
        
        Args:
            song_ids: 一批歌曲ID（逗号分隔的字符串，数量限制在合理范围内）
            
        Returns:
            list: 歌曲详情列表
        """
        self._log(f"🎵 获取歌曲详情: {song_ids[:50]}...", 'debug')
        
        # 尝试多种获取歌曲详情的方法，优化参数格式
        methods = [
            # 方法1: 标准接口，使用正确的参数格式
            {"endpoint": "/song/detail", "params": {'ids': f'[{song_ids}]'}},
            # 方法2: 不使用数组格式
            {"endpoint": "/song/detail", "params": {'ids': song_ids}},
            # 方法3: 添加时间戳参数
            {"endpoint": "/song/detail", "params": {'ids': song_ids, 'timestamp': str(int(time.time() * 1000))}},
            # 方法4: v3接口
            {"endpoint": "/v3/song/detail", "params": {'ids': song_ids}}
        ]
        
        for method in methods:
            try:
                # 为这个请求临时使用更严格的错误处理
                temp_max_retries = 2
                original_max_retries = self.max_retries
                self.max_retries = temp_max_retries
                
                # 发送请求
                result = self._request(method["endpoint"], method["params"])
                
                # 恢复原始重试次数
                self.max_retries = original_max_retries
                
                if result and result.get('code') == 200:
                    songs = result.get('songs', [])
                    if songs:
                        self._log(f"✅ 成功获取{len(songs)}首歌曲详情", 'debug')
                        return songs
            except Exception as e:
                self._log(f"⚠️ 获取歌曲详情时出错: {str(e)}", 'warning')
                continue
        
        self._log(f"❌ 无法获取歌曲详情", 'error')
        return None
    
    def get_song_url(self, song_id, quality='standard'):
        """获取歌曲URL，支持多种音质和降级处理，新增模拟浏览器行为获取真实音频链接
        
        Args:
            song_id: 歌曲ID
            quality: 音质等级
                standard: 标准音质
                high: 高品质
                super: 无损音质
            
        Returns:
            str: 歌曲URL
        """
        self._log(f"🎵 获取歌曲URL: ID={song_id}, 音质={quality}", 'info')
        
        # 模拟浏览器播放行为，尝试获取真实音频链接
        # 首先尝试获取歌曲播放参数
        play_info = self._get_play_info(song_id, quality)
        if play_info:
            return play_info
        
        # 音质映射
        quality_map = {
            'standard': 'standard',  # 标准音质
            'high': 'exhigh',        # 高品质
            'super': 'lossless'      # 无损音质
        }
        
        level = quality_map.get(quality, 'standard')
        
        # 尝试不同的URL获取接口
        methods = [
            # 方法1: v1接口
            {"endpoint": "/song/url/v1", "params": {'id': song_id, 'level': level}},
            # 方法2: 旧版接口
            {"endpoint": "/song/url", "params": {'id': song_id, 'level': level}},
            # 方法3: 备用参数格式
            {"endpoint": "/song/url", "params": {'id': song_id, 'br': 320000 if level != 'standard' else 128000}},
            # 方法4: v3接口
            {"endpoint": "/v3/song/url", "params": {'id': song_id, 'level': level}}
        ]
        
        # 尝试所有方法
        for method in methods:
            result = self._request(method["endpoint"], method["params"])
            
            if result and result.get('code') == 200:
                data = result.get('data', [])
                if data and isinstance(data, list):
                    song_data = data[0]
                    # 检查是否有播放权限
                    if song_data.get('freeTrialInfo') is not None:
                        self._log(f"⚠️ 歌曲 {song_id} 仅有试听片段，尝试使用标准音质...", 'warning')
                        # 尝试使用标准音质
                        if quality != 'standard':
                            return self.get_song_url(song_id, 'standard')
                    
                    url = song_data.get('url')
                    if url:
                        # 检查URL是否有效
                        if 'http' in url:
                            self._log(f"✅ 成功获取歌曲URL: {url[:50]}...", 'debug')
                            return url
                elif data and isinstance(data, dict) and data.get('url'):
                    # 处理可能的不同响应格式
                    url = data.get('url')
                    if 'http' in url:
                        self._log(f"✅ 成功获取歌曲URL: {url[:50]}...", 'debug')
                        return url
        
        # 如果所有方法都失败，降级尝试标准音质
        if quality != 'standard':
            self._log(f"⚠️ 无法获取{quality}音质，尝试标准音质", 'warning')
            return self.get_song_url(song_id, 'standard')
        
        self._log(f"❌ 获取歌曲URL失败: 歌曲ID={song_id}", 'error')
        return None
        
    def _get_play_info(self, song_id, quality='standard'):
        """模拟浏览器播放行为，获取真实音频链接
        
        Args:
            song_id: 歌曲ID
            quality: 音质等级
            
        Returns:
            str: 音频URL或None
        """
        try:
            # 音质对应的br参数 (码率)
            br_map = {
                'standard': 128000,
                'high': 320000,
                'super': 999000  # 无损音质
            }
            br = br_map.get(quality, 128000)
            
            # 方法1: 模拟播放器获取音乐链接
            self._log(f"🔍 尝试模拟浏览器获取音频链接...", 'debug')
            
            # 更新请求头，更接近浏览器环境
            browser_headers = self.headers.copy()
            browser_headers.update({
                'Accept': '*/*',
                'Accept-Encoding': 'identity',
                'Range': 'bytes=0-',
                'X-Real-IP': '118.88.88.88',  # 模拟IP
                'x-router': 'music.163.com'
            })
            
            # 构建播放器参数
            player_params = {
                'id': song_id,
                'ids': f"[{song_id}]",
                'br': br,
                'v': 0,
                'csrf_token': '',
                'timestamp': self._get_timestamp()
            }
            
            # 尝试播放器API
            player_endpoints = [
                "/song/enhance/player/url",
                "/song/enhance/player/url/v1",
                "/song/enhance/player/url/v2"
            ]
            
            for endpoint in player_endpoints:
                try:
                    # 使用requests直接发送请求，模拟浏览器行为
                    url = f"{self.api_domains[0]}{endpoint}"
                    
                    # 复制cookies以避免修改原始值
                    cookies_to_use = self.cookies.copy()
                    
                    # 如果有MUSIC_U cookie，保留它（很重要）
                    if 'MUSIC_U' in cookies_to_use:
                        self._log(f"🍪 使用MUSIC_U cookie", 'debug')
                    
                    # 发送请求
                    response = requests.post(
                        url,
                        headers=browser_headers,
                        cookies=cookies_to_use,
                        data=player_params,
                        timeout=15,
                        verify=True
                    )
                    
                    if response.status_code == 200:
                        try:
                            result = response.json()
                            if result.get('code') == 200:
                                # 处理播放器API响应
                                data = result.get('data', [])
                                if data and isinstance(data, list) and len(data) > 0:
                                    song_data = data[0]
                                    url = song_data.get('url')
                                    if url and 'http' in url:
                                        self._log(f"✅ 成功通过播放器API获取音频链接: {url[:50]}...", 'info')
                                        # 检查是否为浏览器中观察到的m704.music.126.net格式
                                        if 'music.126.net' in url:
                                            return url
                                        # 尝试直接访问获取重定向后的真实链接
                                        try:
                                            head_response = requests.head(
                                                url,
                                                headers=browser_headers,
                                                cookies=cookies_to_use,
                                                allow_redirects=True,
                                                timeout=10
                                            )
                                            if head_response.status_code == 200:
                                                real_url = head_response.url
                                                self._log(f"✅ 成功获取重定向后的真实链接: {real_url[:50]}...", 'debug')
                                                return real_url
                                        except Exception as e:
                                            self._log(f"⚠️ 获取重定向链接失败: {str(e)}", 'debug')
                        except json.JSONDecodeError:
                            self._log(f"❌ 播放器API返回非JSON响应", 'debug')
                except Exception as e:
                    self._log(f"⚠️ 播放器API请求失败: {str(e)}", 'debug')
                    continue
            
            # 方法2: 尝试模拟客户端播放器获取链接
            self._log("🔄 尝试使用客户端模拟API...", 'debug')
            
            return None
            
        except Exception as e:
            self._log(f"❌ 模拟浏览器获取音频链接失败: {str(e)}", 'error')
            return None
    
    def get_request_stats(self):
        """获取API请求统计信息
        
        Returns:
            dict: 统计信息
        """
        success_rate = 0
        if self.stats['total_requests'] > 0:
            success_rate = (self.stats['successful_requests'] / self.stats['total_requests']) * 100
        
        stats = self.stats.copy()
        stats['success_rate'] = f"{success_rate:.1f}%"
        return stats
    
    def print_summary(self):
        """打印API调用摘要信息"""
        stats = self.get_request_stats()
        self._log("📊 API调用统计:", 'info')
        self._log(f"   总请求数: {stats['total_requests']}", 'info')
        self._log(f"   成功请求: {stats['successful_requests']} ({stats['success_rate']})", 'info')
        self._log(f"   失败请求: {stats['failed_requests']}", 'info')
        self._log(f"   域名切换: {stats['domain_switches']}次", 'info')
        self._log(f"   Cookie更新: {stats['cookie_updates']}次", 'info')
    
    def get_login_status(self):
        """获取登录状态
        
        Returns:
            dict: 登录状态信息
        """
        self._log("🔍 检查登录状态...", 'debug')
        endpoint = "/login/status"
        result = self._request(endpoint)
        
        if result and result.get('code') == 200:
            data = result.get('data', {})
            login = data.get('account') is not None
            self._log(f"✅ 登录状态: {'已登录' if login else '游客模式'}", 'debug')
            return data
        
        self._log("⚠️ 无法获取登录状态", 'warning')
        return None
    
    # 兼容方法名
    def get_playlist_tracks(self, playlist_id):
        """获取歌单歌曲列表（兼容方法名）
        
        Args:
            playlist_id: 歌单ID
            
        Returns:
            list: 歌曲列表
        """
        return self.get_playlist_songs(playlist_id)