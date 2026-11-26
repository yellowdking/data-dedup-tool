# 数据查重程序 (Data Dedup Tool)

[![CI & Windows Release](https://github.com/yellowdking/data-dedup-tool/actions/workflows/ci-release.yml/badge.svg)](https://github.com/yellowdking/data-dedup-tool/actions/workflows/ci-release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

一款面向 Windows 桌面的高效数据查重与多表处理程序（**v1.3.0**）。支持 **Word (`.docx`) / Excel (`.xlsx`, `.xlsm`) / CSV / TSV / TXT / 目录树 / 本地文件夹** 多格式批量导入与解析，内置多模式智能编码识别引擎、**双列表覆盖率与缺漏对比**（一键核对目标清单是否全部包含于已有基准库并定位缺失项）、**多表工作台**（跨表查重、表内查重、交集与独有分析、多模式合并）以及 SQLite 本地多库索引，并支持一键导出带样式的多工作表 Excel、高速 Word 文档、CSV 与 TXT 报表。

## v1.3.0 重点更新

- **双列表覆盖率与缺漏对比（查全 / 找缺失）**：全新升级对比工作区，支持左右对称输入**基准清单 A**（支持一键直接载入当前 SQLite 总库或拖入多格式文件/粘贴文本）与**目标核对清单 B**，支持 **A/B 一键互换**，以及**智能编码比对**与**精确文本行比对**双模式。
- **目标清单逐条核对与覆盖率看板**：实时展示覆盖率百分比进度条、命中/缺失汇总结论及四个统计指标卡；保留目标清单原始顺序逐条展示状态（`已包含` / `缺失`）及基准 A 匹配文件名与标签，支持 **`全部` / `只看缺失` / `只看已包含`** 快捷筛选胶囊与实时搜索，适配浅色/深色主题高对比度配色。
- **编号与排行噪声过滤加固**：针对带序号或排行标记的外部清单（如 `#1`、`No.25`、`TOP250`、`Rank 100`、`第1名` 等）强化解析器过滤，避免将排名编号误判为有效编码，同时精准提取同行真实编码。
- **一键复制与 5 工作表覆盖率报告**：支持一键复制全部缺失编码或已包含编码，后台异步导出缺失清单（TXT / CSV）及包含**覆盖率总览、目标清单逐条核对、缺失清单、已包含对照、仅基准A独有** 5 个工作表的专业 Excel 对比报告。

<details>
<summary>查看 v1.2.0 更新摘要</summary>

- **解析与多格式兼容加固**：全面优化域名前缀与附属文件过滤、多列 CSV/TSV 与 `tree /f` 目录树识别、重复/多编码行标签保留及常见日期/版本号/UUID 误判拦截，内置规则示例保持纯合成中性风格。
- **多表工作台进阶体验**：新增多工作表 Excel / Word 表格与表头行导入选择弹窗、列管理（重命名/删列/列画像）、单表快捷去重、按表有序组合键对齐、表快照防错配校验，全流程后台多线程异步导出与入库零卡顿。
- **数据安全与性能跃升**：SQLite 统计查询改为纯 SQL 聚合（2 万条记录 `< 10ms`），WAL 备份恢复支持完整校验与原子切换；Word `.docx` 大表导出提速 10 倍以上，Excel/CSV 全面防护公式注入、保留工作表名与非法字符，并支持写前锁文件秒级检测。
- **桌面壳体与工程质量**：新增单实例防冲突锁、窗口关闭线程安全清理、全局崩溃日志落盘、数据总库双击记录编辑、高 DPI/深浅色对比度优化、便携模式（`portable.flag`）以及精简 ~30% 体积的 Windows 单文件 EXE（内嵌 PE 版本信息、CI 冻结 EXE 冒烟测试与 `SHA256SUMS.txt`）。

</details>

## 核心特性

### 1. 多格式文档与表格导入
- **Word (`.docx`) 支持**：自动提取正文段落、嵌入表格（支持合并单元格清洗与可选嵌套表）与页眉页脚文本，附带精确来源定位（如 `文档.docx [表格1 行2]`）。
- **Excel (`.xlsx` / `.xlsm`) 支持**：自动读取多工作表（Multi-Sheet），支持自选工作表与表头行，智能规范化浮点整数编号（如 `12345.0 -> 12345`）、日期格式与缺失/重复表头，支持未缓存公式提示与附带工作表/行号定位。
- **CSV / TSV / TXT / 目录树与文件夹扫描**：自动探测 UTF-8 / UTF-8-SIG / GBK / GB18030 / UTF-16 / UTF-32 等多编码与分隔符，支持 `tree /f` 目录树、文件清单及本地目录高速递归扫描。
- **入库前四分类预览**：自动划分为**新增待入库**、**库内重复**、**批内重复**、**未识别与待确认**，支持预览表内人工修正编码后再次校验入库，拖拽文件自动增量追加至队列。

### 2. 多表工作台
- **多源加载与列管理**：支持通过导入选择弹窗挑选 Excel 指定工作表、Word 表格及表头行，一键载入当前 SQLite 总库作为数据表，支持表顺序调整、批量移除、列重命名/删除与实时搜索预览。
- **跨表查重、表内查重与单表去重**：
  - **智能编码模式**：自动从指定列或全列提取归一化标准编码进行比对。
  - **指定列值模式**：支持单列或按表有序多列组合键精确查重，可配置忽略大小写、去除首尾空白与标点过滤。
  - 一键生成结构化分析视图：**跨表重复分组**、**表内重复分组**、**全表共同交集**、**各表独有记录**及**未识别记录**，支持双击跳转原表行或查看完整行详情。
- **灵活的多表合并引擎**：
  - **全量纵向追加（Union All）**：支持按列名对齐或按列位置对齐，保留所有记录。
  - **纵向合并并去重（Union Dedup）**：支持 5 种去重保留策略——保留首条、保留末条、保留字段最完整记录、保留标签评分最高记录，以及**字段互补融合（Coalesce）**。
  - **主键横向关联（Join）**：支持左连接（Left）、内连接（Inner）与全外连接（Outer），支持一对多安全行数上限保护与首次匹配模式。
  - 合并结果可直接预览、后台异步导出，或一键写入当前 SQLite 数据总库。

### 3. 多模式智能编码解析引擎
- 内置多阶段预清洗与规则匹配引擎，支持全角字符归一化、路径与目录树符号剥离、站点前缀与冗余修饰词清理。
- 支持标准字母数字编码、紧凑无连字符编码、数字+字母混合前缀、日期索引型、长短编码等多种格式归一化，并准确区分常规日期、UUID、分辨率及技术标识（如 `2026-11-26`、`1080P`、`ISO-9001` 等）以减少误识别。
- 自动剥离紧邻中文/英文后缀标签（如版本、清晰度、字幕标记）与分集标记（如 `CD1/CD2`、`Part1/Part2`），支持单行多编码提取、父目录回退解析及自定义正则表达式规则（内置防回溯校验与实时清洗预览）。

### 4. 快速查重、双列表覆盖率对比、总库编辑与重复项清理
- **与当前库比对（不修改库）**：粘贴多行文本或拖入 TXT / Word / Excel / CSV 文件，后台异步即时区分已存在、未找到（新数据）与未识别项，支持一键入库或导出清单。
- **双列表覆盖率与缺漏对比（查全 / 找缺失）**：支持将当前总库、多格式文件（TXT / Word / Excel / CSV / TSV）或粘贴文本设为**基准清单 A** 与**目标核对清单 B**，支持 A/B 互换以及**智能编码** / **精确文本行**双模式；实时计算目标清单覆盖率，保留原始顺序逐条标记`已包含`与`缺失`并附带基准 A 匹配文件名与标签，支持`全部 / 只看缺失 / 只看已包含`快捷筛选、一键复制缺失编码，以及异步导出缺失清单（TXT/CSV）与 5 工作表 Excel 完整覆盖率报告。
- **数据总库管理**：支持双击记录打开详情与在线编辑弹窗（修改标准编码、标签、分集、备注），支持快捷键 `Ctrl+1..6` 切页、`Ctrl+F` 搜索、`F5` 刷新、`Ctrl+C` 复制与 `Delete` 安全删除。
- **重复项中心与安全清理**：按标准编码分组展示重复记录，清晰区分**疑似分集**与真正重复项，提供智能勾选策略、全选误删提醒及删除前自动 `.bak` 备份。

### 5. SQLite 多库管理与多格式高速安全导出
- 支持创建、打开与切换多个独立 `.db` 数据库，采用 WAL 模式与 SQL 聚合索引，支持在线热备份、恢复前完整性校验与自定义备份保留数量。
- 支持后台多线程导出为：
  - **Excel (`.xlsx`)**：多工作表报表，具备公式注入防护、工作表名安全清洗、超长单元格截断保护、自适应列宽、冻结表头与斑马纹样式。
  - **Word (`.docx`)**：基于底层 XML 快速构建，数千行表格秒级导出带标题、统计摘要与样式表格的结构化文档。
  - **CSV (`.csv`, UTF-8-SIG)** 与 **TXT (`.txt`)**：兼容 Excel 直接打开与纯文本清单流转，内置公式注入转义与换行符清洗。
- 基于 PySide6 (Qt6) 构建，支持浅色 / 深色主题即时切换、单实例保护与 `<data_dir>/logs/crash.log` 崩溃日志自动记录。

## 快速开始

### 方式一：直接下载预编译版本（推荐）

前往 [Releases](https://github.com/yellowdking/data-dedup-tool/releases) 页面下载最新版本的 `data-dedup-tool-windows-x64.exe`（或解压 `data-dedup-tool-windows-x64.zip` 中的 `数据查重程序.exe`），双击即可运行，无需安装 Python 环境。可对照随附的 `SHA256SUMS.txt` 校验文件完整性。

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

## 便携模式与命令行参数

### 便携模式（Portable Mode）

默认情况下，单文件 EXE 会将配置文件、数据库与备份保存在 `%APPDATA%\DataDedupTool\` 中。若希望程序与数据随 U 盘或指定目录整体携带：

1. 在 `data-dedup-tool.exe`（或 `数据查重程序.exe`）**同级目录**下新建一个空文件，命名为 `portable.flag`。
2. 再次启动程序时，配置、数据库与日志将自动保存至程序旁的 `user_data/` 目录，且目录内路径以相对路径记录，移动文件夹后无需重新配置。

> 请确保便携目录具有写入权限（避免在只读的系统 `Program Files` 目录下启用 `portable.flag`）。

### 命令行参数

```powershell
# 指定启动时打开的数据库
data-dedup-tool.exe --db "D:\work\records.db"

# 指定独立的用户数据与配置目录
data-dedup-tool.exe --data-dir "D:\work\dedup_data"

# 指定启动主题（light 或 dark）
data-dedup-tool.exe --theme dark

# 无头环境运行自检（验证 Qt、SQLite、openpyxl、python-docx 与资源完整性）
$env:QT_QPA_PLATFORM = "offscreen"
data-dedup-tool.exe --smoke-test
```

## 运行测试与本地打包

```powershell
# 安装测试与打包依赖（Pillow 仅在重新生成应用图标时需要，不会被打包进 EXE）
pip install -r requirements.txt
pip install pytest pyinstaller Pillow

# 运行完整单元与无头 UI 自动化测试
$env:QT_QPA_PLATFORM = "offscreen"
pytest -v

# 打包为精简单文件 Windows 可执行程序并自动运行 --smoke-test 冒烟验证
# 输出：dist/data-dedup-tool.exe 与 dist/数据查重程序.exe
python scripts/build_exe.py
```

## 项目结构

```text
data-dedup-tool/
├── src/
│   ├── app.py                  # 应用入口、高 DPI、单实例锁、崩溃日志与 --smoke-test
│   ├── core/                   # 核心引擎
│   │   ├── io_utils.py         # 共享编码探测、单元格归一化与附属扩展名定义
│   │   ├── models.py           # 数据总库核心模型
│   │   ├── table_data.py       # 多表工作台数据结构与配置模型
│   │   ├── doc_reader.py       # Word / Excel / CSV / TXT / DB 多格式加载器
│   │   ├── table_dedup.py      # 多表跨表/表内查重引擎
│   │   ├── table_merge.py      # 多表追加/去重合并/横向关联引擎
│   │   ├── table_engine.py     # 多表引擎统一导出接口
│   │   ├── table_exporter.py   # 多工作表 Excel / 高速 Word / CSV / TXT 报表导出
│   │   ├── parser.py           # 多模式智能编码解析引擎
│   │   ├── normalizer.py       # 文本预清洗、全角与编码归一化
│   │   ├── rules.py            # 规则库、防回溯校验与黑名单配置
│   │   ├── scanner.py          # 多格式文档与目录递归扫描器
│   │   ├── database.py         # SQLite WAL 多库仓储与热备份管理
│   │   ├── dedup.py            # 单库查重、交叉比对与清理引擎
│   │   ├── exporter.py         # 数据库结果安全导出器
│   │   └── config.py           # 用户配置持久化与便携模式支持
│   ├── ui/                     # PySide6 桌面界面视图、模型与组件（含多表工作台）
│   └── resources/              # 图标与静态资源
├── tests/                      # 核心引擎、导出安全与无头 UI 自动化测试套件
├── scripts/
│   ├── build_exe.py            # PyInstaller 精简单文件打包与冒烟验证脚本
│   └── version_info.py         # Windows PE VSVersionInfo 版本元数据生成器
├── build.spec                  # PyInstaller 精简构建配置（含 python-docx 模板）
├── .github/workflows/          # GitHub Actions CI、冻结 EXE 冒烟测试与自动发布流水线
├── pyproject.toml
├── requirements.txt
└── LICENSE
```

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
