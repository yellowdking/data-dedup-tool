# 数据查重程序 (Data Dedup Tool)

[![CI & Windows Release](https://github.com/yellowdking/data-dedup-tool/actions/workflows/ci-release.yml/badge.svg)](https://github.com/yellowdking/data-dedup-tool/actions/workflows/ci-release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

一款面向 Windows 桌面的高效数据与文件编码查重程序。支持 TXT 清单、目录树导出文本、CSV 表格与本地目录递归扫描，内置多模式规则引擎精准提取并归一化标准编码，结合 SQLite 本地多库索引实现毫秒级查重、跨文件交叉比对、智能去重清理与 Excel/CSV/TXT 导出。

## 核心特性

- **多模式智能编码解析引擎**
  - 内置多阶段预清洗与规则匹配引擎，自动剥离路径符号、目录树制表符、站点前缀与冗余修饰词
  - 支持标准字母数字编码、紧凑无连字符编码、数字+字母混合前缀、日期索引型、长短编码等多种格式归一化
  - 自动剥离版本/清晰度标签（如 `4K`、`8K`、字幕标记等）与分集标记（如 `CD1/CD2`、`Part1/Part2`），与标准编码独立存储
  - 内置常见技术词与附属文件过滤黑名单（如 `H264`、`1080P`、`.jpg/.nfo/.srt` 等），降低误识别率
  - 支持单行多编码提取与父目录回退解析，并支持用户自定义正则表达式规则
- **批量导入与入库前预览**
  - 支持拖拽或批量选择 TXT / 目录树（`tree /f`）/ Everything 导出清单 / CSV 文件，以及本地文件夹递归扫描
  - 自动识别 UTF-8 / UTF-8-SIG / GBK / GB18030 / UTF-16 等多种文本编码
  - 入库前自动分类预览：**新增待入库**、**库内重复**、**批内重复**、**未识别与待确认**（支持在预览表中人工修正编码后入库）
- **快速查重与交叉比对**
  - **与当前库比对（不修改库）**：粘贴多行文本或拖入 TXT，实时区分已存在、未找到（新数据）与未识别项，支持一键入库或导出清单
  - **双文件交叉比对**：选择两份清单文件（文件 A / 文件 B），快速得出交集（重复项）、仅 A 有、仅 B 有三组结果并导出
- **重复项中心与安全清理**
  - 按标准编码分组展示重复记录，清晰区分**疑似分集**与真正重复项
  - 提供多种智能勾选策略（保留最高标签评分、保留最早、保留最新等），删除前自动创建 `.bak` 数据库备份
- **SQLite 多库管理与多格式导出**
  - 支持创建、打开与切换多个独立 `.db` 数据库，WAL 模式保障高频写入稳定
  - 支持导出全库数据、筛选结果、重复分组及比对结果为带样式的 `.xlsx`（Excel）、`.csv`（UTF-8-SIG）与 `.txt` 清单
- **现代化桌面界面**
  - 基于 PySide6 (Qt6) 打造，支持浅色 / 深色主题即时切换与响应式多线程后台处理

## 快速开始

### 方式一：直接下载预编译版本（推荐）

前往 [Releases](https://github.com/yellowdking/data-dedup-tool/releases) 页面下载最新版本的 `data-dedup-tool-windows-x64.exe`（或解压 `data-dedup-tool-windows-x64.zip` 中的 `数据查重程序.exe`），双击即可运行，无需安装 Python 环境。

### 方式二：源码运行

要求 Python 3.11 及以上版本（推荐 Python 3.12+）：

```powershell
git clone https://github.com/yellowdking/data-dedup-tool.git
cd data-dedup-tool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.app
```

## 运行测试与本地打包

```powershell
# 安装开发与打包依赖
pip install pytest pyinstaller

# 运行单元测试
pytest -v

# 打包为单文件 Windows 可执行程序 (dist/数据查重程序.exe)
python scripts/build_exe.py
```

## 项目结构

```text
data-dedup-tool/
├── src/
│   ├── app.py              # 应用入口与高 DPI 初始化
│   ├── core/               # 核心引擎（数据模型、规则库、归一化、解析器、扫描器、SQLite、查重、导出、配置）
│   ├── ui/                 # PySide6 桌面界面视图与组件
│   └── resources/          # 图标与静态资源
├── tests/                  # 单元测试套件
├── scripts/
│   └── build_exe.py        # PyInstaller 单文件打包脚本
├── build.spec              # PyInstaller 构建配置
├── .github/workflows/      # GitHub Actions CI & 自动发布流水线
├── pyproject.toml
├── requirements.txt
└── LICENSE
```

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
