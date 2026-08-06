---
name: gongzuo
version: "2.0"
description: 文件智能索引与秒级检索。全文件正文全文搜(FTS5 trigram)+图片视频AI视觉描述，SQLite本地库。
author: Dabao
trigger: 用户要索引/扫描/搜索/查找/定位某个文件夹里的文件、文档、图片、视频，或要从已索引库中秒级检索内容
---

# 文件智能索引与秒级检索 (gongzuo)

## 概述

统一索引方案，一个本地 SQLite 库覆盖两类检索：

1. **全文件索引**（doc_indexer.py）— 所有非媒体文件的**正文全文**（txt/md/pdf/docx/xlsx/pptx/doc/xls/epub/xmind/代码/配置…），
   图片/视频/音频存元数据（文件名/路径可搜）。FTS5 **trigram** 分词器，中文 3 字子串精准命中，秒级。
2. **媒体 AI 描述**（media_indexer.py，原功能）— 智谱 glm-4.6v-flash 视觉模型给图片/视频生成语义描述，
   可搜"电动轮椅 户外 场景"这类内容语义。

两者都进 SQLite FTS5，搜索入口统一为 `file_search.py`。

## 模式1: 全文件索引（doc_indexer.py）

```bash
python scripts/doc_indexer.py --root "D:\BaiduSyncdisk" --db "D:\zhipuai_test\file_index.db" [--workers 8]
# 增量：重跑自动跳过 path+mtime+size 未变的文件，只处理新增/变更
# 媒体描述回填（媒体库存在时）:
python scripts/doc_indexer.py --root "D:\BaiduSyncdisk" --merge-media "D:\zhipuai_test\media_index.db"
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

**日常问答模式**：用户随口问资料/账号/文件类问题（如"我的QQ机器人账号有哪些"、"提成规则是啥"）时，
直接用 file_search.py 查库秒回，不要重新扫描、不要问用户想找什么文件。答案给出文件路径+关键内容。

## 模式3: 媒体 AI 描述索引（media_indexer.py）

仅当需要图片/视频的**内容语义**搜索时使用（如"找电动轮椅的产品图"）：

```bash
# 阿里百炼 qwen3-vl-plus（默认，推荐：~0.002元/张，8并发≈150张/分）
python scripts/media_indexer.py --root "D:\BaiduSyncdisk" --db "D:\zhipuai_test\media_index.db" --workers 8 --provider dashscope
# 智谱 glm-4.6v-flash（免费但 QPS 极低 ~1.2个/分/并发）
python scripts/media_indexer.py --root "D:\BaiduSyncdisk" --db "D:\zhipuai_test\media_index.db" --workers 3 --provider zhipu
# 分批/试跑: --limit N --shuffle（随机取样）；断点续传: 重跑自动跳过 path+mtime 未变
python scripts/enhance_segmentation.py --db "D:\zhipuai_test\media_index.db"
python scripts/doc_indexer.py --root "D:\BaiduSyncdisk" --merge-media "D:\zhipuai_test\media_index.db"  # 回填到全文件库
```

- `--provider dashscope|zhipu`（默认 dashscope）、`--model` 自定义模型、`--limit N`/`--shuffle` 分批试跑
- 依赖: `DASHSCOPE_API_KEY`（百炼）或 `GLM_API_KEY`（智谱），写在 `~/.hermes/.env` 或 `D:\@佳康顺矩阵\hermes\.env`
- 脚本自动累计 token 消耗并在结束时打印（费用以百炼账单为准）
- 已知坑: qwen3-vl 拒绝 <100px 小图（脚本自动放大兜底）；json 解析正则应写单反斜杠 `\{.*\}`（双反斜杠会永远解析失败，2026-08 已修复）

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
| pdf | PyMuPDF 逐页(≤500页) | 扫描件无文字层→搜不到(需OCR) |
| doc(老格式) | antiword -m UTF-8 | RTF/HTML伪装嗅探→olefile UTF-16流抽可读段 |
| xls | xlrd | — |
| epub/xmind/odt | zip 解包抽文本/标题 | — |
| 媒体(jpg/mp4…) | 元数据 only | 描述靠 --merge-media 回填 |

### jieba 分词（搜索端）
file_search.py 用 jieba.cut_for_search 拆查询词，不落库，无索引期开销。

### 依赖
- 全文件索引: fitz(PyMuPDF), python-docx, openpyxl, python-pptx, xlrd, olefile
- 搜索: jieba（本机已装）
- 媒体AI: 百炼 DASHSCOPE_API_KEY 或智谱 GLM_API_KEY, Pillow, jieba, ffmpeg/ffprobe
- 系统: antiword（git-bash 自带 /mingw64/bin）

## 已知坑

- **SQLite 多线程**：doc_indexer 用每线程独立连接（threading.local）+ busy_timeout=30000，不要改回共享连接
- **FTS5 trigram 只认 ≥3 字符**：2字中文词自动走 LIKE，别惊讶命中方式不同
- 超大文件(>100万字符)正文截断，超限部分搜不到
- 老 .doc（WPS/加密/损坏）部分失败，error 标记 `PERMANENT|...` 不再重试
- 原媒体库 D:\zhipuai_test\media_index.db **已不存在**（2026-08 核实），需重跑 media_indexer.py
- 模型ID/免费模型限速问题见 references/zhipuai-model-history.md
- doc_indexer.py 的 log 写在库同目录 doc_indexer.log

## 当前已索引库

| 库 | 内容 | 状态 |
|----|------|------|
| D:\zhipuai_test\file_index.db | 全文件正文+媒体元数据 | 11,225 文件 (1,564 文档正文 + 9,720 媒体元数据)，99MB，失败 1（二进制webgl库） |
| D:\zhipuai_test\media_index.db | 媒体AI描述(旧) | 已丢失，可重跑重建 |

## 参考文档

- `references/integration-plan.md` — 整合方案全文（开源对比/架构/覆盖矩阵/限制/后续增强）
- `references/zhipuai-model-history.md` — 视觉模型变更历史
- `references/retry-failed-files.md` — 媒体索引失败重试模式
