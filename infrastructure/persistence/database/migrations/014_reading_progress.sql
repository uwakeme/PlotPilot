-- 阅读进度：每本书一条记录，仅记「读到哪一章」
CREATE TABLE IF NOT EXISTS reading_progress (
    novel_id TEXT PRIMARY KEY,
    last_chapter_number INTEGER NOT NULL DEFAULT 1,
    last_read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- 书库默认排序「最近阅读」用
CREATE INDEX IF NOT EXISTS idx_reading_progress_last_read
  ON reading_progress(last_read_at DESC);