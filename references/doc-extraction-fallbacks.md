# 文档提取兜底技巧库（D:\BaiduSyncdisk 45,799 文件全量索引实战）

> 2026-08-05 实测。doc_indexer.py 已内置全部兜底；本文件记录每种格式的**失败签名与修复模式**，
> 排查"某文件搜不到/被标失败"时直接对照。

## 老版 .doc（OLE2，魔数 d0cf11e0a1b11ae1）

降级链：antiword → 头嗅探(RTF/HTML 伪装) → olefile WordDocument 流。

1. **antiword**（git-bash 自带 `/mingw64/bin/antiword`，必须 `-m UTF-8.txt`）能解大部分真 .doc。
   失败签名：
   - `text stream too small to handle` = 空文档/纯表头，**不代表文件坏**
   - `is not a Word Document` = RTF/HTML 伪装成 .doc 扩展名，或 WPS 变体
2. **头嗅探**（读 1KB）：`{\rtf` 开头 → rtf_to_text（剥控制字 `\xxx`、花括号、`\'xx` 十六进制转义）；
   `<html`/`<!doc`/`mime` 开头 → 剥 script/style 后当 HTML 文本
3. **olefile WordDocument 流兜底**（中文 Word 文本存 UTF-16LE）：
   - **坑**：整个流按 UTF-16LE 解码后做"可读字符占比"检查会**误判为乱码**——二进制结构混在
     文本里，CJK 占比常 <50%，全文 `_is_garbage` 把好文档毙了（实测 10KB 提成规则.doc 中招）
   - **正解**：正则抽可读段再拼接：
     `[\u4e00-\u9fff\u0020-\u007e\u3000-\u303f\uff00-\uffef\u3040-\u30ff\uac00-\ud7af]{3,}`
     对每个 run 单独过 `_is_garbage`（可读字符占比 <0.5 丢弃），join 换行
   - 纯 ASCII 老文档兜底：非打印字节替换为空格后按 ascii 解，长度>20 才收
4. 兜底全失败 → 抛 ValueError，入库 error（可重试）

## WPS 生成的不规范 xlsx

- 失败签名：`openpyxl ... TypeError: expected <class 'openpyxl.styles.fills.Fill'>`
  —— WPS 写入的样式对象类型不合 openpyxl 预期，`load_workbook` 阶段直接炸
- 这是**数据可读但样式坏**的软失败（不是加密/损坏），别放弃内容
- 正解：zipfile 解包——`xl/sharedStrings.xml` 建字符串表（`<t>` 标签内容 + html.unescape），
  逐 `xl/worksheets/sheetN.xml` 按 `<row>` → `<c>` → 取 `<t>`（inline 文本）/ `<v>`（数值，
  `t="s"` 时用字符串表索引转换）提取；cell 间空格 join
- 实测 5 个报表 xlsx 全中招，兜底后全部可搜

## 伪装成 txt 的 zip

- 失败签名：文本编码探测全失败 → 误标 `PERMANENT|binary`（实测 811KB 的
  `新建 文本文档.txt` 实际是素材压缩包，内含几十个口播文案文件）
- 正解：文本分支**先嗅 `PK\x03\x04` 魔数** → zipfile 列包内清单入库（doc_type=zip）
- 同理 docx/xlsx/epub 全是 zip，改名文件都能用魔数识别救回

## 文本编码级联（decode_text）

`BOM(utf-8-sig / utf-16) → utf-8 strict → gbk → big5 → latin-1(容忍)`
二进制检测：前 8KB 中控制字符（<9 或 13<x<32）占比 >10% → 判二进制；**空样本跳过检测**。
GBK 在中文 Windows 环境命中率很高，Big5 是台港文件兜底。

## 噪音处理（不进库、不计失败）

- **目录级**：node_modules/.git/.cache/.codex_*/.venv/dist/build + 所有点开头目录。
  实测 `.codex_tmp`/`.codex_work`/`.cache/codex-runtimes` 一个目录就藏 2.3 万个 js/map
  （AI 工作目录噪音）——不排除会淹没真实文档（45,799 → 排除后 12,042）
- **文件名级**：thumbs.db/ehthumbs.db/desktop.ini/.ds_store——Windows 缩略图缓存，
  **按名跳过**（skip|noise-name）而非记 error，否则 60 个 Thumbs.db 全算"失败"。
  **`~$*.docx/xlsx/pptx`（Office/WPS 锁文件）同样按名跳过**——用户开着文档时跑索引必生成，
  docx 分支会报 `ERR|PackageNotFoundError|Package not found at '...~$仓库登记.docx'` 记成失败；
  `~$` 前缀 + 无真实 zip 结构，按名跳过即正解（2026-08-06 实测 `~$仓库登记.docx` 进 error 表）
- **扩展名级**：字体(.ttf/.otf/.ttc/.bcmap)/sourcemap(.map)/二进制(.exe/.dll/.wasm)等

## SQLite 多线程索引

- 失败签名：`sqlite3.InterfaceError: bad parameter or other API misuse`
  （8 个 worker 共享一个连接，即使 `check_same_thread=False` 也会炸）
- 正解：`threading.local` 每线程独立连接 + `PRAGMA busy_timeout=30000` + WAL；
  主线程连接只管建 schema 和收尾（merge/commit）
- 实测：8 线程全量 12,042 文件 ~5 秒；增量重跑 1.2 万文件仅 1 秒

## 搜索策略要点

- FTS5 **trigram** 对中文做 3 字子串倒排：`MATCH '"电动轮椅"'` 精准命中，无 unicode61
  单字拆分噪音；但 **2 字查询 trigram 直接返回 0** → jieba 分词后 ≥3 字词走 MATCH、
  <3 字词走 LIKE（文件名+目录+预览+正文前 2 万字），零命中自动降级纯 LIKE
- 首次查询慢是 jieba 词典加载（~0.9s），稳态 1-40ms
- 排序：`CASE WHEN file_name LIKE ? THEN 0 ELSE 1 END` 文件名命中优先 → FTS rank / 修改时间
