-- H-06 子项 B：宏观诊断的注入经**提案确认**（诊断结果加「注入决定」列）
--
-- 背景
-- ----
-- 宏观诊断跑完会把「系统叙事校准」指令写进 macro_diagnosis_results.context_patch，
-- 由 context_budget_allocator._get_diagnosis_breakpoints 读出来
-- **静默注入**生成上下文头部 ⇒ 诊断结论直接改写后续章节怎么写，
-- 作者既看不到、也没机会拒绝。
--
-- 为什么要**新列**而不是复用 resolved
-- ------------------------------------
-- resolved 的既有语义是「已处理 ⇒ 停注入」（作者说这条我处理完了），
-- 与「批准注入」方向**相反** ⇒ 复用会让两件事共用一个布尔，语义打架
-- （这正是登记表点出的「resolved 语义方向相反」）。
-- ⇒ 新增 injection_decision：pending（待确认）/ approved（批准 ⇒ 注入）/
--   rejected（否决 ⇒ 不注入）；decided_at / decided_by 记决定人与时间。
--
-- 默认值 'pending'：新诊断一律待确认。注入侧是否「必须等批准」由
-- config/performance.yaml 的 macro_diagnosis_injection.enabled 控制
-- （默认关闭 ⇒ 行为与改造前逐位一致）。
--
-- ⚠ **文件名决定执行顺序**（迁移 runner 按文件名字母序跑）：`NNN_` 前缀的排在
--   `add_*` 之前 ⇒ 给 `add_macro_diagnosis_results.sql` 建的表加列，
--   本文件必须用 `add_` 命名**且排在建表文件之后**
--   （`add_macro_diagnosis_results.` < `add_macro_diagnosis_results_`）。
--   否则全新安装时表还不存在 ⇒ 「no such table」⇒ 该迁移失败。
--
-- 存量行也会变成 'pending'：关闭开关时它们照旧被注入（行为不变）；
-- 打开开关时它们需要重新决定一次（这正是「静默注入」改成「经确认」应有的后果）。

ALTER TABLE macro_diagnosis_results
    ADD COLUMN injection_decision TEXT NOT NULL DEFAULT 'pending';

ALTER TABLE macro_diagnosis_results
    ADD COLUMN decided_at TEXT;

ALTER TABLE macro_diagnosis_results
    ADD COLUMN decided_by TEXT;
