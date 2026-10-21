-- F-06：Anti-AI 已学模式（规则库）落库 + 版本化。
--
-- 背景：`anti_ai_learning` 以前把「学到的」模式只放在**内存 dict**里
-- （挂在全局单例上）⇒ 重启即失；而 `get_approved_patterns()` 全仓无调用者
-- ⇒ 从来没有回灌进扫描器。登记表 F-06 的不变量是**规则库版本化**。
--
-- 本迁移只负责库，不参与判断：
--   * keyword   —— **字面关键词**（不是正则）⇒ 无正则注入、无灾难性回溯。
--                   扫描时用 re.escape 转成字面量模式。
--   * severity  —— info / warning / critical（白名单在代码里校验）。
--   * status    —— approved（进扫描器）/ rejected（人工驳回，保留行以便版本不回退）。
--   * rule_version —— 规则库版本号：每新增一条模式 +1；**驳回不回退**
--                   （内容变少但版本仍可追溯「这批规则是第几版」）。
--   * source    —— ai（模型归纳）/ manual（人工添加）。
--
-- 迁移执行器是逐语句执行且中途不 commit，所以这里**不写** PRAGMA foreign_keys。
CREATE TABLE IF NOT EXISTS anti_ai_learned_patterns (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'info',
    category TEXT NOT NULL DEFAULT '本书特有',
    reason TEXT NOT NULL DEFAULT '',
    hint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'approved',
    source TEXT NOT NULL DEFAULT 'ai',
    rule_version INTEGER NOT NULL DEFAULT 1,
    first_seen_chapter INTEGER NOT NULL DEFAULT 0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_anti_ai_learned_patterns_novel
    ON anti_ai_learned_patterns(novel_id, status);
