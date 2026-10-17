小说：{novel_id}
即将创作：第 {chapter_number} 章
当前阶段：{phase_hint}

本章大纲：
{chapter_outline}

最近章节：
{recent_digest}

可选槽位：
{slot_catalog}

请返回 JSON：
{
  "weights": {
    "recent_chapters": 0.9,
    "storyline_context": 0.8
  },
  "evaluated": true,
  "reason": "本章要回收第二幕埋下的线索，且必须承接上一章的现场"
}
