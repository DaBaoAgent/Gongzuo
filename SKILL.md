---
name: gongzuo
version: "2.0"
description: 文件智能索引与秒级检索。全文件正文全文搜(FTS5 trigram)+图片视频AI视觉描述，SQLite本地库。
author: Dabao
trigger: 用户要索引/扫描/搜索/查找/定位某个文件夹里的文件、文档、图片、视频，或要从已索引库中秒级检索内容
---

# 文件智能索引与秒级检索 (gongzuo)

> **🔄 合并说明（2026-08-09）**：本技能为"本地文件/媒体索引检索"唯一伞形入口，已吸收两个重叠技能：
> - `media-indexer` → 其视觉媒体索引能力本文"模式3"已完整覆盖；新增 `references/indexer-patterns.md`（视觉API调用/JSON解析/多线程坑的工程模式）
> - `local-file-index` → 其工程模式（FTS5 trigram/提取兜底链/增量）本文已完整覆盖；新增 `references/doc-extraction-fallbacks.md`（各格式提取失败签名与正解）
> - 检索前先 `file_search.py --stats` 确认库存在（历史教训：库目录被清理后 watchdog 全失效）。

## 概述

统一索引方案，一个本地 SQLite 库覆盖两类检索：

1. **全文件索引**（doc_indexer.py）— 所有非媒体文件的**正文全文**（txt/md/pdf/docx/xlsx/pptx/doc/xls/epub/xmind/代码/配置…），
   图片/视频/音频存元数据（文件名/路径可搜）。FTS5 **trigram** 分词器，中文 3 字子串精准命中，秒级。
2. **Umi-OCR 图片/视频识别**（doc_indexer.py --ocr-media/--ocr-video）— 本地免费 OCR 提取图中文字
   与视频抽帧字幕/画面文字，可搜图片上的字、视频里的台词。

两者都进 SQLite FTS5，搜索入口统一为 `file_search.py`。

## 模式1: 全文件索引（doc_indexer.py）

```bash
python scripts/doc_indexer.py --root "D:\BaiduSyncdisk" --db "D:\@kaifa\gongzuo-data\file_index.db" [--workers 8]
# 增量：重跑自动跳过 path+mtime+size 未变的文件，只处理新增/变更
# 图片OCR（Umi-OCR 本地识别，断点续传，可反复重跑）:
python scripts/doc_indexer.py --ocr-media
# 仅扫描件PDF识别:
python scripts/doc_indexer.py --ocr-pdf
# 视频抽帧OCR（每2秒1帧→识别画面文字/字幕，--exclude-video-dir 可排除目录）:
python scripts/doc_indexer.py --ocr-video
```

- 噪音自动排除：node_modules / .git / .cache / .codex_* / 字体(.ttf) / 资源映射(.map) / 二进制(.exe) 等
- 覆盖格式：纯文本60+种(编码自动探测 UTF-8→GBK→Big5)、docx/xlsx/pptx、pdf(PyMuPDF)、
  doc(antiword→olefile UTF-16兜底)、xls(xlrd)、epub/xmind/odt(zip解包)、eml、zip包内清单
- 断点续传：已索引未变文件秒跳过

## 模式2: 秒级搜索（file_search.py）

```bash
python scripts/file_search.py "电动轮椅"                  # 全文+文件名，秒级
python scripts/file_search.py "接待话术" --ext doc,docx    # 按扩展名
python scripts/file_search.py "提成" --folder "12 抖店"    # 按目录
python scripts/file_search.py "展会" --type media          # 只搜媒体
python scripts/file_search.py "报表" --copy 3              # 复制第3个结果路径到剪贴板
python scripts/file_search.py "关键词" --open 1            # 打开第1个结果
python scripts/file_search.py "关键词" --dir 2             # 资源管理器定位第2个结果
python scripts/file_search.py "关键词" --content 1         # 打印第1个结果的全文
python scripts/file_search.py "关键词" --json              # JSON输出(agent/脚本用)
python scripts/file_search.py --stats                      # 库状态
```

搜索策略：jieba 分词 → ≥3字词组 FTS5 trigram 精准匹配 → <3字 LIKE 兜底（文件名+目录+预览+正文前2万字）→ 零命中自动降级全子串 LIKE。
**媒体优先排序**：命中结果按 `is_media=1 优先` 排列，图片/视频排最前——找图（OCR文字/文件名命中）时不用翻长文档。

**日常问答模式**：用户随口问资料/账号/文件类问题（如"我的QQ机器人账号有哪些"、"提成规则是啥"）时，
直接用 file_search.py 查库秒回，不要重新扫描、不要问用户想找什么文件。答案给出文件路径+关键内容。

## 模式5: 每日自动增量索引（cron）

已配置 Hermes cron 任务 `gongzuo每日增量索引`（job_id 9ef880df69ed）：
每天 **04:00** 自动跑 `doc_indexer.py --root D:\BaiduSyncdisk` 增量刷新。

- watchdog 脚本：`C:\Users\xxx13\AppData\Local\hermes\scripts\gongzuo_daily_index.py`
- 行为：有新增/变更 → 输出摘要；有失败 → 警报；无变化 → 静默
- 查看运行记录：`cronjob(action='list')`；改投递目标用 `cronjob(action='update', deliver='telegram')`

## 模式4: 状态查询

```bash
python scripts/file_search.py --stats        # 全文件库
python scripts/doc_indexer.py --stats        # 同
```

## 技术细节

### 全文件库 Schema（file_index.db）
```
files 表: file_path(唯一) file_name file_ext file_size file_mtime folder_path
          is_media doc_type content_preview(300字) content_text(全文,上限100万字符) error
files_search: FTS5 trigram 外部内容表(file_path/file_name/folder_path/preview/content_text)
```
trigram 按 3 字子串建倒排索引：`MATCH '"电动轮椅"'` 精准命中；2字查询由 LIKE 兜底；索引体积≈正文3倍。

### 提取矩阵（doc_indexer.py）
| 格式 | 提取 | 失败兜底 |
|------|------|----------|
| txt/md/json/yaml/csv/html/js/py… | 编码探测直读 | 二进制检测→跳过 |
| docx/xlsx/pptx | python-docx/openpyxl/python-pptx | — |
| pdf | PyMuPDF 逐页(≤500页) | 无文字层(扫描件)→ `--ocr-pdf` 用 Umi-OCR 逐页渲染识别 |
| doc(老格式) | antiword -m UTF-8 | RTF/HTML伪装嗅探→olefile UTF-16流抽可读段 |
| xls | xlrd | — |
| epub/xmind/odt | zip 解包抽文本/标题 | — |
| 图片 jpg/png/bmp/webp/tif | 元数据 only | `--ocr-media` 用 Umi-OCR 提取图中文字 |
| 图片 heic/heif/psd | 元数据 only | Pillow(pillow-heif) 转 PNG 后 Umi-OCR |
| 视频/音频 | 元数据 only | 描述靠 --merge-media 回填 |

### Umi-OCR 图片识别集成（模式1.5）

`doc_indexer.py --ocr-media` 会调用本机 Umi-OCR(127.0.0.1:1224)给**无正文图片**提取文字入库：

- 覆盖 jpg/jpeg/png/webp/bmp/tif + heic/heif/psd(Pillow 转换)，超大图自动缩至 2880px
- **断点续传**：已有 content_text 的跳过；失败标 `ERR|OCR|...` 下次重跑自动重试
- **单线程**跑（Umi-OCR 后端并发差，勿加 workers）；~0.2-1s/张
- 无文字图片标记 `【无文字图片】`（doc_type=media-ocr-notext），避免反复重试
- 全量 8296 张约 1.5-2.5 小时；增量只处理新增/失败，秒级
- 依赖：Umi-OCR exe 已装 `D:\@kaifa\Umi-OCR\`（**单图/交互式识别用 umi-ocr 技能**），Pillow + pillow-heif
- 坑：HEIC 必须 `from pillow_heif import register_heif_opener; register_heif_opener()` 再 Image.open，否则 cannot identify
- 坑：伪装 HEIC（扩展名 .jpg 但内容 ftyp 魔数）→ `_image_to_b64` 已做 ftyp 嗅探自动转换，Umi-OCR 报 code=301 imdecode failed 时先查这个
- 坑：超大图(>30MB)直接送 OCR 会卡死 → 已自动跳过；手机原图缩 720p 提速（详见 umi-ocr 技能 Pitfalls）

### jieba 分词（搜索端）
file_search.py 用 jieba.cut_for_search 拆查询词，不落库，无索引期开销。

### 依赖
- 全文件索引: fitz(PyMuPDF), python-docx, openpyxl, python-pptx, xlrd, olefile
- 搜索: jieba（本机已装）
- 图片OCR: Umi-OCR exe(D:\@kaifa\Umi-OCR\,skill: umi-ocr), Pillow, pillow-heif(HEIC)
- 视频OCR: ffmpeg/ffprobe (C:\Users\xxx13\ffmpeg\ffmpeg-8.1.1-essentials_build\bin)
- 系统: antiword（git-bash 自带 /mingw64/bin）

## 已知坑

- **SQLite 多线程**：doc_indexer 用每线程独立连接（threading.local）+ busy_timeout=30000，不要改回共享连接
- **FTS5 trigram 只认 ≥3 字符**：2字中文词自动走 LIKE，别惊讶命中方式不同
- 超大文件(>100万字符)正文截断，超限部分搜不到
- 老 .doc（WPS/加密/损坏）部分失败，error 标记 `PERMANENT|...` 不再重试
- doc_indexer.py 的 log 写在库同目录 doc_indexer.log

## 当前已索引库

| 库 | 内容 | 状态 |
|----|------|------|
| D:\@kaifa\gongzuo-data\file_index.db | 全文件正文+媒体元数据+图片OCR文字 | 12,076 文件；图片 OCR 全量完成 2026-08-09：6,344 识别 + 846 无文字 + 30 超大跳过 + 16 扫描PDF，0 失败 |

## 参考文档

- `references/integration-plan.md` — 整合方案全文（开源对比/架构/覆盖矩阵/限制/后续增强）
- `references/retry-failed-files.md` — 媒体索引失败重试模式
