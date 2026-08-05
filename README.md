<!-- README-PROMO:START -->
<p align="center">
  <img src="assets/readme/hero.webp" alt="Gongzuo 离线中文全文检索引擎：秒级搜索 PDF、Office、代码和文本正文" width="100%" />
  <img src="assets/readme/workflow.webp" alt="Gongzuo 工作流程：本地文档增量索引、中文精准搜索、结果快速打开与定位" width="100%" />
  <img src="assets/readme/beginner.webp" alt="Gongzuo 新手三步开始：安装依赖、索引文件夹、搜索中文关键词" width="100%" />
</p>
<!-- README-PROMO:END -->
<div align="center">

# Gongzuo 工作

### 秒级中文全文检索 · 全文件智能索引引擎

**Offline Full-Text File Search Engine for Chinese Documents — SQLite FTS5 + Trigram + jieba**

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![SQLite FTS5](https://img.shields.io/badge/SQLite-FTS5%20Trigram-green)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![中文](https://img.shields.io/badge/中文-全文检索-red)

</div>

**Gongzuo（工作）** 是一个离线、隐私优先的本地文件全文搜索引擎。它索引硬盘上所有文档（PDF / Word / Excel / PPT / 文本 / 代码 / 思维导图…）的**正文内容**，用 SQLite FTS5 **trigram** 分词器实现中文 3 字子串精准命中，**4 万+ 文件全库查询仅需 1~40ms**。图片 / 视频按文件名、路径索引（可选接入 AI 视觉模型生成语义描述）。

> 它是 Everything（只搜文件名）、Recoll / DocFetcher（重 GUI、中文分词难配）、AnyTXT（闭源）之外，一个**开源、可脚本化、中文精准**的替代品。

---

## ✨ 特性

| | 说明 |
|---|---|
| ⚡ **秒级检索** | SQLite FTS5 倒排索引，46,000 文件全库查询 1~40ms（含 LIKE 兜底 <0.5s） |
| 🎯 **中文精准** | trigram 按 3 字子串建索引：`MATCH '"电动轮椅"'` 精准命中，无单字拆分噪音 |
| 🔒 **离线隐私** | 一切本地 SQLite，银行流水 / 账号密码 / 商业资料不出本机 |
| 📄 **全格式覆盖** | PDF(PyMuPDF) · docx/xlsx/pptx · doc/xls(老格式) · epub/xmind/odt · eml · 60+ 文本/代码格式，编码自动探测 UTF-8→GBK→Big5 |
| 🧹 **智能噪音排除** | node_modules / .git / .cache / AI 工作目录 / 字体 / 二进制自动跳过 |
| ♻️ **增量索引** | path+mtime+size 比对，重跑 12,000 文件仅 ~1 秒 |
| 📋 **直达操作** | 复制路径 / 直接打开 / 资源管理器定位 / 全文预览，一条命令搞定 |
| 🤖 **可脚本化** | `--json` 输出，适合接入 Agent / RAG / 自动化工作流 |

## 🔍 快速开始

```bash
# 1) 安装依赖
pip install pymupdf python-docx openpyxl python-pptx xlrd olefile jieba

# 2) 索引（首次全量，之后增量秒级）
python scripts/doc_indexer.py --root /path/to/your/folder --db /path/to/file_index.db

# 3) 搜索（秒级）
python scripts/file_search.py "电动轮椅"                    # 全文 + 文件名
python scripts/file_search.py "接待话术" --ext pdf,docx     # 按扩展名
python scripts/file_search.py "提成" --folder "12 抖店"     # 按目录
python scripts/file_search.py "展会" --type media           # 只搜媒体
python scripts/file_search.py "报表" --copy 3               # 复制第3个结果路径
python scripts/file_search.py "关键词" --open 1             # 直接打开
python scripts/file_search.py --stats                       # 索引状态
```

## 📦 覆盖格式

| 类别 | 格式 |
|---|---|
| 纯文本/代码 | txt · md · json · yaml · csv · html · js/ts · py · 60+ 种（编码自动探测） |
| Office 新版 | docx · xlsx · pptx（含表格、备注） |
| Office 老版 | doc（antiword → OLE UTF-16 流兜底）· xls（xlrd） |
| PDF | PyMuPDF 逐页抽取（含扫描件 OCR 扩展点） |
| 思维导图 | xmind（节点文字） |
| 电子书 | epub（章节全文） |
| 邮件 | eml |
| 压缩包 | zip / 7z / rar（包内文件清单） |
| 媒体 | 图片/视频/音频按文件名+路径索引（可选 AI 视觉语义描述） |

## 🏗️ 架构

```
文件夹(任意大小)
   │  doc_indexer.py — 8线程增量扫描：文本直读 / Office / PDF / 老格式兜底 / 噪音排除
   ▼
SQLite (file_index.db)
   ├── files 表（路径/名称/大小/预览/全文/类型）
   └── files_search — FTS5 trigram 倒排索引（外部内容表）
   ▼
   │  file_search.py — jieba 分词 → trigram 精准匹配 → LIKE 兜底 → 自动降级
   ▼
   秒级结果：编号+预览高亮+复制/打开/定位/JSON
```

## 🆚 为什么不用其他方案

| 方案 | 局限 | Gongzuo |
|---|---|---|
| Everything | 只搜文件名，无正文 | ✅ 全文 |
| Windows 自带搜索 | 慢、中文差 | ✅ 1~40ms |
| Recoll (Xapian) | GUI 重、中文分词需折腾 | ✅ 开箱即用 |
| DocFetcher | 按文件夹建索引、无统一库 | ✅ 一个库搜全部 |
| AnyTXT Searcher | 闭源、无法自动化 | ✅ 开源可脚本化 |
| ripgrep | 每次全盘扫，非秒级 | ✅ 预索引倒排 |

## 🧠 搜索原理

1. **jieba 分词**拆查询 → ≥3 字词组走 **FTS5 trigram** 精准子串匹配（中文 3 字索引，无单字噪音）
2. **<3 字词**（如"轮椅"）自动走 LIKE 兜底：文件名+目录+预览+正文前 2 万字
3. FTS 零命中 → 自动降级全子串 LIKE，保证召回
4. 排序：文件名命中优先 → FTS rank / 修改时间

## 🛠️ 集成与扩展

- **Agent 问答**：`--json` 输出可直接接入 LLM Agent，实现"问一句秒回文件在哪"
- **定时增量**：`schtasks` / cron 每周自动重跑，保持索引最新
- **OCR 扩展点**：扫描型 PDF 可接 PaddleOCR 识别后入库
- **AI 视觉描述**：`media_indexer.py` 用智谱 glm-4.6v-flash 免费模型给图片/视频生成语义描述，`--merge-media` 一键回填

## 📜 License

[MIT](LICENSE)

## 👤 贡献者

- **Dabao** — 设计 & 实现
