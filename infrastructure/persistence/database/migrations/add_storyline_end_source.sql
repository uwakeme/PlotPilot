-- G-03：故事线预估收尾章的来源标注（补完「落到产物」这半）
--
-- 背景：`estimated_chapter_end` 原先只有一个 INTEGER 列，代码与模型给的值
-- 在这列上完全同形 —— 落库后无法分辨「第 50 章是模型判断的，还是代码兜底的」。
-- 上一轮把它降级为「只进日志 + docstring 说明未落库」，本迁移补上缺的落库。
--
-- 列定义（ALTER ADD COLUMN，SQLite 允许，且对既有行会给 DEFAULT）：
--   estimated_chapter_end_source TEXT NOT NULL DEFAULT ''
--   '' = 旧数据未标注（**不等于** 'ai'）—— 迁移不改写历史行的语义，
--        凭空回填 'ai' 就是在伪造「这是模型的判断」
--   'ai'       模型给了收尾章
--   'fallback' 模型没给，代码兜底（+12 章等）
--
-- 迁移执行器是逐语句执行且中途不 commit，所以这里**不写** PRAGMA foreign_keys：
-- 见 add_nullable_governance_measurements.sql 顶部的完整理由。

ALTER TABLE storylines ADD COLUMN estimated_chapter_end_source TEXT NOT NULL DEFAULT '';
ALTER TABLE storylines ADD COLUMN estimated_chapter_end_note TEXT NOT NULL DEFAULT '';
