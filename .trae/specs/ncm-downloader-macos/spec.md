# NetEase Music Downloader for macOS Spec

## Why
现有的 Python 编写的网易云音乐下载器需要手动在终端运行，缺乏图形界面，不够便捷。通过 Swift 重构为 macOS 原生应用，提供直观的 UI 和流畅的体验，同时保持轻量化。

## What Changes
- 使用 Swift + SwiftUI 开发完整的 macOS 原生应用
- 通过歌单ID或歌单链接从网易云音乐API获取歌曲列表
- 支持多种音质选择（标准、高品质、无损）
- 提供用户可配置的下载选项界面
- 实现歌单文件夹自动创建功能
- 添加音乐元数据（艺术家、专辑、歌曲名）
- 支持下载歌词（原文 + 翻译）

## Impact
- Affected specs: 新功能，无现有 spec 影响
- Affected code: 全新项目，从零开始构建

## ADDED Requirements

### Requirement: 歌单信息获取
应用 SHALL 支持通过歌单ID或歌单链接获取歌单信息和歌曲列表。

#### Scenario: 获取歌单
- **WHEN** 用户输入歌单ID或歌单链接并点击获取
- **THEN** 应用从网易云音乐API获取歌单信息并显示歌曲列表

### Requirement: 歌曲下载
应用 SHALL 支持从网易云音乐API获取歌曲下载URL并进行下载。

#### Scenario: 下载歌曲
- **WHEN** 用户点击开始下载
- **THEN** 应用批量下载歌曲到指定目录

### Requirement: 下载选项配置
应用 SHALL 提供清晰的 UI 界面，允许用户自由调整各类下载选项。

#### Scenario: 配置下载选项
- **WHEN** 用户打开设置面板
- **THEN** 用户可以选择音质、输出目录、是否下载歌词等选项

### Requirement: 歌单文件夹管理
应用 SHALL 在下载前自动创建以歌单名称命名的文件夹，并将下载的音乐放入对应文件夹。

#### Scenario: 创建歌单文件夹
- **WHEN** 开始下载歌单歌曲
- **IF** 文件夹不存在
  - **THEN** 创建以歌单名称命名的文件夹
- **IF** 文件夹已存在
  - **THEN** 直接添加文件，不重新创建

### Requirement: 音乐元数据写入
应用 SHALL 在下载后的音乐文件中写入基本元数据（艺术家、专辑、歌曲名），不包含专辑封面或图片。

#### Scenario: 写入元数据
- **WHEN** 完成歌曲下载
- **THEN** 将艺术家、专辑名、歌曲名写入音频文件的元数据标签

### Requirement: 歌词下载（可选）
应用 SHALL 支持额外下载歌词文件，包含原文歌词及翻译（如果有）。

#### Scenario: 下载歌词
- **WHEN** 用户启用歌词下载选项
- **AND** 歌曲存在对应歌词
- **THEN** 创建 .lrc 文件，包含时间轴和歌词内容

## MODIFIED Requirements
无 - 全新功能

## REMOVED Requirements
无 - 全新项目
