---
name: gongzuo
version: "2.0"
description: 文件智能索引与秒级检索。全文件正文全文搜(FTS5 trigram)+Umi-OCR图片视频文字识别，SQLite本地库。
author: Dabao
trigger: 用户要索引/扫描/搜索/查找/定位某个文件夹里的文件、文档、图片、视频，或要从已索引库中秒级检索内容
---

# 文件智能索引与秒级检索 (gongzuo)

> **🔄 合并说明（2026-08-09）**：本技能为"本地文件/媒体索引检索"唯一伞形入口，已吸收两个重叠技能：
> - `media-indexer` → 其媒体索引能力由 Umi-OCR 本地识别（`--ocr-media`/`--ocr-video`）取代；原付费视觉描述功能已移除
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

推荐配置每日 **04:00** cron 自动跑 `doc_indexer.py --root D:\BaiduSyncdisk` 增量刷新（断点续传，重跑即增量）。

- watchdog 脚本：`gongzuo_daily_index.py`（放 Hermes scripts 目录）
- 可选汇编：`gongzuo_assembly.py` 把新增数据整合进数据看板 md（增量快照 `.gongzuo_assembly_state.json`，--init 首次部署标记现有文件）
- 行为：有新增/变更 → 输出摘要（含汇编结果）；有失败 → 警报；无变化 → 静默
- 查看运行记录：`cronjob(action='list')`；改投递目标用 `cronjob(action='update', deliver='telegram')`

## 模式6: Viking 知识库导出（lib_to_viking.py，2026-08-23）

把任意 gongzuo 索引库内容导出到 OpenViking 记忆库（HTTP API），供语义检索/跨会话召回。**参数化通用版**：

```bash
python scripts/lib_to_viking.py --db "库.db" --root "根目录" \
    --base "viking://user/default/resources/库名" --name 库名
```

- 首次全量导出，之后增量只传新增/变更（指纹=mtime+size，状态文件 `<name>_to_viking.state.json`）
- 幂等：同 URI 重复添加=更新；409 busy 自动重试（3 次×4s，队列慢时快速跳过，失败文件下次增量自动补）
- 坑：库中若有 root 外的文件，relpath 会以 `..` 开头 → viking 拒绝 `Unsafe URI traversal`。已修复：自动映射为 `盘符_绝对路径` 安全 URI
- 每文件渲染 markdown（元数据+OCR文字+【画面】描述）→ temp_upload → POST /resources
- watchdog 语义：有新增打印摘要、无新增静默（exit 0）
- 依赖：OpenViking 服务（127.0.0.1:1933，dev 模式无鉴权），需先有 doc_indexer 建好的索引库

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
| 视频/音频 | 元数据 only | `--ocr-video` 抽帧 Umi-OCR 识别画面文字/字幕 |

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

### Qwen3-VL 场景描述集成（模式1.6，2026-08-14 上线）

本地视觉模型给图片补"画面里是什么"——Umi-OCR 只出文字，Qwen3-VL 出主体/颜色/场景描述，进 FTS 可搜（如"红色电动轮椅"、"摄影棚"、"展厅"）：

```bash
# 缺口模式(默认): 只处理无文字/未处理/超大跳过图片(~678张,断点续传)
python scripts/doc_indexer.py --vision-desc
# 全部图片(6382张 ≈ 46小时,慎用;建议拆夜跑或先跑缺口)
python scripts/doc_indexer.py --vision-desc-all
# 限量测试
python scripts/doc_indexer.py --vision-desc --vision-limit 3
# 视频场景描述(缺口模式): 片头1帧(2s)+片尾1帧(尾-2s)→Qwen3-VL→[mm:ss]入库, ~24s/条
python scripts/doc_indexer.py --vision-video [--vision-limit N]
```

- 写入：content_text 追加 `【画面】 描述`（原 OCR 文字保留），doc_type=`media-vision`(缺口)/`media-vision-full`(全量)，断点续传（doc_type 以 vision 开头跳过）
- 前置：本地 llama-server 跑在 127.0.0.1:1234（模型与启动方式见 llama-cpp 技能），未启动会直接报错提示命令
- 速度：720px 缩图 + 80 tokens ≈ **26s/张**（20-35s 区间）→ 缺口 678 张 ≈ 5 小时（通宵档）；全量 6382 张 ≈ 46 小时
- 分辨率基准（2026-08-14 实测单图 1024x1536）：原图不压缩 136s（慢4-5倍无收益）；**720px≈32s 细节最稳**（型号/配色/小字）；600px≈35s 有轻微歧义（手动→电动）；**500px≈24s 主体级下限**（五大模块/主体/场景全对，配色小偏差）。索引用途 720px 为默认甜点
- 服务器被杀后不会自愈：`--vision-desc` 报"视觉服务器未启动"时，手动重启 llama-server 即可
- 坑（2026-08-14 实测修复）：①HEIC 必须先 `register_heif_opener()` 否则 cannot identify（已写进 vision_describe_image）②.gif 不在 OCR_EXTS 会漏（已加入 PIL_CONVERT_EXTS）③视觉上限 60MB（>60MB 长图跳过）④服务器冷启动约 15s，索引器会等 60s（_vision_up wait=True）
- 模型：Qwen3-VL-8B Q4_K_M + mmproj（unsloth 文本 + 官方投影器配对），细节见 llama-cpp 技能

### 智谱 GLM-4V-Flash 场景描述（模式1.7，2026-08 上线）

云端免费视觉模型给图片/视频补"画面里是什么"——无需本地 llama-server，速度 ~1.2-1.4s/张（比 Qwen3-VL 本地快 20 倍）：

```bash
# 图片缺口模式(默认): 只处理无文字/未处理/超大跳过图片
python scripts/doc_indexer.py --vision-desc-zhipu
# 视频场景描述(智谱): 头尾各1帧 → glm-4v-flash → [mm:ss] 入库, ~3s/条
python scripts/doc_indexer.py --vision-video-zhipu
# 限量测试
python scripts/doc_indexer.py --vision-desc-zhipu --vision-limit 3
```

- **⚠ 重要坑**：`--ocr-media`/`--vision-*`/`--ocr-video` 等模式**必须显式带 `--root`**，否则 main() 会先无条件 scan 默认 root 把无关文件写进库！每次调用都要 `--root "目标目录" --db 库路径`
- **视频抽帧策略（2026-08-23 起）**：视频 OCR/场景描述一律**头尾各 1 帧**（2s 处 + 尾-2s 处），不再逐 2 秒扫全片——`_extract_head_tail_frames()` 实现
- 图片写入：content_text 追加 `【画面】 描述`，doc_type=`media-vision-zhipu`，断点续传
- 依赖：`ZHIPU_API_KEY` 环境变量（.env / setx 均可），智谱 open.bigmodel.cn，模型 glm-4v-flash（免费）
- 与本地 Qwen3-VL（--vision-desc）并存：缺口查询条件相同，先跑哪个就锁定 doc_type，另一个自动跳过已处理图
- 坑：智谱敏感审核（code 1301）极小图会误判拦截；超长图（详情页长图）需分段裁剪识别（详见 gongzuo 技能内文档）

### jieba 分词（搜索端）
file_search.py 用 jieba.cut_for_search 拆查询词，不落库，无索引期开销。

### 依赖
- 全文件索引: fitz(PyMuPDF), python-docx, openpyxl, python-pptx, xlrd, olefile
- 搜索: jieba（本机已装）
- 图片OCR: Umi-OCR(本地安装), Pillow, pillow-heif(HEIC)
- 图片场景描述: 智谱 glm-4v-flash 云端(免费,ZHIPU_API_KEY) 或 Qwen3-VL-8B llama-server(127.0.0.1:1234,本地模型)
- 视频OCR: ffmpeg/ffprobe（需加入 PATH）
- Viking 导出: OpenViking 服务(127.0.0.1:1933)
- 系统: antiword（git-bash 自带 /mingw64/bin）

## 已知坑

- **SQLite 多线程**：doc_indexer 用每线程独立连接（threading.local）+ busy_timeout=30000，不要改回共享连接
- **FTS5 trigram 只认 ≥3 字符**：2字中文词自动走 LIKE，别惊讶命中方式不同
- 超大文件(>100万字符)正文截断，超限部分搜不到
- 老 .doc（WPS/加密/损坏）部分失败，error 标记 `PERMANENT|...` 不再重试
- doc_indexer.py 的 log 写在库同目录 doc_indexer.log
- **弹窗坑**：pythonw 下 spawn 控制台程序（ffmpeg/ffprobe/7z/antiword）必须带 `creationflags=0x08000000`（CREATE_NO_WINDOW），否则每抽一帧弹一个黑窗（2026-08-14 实测：视频 vision 任务每 43s 闪一次窗）

## 当前已索引库

| 库 | 内容 | 状态 |
|----|------|------|
| D:\@kaifa\gongzuo-data\file_index.db | 全文件正文+媒体元数据+图片OCR文字 | 2026-08-14 实测 5,609 文件（文档 679 + 媒体 4,930），失败 0，库 131 MB（清理后计数） |

## 参考文档

- `references/integration-plan.md` — 整合方案全文（开源对比/架构/覆盖矩阵/限制/后续增强）
- `references/retry-failed-files.md` — 媒体索引失败重试模式
