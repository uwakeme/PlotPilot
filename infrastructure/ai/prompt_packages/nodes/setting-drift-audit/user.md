【小说】{novel_title}　第 {chapter_number} 章

【基准事实清单（设定真源，正文必须遵守）】
{setting_facts}

【章节正文】
{chapter_content}
（以上为第 {segment_index} 段，共 {segment_total} 段。若共 1 段则这是完整正文；否则这只是章节节选，请只就本段出现的文字判断，不要替未提供的部分下结论。）

请比对正文与基准事实，输出可被 json.loads 解析的 JSON：
{
  "drift_score": 0到100的整数（0=本段完全一致，100=本段严重漂移）,
  "issues": [
    {
      "severity": "critical | warning | info",
      "category": "ability | location | rule | fact",
      "description": "哪条设定被如何违反（引用清单条目 + 正文摘录）",
      "suggestion": "最小修正建议（一句话）"
    }
  ],
  "checked_facts": 本段实际比对过的基准事实数量
}
只输出 JSON。
