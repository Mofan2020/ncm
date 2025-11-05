# 网易云音乐歌单下载器 (macOS优化版)

![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)
![Platform](https://img.shields.io/badge/Platform-macOS-green.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## 项目简介

这是一个专为macOS系统优化的网易云音乐歌单下载工具，支持获取公开歌单信息并下载其中的歌曲。工具具有以下特点：

- 🚀 支持多种音质选择（标准、较高、极高、无损、Hi-Res）
- 🎯 自动从歌单链接提取ID，操作更便捷
- 📊 实时显示下载进度和统计信息
- 🔄 智能音质降级机制，保障歌曲可下载性
- 🛡️ 完善的错误处理和用户提示
- 🌐 支持游客Cookie获取，减少API访问限制
- 💾 自动创建下载目录，默认保存至`~/Music/Downloads`

## 安装说明

### 1. 克隆或下载项目

```bash
git clone https://your-repo-url/ncm.git
cd ncm
```

### 2. 安装依赖

```bash
pip3 install -r requirements.txt
```

依赖项说明：
- `requests>=2.28.0`: 处理HTTP请求
- Python 3.8或更高版本

## 使用方法

### 基本用法

```bash
python3 main.py <歌单ID或URL>
```

### 参数说明

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `playlist_id` | 歌单ID或歌单URL (必填) | 无 |
| `--quality` | 音质选择 (standard/higher/exhigh/lossless/hires) | standard |
| `--overwrite` | 覆盖已存在的文件 | 否 |
| `--skip-url-check` | 跳过URL检查直接开始下载 | 否 |
| `--debug` | 显示详细调试信息 | 否 |
| `-h, --help` | 显示帮助信息 | 无 |

### 使用示例

1. **基本下载**
   ```bash
   python3 main.py 3778678
   ```

2. **使用歌单URL**
   ```bash
   python3 main.py https://music.163.com/playlist?id=3778678
   ```

3. **无损音质下载**
   ```bash
   python3 main.py 3778678 --quality lossless
   ```

4. **覆盖已存在文件**
   ```bash
   python3 main.py 3778678 --overwrite
   ```

5. **调试模式**
   ```bash
   python3 main.py 3778678 --debug
   ```

## 输出说明

- **成功信息**: 绿色文本和✅图标
- **提示信息**: 蓝色文本和💡图标
- **警告信息**: 黄色文本和⚠️图标
- **错误信息**: 红色文本和❌图标

下载完成后，程序会显示统计信息，包括：
- 总歌曲数量
- 成功下载数量
- 失败下载数量
- 下载耗时

## 功能说明

### 1. 自动ID提取

支持直接输入歌单链接，程序会自动提取歌单ID：
```
https://music.163.com/playlist?id=3778678  # 自动提取ID: 3778678
```

### 2. 音质选择

- `standard`: 标准品质 (128kbps MP3)
- `higher`: 较高品质 (192kbps MP3)
- `exhigh`: 极高品质 (320kbps MP3)
- `lossless`: 无损品质 (FLAC)
- `hires`: Hi-Res音质 (高品质无损)

### 3. 智能降级机制

如果请求的音质不可用，程序会自动尝试降级到下一个可用音质，确保最大程度的下载成功率。

### 4. 错误处理

程序具有完善的错误处理机制，包括：
- API请求失败重试
- 网络异常处理
- 文件保存错误处理
- 用户中断（Ctrl+C）优雅退出

## 常见问题

### 1. 下载失败怎么办？

- 检查网络连接
- 确认歌单ID/URL是否正确
- 使用`--debug`参数查看详细错误信息
- 尝试使用`--skip-url-check`参数跳过URL检查

### 2. 歌曲下载后无法播放？

- 检查文件是否完整下载
- 确认本地播放器支持下载的音频格式
- 尝试使用更低品质重新下载

### 3. 下载速度慢？

- 尝试降低音质设置
- 检查网络连接质量
- 避免同时下载过多歌曲

## 注意事项

1. 本工具仅用于下载公开歌单，不支持付费歌曲
2. 请遵守相关法律法规，仅下载和使用有版权的音乐
3. 建议合理使用API，避免过于频繁的请求
4. 部分歌曲可能因版权原因无法下载

## 版权声明

本项目仅用于学习和交流，不用于商业用途。所有音乐资源版权归网易云音乐所有。

## 许可证

[MIT License](https://opensource.org/licenses/MIT)

## 系统要求

- macOS系统
- Python 3.8或更高版本
- requests库