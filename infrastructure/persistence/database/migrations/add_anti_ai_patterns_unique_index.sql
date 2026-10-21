-- F-06 复审 B1/§3.6a：补上 (novel_id, keyword) 唯一约束。
--
-- 背景：建表语句（add_anti_ai_learned_patterns.sql）只有
--   `id TEXT PRIMARY KEY`（id 每次都是新 uuid）
-- 而仓储代码的注释曾写「表的唯一键在 (novel_id, keyword) 上」——**那是假的**，
-- 实际没有任何唯一约束 ⇒ 同一关键词可能攒出多行
-- ⇒ 扫描器会把同一条规则算成多次命中。
--
-- 先去重（同一 (novel_id, keyword) 只留**最早插入**的那行），再建唯一索引。
-- 用 rowid 而不是 MIN(id)：id 是 uuid，字典序与插入先后无关；
-- rowid 随插入递增，MIN(rowid) 才是「最早的那条」。
--
-- 迁移执行器逐语句执行且中途不 commit，所以这里**不写** PRAGMA foreign_keys。
DELETE FROM anti_ai_learned_patterns
WHERE rowid NOT IN (
    SELECT MIN(rowid) FROM anti_ai_learned_patterns GROUP BY novel_id, keyword
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_anti_ai_learned_patterns_unique
    ON anti_ai_learned_patterns(novel_id, keyword);
