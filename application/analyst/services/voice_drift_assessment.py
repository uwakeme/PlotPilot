# -*- coding: utf-8 -*-
"""文风漂移的**书内相对**判定（第 89 批 · N-24）。

为什么不能只用绝对阈值
----------------------
相似度 = 「本章离**开局风格**有多远」。而一本书写到 500 章后风格本来就会变：

* 实测（600 章那本，第 87 批真跑）：开局章节 **0.5364 ~ 0.6589**，
  五百多章之后 **0.4034 ~ 0.5320**；
* ⇒ 绝对阈值 0.50 对长书**几乎每章都命中**
  ⇒ 假告警 + 每次都触发定向修文 ⇒ **白烧钱**。

⇒ 改成**书内相对**：拿「**本书最近 N 章相似度的中位数**」当**常态**，
  判据变成「这一章比这本书**最近的常态**低多少」：

* 常态在 0.65 的书 ⇒ 0.45 是明显异常；
* 常态在 0.45 的书 ⇒ 0.45 **就是常态** ⇒ 不该报、更不该花钱去改。

⚠ **长期偏移不由本模块负责**：常态整体下移是「这本书整体已经偏了」，
  不是「某一章坏了」⇒ 它作为**指标**单独上报（``book_median``），
  **不**拿去触发改写 —— 把第 528 章拉回第 1 章的文风，本身就是错的改写目标。

⚠ **历史不足时回落到绝对阈值**：绝对阈值的标定口径就是「开局健康」
  ⇒ 对**新书**（还没攒够常态样本）它是对的。
"""
from __future__ import annotations

import statistics
from typing import Iterable, List, Optional

#: 近期常态取最近多少章的相似度。
RECENT_MEDIAN_WINDOW = 20
#: 至少要多少个样本才敢用「近期常态」（样本太少 ⇒ 中位数不稳 ⇒ 回落绝对阈值）。
#:
#: ⚠ 取 **6** 而不是 5：告警窗口本身要 5 章 ⇒ 若常态也只要 5 个样本，
#:   那「绝对兜底」在告警路径上**永远走不到** ⇒ 配置里的绝对阈值就成了死旋钮。
#:   ⇒ 6 ⇒ **新书的前 5 章**走绝对阈值（正好是标定口径「开局健康」），
#:     从第 6 个样本起改用相对判定。
RECENT_MEDIAN_MIN_SAMPLES = 6
#: 低于近期常态多少 ⇒ 算「异常」（漂移告警）。
DRIFT_RELATIVE_DROP_ALERT = 0.15
#: 低于近期常态多少 ⇒ 值得花钱做定向修文（比告警更严，因为它要**烧调用**）。
DRIFT_RELATIVE_DROP_REWRITE = 0.25


def recent_similarity_median(
    scores: Iterable[Optional[float]],
    exclude_chapter_number: Optional[int] = None,
    chapters: Optional[Iterable[Optional[int]]] = None,
    min_samples: int = RECENT_MEDIAN_MIN_SAMPLES,
) -> Optional[float]:
    """近期常态 = 最近一批相似度的**中位数**；样本不足 ⇒ ``None``。

    ⚠ ``exclude_chapter_number``：**判当前章时必须把自己排除掉**
      ⇒ 否则一个异常低值会把自己的中位数拉下来 ⇒ 把自己「洗白」。

    ⚠ 用**中位数**而不是均值：常态里混进几章异常值时，中位数不动。
    """
    if chapters is None:
        pairs = [(None, raw) for raw in scores]
    else:
        pairs = list(zip(chapters, scores))

    values: List[float] = []
    for chapter_number, raw in pairs:
        if raw is None:
            continue
        if exclude_chapter_number is not None and chapter_number == exclude_chapter_number:
            continue
        values.append(float(raw))
    if len(values) < min_samples:
        return None
    return float(statistics.median(values))


def relative_drop(similarity: float, median: float) -> float:
    """相对跌幅（≥ 0）：``(常态 - 本章) / 常态``。

    ⚠ 常态 ≤ 0 时按 0 处理（没有可比的中位数 ⇒ 不该判成异常）。
    """
    if median is None or median <= 0:
        return 0.0
    return max(0.0, (float(median) - float(similarity)) / float(median))


def assess(
    similarity: Optional[float],
    median: Optional[float],
    absolute_alert_threshold: float,
    absolute_rewrite_threshold: float,
    relative_alert_drop: float = DRIFT_RELATIVE_DROP_ALERT,
    relative_rewrite_drop: float = DRIFT_RELATIVE_DROP_REWRITE,
) -> dict:
    """判定一章的相似度：**相对优先，历史不足时回落绝对**。

    Returns:
        ``{"basis", "relative_drop", "book_median", "alert", "rewrite"}``

        * ``basis`` = ``"relative"``（有常态可比）或 ``"absolute"``（回落）
        * ``alert``  = 该不该报漂移告警
        * ``rewrite`` = 该不该花钱做定向修文
    """
    if similarity is None:
        return {
            "basis": "absolute",
            "relative_drop": None,
            "book_median": None,
            "alert": False,
            "rewrite": False,
        }

    if median is None:
        # ⚠ 历史不足（新书 / 刚开写）⇒ 回落绝对阈值（标定口径 = 「开局健康」）
        return {
            "basis": "absolute",
            "relative_drop": None,
            "book_median": None,
            "alert": float(similarity) < float(absolute_alert_threshold),
            "rewrite": float(similarity) < float(absolute_rewrite_threshold),
        }

    drop = relative_drop(similarity, median)
    return {
        "basis": "relative",
        "relative_drop": round(drop, 4),
        "book_median": round(float(median), 4),
        "alert": drop > float(relative_alert_drop),
        "rewrite": drop > float(relative_rewrite_drop),
    }
