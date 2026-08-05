# 失败文件重试模式

当索引完成后有大量失败文件时的处理策略。

## 诊断

```python
import sqlite3
conn = sqlite3.connect("media_index.db")
rows = conn.execute("""
    SELECT error, COUNT(*) as cnt
    FROM media_files WHERE error IS NOT NULL
    GROUP BY error ORDER BY cnt DESC
""").fetchall()
for r in rows:
    print(f"  [{r[1]:4d}] {r[0][:120]}")
```

## 常见失败分类及处理

| 错误类型 | 可修复? | 处理方式 |
|----------|:------:|----------|
| `TypeError: expected str instance, dict found` | ✅ | JSON 解析 bug — 修复 `parse_json_response` |
| `ProgrammingError: type 'list' is not supported` | ✅ | 同上，details 为 list 时未展平 |
| `UnidentifiedImageError` | ❌ | 图片文件损坏，标记 `PERMANENT\|BAD_IMAGE` |
| `MODERATION` / 内容审查 | ❌ | 标记 `PERMANENT\|MOD` |
| `rate limit` / `速率限制` | ⚠️ | 等待或换付费模型 |

## 重试脚本要点

1. **查询待重试**: filter `error IS NOT NULL AND error NOT LIKE 'PERMANENT|%'`
2. **JSON 解析修复**: 使用递归 `safe_str()` 函数展平任意嵌套的 dict/list
3. **速率限制**: 请求间隔 ≥12s，失败退避 30s → 60s → 90s → 120s
4. **永久失败标记**: `UPDATE media_files SET error='PERMANENT|reason' WHERE id=?`
5. **FTS 同步**: 成功修复后 `DELETE + INSERT` 更新 media_search 表
6. **不可修复**: 损坏图片(PIL 无法打开)、内容审查拒绝

## 安全重试间隔

免费模型 `glm-4.6v-flash`:
- 成功请求间隔: 12s
- 速率限制退避: 30s × attempt
- 网络错误退避: 10s × attempt
- 批量上限: ~50 文件/批次（超过换 FlashX）

## 参考实现

`D:\zhipuai_test\retry_failed.py` — 本次会话中使用的完整重试脚本。
