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
# 视频抽帧OCR（头尾各1帧→识别画面文字/字幕，--exclude-video-dir 可排除目录）:
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
python scripts/file_search.py "轮椅" --after 2026-08-01    # 只搜修改时间 >= 该日期
python scripts/file_search.py "轮椅" --before 2026-01-01   # 只搜修改时间 <= 该日期
python scripts/file_search.py "报表" --copy 3              # 复制第3个结果路径到剪贴板
python scripts/file_search.py "关键词" --open 1            # 打开第1个结果
python scripts/file_search.py "关键词" --dir 2             # 资源管理器定位第2个结果
python scripts/file_search.py "关键词" --content 1         # 打印第1个结果的全文
python scripts/file_search.py "关键词" --json              # JSON输出(agent/脚本用)
python scripts/file_search.py --stats                      # 库状态
python scripts/file_search.py --stats --full               # 库状态+doc_type分布+待处理缺口
```

搜索策略：jieba 分词 → ≥3字词组 FTS5 trigram 精准匹配 → <3字 LIKE 兜底（文件名+目录+预览+正文前2万字）→ 零命中自动降级全子串 LIKE。
**媒体优先排序**：命中结果按 `is_media=1 优先` 排列，图片/视频排最前——找图（OCR文字/文件名命中）时不用翻长文档。

**日常问答模式**：用户随口问资料/账号/文件类问题（如"我的QQ机器人账号有哪些"、"提成规则是啥"）时，
直接用 file_search.py 查库秒回，不要重新扫描、不要问用户想找什么文件。答案给出文件路径+关键内容。

**QQ 渠道秒开图（2026-08-14 已验证）**：用户从 QQ 问资料时，图片/视频命中结果**直接在回复里带 `MEDIA:D:\绝对\路径`**（多张可跟多个 MEDIA: 标签），网关 QQ 适配器自动上传发送到 QQ 聊天窗（本地文件分块上传，单文件 ≤~100MB，无需外链）。主动推图/测试用 qqbot-deploy 技能 `scripts/send_image_message.py`。注意：QQ 发图只支持图片/视频/音频/文件类格式（jpg/png/webp/mp4/mp3…），回复纯路径文字用户看不到图——必须带 MEDIA: 标签。

## 模式3: 微信聊天记录导入（CipherTalk json → 可搜正文）

微信导出（CipherTalk 等）的 json 直接索引有两大坑：① 整 dump 含 JSON 语法噪音；
② 大文件（>100万字符）被截断，后半段消息搜不到。必须先转换：

```bash
python scripts/chat_json_to_txt.py "D:\@佳康顺\聊天记录\佳康顺聊天记录"
# 每个 <聊天>.json → 同目录/_chattext/<聊天>.txt，格式 "[时间] 发送者: 内容" 一行一条
# 幂等: 源未变自动跳过; --force 强制重转
```

转换后再增量索引（含 _chattext txt，压缩比 ~7%，正文质量远高于原始 json）：

```bash
python scripts/doc_indexer.py --root "D:\@佳康顺\聊天记录\佳康顺聊天记录" --db "D:\@kaifa\gongzuo-data\file_index.db"
```

坑：CipherTalk 消息字段是 `messages[]`（formattedTime/senderDisplayName/isSend/content/type），
非文本消息（图片/语音/动画表情）content 是 URL 或空，转换脚本统一标 `【类型】`；
list 顶层格式（如蒸馏中间产物）无 messages 字段会自动跳过。

## 模式4: 每日自动增量索引 + 数据汇总（cron，2026-08-23 起统一由 worklife_daily_index.py 管）

已配置 Hermes cron 任务 `每日增量抓取合并(BDSync+XHP+会话)→Viking`（job 9ef628304b40，每天 **01:00**，script=hermes/scripts/worklife_daily_index.py），
一次跑完**索引+OCR+描述+导出Viking+数据汇总**（原 04:00 gongzuo 增量索引任务已并入删除，避免重复扫描抢 Umi-OCR）：

- 三路串行：
  - BaiduSyncdisk（file_index.db）→ 增量索引+OCR+智谱描述 → `lib_to_viking.py` 导出
  - XHP-知识库（xhp_kb.db）→ 同上
  - Hermes 会话（state.db）→ `hermes_to_viking.py`
- 每路 watchdog：有新增/失败才通知，无变化静默
- **数据汇总（原 04:00 职责并入）**：索引链路无异常时，追加 `gongzuo_assembly.py`（→ @数据汇总 7 份汇编 md）
  和 `gen_skill_list.py`（→ 06-后装技能清单，每日全量重生成）
- **并发互斥锁**（2026-08-23 新增）：worklife 写 PID 锁 `D:\@kaifa\gongzuo-data\worklife_daily.lock`，
  死锁残留自动清理；04:00 已删除无并发方，锁仍保留防手动重入
- 脚本本体在**技能 scripts/**（`worklife_daily_index.py` + `lib_to_viking.py`，cron 入口是 hermes/scripts 的同名薄副本），改逻辑改技能目录再同步副本
- 健康检查：每周一 06:30 cron `gongzuo_health_check.py`（job 263e2e784a0b）——库存在+文件数阈值+FTS一致性+Viking可达，全部正常静默，异常才通知

## 模式5: Viking 知识库导出（lib_to_viking.py，2026-08-23）

把任意 gongzuo 索引库的内容导出到 OpenViking 记忆库（127.0.0.1:1933），供语义检索/跨会话召回。**参数化通用版**，替代旧的 bdsync/xhp 专用脚本：

```bash
# 导出一个库（首次全量，之后增量只传新增/变更，指纹=mtime+size）
python scripts/lib_to_viking.py --db "D:\@kaifa\gongzuo-data\file_index.db" \
    --root "D:\BaiduSyncdisk" \
    --base "viking://user/default/resources/bdsync" \
    --name BaiduSyncdisk
# 状态文件: <name>_to_viking.state.json（默认 db 同目录，--state-dir 可覆盖）；有新增打印摘要、无新增静默（watchdog 语义）
```

- **增量只拉变更集（2026-08-23 优化）**：先查轻量列算变更集（指纹=mtime|size 对比 state），无变更直接静默返回（0.5s 级）；
  有变更才按需 SELECT 正文，不再全表拉 30MB content_text
- 幂等：同 URI 重复添加=更新；409 busy 自动重试（3 次×4s，队列慢时快速跳过，失败文件下次增量自动补）
- **坑：URI 冲突（2026-08-23 修复）**：同目录同名不同扩展名（1.docx/1.mp4/1.txt）splitext 后 base 相同 → 映射同一 URI，后到者 ResourceBusyError 永久失败。已修复：预计算冲突 base，冲突文件改用完整文件名（1.docx → 1.docx.md）
- **坑：viking 处理故障排查（2026-08-23 实战）**：① add 全 409/处理卡住先查 `proc log` 有无 embedding 500——llama-server 默认 batch=512，超长输入必挂，重启加 `-b 4096`；② server 重启必须 `cwd=D:\@kaifa`（workspace=./data 相对 cwd），否则落到 openviking\data 旧区，真数据在 `D:\@kaifa\data\viking`；③ `default/temp/default/` 批次数是**上传留存副本只增不减**，不是待处理队列——判断处理进度看正式区 `user/default/resources/<区>/*.md` 数量；④ server 被杀后残留 `.path.ovlock` 锁（全库 5925 个）会让同 URI 再 add 报 ResourceBusyError，重启即恢复（锁是内存态）；⑤ temp 孤儿（add 失败的 upload 残留）可备份后直接清，不影响 state 指纹
- **分批导出（2026-08-23 新增）**：viking 消化 ~13-15s/资源，单次全量提交后半段必 409 白等。`--max-adds N` 限量（本轮成功 N 个即停，剩余留待下轮）。批量编排脚本 `viking_drain.py`（技能 scripts/）：等队列(temp 批次数 ≤20)→XHP 补漏→BDSync 300/轮循环，自动排空积压；~5071 个 ≈ 19 小时
- 坑：**死锁残留**——导出进程被杀后 `{name}_to_viking.lock` 不清理，重跑报"已有导出实例在运行"；确认无 python 导出进程后直接删锁
- **坑：URI traversal（2026-08-23 实测）**：库中若有 root 外的文件（如 file_index.db 混入的 `D:\@佳康顺` 聊天记录），relpath 会以 `..` 开头 → viking 403 `Unsafe URI traversal`。已修复：root 外文件自动映射为 `盘符_绝对路径` 安全 URI（如 `d_@佳康顺/聊天记录/...`）
- 每文件渲染成 markdown（元数据+OCR文字+【画面】智谱描述）→ temp_upload → POST /resources
- 已索引库：`file_index.db`（BDSync 工作库 + @佳康顺 聊天记录）、`xhp_kb.db`（XHP-知识库）、`state.db`（Hermes 会话）
- 首次全量：9696 文件约 2 小时提交（viking 异步队列消化 ~15s/资源，队列积压时 409 快速跳过、增量续跑）；增量秒级

### 自动调用 / 秒问秒回（2026-08-23 起统一管理）

新开会话/QQ bot 问工作生活资料时，**无需手动索引，直接按序查**：
1. `python scripts/file_search.py "关键词"` — 本地 SQLite FTS5 秒级全文检索（含 OCR 文字+智谱画面描述），带 `MEDIA:` 路径 QQ 直发
2. `viking_search`（Hermes 内置工具）— OpenViking 语义检索，命中 `viking://user/default/resources/bdsync|/xhp_knowledge/...`
3. 每日 01:00 合并 cron 自动增量（新文件次日可查）
日常问答模式：直接 file_search 查库秒回，不要重新扫描、不要问用户找什么文件。

- watchdog 脚本：`C:\Users\xxx13\AppData\Local\hermes\scripts\gongzuo_daily_index.py`（**已停用**，职责并入 01:00 worklife；保留作手动工具）
- 汇编脚本：`C:\Users\xxx13\AppData\Local\hermes\scripts\gongzuo_assembly.py`（增量快照 `.gongzuo_assembly_state.json`，只收源目录直接文件，--init 首次部署标记现有文件）
- 06 生成脚本：`C:\Users\xxx13\AppData\Local\hermes\scripts\gen_skill_list.py`（每日全量重生成 06-后装技能清单：rglob 枚举 SKILL.md + .usage.json 使用次数，排除 .archive 归档区；2026-08-15 起纳入 cron，不再依赖手动生成）
- 7 份汇编 md（`D:\BaiduSyncdisk\@数据汇总` 目录，序号开头）：01-账号密码总看板 / 02-Api Key总览 / 03-个人数据总览 / 04-公司资质与证书 / 05-工作生活记录总览 / 06-后装技能清单(gen_skill_list.py 生成) / 07-开发技能提示词看板
- 行为：有新增/变更 → 输出摘要（含汇编结果）；06 每日生成单独一行（deliver=local 静默落库）；有失败 → 警报；无变化 → 静默
- 查看运行记录：`cronjob(action='list')`；改投递目标用 `cronjob(action='update', deliver='telegram')`

## 模式6: 状态查询

```bash
python scripts/file_search.py --stats        # 全文件库
python scripts/file_search.py --stats --full # + doc_type 分布 + 图片/视频待处理缺口
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

### 智谱 GLM-4V-Flash 场景描述（模式1.7，2026-08-17 上线）

云端免费视觉模型给图片/视频补"画面里是什么"——无需本地 llama-server，速度 ~1.2-1.4s/张（比 Qwen3-VL 本地快 20 倍）：

```bash
# 图片缺口模式(默认): 只处理无文字/未处理/超大跳过图片(2124张 ≈ 50分钟)
python scripts/doc_indexer.py --vision-desc-zhipu
# 视频场景描述(智谱): 头尾各1帧 →glm-4v-flash→[mm:ss]入库, ~3s/条
python scripts/doc_indexer.py --vision-video-zhipu
# 限量测试
python scripts/doc_indexer.py --vision-desc-zhipu --vision-limit 3
```

- **⚠ 重要坑（2026-08-23 实测）**：`--ocr-media`/`--vision-*`/`--ocr-video` 等模式**必须显式带 `--root`**，否则 main() 会先无条件 scan 默认 root（D:\BaiduSyncdisk）把无关文件写进库！每次调用都要 `--root "目标目录" --db 库路径`
- **视频抽帧策略（2026-08-23 用户要求）**：视频 OCR/场景描述一律**头尾各 1 帧**（2s 处 + 尾-2s 处），不再逐 2 秒扫全片——`_extract_head_tail_frames()` 实现，68 视频 136 帧约 3-4 分钟
- **视频帧缓存（2026-08-23 新增）**：抽帧结果按 (mtime,size,时长) 指纹缓存到 `D:\@kaifa\gongzuo-data\frames_cache\`，视频 OCR 与视频描述两个 pass 共享同一批帧，二次命中 ~0s（首次 1-3s/视频）
- 图片写入：content_text 追加 `【画面】 描述`（原 OCR 文字保留），doc_type=`media-vision-zhipu`，断点续传（doc_type 以 vision 开头跳过）
- 视频写入：`[mm:ss] 【画面】 描述` 逐帧入库，doc_type=`video-vision`，断点续传（doc_type 以 video-vision 开头跳过）
- **抽帧规则（2026-08-17 实测）**：>15s 视频抽 **4 帧**（片头2s + 中间1/3 + 中间2/3 + 片尾尾-2s，821帧/260视频≈31%增益）；≤15s 仅 2 帧（头尾）。重跑已识别视频需先 `UPDATE files SET doc_type=NULL WHERE lower(file_ext) IN ('.mp4','.mov') AND doc_type='video-vision'` 重置（旧描述先 ALTER 加临时列备份，跑完验证后 DROP）
- 依赖：`ZHIPU_API_KEY` 环境变量（.env / setx 均可），智谱 open.bigmodel.cn，模型 glm-4v-flash（免费）
- 与本地 Qwen3-VL（--vision-desc，26s/张）并存：缺口查询条件相同，先跑哪个就锁定 doc_type，另一个自动跳过已处理图
- 坑：64KB base64 上限内的 720px 缩图直接可用；>60MB 原图跳过（skipped_big）
- **坑：智谱敏感审核（code 1301）**：极小图（如 118x210）会被误判"敏感内容"拦截报 HTTP 400。兜底：用本地 Qwen3-VL 识别这些图（`--vision-desc`，本地无审核限制）
- **坑：超长图（详情页长图）**：`thumbnail((720,720))` 会把 1440×72451 长图压成 14×720 细条无法识别。正解=分段裁剪：PIL `Image.MAX_IMAGE_PIXELS=None` 解除解压炸弹限制 → 按每段 5000px 高切段 → 每段 `thumbnail((720,720))` → 逐段智谱识别（~2s/段）→ 拼接 `[段1]...| [段N]...` 入库（71MB 长图 15 段 32s 实测）。描述 prompt 强调"产品详情页一部分+文字信息"，可提取卖点/参数/证书文字

### Qwen3-VL 场景描述集成（模式1.6，2026-08-14 上线）

本地视觉模型给图片补"画面里是什么"——Umi-OCR 只出文字，Qwen3-VL 出主体/颜色/场景描述，进 FTS 可搜（如"红色电动轮椅"、"摄影棚"、"展厅"）：

```bash
# 缺口模式(默认): 只处理无文字/未处理/超大跳过图片(~678张,断点续传)
python scripts/doc_indexer.py --vision-desc
# 全部图片(6382张 ≈ 46小时,慎用;建议拆夜跑或先跑缺口)
python scripts/doc_indexer.py --vision-desc-all
# 限量测试
python scripts/doc_indexer.py --vision-desc --vision-limit 3
# 视频场景描述(缺口模式,本地): 抽帧规则同智谱(>15s 4帧/≤15s 2帧)→Qwen3-VL→[mm:ss]入库, ~24s/条
python scripts/doc_indexer.py --vision-video [--vision-limit N]
```

- 写入：content_text 追加 `【画面】 描述`（原 OCR 文字保留），doc_type=`media-vision`(缺口)/`media-vision-full`(全量)，断点续传（doc_type 以 vision 开头跳过）
- 前置：llama-server 必须跑在 127.0.0.1:1234（`Startup\Hermes_VisionServer.cmd` 开机自启，或 `D:\@kaifa\models` 下手动起），未启动会直接报错提示命令
- 速度：720px 缩图 + 80 tokens ≈ **26s/张**（20-35s 区间）→ 缺口 678 张 ≈ 5 小时（通宵档）；全量 6382 张 ≈ 46 小时
- 分辨率基准（2026-08-14 实测单图 1024x1536）：原图不压缩 136s（慢4-5倍无收益）；**720px≈32s 细节最稳**（型号/配色/小字）；600px≈35s 有轻微歧义（手动→电动）；**500px≈24s 主体级下限**（五大模块/主体/场景全对，配色小偏差）。索引用途 720px 为默认甜点
- 服务器被杀后不会自愈：`--vision-desc` 报"视觉服务器未启动"时，跑 `Startup\Hermes_VisionServer.cmd` 或手动起 llama-server
- 坑（2026-08-14 实测修复）：①HEIC 必须先 `register_heif_opener()` 否则 cannot identify（已写进 vision_describe_image）②.gif 不在 OCR_EXTS 会漏（已加入 PIL_CONVERT_EXTS）③视觉上限 60MB（>60MB 长图跳过）④服务器冷启动约 15s，索引器会等 60s（_vision_up wait=True）
- 模型：Qwen3-VL-8B Q4_K_M + mmproj（unsloth 文本 + 官方投影器配对），细节见 llama-cpp 技能

### jieba 分词（搜索端）
file_search.py 用 jieba.cut_for_search 拆查询词，不落库，无索引期开销。

### 依赖
- 全文件索引: fitz(PyMuPDF), python-docx, openpyxl, python-pptx, xlrd, olefile
- 搜索: jieba（本机已装）
- 图片OCR: Umi-OCR exe(D:\@kaifa\Umi-OCR\,skill: umi-ocr), Pillow, pillow-heif(HEIC)
- 图片/视频场景描述: 智谱 glm-4v-flash 云端(免费,key=ZHIPU_API_KEY) 或 Qwen3-VL-8B llama-server(127.0.0.1:1234,模型在 D:\@kaifa\models\,自启 Startup\Hermes_VisionServer.cmd)
- 视频OCR: ffmpeg/ffprobe (C:\Users\xxx13\ffmpeg\ffmpeg-8.1.1-essentials_build\bin)
- 系统: antiword（git-bash 自带 /mingw64/bin）

## 已知坑

- **SQLite 多线程**：doc_indexer 用每线程独立连接（threading.local）+ busy_timeout=30000，不要改回共享连接
- **FTS5 trigram 只认 ≥3 字符**：2字中文词自动走 LIKE，别惊讶命中方式不同
- 超大文件(>100万字符)正文截断，超限部分搜不到
- 老 .doc（WPS/加密/损坏）部分失败，error 标记 `PERMANENT|...` 不再重试
- doc_indexer.py 的 log 写在库同目录 doc_indexer.log（RotatingFileHandler 5MB×3 轮转）
- **弹窗坑**：pythonw 下 spawn 控制台程序（ffmpeg/ffprobe/7z/antiword）必须带 `creationflags=0x08000000`（CREATE_NO_WINDOW），否则每抽一帧弹一个黑窗（2026-08-14 实测：视频 vision 任务每 43s 闪一次窗）

## 当前已索引库

| 库 | 内容 | 状态 |
|----|------|------|
| D:\@kaifa\gongzuo-data\file_index.db | 全文件正文+媒体元数据+图片OCR文字+画面描述 | 2026-08-17 实测 10,684 文件（图片 10,059 全可搜 + 视频 260 全画面描述 + 文档/音频 ~365），失败 0，库 194 MB |
| D:\@kaifa\gongzuo-data\xhp_kb.db | XHP-知识库 全文件正文+OCR+画面描述 | 2026-08-23 实测 1,111 文件 |

## 参考文档

- `references/integration-plan.md` — 整合方案全文（开源对比/架构/覆盖矩阵/限制/后续增强）
- `references/retry-failed-files.md` — 媒体索引失败重试模式
