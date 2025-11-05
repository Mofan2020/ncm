#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网易云音乐歌单下载工具
功能：通过歌单ID获取并下载歌单中的所有歌曲
专为macOS系统优化，提供友好的用户界面和稳定的下载体验
"""

import os
import sys
import time
import argparse
import traceback
import signal
import platform
import random
from datetime import datetime
from netease_api import NeteaseAPI
from downloader import SongDownloader

def ensure_download_directory(custom_dir=None):
    """确保下载目录存在，提供多级备选方案以提高macOS兼容性"""
    # 定义备选目录列表
    directories = []
    
    # 如果提供了自定义目录，优先使用
    if custom_dir:
        directories.append(custom_dir)
    
    # macOS特有的目录选项
    if platform.system() == 'Darwin':  # macOS
        # 主要音乐目录
        music_dir = os.path.expanduser("~/Music")
        directories.append(os.path.join(music_dir, "Downloads"))
        
        # 备选目录1: 用户下载文件夹中的Music子目录
        user_downloads = os.path.expanduser("~/Downloads")
        directories.append(os.path.join(user_downloads, "Music"))
        directories.append(os.path.join(user_downloads, "网易云音乐"))
        
        # 备选目录2: 桌面
        desktop = os.path.expanduser("~/Desktop")
        directories.append(os.path.join(desktop, "网易云音乐下载"))
    
    # 通用备选目录
    directories.append(os.path.join(os.getcwd(), "网易云音乐下载"))
    directories.append(os.path.join(os.getcwd(), "downloads"))
    
    # 系统临时目录作为最后备选
    import tempfile
    directories.append(tempfile.mkdtemp(prefix="netease_music_"))
    
    # 尝试每个目录，直到找到可用的
    for download_dir in directories:
        try:
            # 确保目录存在
            if not os.path.exists(download_dir):
                os.makedirs(download_dir, exist_ok=True)
                print(f"📁 已创建下载目录: {download_dir}")
            
            # 测试目录可写性
            test_file = os.path.join(download_dir, ".write_test")
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
            
            return download_dir
        except PermissionError:
            print(f"🔒 权限不足，无法写入: {download_dir}")
        except Exception as e:
            print(f"⚠️ 目录访问失败 {download_dir}: {str(e)}")
    
    # 理论上不应该到达这里，但为了保险起见
    return os.getcwd()

def validate_playlist_id(playlist_id):
    """增强的歌单ID验证函数，支持多种URL格式"""
    # 处理空输入
    if not playlist_id:
        raise ValueError("❌ 歌单ID不能为空")
    
    # 从各种可能的URL格式中提取歌单ID
    if isinstance(playlist_id, str) and ('163.com' in playlist_id or 'music.163.com' in playlist_id):
        import re
        # 定义多种可能的URL格式模式
        patterns = [
            r'id=(\d+)',          # playlist?id=123456
            r'playlist/(\d+)',    # playlist/123456
            r'play/\d+/(\d+)',   # play/123456/789012
            r'show\?id=(\d+)'     # show?id=123456
        ]
        
        for pattern in patterns:
            match = re.search(pattern, playlist_id)
            if match:
                playlist_id = match.group(1)
                print(f"🔗 从链接中提取歌单ID: {playlist_id}")
                break
    
    # 确保是纯数字且长度合理（网易云ID通常是6-12位）
    playlist_id_str = str(playlist_id)
    if not playlist_id_str.isdigit():
        raise ValueError(f"❌ 无效的歌单ID: {playlist_id}，请确保ID为纯数字或有效的歌单链接")
    
    # 检查ID长度是否合理
    if len(playlist_id_str) < 5 or len(playlist_id_str) > 15:
        print(f"⚠️  警告: 歌单ID长度异常 ({len(playlist_id_str)}位)，可能导致下载失败")
    
    return playlist_id_str

def print_header():
    """打印美化的程序头部信息，提供更多系统信息"""
    os.system('clear' if platform.system() == 'Darwin' else 'cls')  # 清屏
    
    header = """
    🎵 网易云音乐歌单下载器 🎵
    🚀 专为macOS优化 | 稳定可靠的歌单下载工具
    ==========================================
    """
    print(header)
    
    # 显示系统和版本信息
    print(f"📱 系统: {platform.system()} {platform.release()}")
    print(f"🐍 Python版本: {platform.python_version()}")
    print(f"⏰ 当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 50)
    print()

def handle_keyboard_interrupt(signum=None, frame=None):
    """增强的键盘中断处理函数，提供平滑退出体验"""
    print("\n\n🛑 接收到中断信号")
    print("🔄 正在停止当前操作...")
    # 给当前操作一点时间来清理资源
    time.sleep(0.5)
    print("✅ 程序已安全退出")
    print("感谢使用网易云音乐歌单下载器！")
    sys.exit(0)

def setup_signal_handlers():
    """设置信号处理器，提升macOS上的用户体验"""
    try:
        signal.signal(signal.SIGINT, handle_keyboard_interrupt)   # Ctrl+C
        signal.signal(signal.SIGTERM, handle_keyboard_interrupt)  # 终止信号
        if platform.system() == 'Darwin':  # macOS特有信号
            signal.signal(signal.SIGHUP, handle_keyboard_interrupt)  # 挂起信号
    except Exception as e:
        print(f"⚠️ 无法设置信号处理器: {e}")

def main():
    """主函数 - 优化的用户交互和错误处理"""
    # 初始化计数器变量，确保在所有代码路径中都有定义
    success_count = 0
    fail_count = 0
    skip_count = 0
    failed_songs = []
    skipped_songs = []
    
    print("DEBUG: 主函数开始执行")
    
    # 设置信号处理器
    print("DEBUG: 设置信号处理器")
    setup_signal_handlers()
    print("DEBUG: 信号处理器设置完成")
    
    # 打印程序头部
    print("DEBUG: 显示程序头部信息")
    print_header()
    print("DEBUG: 头部信息显示完成")
    
    print("DEBUG: 创建参数解析器")
    parser = argparse.ArgumentParser(
        description='网易云音乐歌单下载工具 (macOS优化版)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py 12345678                # 基本下载
  python main.py https://music.163.com/playlist?id=12345678  # 从链接下载
  python main.py 12345678 --quality lossless  # 无损音质
  python main.py 12345678 --overwrite         # 覆盖已存在文件"""
    )
    print("DEBUG: 添加参数定义")
    parser.add_argument('playlist_id', type=str, nargs='?', help='歌单ID或歌单URL')
    parser.add_argument('--quality', type=str, default='standard', 
                        choices=['standard', 'higher', 'exhigh', 'lossless', 'hires'],
                        help='音质选择 (默认: standard)')
    parser.add_argument('--overwrite', action='store_true', help='覆盖已存在的文件')
    parser.add_argument('--skip-url-check', action='store_true', 
                      help='跳过URL检查直接开始下载（可加快处理速度）')
    parser.add_argument('--debug', action='store_true', help='显示详细调试信息')
    
    print("DEBUG: 解析命令行参数")
    args = parser.parse_args()
    print(f"DEBUG: 参数解析完成 - playlist_id: {args.playlist_id}, quality: {args.quality}, debug: {args.debug}")
    
    # 如果没有提供歌单ID，从用户输入获取
    if not args.playlist_id:
        args.playlist_id = input("请输入歌单ID或链接: ").strip()
        while not args.playlist_id:
            args.playlist_id = input("歌单ID不能为空，请重新输入: ").strip()
    
    print("DEBUG: 验证歌单ID")
    # 验证歌单ID
    try:
        playlist_id = validate_playlist_id(args.playlist_id)
        print(f"DEBUG: 歌单ID验证成功: {playlist_id}")
    except ValueError as e:
        print(str(e))
        print("\n💡 提示: 您可以直接粘贴网易云音乐的歌单链接")
        return 1
    
    print("DEBUG: 确定下载目录")
    # 确保下载目录存在
    download_dir = ensure_download_directory()
    print(f"DEBUG: 下载目录确定: {download_dir}")
    print(f"\n📂 下载目录: {download_dir}")
    
    print("DEBUG: 初始化网易云音乐API客户端")
    # 初始化API和下载器
    print("\n🔄 初始化网易云音乐API客户端...")
    try:
        api = NeteaseAPI(debug=args.debug)
        print("✅ API客户端初始化成功")
    except Exception as e:
        print(f"❌ API客户端初始化失败: {e}")
        return 1
    
    print("DEBUG: 尝试获取游客Cookie")
    # 尝试获取游客Cookie以提高API稳定性
    print("\n🍪 获取游客Cookie以提升下载成功率...")
    if hasattr(api, 'get_guest_cookie'):
        max_retries = 3
        for retry in range(max_retries):
            if api.get_guest_cookie():
                print("✅ Cookie获取成功")
                break
            else:
                print(f"⚠️ Cookie获取失败 (尝试 {retry + 1}/{max_retries})，1秒后重试...")
                time.sleep(1)
        else:
            print("⚠️ Cookie获取失败，将尝试不使用Cookie继续")
    else:
        print("ℹ️ API版本不支持获取Cookie功能")
    
    print("DEBUG: 准备获取歌单歌曲")
    # 确保使用正确的歌曲列表获取方法
    # 尝试多种可能的方法名称，提高兼容性
    get_playlist_methods = [
        getattr(api, 'get_playlist_tracks', None),
        getattr(api, 'get_playlist_songs', None),
        getattr(api, 'get_playlist', None)
    ]
    
    get_songs_method = next((method for method in get_playlist_methods if method is not None), None)
    if get_songs_method is None:
        print("❌ 不支持的API版本: 找不到获取歌单歌曲的方法")
        return 1
    
    print("DEBUG: 开始获取歌单信息")
    try:
            # 获取歌单信息，添加重试机制
            print(f"\n📋 正在获取歌单信息 (ID: {playlist_id})...")
            max_retries = 3
            playlist_info = None
            
            for retry in range(max_retries):
                playlist_info = api.get_playlist_info(playlist_id)
                if playlist_info:
                    break
                print(f"⚠️ 获取歌单信息失败，{retry + 1}/{max_retries}，尝试重试...")
                time.sleep(1)
            
            if not playlist_info:
                print("❌ 获取歌单信息失败，请检查:")
                print("  - 歌单ID是否正确")
                print("  - 歌单是否为公开歌单")
                print("  - 网络连接是否正常")
                return 1
            
            # 美化显示歌单信息
            playlist_name = playlist_info.get('name', '未知歌单')
            creator = playlist_info.get('creator', {}).get('nickname', '未知用户')
            # 安全处理描述信息，防止NoneType错误
            description = (playlist_info.get('description') or '无').strip()
            track_count = playlist_info.get('trackCount', 0)
            play_count = playlist_info.get('playCount', 0)
            cover_img = playlist_info.get('coverImgUrl', '')
            
            print("\n" + "-" * 50)
            print(f"🎵 歌单名称: {playlist_name}")
            print(f"👤 创建者: {creator}")
            print(f"📊 歌曲数量: {track_count}")
            print(f"🔊 播放次数: {play_count:,}")
            
            # 美化描述显示
            if description:
                desc_lines = description.split('\n')
                print(f"📝 描述: {desc_lines[0][:50]}{'...' if len(desc_lines[0]) > 50 else ''}")
                for line in desc_lines[1:3]:  # 最多显示3行
                    print(f"        {line[:50]}{'...' if len(line) > 50 else ''}")
                if len(desc_lines) > 3 or any(len(l) > 50 for l in desc_lines[:3]):
                    print(f"        ...")
            
            print("-" * 50)
            
            # 添加调试信息
            print(f"DEBUG: 歌单信息 - 名称: {playlist_name}, 描述: {description}")
            
            # 询问用户是否继续下载
            if not args.overwrite:
                response = input(f"\n确认下载 {track_count} 首歌曲? (y/n): ").lower()
                if response not in ['y', 'yes', '']:
                    print("✅ 已取消下载")
                    return 0
            
            # 获取歌曲列表，添加重试机制
            print(f"\n🎵 正在获取歌曲列表...")
            print(f"DEBUG: 准备调用API获取歌曲列表")
            songs = None
            
            for retry in range(max_retries):
                # 优先使用get_playlist_songs方法，这是我们已经修复的方法
                if hasattr(api, 'get_playlist_songs'):
                    print(f"DEBUG: 调用api.get_playlist_songs方法")
                    songs = api.get_playlist_songs(playlist_id)
                else:
                    # 回退到之前的方法
                    songs = get_songs_method(playlist_id)
                    
                if songs:
                    # 验证获取的歌曲数量
                    fetched_count = len(songs)
                    print(f"DEBUG: 获取到{fetched_count}首歌曲")
                    if fetched_count < track_count:
                        print(f"⚠️ 获取到的歌曲数量({fetched_count})少于歌单总数量({track_count})，尝试重试...")
                        songs = None
                    else:
                        break
                print(f"⚠️ 获取歌曲列表失败，{retry + 1}/{max_retries}，尝试重试...")
                time.sleep(1)
            
            if not songs:
                print("❌ 获取歌曲列表失败")
                return 1
            
            total_songs = len(songs)
            print(f"✅ 成功获取 {total_songs} 首歌曲")
            if total_songs != track_count:
                print(f"📊 注意: 获取到的歌曲数量({total_songs})与歌单信息中显示的数量({track_count})不一致")
                # 尝试再次获取完整歌曲列表
                if hasattr(api, 'get_playlist_info') and total_songs < track_count:
                    print(f"🔄 尝试直接从歌单详情中获取完整歌曲列表...")
                    playlist = api.get_playlist_info(playlist_id)
                    if playlist and 'trackIds' in playlist:
                        track_ids = [str(track['id']) for track in playlist['trackIds']]
                        if len(track_ids) > total_songs:
                            print(f"📋 发现{len(track_ids)}首歌曲ID，尝试批量获取详情...")
                            # 分批获取歌曲详情
                            batch_size = 50
                            all_tracks = []
                            for i in range(0, len(track_ids), batch_size):
                                batch_ids = track_ids[i:i+batch_size]
                                print(f"🔄 获取第{i+1}-{min(i+batch_size, len(track_ids))}首歌曲信息...")
                                tracks_batch = api.get_songs_detail(",".join(batch_ids))
                                if tracks_batch:
                                    all_tracks.extend(tracks_batch)
                                time.sleep(0.5)
                            songs = all_tracks
                            total_songs = len(songs)
                            print(f"✅ 成功获取完整歌曲列表，共{total_songs}首歌曲")
            print(f"🎯 音质设置: {args.quality}")
            print(f"🔄 覆盖已存在文件: {'是' if args.overwrite else '否'}")
            
            # 初始化下载器
            print("\n🔄 初始化歌曲下载器...")
            try:
                downloader = SongDownloader(
                    download_dir, 
                    quality=args.quality, 
                    overwrite=args.overwrite
                )
                print("✅ 下载器初始化成功")
            except Exception as e:
                print(f"❌ 下载器初始化失败: {e}")
                return 1
            
            # 启动下载
            print("\n" + "=" * 60)
            print("🚀 开始下载歌曲...")
            print("=" * 60)
            print(f"🎯 目标目录: {download_dir}")
            print(f"📂 文件质量: {args.quality}")
            print(f"🔄 覆盖模式: {'开启' if args.overwrite else '关闭'}")
            print("=" * 60)
            
            # 使用增强版下载器的批量下载功能
            if hasattr(downloader, 'batch_download'):
                # 直接使用增强版下载器，它会自行处理URL获取和音质降级
                print("🔄 使用增强版批量下载功能...")
                stats = downloader.batch_download(songs, api_client=api)
                
                # 显示下载统计
                print("\n" + "=" * 60)
                print("🎉 下载任务完成!")
                print("=" * 60)
                print(f"📝 总计: {stats.get('total', total_songs)} 首")
                print(f"✅ 成功: {stats.get('success', 0)} 首")
                print(f"❌ 失败: {stats.get('fail', 0)} 首")
                print(f"⏱️  总耗时: {int(stats.get('time_elapsed', 0) // 60)}分{int(stats.get('time_elapsed', 0) % 60)}秒")
                
                if stats.get('failed_songs'):
                    # 保存失败列表到文件
                    fail_log = os.path.join(download_dir, '下载失败列表.txt')
                    with open(fail_log, 'w', encoding='utf-8') as f:
                        f.write(f"歌单 '{playlist_name}' 下载失败列表\n")
                        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                        for song_info in stats.get('failed_songs', []):
                            # 处理字典类型的song_info
                            if isinstance(song_info, dict):
                                song_name = song_info.get('name', '未知歌曲')
                                artists_data = song_info.get('artists', []) or song_info.get('ar', [])
                                if isinstance(artists_data, list):
                                    artists = ", ".join([artist.get('name', '未知艺术家') for artist in artists_data])
                                else:
                                    artists = '未知艺术家'
                                display_text = f"{artists} - {song_name}"
                                f.write(f"- {display_text}\n")
                            else:
                                f.write(f"- {song_info}\n")
                    
                    print(f"\n❌ 失败的歌曲 ({len(stats.get('failed_songs', []))}首):")
                    for i, song_info in enumerate(stats.get('failed_songs', [])[:10], 1):
                        # 处理字典类型的song_info
                        if isinstance(song_info, dict):
                            song_name = song_info.get('name', '未知歌曲')
                            artists_data = song_info.get('artists', []) or song_info.get('ar', [])
                            if isinstance(artists_data, list):
                                artists = ", ".join([artist.get('name', '未知艺术家') for artist in artists_data])
                            else:
                                artists = '未知艺术家'
                            display_text = f"{artists} - {song_name}"
                        else:
                            display_text = str(song_info)
                        print(f"  {i:3d}. {display_text[:60]}{'...' if len(display_text) > 60 else ''}")
                    if len(stats.get('failed_songs', [])) > 10:
                        print(f"  ... 还有{len(stats.get('failed_songs', [])) - 10}首失败歌曲")
                    print(f"\n📝 失败列表已保存到: {fail_log}")
            else:
                # 兼容旧版下载器，手动处理每首歌，但使用优化的进度显示
                success_count = 0
                fail_count = 0
                skip_count = 0
                failed_songs = []
                skipped_songs = []
                start_time = time.time()
                
                # 进度条设置
                progress_width = 40
                
                for i, song in enumerate(songs, 1):
                    try:
                        song_name = song.get('name', '未知歌曲')
                        # 支持不同的艺术家数据格式
                        artists_list = []
                        if 'ar' in song:
                            artists_list = [ar.get('name', '未知艺术家') for ar in song.get('ar', [])]
                        elif 'artists' in song:
                            artists_list = [ar.get('name', '未知艺术家') for ar in song.get('artists', [])]
                        
                        artists = "、".join(artists_list) if artists_list else "未知艺术家"
                        
                        # 显示进度条
                        progress = i / total_songs
                        bar_length = int(progress * progress_width)
                        bar = '█' * bar_length + '-' * (progress_width - bar_length)
                        percent = progress * 100
                        
                        # 动态显示进度
                        sys.stdout.write(f"\r[{bar}] {percent:.1f}% | {i}/{total_songs} | {song_name[:30]:<30}")
                        sys.stdout.flush()
                        
                        # 检查文件是否已存在
                        filename = downloader._sanitize_filename(f"{artists} - {song_name}.mp3")
                        file_path = os.path.join(download_dir, filename)
                        
                        if os.path.exists(file_path) and not args.overwrite:
                            skip_count += 1
                            skipped_songs.append(f"{artists} - {song_name}")
                            sys.stdout.write(f"\r⏭️ [{bar}] {percent:.1f}% | {i}/{total_songs} | {song_name[:30]:<30} - 已存在，跳过\n")
                            sys.stdout.flush()
                            continue
                        
                        # 获取歌曲URL，添加重试逻辑
                        song_url = None
                        for retry in range(2):
                            song_url = api.get_song_url(song['id'], args.quality)
                            if song_url:
                                break
                            time.sleep(0.5)
                        
                        if not song_url:
                            # 尝试降级音质
                            if args.quality in ['lossless', 'hires', 'exhigh']:
                                fallback_quality = 'higher' if args.quality in ['lossless', 'hires'] else 'standard'
                                print(f"\n⚠️ 尝试使用降级音质 {fallback_quality}...")
                                song_url = api.get_song_url(song['id'], fallback_quality)
                        
                        if not song_url:
                            fail_count += 1
                            failed_songs.append(f"{artists} - {song_name}")
                            sys.stdout.write(f"\r❌ [{bar}] {percent:.1f}% | {i}/{total_songs} | {song_name[:30]:<30} - 获取URL失败\n")
                            sys.stdout.flush()
                            continue
                        
                        # 下载歌曲
                        success = downloader.download(song_url, filename)
                        if success:
                            success_count += 1
                            sys.stdout.write(f"\r✅ [{bar}] {percent:.1f}% | {i}/{total_songs} | {song_name[:30]:<30} - 下载成功\n")
                            sys.stdout.flush()
                        else:
                            fail_count += 1
                            failed_songs.append(f"{artists} - {song_name}")
                            sys.stdout.write(f"\r❌ [{bar}] {percent:.1f}% | {i}/{total_songs} | {song_name[:30]:<30} - 下载失败\n")
                            sys.stdout.flush()
                        
                        # 随机延迟，避免请求过于频繁
                        time.sleep(random.uniform(0.3, 1.0))
                        
                    except Exception as e:
                        fail_count += 1
                        failed_songs.append(f"{artists} - {song_name}")
                        sys.stdout.write(f"\r❌ [{bar}] {percent:.1f}% | {i}/{total_songs} | {song_name[:30]:<30} - 异常: {str(e)}\n")
                        sys.stdout.flush()
                
                time_elapsed = time.time() - start_time
                
                # 显示下载统计
                print("\n" + "=" * 60)
                print("🎉 下载任务完成!")
                print("=" * 60)
                print(f"📝 总计: {total_songs} 首")
                print(f"✅ 成功: {success_count} 首")
                print(f"❌ 失败: {fail_count} 首")
                print(f"⏭️  跳过: {skip_count} 首")
                print(f"⏱️  总耗时: {int(time_elapsed // 60)}分{int(time_elapsed % 60)}秒")
                
                # 保存失败列表
                if failed_songs:
                    fail_log = os.path.join(download_dir, '下载失败列表.txt')
                    with open(fail_log, 'w', encoding='utf-8') as f:
                        f.write(f"歌单 '{playlist_name}' 下载失败列表\n")
                        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                        for song_info in failed_songs:
                            # 处理字典类型的song_info
                            if isinstance(song_info, dict):
                                song_name = song_info.get('name', '未知歌曲')
                                artists_data = song_info.get('artists', []) or song_info.get('ar', [])
                                if isinstance(artists_data, list):
                                    artists = ", ".join([artist.get('name', '未知艺术家') for artist in artists_data])
                                else:
                                    artists = '未知艺术家'
                                display_text = f"{artists} - {song_name}"
                                f.write(f"- {display_text}\n")
                            else:
                                f.write(f"- {song_info}\n")
                    
                    print(f"\n❌ 失败的歌曲 ({len(failed_songs)}首):")
                    for i, song_info in enumerate(failed_songs[:10], 1):
                        # 处理字典类型的song_info
                        if isinstance(song_info, dict):
                            song_name = song_info.get('name', '未知歌曲')
                            artists_data = song_info.get('artists', []) or song_info.get('ar', [])
                            if isinstance(artists_data, list):
                                artists = ", ".join([artist.get('name', '未知艺术家') for artist in artists_data])
                            else:
                                artists = '未知艺术家'
                            display_text = f"{artists} - {song_name}"
                        else:
                            display_text = str(song_info)
                        print(f"  {i:3d}. {display_text[:60]}{'...' if len(display_text) > 60 else ''}")
                    if len(failed_songs) > 10:
                        print(f"  ... 还有{len(failed_songs) - 10}首失败歌曲")
                    print(f"📝 失败列表已保存到: {fail_log}")
                
                # 显示跳过的歌曲信息
                if skipped_songs:
                    print(f"\n⏭️  跳过的歌曲 ({len(skipped_songs)}首): 文件已存在")
                
                time_elapsed = time.time() - start_time
                
                print("\n" + "=" * 50)
                print("🎉 下载任务完成!")
                print("=" * 50)
                print(f"📝 总计: {total_songs} 首")
                print(f"✅ 成功: {success_count} 首")
                print(f"❌ 失败: {fail_count} 首")
                print(f"⏱️  总耗时: {int(time_elapsed // 60)}分{int(time_elapsed % 60)}秒")
                
                if failed_songs:
                    print("\n失败的歌曲:")
                    for song_info in failed_songs:
                        # 处理字典类型的song_info
                        if isinstance(song_info, dict):
                            song_name = song_info.get('name', '未知歌曲')
                            artists_data = song_info.get('artists', []) or song_info.get('ar', [])
                            if isinstance(artists_data, list):
                                artists = ", ".join([artist.get('name', '未知艺术家') for artist in artists_data])
                            else:
                                artists = '未知艺术家'
                            display_text = f"{artists} - {song_name}"
                            print(f"  - {display_text}")
                        else:
                            print(f"  - {song_info}")
            
            # 在macOS上自动打开下载目录
            print(f"\n🎯 所有歌曲已保存至: {download_dir}")
            
            if platform.system() == 'Darwin' and (success_count > 0 or skip_count > 0):
                try:
                    response = input("\n📂 是否打开下载目录? (y/n): ").lower()
                    if response in ['y', 'yes', '']:
                        print("🔍 正在打开下载目录...")
                        os.system(f"open '{download_dir}'")
                except Exception as e:
                    print(f"⚠️ 无法打开下载目录: {e}")
            
            print("\n感谢使用网易云音乐歌单下载器!")
            print("🚀 祝您使用愉快!")
    
    except Exception as e:
        print(f"\n❌ 处理过程中出错: {e}")
        if args.debug:
            print("\n详细错误信息:")
            print(traceback.format_exc())
        else:
            print("\n💡 提示: 使用 --debug 参数查看详细错误信息")
        return 1
    except KeyboardInterrupt:
        handle_keyboard_interrupt()
        return 130
    
    return 0

if __name__ == "__main__":
    sys.exit(main())