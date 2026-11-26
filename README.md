# 数据查重程序 (Data Dedup Tool)

[![CI & Windows Release](https://github.com/yellowdking/data-dedup-tool/actions/workflows/ci-release.yml/badge.svg)](https://github.com/yellowdking/data-dedup-tool/actions/workflows/ci-release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

一款面向 Windows 桌面的高效数据查重与多表处理程序（v1.1.0）。支持 **Word (`.docx`) / Excel (`.xlsx`, `.xlsm`) / CSV / TSV / TXT / 目录树 / 本地文件夹** 多格式批量导入与解析，内置多模式智能编码识别引擎、**多表工作台**（跨表查重、表内查重、交集与独有分析、多模式合并）以及 SQLite 本地多库索引，并支持一键导出带样式的多工作表 Excel、Word 文档、CSV 与 TXT 报表。

## 核心特性

### 1. 多格式文档与表格导入
- **Word (`.docx`) 支持**：自动提取正文段落、嵌入表格（支持合并单元格清洗）与页眉页脚文本，附带精确来源定位（如 `文档.docx [表格1 行2]`）。
- **Excel (`.xlsx` / `.xlsm`) 支持**：自动读取多工作表（Multi-Sheet），智能规范化浮点整数编号（如 `12345.0 -> 12345`）、日期格式与缺失/重复表头，支持附带工作表与行号定位。
- **CSV / TSV / TXT / 目录树与文件夹扫描**：自动探测 UTF-8 / UTF-8-SIG / GBK / GB18030 / UTF-16 编码与分隔符，支持 `tree /f` 目录树、Everything 导出清单及本地目录递归扫描。
- **入库前四分类预览**：自动划分为**新增待入库**、**库内重复**、**批内重复**、**未识别与待确认**，支持在预览表中人工修正编码后入库。

### 2. 多表工作台（v1.1.0 新增）
- **多源加载与统一管理**：同时加载多个 Excel 工作表、Word 表格、CSV/TSV、TXT 解析表，或一键载入当前 SQLite 总库作为数据表，支持实时搜索预览与表管理。
- **跨表查重与表内查重**：
  - **智能编码模式**：自动从指定列或全列提取归一化标准编码进行比对。
  - **指定列值模式**：支持单列或多列组合键精确查重，可配置忽略大小写、去除首尾空白与标点过滤。
  - 一键生成结构化分析视图：**跨表重复分组**、**表内重复分组**、**全表共同交集**、**各表独有记录**及**未识别记录**。
- **灵活的多表合并引擎**：
  - **全量纵向追加（Union All）**：支持按列名对齐或按列位置对齐，保留所有记录。
  - **纵向合并并去重（Union Dedup）**：支持 5 种去重保留策略——保留首条、保留末条、保留字段最完整记录、保留标签评分最高记录，以及**字段互补融合（Coalesce）**。
  - **主键横向关联（Join）**：支持左连接（Left）、内连接（Inner）与全外连接（Outer），以主表为基准扩展关联字段。
  - 合并结果可直接预览、导出，或一键写入当前 SQLite 数据总库。

### 3. 多模式智能编码解析引擎
- 内置多阶段预清洗与规则匹配引擎，支持全角字符归一化、路径与目录树符号剥离、站点前缀与冗余修饰词清理。
- 支持标准字母数字编码、紧凑无连字符编码、数字+字母混合前缀、日期索引型、长短编码等多种格式归一化，并准确区分常规日期、分辨率及编码技术词（如 `2026-11-26`、`1080P`、`H264` 等）以减少误识别。
- 自动剥离紧邻中文/英文后缀标签（如版本、清晰度、字幕标记）与分集标记（如 `CD1/CD2`、`Part1/Part2`），支持单行多编码提取、父目录回退解析及自定义正则表达式规则。

### 4. 快速查重、交叉比对与重复项清理
- **与当前库比对（不修改库）**：粘贴多行文本或拖入 TXT / Word / Excel / CSV 文件，即时区分已存在、未找到（新数据）与未识别项，支持一键入库或导出清单。
- **双文件交叉比对**：支持 TXT / Word / Excel / CSV 任意组合比对，快速得出交集（重复项）、仅 A 有、仅 B 有三组结果。
- **重复项中心与安全清理**：按标准编码分组展示重复记录，清晰区分**疑似分集**与真正重复项，提供多种智能勾选策略，删除前自动创建 `.bak` 数据库备份。

### 5. SQLite 多库管理与多格式报表导出
- 支持创建、打开与切换多个独立 `.db` 数据库，采用 WAL 模式保障大数据量写入性能与稳定性。
- 支持将单表、多表工作台合并结果、多表查重报告及库内筛选结果导出为：
  - **Excel (`.xlsx`)**：支持多工作表（概览统计 / 跨表重复 / 表内重复 / 交集 / 各表独有）与自适应列宽、冻结表头及斑马纹样式。
  - **Word (`.docx`)**：生成带标题、统计摘要与格式化表格的结构化报告文档。
  - **CSV (`.csv`, UTF-8-SIG)** 与 **TXT (`.txt`)**：兼容 Excel 直接打开与纯文本清单流转。
- 基于 PySide6 (Qt6) 构建，支持浅色 / 深色主题即时切换与多线程无卡顿后台处理。

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

# 打包为单文件 Windows 可执行程序 (dist/data-dedup-tool.exe 与 dist/数据查重程序.exe)
python scripts/build_exe.py
```

## 项目结构

```text
data-dedup-tool/
├── src/
│   ├── app.py                  # 应用入口与高 DPI 初始化
│   ├── core/                   # 核心引擎
│   │   ├── models.py           # 数据总库核心模型
│   │   ├── table_data.py       # 多表工作台数据结构与配置模型
│   │   ├── doc_reader.py       # Word / Excel / CSV / TXT / DB 多格式加载器
│   │   ├── table_engine.py     # 多表跨表/表内查重与多模式合并引擎
│   │   ├── table_exporter.py   # 多工作表 Excel / Word / CSV / TXT 报表导出
│   │   ├── parser.py           # 多模式智能编码解析引擎
│   │   ├── normalizer.py       # 文本预清洗、全角与编码归一化
│   │   ├── rules.py            # 规则库与黑名单配置
│   │   ├── scanner.py          # 多格式文档与目录递归扫描器
│   │   ├── database.py         # SQLite WAL 多库仓储
│   │   ├── dedup.py            # 单库查重、交叉比对与清理引擎
│   │   ├── exporter.py         # 数据库结果导出器
│   │   └── config.py           # 用户配置持久化
│   ├── ui/                     # PySide6 桌面界面视图与组件（含多表工作台）
│   └── resources/              # 图标与静态资源
├── tests/                      # 自动化单元测试套件
├── scripts/
│   └── build_exe.py            # PyInstaller 单文件打包脚本
├── build.spec                  # PyInstaller 构建配置（含 python-docx 模板打包）
├── .github/workflows/          # GitHub Actions CI & 自动发布流水线
├── pyproject.toml
├── requirements.txt
└── LICENSE
```

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
