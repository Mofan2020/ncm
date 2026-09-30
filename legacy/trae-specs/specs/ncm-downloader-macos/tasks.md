# Tasks

## Phase 1: 项目基础架构

- [x] Task 1.1: 创建 Xcode 项目配置 (project.yml)
  - [x] SubTask 1.1.1: 创建项目根目录结构
  - [x] SubTask 1.1.2: 编写 project.yml 配置
  - [x] SubTask 1.1.3: 生成 Xcode 项目文件

- [x] Task 1.2: 创建应用入口和主窗口
  - [x] SubTask 1.2.1: 创建 App 入口文件 (NCMDownloaderApp.swift)
  - [x] SubTask 1.2.2: 创建主 ContentView
  - [x] SubTask 1.2.3: 配置 Info.plist

## Phase 2: 网易云音乐 API 服务

- [x] Task 2.1: 实现 API 客户端
  - [x] SubTask 2.1.1: 实现歌单信息获取接口
  - [x] SubTask 2.1.2: 实现歌曲URL获取接口
  - [x] SubTask 2.1.3: 实现歌词获取接口

- [x] Task 2.2: 实现歌曲下载服务
  - [x] SubTask 2.2.1: 实现歌曲下载功能
  - [x] SubTask 2.2.2: 实现元数据写入功能
  - [x] SubTask 2.2.3: 实现歌词写入功能

## Phase 3: UI 界面开发

- [x] Task 3.1: 主界面开发
  - [x] SubTask 3.1.1: 创建歌单ID输入区域
  - [x] SubTask 3.1.2: 创建歌单/歌曲列表展示
  - [x] SubTask 3.1.3: 创建下载进度显示

- [x] Task 3.2: 设置界面开发
  - [x] SubTask 3.2.1: 创建音质选择 (标准/高品质/无损)
  - [x] SubTask 3.2.2: 创建输出目录选择
  - [x] SubTask 3.2.3: 创建歌词下载开关
  - [x] SubTask 3.2.4: 创建歌单文件夹开关

## Phase 4: 业务逻辑整合

- [x] Task 4.1: 实现歌单文件夹管理
  - [x] SubTask 4.1.1: 创建文件夹检查逻辑
  - [x] SubTask 4.1.2: 实现文件夹创建功能

- [x] Task 4.2: 实现下载流程
  - [x] SubTask 4.2.1: 创建下载任务管理器
  - [x] SubTask 4.2.2: 实现批量下载逻辑
  - [x] SubTask 4.2.3: 实现进度跟踪与更新

## Phase 5: 系统集成

- [x] Task 5.1: 菜单栏与快捷键
  - [x] SubTask 5.1.1: 添加应用菜单项
  - [x] SubTask 5.1.2: 添加 Command 快捷键支持

- [x] Task 5.2: 窗口管理
  - [x] SubTask 5.2.1: 支持窗口大小调整
  - [x] SubTask 5.2.2: 支持深浅色模式

## Phase 6: 测试与优化

- [x] Task 6.1: 功能测试
  - [x] SubTask 6.1.1: 测试歌单获取功能
  - [x] SubTask 6.1.2: 测试歌曲下载功能
  - [x] SubTask 6.1.3: 测试元数据写入

- [x] Task 6.2: 性能优化
  - [x] SubTask 6.2.1: 优化下载性能
  - [x] SubTask 6.2.2: 确保后台线程处理

# Task Dependencies
- Task 1.1 完成后才能进行 Task 1.2
- Task 2.1 完成后才能进行 Task 2.2
- Task 2.1 和 Task 2.2 完成后才能进行 Task 3.1 和 Task 3.2
- Task 3.1 和 Task 3.2 完成后才能进行 Task 4.1 和 Task 4.2
