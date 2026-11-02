-- 第 82 批（N-01）：把「张力有没有真的评过」从「值是不是 50.0」里**拆出来**
--
-- ⚠ 病根：chapters 四列张力都是 `REAL DEFAULT 50.0`
--   ⇒ 「从没评过张力」与「恰好得 50 分」在存储层**完全同形**；
--   ⇒ 下游那条「分辨默认 50」的启发式判据（monitor.py）永远不成立
--     （它要求 `plot_tension IS NULL`，而该列有 DEFAULT 50.0 ⇒ 恒假）
--   ⇒ 未评估章节在张力曲线上是货真价实的「平缓 5/10」**假点**，
--     并且参与均值/方差统计。
--
-- ⇒ 加一列**显式标记**。⚠ **不动现有四列的列约束**（可回滚、风险最小）：
--   `tension_score` 仍是 0-100 的 REAL，实体默认值仍是 50.0（那条契约不动）。
--
-- 默认值 0（未评估）：新建章节在评分前就是「没评过」⇒ 这是**安全的一侧**
--   （宁可漏一个已评估，也不把没评过的当成评过的）。
ALTER TABLE chapters ADD COLUMN tension_evaluated INTEGER NOT NULL DEFAULT 0;

-- 回填存量：只有「四列都是建表默认值 50.0 **且** 章节还是 draft **且** 正文为空」
--   的行才是**真·未评估**；其余一律按「评过」处理。
-- ⚠ 方向是**宁可多算一个已评估**，也不把真实评分当成没评过
--   （后者会把真实数据从曲线上抹掉 ⇒ 那是更贵的错）。
UPDATE chapters
   SET tension_evaluated = 1
 WHERE NOT (
        tension_score = 50.0
    AND plot_tension = 50.0
    AND emotional_tension = 50.0
    AND pacing_tension = 50.0
    AND status = 'draft'
    AND (content IS NULL OR content = '')
 );
