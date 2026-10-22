小说：{novel_id}

已在册的角色（不要重复报）：
{known_names}

已知的地点（不要报成角色）：
{location_names}

本章大纲：
{outline}

请找出本章出场、但还没有角色档案的人，返回 JSON：
{
  "characters": [
    {"name": "沈砚", "evidence": "沈砚推门进来，把伞收在脚边"}
  ],
  "reason": "大纲里只有沈砚是未在册的人"
}
