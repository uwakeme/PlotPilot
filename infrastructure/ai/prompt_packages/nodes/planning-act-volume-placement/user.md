小说：{novel_id}
书名：{title}
题材：{genre}

一句话梗概：
{premise}

全书各卷：
{volume_overview}

上一幕（最新一幕）：
{last_act}

锚点与约束：
{anchor_note}

允许选择的卷号：{allowed_volume_numbers}

已观测到的每卷幕数：{observed_acts_per_volume}

请判断接下来这一幕属于哪一卷，返回 JSON：
{
  "volume_number": 2,
  "reason": "上一幕的追杀线在本幕收束，这里开启下一卷的新目标"
}
