-- 全书终审报告表：存储分幕精审结果与汇总报告
CREATE TABLE IF NOT EXISTS book_review_reports (
    id TEXT PRIMARY KEY,                       -- 确定性 id：'{novel_id}:act:{n}' 或 '{novel_id}:final'
    novel_id TEXT NOT NULL,
    scope TEXT NOT NULL,                       -- 'act'（单幕精审） / 'final'（汇总报告）
    act_number INTEGER,                        -- scope='act' 时的幕号，final 为 NULL
    status TEXT NOT NULL DEFAULT 'completed',  -- completed / failed
    payload TEXT NOT NULL DEFAULT '{}',        -- 报告 JSON
    finding_count INTEGER NOT NULL DEFAULT 0,  -- findings 数量（final 为 top_issues 数）
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_book_review_novel ON book_review_reports(novel_id, scope, act_number);
CREATE INDEX IF NOT EXISTS idx_book_review_created ON book_review_reports(novel_id, created_at DESC);
