【故事创意】
{premise}

【小说设定】
名称：{novel_title}
大类：{genre_major}
主题：{genre_theme}
类型：{genre_label}
基调：{world_preset}
特殊要求：{special_requirements}

【类型开篇画像】
{genre_opening_profile}

请生成世界观。

请按照以下 json 格式输出，可被 Python json.loads 解析。只给出 JSON，不要解释，不要 markdown 说明。
每个字段值写成**中文单段文本**，不得换行，不得嵌套对象或数组；不得把一个维度写成字符串；不得省略任何 fields_desc 中列出的子字段。

每个字段写多长由你按该字段**实际要说的内容量**决定：篇幅大的设定（规则、地理、势力）写得详尽，篇幅小的写到位即可；但每个字段都不得少于 20 字、不得超过 500 字。

{
  "style": "请参照大类、主题、类型、与基调生成文风公约；单段、不得换行；勿嵌套JSON或英文键；字数按要说的内容量自定，不少于20字、不多于500字）",
  "worldbuilding": {
{fields_desc}
  }
}
