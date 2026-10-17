-- I-03 / I-04 诚实化：治理报告的两个字段必须能表达「测不出来 / 评估不了」。
--
-- promise_hit_rate 与 should_pause_autopilot 此前都是 NOT NULL DEFAULT 0，
-- 于是「本次未测量」与「评估过、结论是零命中 / 不暂停」在存储层被压成同一个值。
-- 治理层已改为写 NULL（None），这里把两列放开 NOT NULL，否则写入侧会直接报
-- NOT NULL constraint failed。
--
-- SQLite 不支持直接修改已有列的约束，走标准的重建表流程。
-- 文件名以 add_nullable_ 开头，保证字典序落在 add_narrative_governance.sql 之后
-- （建表迁移先跑，本迁移才有源表可搬）。
--
-- 存量行按原值搬迁，不做回溯改写：历史上写入的 0.0 无法区分「真零命中」
-- 与「当年没测出来」，凭空改写等于伪造历史。
--
-- 这里刻意不写 PRAGMA foreign_keys = OFF / = ON：
-- 1. 全仓没有任何外键引用 governance_reports，重建表不需要关外键强制；
-- 2. migration_runner 是逐语句执行、中途不 commit 的，INSERT 之后事务已经开着，
--    而 SQLite 规定 PRAGMA foreign_keys 在事务内是空操作——于是开头的 OFF 会生效、
--    结尾的 ON 永远落不了地，连接的外键强制被留在关闭状态。
-- 保持迁移是纯中性 DDL：不引入本仓库其它迁移都没有的模式，也不留连接级副作用。

CREATE TABLE IF NOT EXISTS governance_reports_nullable (
    report_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    severity TEXT NOT NULL,
    promise_hit_rate REAL DEFAULT 0,
    issues_json TEXT NOT NULL DEFAULT '[]',
    budget_patch_json TEXT NOT NULL DEFAULT '{}',
    should_pause_autopilot INTEGER DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR REPLACE INTO governance_reports_nullable (
    report_id, novel_id, chapter_number, severity, promise_hit_rate,
    issues_json, budget_patch_json, should_pause_autopilot,
    review_status, created_at
)
SELECT
    report_id, novel_id, chapter_number, severity, promise_hit_rate,
    issues_json, budget_patch_json, should_pause_autopilot,
    review_status, created_at
FROM governance_reports;

DROP TABLE governance_reports;

ALTER TABLE governance_reports_nullable RENAME TO governance_reports;

CREATE INDEX IF NOT EXISTS idx_governance_reports_novel_chapter
    ON governance_reports(novel_id, chapter_number);
