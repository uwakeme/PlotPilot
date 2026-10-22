当前为第 {chapter_number} 章，第 {attempt} 次文风定向修正。

当前相似度：{similarity_score}
自动修文触发阈值：{threshold}
{% if previous_diagnosis or previous_next_technique %}
上一轮的情况（**务必换手法，不要重复上一轮**）：
- 上一轮为什么没达标：{previous_diagnosis}
- 上一轮建议的下一轮手法：{previous_next_technique}
{% endif %}

章节大纲：
{outline}

请在不改变剧情事实的前提下，修订以下正文的叙述语气、句式节奏与措辞，使其更贴近既有文风：

{content}

请输出 JSON（rewritten / diagnosis / next_technique）。
