-- 第 67 批（B-5）：反 AI 评分**口径版本** + 历史行换算。
--
-- 背景：第 16 批把归一化从「每万字」改成「每千字」（阈值 5.0/3.0/1.0 → 4.0/2.0/0.8），
--       但 anti_ai_audits 的**历史行没重算、也没记口径版本**
--       ⇒ 生产库 591 行跨刻度混排（同一本书里既有旧刻度又有新刻度）
--       ⇒ 趋势序列读起来像「AI 味暴跌 93%」，纯属刻度切换的假象。
--
-- 本迁移做三件事：
--   ① 加一列 score_scale_version：0=未知 / 1=旧刻度(每万字) / 2=当前刻度(每千字)；
--   ② 把**能判定**的历史行换算到当前刻度；
--   ③ 判不出来的行**保持 0**（读侧会把它标成「口径不可比」）—— 不猜。
--
-- ⚠ 换算怎么做到「精确」：
--   hit_density（每千字命中数）**从来没换过口径** ⇒ 可以用它反推章节字数：
--       chars ≈ total_hits / hit_density * 1000
--   再用命中数反推 raw 分数（权重 3.0/1.0/0.3）：
--       raw = 3.0*critical + 1.0*warning + 0.3*info
--   ⇒ 当前刻度下的分数 = raw * 1000 / chars = raw * hit_density / total_hits
--   ⇒ 旧刻度下的分数 = 上面的 10 倍（封顶 100）
--   ⇒ 拿存储的分数跟这两个假设比对，就能**判定**这一行当初用的是哪套刻度。
--
-- ⚠ 本文件里的权重与阈值是**当时的常量**（与
--   application/audit/services/anti_ai_thresholds.py 单一事实源一致），
--   由 tests/unit/application/audit/test_anti_ai_score_scale_version.py 钉住 ⇒ 漂了就红。

-- ① 加列（重复执行会报 "duplicate column" ⇒ 迁移器视为良性错误跳过）
ALTER TABLE anti_ai_audits ADD COLUMN score_scale_version INTEGER NOT NULL DEFAULT 0;

-- ②a 零分行：分数 0 与刻度无关（raw=0）⇒ 直接标成当前刻度，评级按当前阈值重算。
UPDATE anti_ai_audits
SET score_scale_version = 2,
    severity_score = 0.0,
    overall_assessment = '纯净'
WHERE score_scale_version = 0
  AND total_hits = 0
  AND severity_score = 0;

-- ②b 有命中的行：判定刻度 → 换算 → 按当前阈值重算评级。
WITH scaled AS (
    SELECT
        audit_id,
        MIN(100.0,
            (3.0 * critical_hits + 1.0 * warning_hits + 0.3 * info_hits)
            * hit_density / total_hits
        ) AS new_score
    FROM anti_ai_audits
    WHERE total_hits > 0 AND hit_density > 0
),
decided AS (
    SELECT
        s.audit_id,
        s.new_score,
        CASE
            -- 封顶行（100）：两个假设都是 100 ⇒ 判不出当初那套刻度，
            -- 但「当前刻度下的真值」可以从 raw + hit_density 直接重算 ⇒ 用它。
            WHEN a.severity_score >= 100.0 THEN 1
            -- 存储值与当前刻度假设吻合 ⇒ 本来就是当前刻度 ⇒ **原样保留**（零漂移）。
            WHEN ABS(a.severity_score - s.new_score) <= MAX(0.15, s.new_score * 0.05) THEN 0
            -- 否则是旧刻度 ⇒ 新刻度分数 = 旧分数 / 10（同一 raw、同一字数 ⇒ 精确 10 倍关系）
            ELSE 2
        END AS kind
    FROM scaled s
    JOIN anti_ai_audits a ON a.audit_id = s.audit_id
)
UPDATE anti_ai_audits
SET
    score_scale_version = 2,
    severity_score = CASE decided.kind
        WHEN 0 THEN anti_ai_audits.severity_score
        WHEN 1 THEN ROUND(decided.new_score, 1)
        ELSE ROUND(anti_ai_audits.severity_score / 10.0, 1)
    END,
    overall_assessment = CASE decided.kind
        WHEN 0 THEN anti_ai_audits.overall_assessment
        WHEN 1 THEN CASE
            WHEN ROUND(decided.new_score, 1) >= 4.0 THEN '严重'
            WHEN ROUND(decided.new_score, 1) >= 2.0 THEN '中等'
            WHEN ROUND(decided.new_score, 1) >= 0.8 THEN '轻微'
            ELSE '纯净'
        END
        ELSE CASE
            WHEN ROUND(anti_ai_audits.severity_score / 10.0, 1) >= 4.0 THEN '严重'
            WHEN ROUND(anti_ai_audits.severity_score / 10.0, 1) >= 2.0 THEN '中等'
            WHEN ROUND(anti_ai_audits.severity_score / 10.0, 1) >= 0.8 THEN '轻微'
            ELSE '纯净'
        END
    END
FROM decided
WHERE anti_ai_audits.audit_id = decided.audit_id
  AND anti_ai_audits.score_scale_version = 0;
