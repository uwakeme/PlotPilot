小说：{novel_id}
书名：{title}
题材：{genre}

当前目标章数：{target_chapters}
已完成章数：{completed_chapters}
当前已设的终局预算：{current_finale_budget} 章

尚未收束的主要线索/伏笔：
{remaining_threads}

最近几章梗概：
{recent_digest}

请判断「这本书还差多少章收得住」与「其中多少章留给终局收束」，返回 JSON：
{
  "continue_block": 24,
  "finale_budget": 12,
  "finale_budget_min": 8,
  "finale_budget_max": 18,
  "reason": "主线还剩两处伏笔未回收，加 24 章可自然收束；其中 12 章用于终局落点"
}
