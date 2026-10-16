-- 数据回填：让结构树镜像真实章节进度 + 修复越界的 current_act 指针
--
-- 背景（2026-10-16 实测）
-- ----------------------
-- 引擎写作路径只写 chapters 表，从不回写 story_nodes 的章节节点。实测全库
-- 587 个 chapter 节点，word_count > 0 的数量为 0 —— 这个字段对所有由引擎
-- 写出来的书永远是幽灵字段。
--
-- 而有三处控制流判定靠它判断「这一章动笔没有」：
--   1. engine/runtime/act_planning_delegate.py 闸门 0（prev_act_unwritten）
--   2. 同文件预算闸门的 pending 列表
--   3. application/blueprint/services/continuous_planning_service.py
--      的 _count_written_chapters_in_act
-- 字段恒为 0 意味着这三处恒判「全部未写」：上一幕永远有「未动笔章节」，
-- 新幕开不出来 → 写作找不到下一章 → _current_act_fully_written 把「找不到
-- 这一幕」当成「这一幕写完了」→ current_act 每 tick +1 棘轮式暴涨。
-- 线上实测：结构树只有 5 幕的新书，current_act 被推到 260。
--
-- 代码侧已修（见 story_node_progress_sync.py 与两条写入路径的接线），
-- 本迁移负责把**存量**数据补齐，并对所有安装生效。

-- ── 1. 回填章节节点的字数与状态 ──────────────────────────────────────
-- 只处理能在 chapters 表找到对应行的节点；找不到的行一律不碰，
-- 避免把结构树里其它来源写入的 word_count 冲成 0。幂等。
UPDATE story_nodes
SET word_count = COALESCE(
        (SELECT c.word_count FROM chapters c
          WHERE c.novel_id = story_nodes.novel_id
            AND c.number = story_nodes.number),
        word_count),
    status = COALESCE(
        (SELECT c.status FROM chapters c
          WHERE c.novel_id = story_nodes.novel_id
            AND c.number = story_nodes.number),
        status)
WHERE node_type = 'chapter'
  AND EXISTS (
        SELECT 1 FROM chapters c
        WHERE c.novel_id = story_nodes.novel_id
          AND c.number = story_nodes.number
      );

-- ── 2. 把越界的 current_act 拉回结构真实范围 ──────────────────────────
-- 约定：current_act 为 0-based，且 current_act + 1 = 正在规划/写作的幕号。
-- 指针越过真实幕数时（棘轮产物），把它收敛到「已落库的最大幕号」——
-- 即本幕全部写完后应当去规划/创建的那一幕。合法的 current_act 不受影响
-- （只在 current_act > max_act 时才写）。幂等。
UPDATE novels
SET current_act = (
        SELECT MAX(n.number) FROM story_nodes n
        WHERE n.novel_id = novels.id AND n.node_type = 'act'
    )
WHERE current_act > (
        SELECT MAX(n.number) FROM story_nodes n
        WHERE n.novel_id = novels.id AND n.node_type = 'act'
    );
