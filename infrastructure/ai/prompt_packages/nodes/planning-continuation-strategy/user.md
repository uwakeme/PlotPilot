小说：{novel_id}
要写的章节：第 {chapter_number} 章{chapter_title}
本章目标字数：{target_words}

本章大纲（执行剧本）：
{chapter_outline}

上次中断的草稿（尾部 {draft_char_count} 字里的最后一段）：
{draft_excerpt}

请返回 JSON：
{
  "strategy": "continue",
  "directive": "从『他把信推回桌面』这一句接上，接下来推进大纲里的第二拍：林姐认出笔迹，但不要重写已写过的对峙段落。",
  "reason": "草稿写完的正是大纲第一拍，断点落在两拍之间，接上后仍顺着大纲推进"
}
