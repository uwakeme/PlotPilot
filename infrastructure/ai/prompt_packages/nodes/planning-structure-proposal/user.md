小说：{novel_id}
书名：{title}
题材：{genre}

一句话梗概：
{premise}

全书目标章数：{target_chapters} 章

上限：部 ≤ {max_parts}，每部卷数 ≤ {max_volumes_per_part}，每卷幕数 ≤ {max_acts_per_volume}，每卷高潮节点数 ≤ {max_climaxes_per_volume}

请提出这本书的结构网格与卷内节奏建议，返回 JSON：
{
  "parts": 3,
  "volumes_per_part": 4,
  "acts_per_volume": 5,
  "climaxes_per_volume": 3,
  "hook_style": "每卷末停在主角刚拿到关键证物、但证物指向的人就在下一卷开头出现的地方",
  "reason": "三条主线各自独立成篇，每篇四卷，每卷一个完整的冲突升级周期"
}
