r"""
中文分词增强 — jieba 分词重建 FTS5 索引
======================================
用法: python enhance_segmentation.py --db "D:\path\to\media_index.db"
"""
import argparse
import sqlite3
import sys


def enhance(db_path: str):
    import jieba  # lazy import to avoid startup cost if not needed

    conn = sqlite3.connect(db_path)

    # Add segmented column if missing
    try:
        conn.execute("ALTER TABLE media_files ADD COLUMN segmented_text TEXT DEFAULT ''")
        conn.commit()
        print("Added segmented_text column")
    except sqlite3.OperationalError:
        print("segmented_text column already exists")

    # Process all records
    total = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
    print(f"Total records: {total}")

    done = 0
    batch = []

    rows = conn.execute("""
        SELECT id, file_name, description, tags, folder_path
        FROM media_files
    """).fetchall()

    for row in rows:
        rid, fname, desc, tags, folder = row
        text = f"{fname} {desc or ''} {tags or ''} {folder or ''}"
        seg = " ".join(jieba.cut(text))
        batch.append((seg, rid))
        done += 1

        if len(batch) >= 100:
            conn.executemany("UPDATE media_files SET segmented_text=? WHERE id=?", batch)
            conn.commit()
            batch = []
            if done % 500 == 0:
                print(f"  Progress: {done}/{total} ({done*100//total}%)")

    if batch:
        conn.executemany("UPDATE media_files SET segmented_text=? WHERE id=?", batch)
        conn.commit()

    print(f"Segmentation done: {done} records")

    # Rebuild FTS with segmented content
    print("Rebuilding FTS5 index with segmented content...")
    conn.execute("DROP TABLE IF EXISTS media_search")
    conn.execute("""
        CREATE VIRTUAL TABLE media_search USING fts5(
            file_path, file_name, description, tags, folder_path, segmented_text,
            content=media_files, content_rowid=id
        )
    """)
    conn.execute("""
        INSERT INTO media_search(rowid, file_path, file_name, description, tags, folder_path, segmented_text)
        SELECT id, file_path, file_name, description, tags, folder_path, segmented_text
        FROM media_files
    """)
    conn.commit()

    # Re-create triggers
    conn.execute("DROP TRIGGER IF EXISTS media_ai")
    conn.execute("DROP TRIGGER IF EXISTS media_ad")
    conn.execute("""
        CREATE TRIGGER media_ai AFTER INSERT ON media_files BEGIN
            INSERT INTO media_search(rowid, file_path, file_name, description, tags, folder_path, segmented_text)
            VALUES (new.id, new.file_path, new.file_name, new.description, new.tags, new.folder_path, new.segmented_text);
        END
    """)
    conn.execute("""
        CREATE TRIGGER media_ad AFTER DELETE ON media_files BEGIN
            INSERT INTO media_search(media_search, rowid, file_path, file_name, description, tags, folder_path, segmented_text)
            VALUES ('delete', old.id, old.file_path, old.file_name, old.description, old.tags, old.folder_path, old.segmented_text);
        END
    """)
    conn.commit()

    print("FTS5 index rebuilt with segmented content!")

    # Quick test
    for q in ["电动轮椅", "产品", "截图", "风景"]:
        rows = conn.execute("""
            SELECT m.file_name FROM media_files m
            JOIN media_search s ON s.rowid = m.id
            WHERE media_search MATCH ?
            ORDER BY rank LIMIT 2
        """, (q,)).fetchall()
        print(f"  '{q}' -> {len(rows)} results")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="jieba 分词增强 FTS5 索引")
    parser.add_argument("--db", required=True, help="数据库路径")
    args = parser.parse_args()
    enhance(args.db)
