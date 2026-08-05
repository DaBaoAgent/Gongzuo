# gongzuo 全文件秒级检索整合方案

> 2026-08-05 · 目标：D:\BaiduSyncdisk（165GB / 45,799 文件）**所有文件秒级精准定位，含文件内容全文搜索（视频/图片除外）**

## 一、开源方案调研结论

| 方案 | 类型 | 中文支持 | Windows | 能融入 gongzuo? | 结论 |
|------|------|----------|---------|-----------------|------|
| **Everything** (voidtools) | 文件名索引 | 好(名称) | ✅ | ❌ 独立 GUI，无 Python API | 只搜文件名，不含正文 |
| **Recoll** (Xapian) | 全文索引桌面工具 | 一般(需配 jieba) | ✅(较折腾) | ❌ 独立 GUI+外部提取器链 | 重型，中文调优成本高 |
| **DocFetcher** (Java+Tika) | 全文索引 | 一般 | ✅ | ❌ 独立 GUI，按文件夹建索引 | 无统一库、无 API |
| **AnyTXT Searcher** | 全文索引 | 好 | ✅ | ❌ 闭源 GUI | 好用但无法集成/自动化 |
| **ripgrep** | 即时扫描 | 好 | ✅ | ⚠️ 每次全盘扫 165GB，非秒级 | 适合小目录 |
| **meilisearch / typesense** | 全文检索服务 | 好 | ✅ | ⚠️ 需常驻服务+内存 | 笔记本杀器，杀鸡用牛刀 |
| **SQLite FTS5 自建**（本项目） | 嵌入式全文索引 | **好(trigram 精准子串)** | ✅ | ✅ **与 gongzuo 同架构直接合并** | ✅ 选它 |

**选型理由（自建 FTS5 扩展，不引入新系统）：**
1. **秒级**：FTS5 倒排索引，4.6 万文件查询 <50ms；trigram 分词器对中文做 3 字子串索引，`MATCH '"电动轮椅"'` 精准命中，无单字拆分噪音
2. **离线隐私**：银行流水/提成表/账号密码等敏感资料不出本机
3. **零依赖零服务**：笔记本随时跑，无常驻进程、无内存占用
4. **与 gongzuo 同构**：同一个 SQLite FTS5 家族，未来可合并为一个库、一个搜索入口
5. **增量**：path+mtime+size 比对，重跑只处理新增/变更文件（秒级）

## 二、架构

```
D:\BaiduSyncdisk (165GB / 45,799 文件)
        │
        │ doc_indexer.py（8线程，增量扫描）
        │   ├─ 纯文本/代码/配置… → 编码探测(UTF-8→GBK→Big5) 直接入库
        │   ├─ docx/xlsx/pptx → python-docx / openpyxl / python-pptx
        │   ├─ pdf → PyMuPDF(fitz) 逐页抽取
        │   ├─ doc → antiword → olefile(UTF-16流) 兜底
        │   ├─ xls → xlrd；epub/xmind/odt → zip 解包抽文本
        │   ├─ 图片/视频/音频 → 只存元数据(文件名/路径可搜)
        │   └─ 噪音排除：node_modules/.git/.cache/.codex_* 等 AI 工作目录、
        │                字体(.ttf)/资源映射(.map)/二进制(.exe) 全部跳过
        ▼
D:\zhipuai_test\file_index.db
        ├─ files 表（46K 行：路径/名称/大小/时间/预览/全文/类型/错误）
        └─ files_search 虚表（FTS5 trigram 倒排索引）
        ▼
        │ file_search.py（jieba 分词 → trigram MATCH + LIKE 兜底）
        ▼
   秒级结果：编号列表 + 命中预览高亮 + [--copy 复制路径] [--open 打开] [--dir 定位] [--json]
```

## 三、索引覆盖矩阵

| 类别 | 格式 | 提取方式 | 可搜范围 |
|------|------|----------|----------|
| 文本/代码 | txt md json yaml csv html js py ts… 60+ 种 | 编码探测直读 | 全文 |
| Office 新 | docx xlsx pptx | python-docx / openpyxl / python-pptx | 全文(含表格) |
| Office 旧 | doc xls | antiword / xlrd；doc 失败走 olefile UTF-16 流 | 全文 |
| PDF | pdf | PyMuPDF 逐页(上限500页) | 全文(扫描件需OCR,见限制) |
| 思维导图 | xmind | zip 解包读 content.json 标题 | 节点文字 |
| 电子书 | epub | zip 解包剥 HTML | 全文 |
| 邮件 | eml | email 库 | 正文 |
| 存档 | zip 7z rar | 内文件名清单(7z 需安装 7-Zip) | 包内文件名 |
| 图片/视频/音频 | jpg png mp4 mov mp3… | **元数据 only**（名称/路径可搜；AI 视觉描述需重跑 media_indexer.py 回填） | 文件名/路径 |
| 噪音 | node_modules .git .cache .codex_* ttf map exe… | **跳过不索引** | — |

## 四、搜索策略（精准优先，召回兜底）

1. **jieba 分词** 拆查询 → ≥3 字词组走 **FTS5 trigram**：`MATCH '"电动轮椅"'`（3 字子串精准命中，全库 <50ms）
2. **<3 字词**（如"轮椅"）走 LIKE 兜底：文件名+目录+预览+正文前 2 万字，16K 行 <0.5s
3. 混合查询（"提成 规则"）→ trigram AND + LIKE 短词
4. FTS 零命中 → 自动降级纯 LIKE 全子串搜索，保证召回
5. 排序：文件名命中优先 → FTS rank / 修改时间

## 五、使用

```bash
# 首次/增量全量索引（后台跑）
python doc_indexer.py --root "D:\BaiduSyncdisk" --db "D:\zhipuai_test\file_index.db"

# 搜索（秒级）
python file_search.py "电动轮椅"                    # 全文+文件名
python file_search.py "接待话术" --ext doc,docx     # 指定类型
python file_search.py "提成" --folder "12 抖店"     # 指定目录
python file_search.py "展会" --type media           # 只搜媒体
python file_search.py "报表" --copy 3               # 复制第3个结果路径
python file_search.py "关键词" --open 1             # 直接打开
python file_search.py "关键词" --dir 2              # 资源管理器定位
python file_search.py --stats                       # 库状态
```

**建议自动化**（Windows 任务计划程序，每周增量）：
```
schtasks /create /tn "gongzuo索引" /tr "python D:\...\doc_indexer.py --root D:\BaiduSyncdisk" /sc weekly /d SUN /st 03:00
```

## 六、已知限制（如实声明）

- **扫描型 PDF**（图片扫描件）无文字层，未做 OCR → 搜不到。需要时接 PaddleOCR（中文准，CPU 可跑）
- 单文件正文上限 100 万字符（防日志/超大 PDF 撑爆索引），超限部分不搜
- trigram 索引体积约为正文 3 倍（46K 文件 ≈ 库 200-400MB，可接受）
- 2 字中文词走 LIKE（速度仍 <0.5s），3 字以上走精确索引
- 老 .doc（WPS/加密/损坏）部分提取失败，已标记 error 可重试
- 原媒体 AI 描述库（D:\zhipuai_test\media_index.db）已丢失，图片/视频目前只按文件名/路径搜；要内容描述检索需重跑 media_indexer.py（约 8355 个媒体文件，免费模型限速约 2 小时）

## 七、后续可选增强

1. **OCR 层**：PaddleOCR 对扫描 PDF/截图做文字识别入库（--ocr 开关）
2. **媒体描述回填**：重跑 media_indexer.py 后 `--merge-media` 一键合库
3. **统一查询入口**：两个库合并成一个 search_all.py，媒体+文档一把搜
4. **Everything 插件式联动**：文件名级即时搜索交给 Everything（可选）
