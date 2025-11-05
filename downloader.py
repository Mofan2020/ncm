#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网易云音乐下载器
提供歌曲下载和文件保存功能
"""

import os
import sys
import requests
import time
import random
import shutil
import re
import threading
from urllib.parse import urlparse, unquote

class SongDownloader:
    """网易云音乐歌曲下载器"""
    
    def __init__(self, download_dir, quality='standard', overwrite=False):
        """初始化下载器
        
        Args:
            download_dir: 下载目录
            quality: 音质设置
            overwrite: 是否覆盖已存在的文件
        """
        self.download_dir = download_dir
        self.quality = quality
        self.overwrite = overwrite
        
        # 确保下载目录存在
        self._ensure_download_dir()
        
        # 下载器配置
        self.chunk_size = 16384  # 16KB chunks
        self.timeout = 60  # 60秒超时
        self.max_retries = 3  # 最大重试次数
        
        # 下载器用户代理
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15',
            'Referer': 'https://music.163.com/',
            'Accept-Encoding': 'identity',
            'Accept': '*/*',
            'Range': 'bytes=0-'  # 支持断点续传
        }
        
        # 用于线程安全操作的锁
        self.lock = threading.RLock()
    
    def _ensure_download_dir(self):
        """确保下载目录存在并可写"""
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            
            # 检查目录是否可写
            test_file = os.path.join(self.download_dir, '.test_write')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            print(f"📁 下载目录设置为: {self.download_dir}")
        except Exception as e:
            print(f"❌ 无法创建或访问下载目录: {str(e)}")
            # 尝试使用当前目录作为备选
            fallback_dir = os.getcwd()
            try:
                os.makedirs(fallback_dir, exist_ok=True)
                self.download_dir = fallback_dir
                print(f"⚠️ 已切换至备选下载目录: {fallback_dir}")
            except Exception as fallback_e:
                print(f"❌ 无法使用备选下载目录: {str(fallback_e)}")
                raise RuntimeError("无法创建下载目录") from fallback_e
    
    def _sanitize_filename(self, filename):
        """清理文件名，移除不合法字符
        
        Args:
            filename: 原始文件名
            
        Returns:
            str: 清理后的文件名
        """
        if not filename:
            return "未知歌曲"
        
        # macOS不允许的字符：/ : \ * ? " < > |
        invalid_chars = ['/', ':', '\\', '*', '?', '"', '<', '>', '|', '\0']
        for char in invalid_chars:
            filename = filename.replace(char, '-')
        
        # 移除控制字符
        filename = re.sub(r'[\x00-\x1f\x7f]', '', filename)
        
        # 移除多余空格
        filename = ' '.join(filename.split())
        
        # 限制文件名长度 (macOS上HFS+文件系统限制为255个UTF-16字符)
        max_length = 200
        if len(filename) > max_length:
            # 保留扩展名
            name, ext = os.path.splitext(filename)
            filename = name[:max_length - len(ext)] + ext
        
        return filename
    
    def _get_file_extension(self, headers):
        """从响应头获取文件扩展名
        
        Args:
            headers: HTTP响应头
            
        Returns:
            str: 文件扩展名
        """
        # 默认扩展名
        ext = ".mp3"
        
        # 尝试从Content-Type获取
        content_type = headers.get('Content-Type', '')
        if 'audio/mpeg' in content_type:
            ext = ".mp3"
        elif 'audio/flac' in content_type:
            ext = ".flac"
        elif 'audio/wav' in content_type:
            ext = ".wav"
        elif 'audio/ogg' in content_type:
            ext = ".ogg"
        
        # 尝试从Content-Disposition获取
        content_disposition = headers.get('Content-Disposition', '')
        if 'filename=' in content_disposition:
            try:
                # 提取文件名
                filename_part = content_disposition.split('filename=')[1]
                # 去除引号
                if filename_part.startswith('"'):
                    filename_part = filename_part[1:]
                if filename_part.endswith('"'):
                    filename_part = filename_part[:-1]
                # 解码URL编码的文件名
                filename_part = unquote(filename_part)
                # 获取扩展名
                _, file_ext = os.path.splitext(filename_part)
                if file_ext:
                    ext = file_ext
            except Exception:
                pass
        
        return ext
    
    def download(self, url, filename, api_client=None):
        """下载歌曲（增强版，支持重试和进度优化）
        
        Args:
            url: 歌曲URL
            filename: 保存的文件名
            api_client: API客户端实例，用于在需要时重新获取URL
            
        Returns:
            bool: 是否下载成功
        """
        if not url:
            print(f"❌ 歌曲URL不存在")
            return False
        
        # 重试机制
        for retry in range(self.max_retries):
            try:
                # 发起HEAD请求获取文件信息
                head_response = requests.head(
                    url,
                    headers=self.headers,
                    timeout=self.timeout,
                    allow_redirects=True
                )
                head_response.raise_for_status()
                
                # 获取文件扩展名
                ext = self._get_file_extension(head_response.headers)
                
                # 清理文件名并添加扩展名
                if not filename.endswith(ext):
                    filename = filename.rsplit('.', 1)[0] + ext
                filename = self._sanitize_filename(filename)
                filepath = os.path.join(self.download_dir, filename)
                
                # 检查文件是否已存在
                if os.path.exists(filepath) and not self.overwrite:
                    print(f"⚠️ 文件已存在，跳过下载: {filename}")
                    return True
                
                # 使用临时文件名下载
                temp_file = filepath + '.tmp'
                
                # 显示开始下载信息
                if retry == 0:
                    print(f"📥 开始下载: {filename}")
                else:
                    print(f"🔄 重试下载: {filename} (尝试 {retry+1}/{self.max_retries})")
                
                # 发送请求
                with requests.get(
                    url,
                    headers=self.headers,
                    stream=True,
                    timeout=self.timeout,
                    allow_redirects=True
                ) as response:
                    response.raise_for_status()
                    
                    # 获取文件大小
                    total_size = int(response.headers.get('content-length', 0))
                    
                    # 保存文件
                    downloaded_size = 0
                    start_time = time.time()
                    last_progress_update = start_time
                    
                    with open(temp_file, 'wb') as file:
                        for chunk in response.iter_content(chunk_size=self.chunk_size):
                            if chunk:
                                file.write(chunk)
                                downloaded_size += len(chunk)
                                
                                # 显示下载进度（每秒更新一次，避免频繁刷新）
                                current_time = time.time()
                                if current_time - last_progress_update >= 0.5:
                                    last_progress_update = current_time
                                    if total_size > 0:
                                        progress = (downloaded_size / total_size) * 100
                                        elapsed = current_time - start_time
                                        speed = downloaded_size / elapsed / 1024 if elapsed > 0 else 0
                                        remaining = ((total_size - downloaded_size) / (speed * 1024)) if speed > 0 else 0
                                        
                                        # 使用回车符覆盖当前行
                                        if sys.stdout.isatty():
                                            sys.stdout.write(f"  📥 进度: {progress:.1f}% - {speed:.1f} KB/s - 剩余 {remaining:.1f}秒\r")
                                            sys.stdout.flush()
                    
                    # 完成进度条显示
                    if sys.stdout.isatty() and total_size > 0:
                        sys.stdout.write(f"  📥 进度: 100.0% - 下载完成\n")
                        sys.stdout.flush()
                    else:
                        print(f"  ✅ 下载完成: {filename}")
                    
                    # 检查文件是否完整
                    if total_size > 0 and os.path.getsize(temp_file) != total_size:
                        raise Exception(f"文件下载不完整，预期大小: {total_size}，实际大小: {os.path.getsize(temp_file)}")
                    
                    # 下载完成，重命名临时文件
                    with self.lock:
                        if os.path.exists(filepath):
                            try:
                                os.remove(filepath)
                            except Exception as e:
                                print(f"⚠️ 删除旧文件失败: {str(e)}")
                                raise
                        shutil.move(temp_file, filepath)
                    
                    return True
                    
            except requests.exceptions.RequestException as e:
                error_msg = str(e)
                print(f"❌ 下载请求失败 (重试 {retry+1}/{self.max_retries}): {error_msg}")
                
                # 如果是403错误且有API客户端，尝试重新获取URL
                if '403' in error_msg and api_client:
                    print("🔄 尝试重新获取歌曲URL...")
                    try:
                        # 这里假设api_client有一个get_song_url方法
                        # 在实际使用时需要确保传入正确的API客户端
                        if hasattr(api_client, 'get_song_url'):
                            # 注意：这里需要知道歌曲ID才能重新获取URL
                            # 在batch_download方法中会处理这个逻辑
                            pass
                    except Exception:
                        pass
                
                if retry < self.max_retries - 1:
                    # 指数退避策略
                    delay = (2 ** retry) + (random.random() * 0.5)
                    print(f"⏱️ {delay:.1f}秒后重试...")
                    time.sleep(delay)
            except Exception as e:
                print(f"❌ 下载过程出错: {str(e)}")
                if retry < self.max_retries - 1:
                    time.sleep(1)
            finally:
                # 清理临时文件
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
        
        print(f"❌ 所有重试均失败，放弃下载: {filename}")
        return False
    
    def batch_download(self, songs_info, api_client=None):
        """批量下载歌曲（增强版，支持音质降级和错误恢复）
        
        Args:
            songs_info: 歌曲信息列表
            api_client: API客户端实例，用于获取歌曲URL和在需要时重新获取
            
        Returns:
            dict: 下载统计信息
        """
        if not songs_info:
            print("📭 歌曲列表为空")
            return {
                'total': 0,
                'success': 0,
                'fail': 0,
                'failed_songs': []
            }
        
        success_count = 0
        fail_count = 0
        failed_songs = []
        
        total = len(songs_info)
        
        print(f"📋 准备下载 {total} 首歌曲")
        print(f"🎵 下载音质: {self.quality}")
        print(f"📝 覆盖已有文件: {'是' if self.overwrite else '否'}")
        print(f"📁 下载目录: {self.download_dir}")
        print("=" * 60)
        
        start_time = time.time()
        
        # 处理每首歌曲
        for i, song_info in enumerate(songs_info, 1):
            song_name = song_info.get('name', '未知歌曲')
            # 兼容不同的API响应格式
            artists_data = song_info.get('artists', []) or song_info.get('ar', [])
            artists = ", ".join([artist.get('name', '未知艺术家') for artist in artists_data])
            song_id = song_info.get('id', '未知ID')
            
            print(f"\n[{i}/{total}] 正在处理: {song_name} - {artists}")
            
            # 获取歌曲URL（支持音质降级）
            url = None
            qualities_to_try = [self.quality] if self.quality == 'standard' else [self.quality, 'standard']
            
            for quality in qualities_to_try:
                if api_client and hasattr(api_client, 'get_song_url'):
                    print(f"🔍 尝试获取{quality}音质的URL...")
                    url = api_client.get_song_url(song_id, quality)
                    if url:
                        print(f"✅ 成功获取{quality}音质URL")
                        break
                    # 避免请求过快
                    time.sleep(0.5)
                else:
                    break  # 没有API客户端，无法获取URL
            
            if url:
                # 构建文件名
                filename_base = f"{artists} - {song_name}"
                
                # 下载歌曲
                success = self.download(url, filename_base, api_client)
                
                if success:
                    success_count += 1
                else:
                    fail_count += 1
                    failed_songs.append({
                        'name': song_name,
                        'artists': artists,
                        'id': song_id
                    })
            else:
                fail_count += 1
                failed_songs.append({
                    'name': song_name,
                    'artists': artists,
                    'id': song_id
                })
                print(f"❌ 无法获取歌曲URL")
            
            # 避免请求过快，添加延迟
            if i < total:
                # 根据剩余歌曲数量动态调整延迟时间
                remaining = total - i
                delay = max(0.5, min(2, 3 / (remaining + 1)))
                print(f"⏱️ 等待 {delay:.1f} 秒后继续下一首...")
                time.sleep(delay)
        
        # 计算下载时间
        total_time = time.time() - start_time
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        
        print(f"\n{"=" * 60}")
        print(f"📊 下载完成！")
        print(f"📝 总计: {total} 首")
        print(f"✅ 成功: {success_count} 首")
        print(f"❌ 失败: {fail_count} 首")
        print(f"⏱️ 耗时: {minutes}分{seconds}秒")
        
        if failed_songs:
            print("\n❌ 失败的歌曲列表:")
            for song in failed_songs:
                print(f"  - {song['artists']} - {song['name']} (ID: {song['id']})")
            
            # 保存失败列表到文件
            try:
                fail_log_path = os.path.join(self.download_dir, "failed_downloads.txt")
                with open(fail_log_path, 'w', encoding='utf-8') as f:
                    f.write(f"# 下载失败的歌曲列表 (生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')})\n\n")
                    for song in failed_songs:
                        f.write(f"{song['artists']} - {song['name']} (ID: {song['id']})\n")
                print(f"\n📝 失败列表已保存至: {fail_log_path}")
            except Exception as e:
                print(f"⚠️ 保存失败列表时出错: {str(e)}")
        
        return {
            'total': total,
            'success': success_count,
            'fail': fail_count,
            'failed_songs': failed_songs,
            'time_elapsed': total_time
        }