"""第 92 批：本书**已登记**的角色名 —— 角色白名单的**唯一权威源**。

⚠ 为什么需要它（生产实证，不是推断）
------------------------------------
生产书《灰引书》写到第 556 章时，``Bible`` 角色表**只有 5 个**角色
（陆深 / 宋扬 / 沈夜 / 林姐 / 周队），
而正文里实际有 **40+** 个有名角色 —— 记忆引擎早已登记（``character_states``），
只是**没进 Bible 角色表**。

⇒ 后果：**引擎自己两条链路打架**

1. 生成侧（事实锁）写「只可使用：陆深, 宋扬, 沈夜, 林姐, 周队」，
   而同一章的章纲写「出场提示：谢迟、沈夜、陆深、宋扬」
   ⇒ 模型被要求「不许用」这一章的主角；
2. 审计侧（反 AI · K 类）把「谢迟」判成 ``critical`` 越界角色，
   并被机检核对盖上 ``confirmed_out_of_whitelist``
   ⇒ **误报看起来很权威**（第 22 批那次「核对」反而让误报更可信了）。

⇒ 事实锁的原意是「禁止**创造**任何其他有名字的角色」
⇒ 「没登记进 Bible」**不等于**「越界」
⇒ 口径应当是「**本书从没出现过**」。

本模块做什么
------------
把白名单口径从「Bible 角色表」放宽为「**Bible ∪ 记忆引擎已登记的角色**」，
并且**只有这一处实现** —— 四处消费者全部走这里：

1. 生成侧：``FactLockBuilder``（事实锁文本 + 设定漂移基准事实）
2. 审计侧提示词变量：``daemon_host._allowed_character_names``
3. 审计侧机检核对：``continuations._whitelist_variants``

⇒ 口径**不会分叉**（此前四处各抄一份 ``character_names(bible)``）。

⚠ 取不到就**退回 Bible 那一半**（不崩、不臆测）——
   「白名单窄一点」只会导致**误报变多**，不会导致漏报。
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: 角色名的**形状**：2~5 个汉字（不含 ASCII / 标点 / 空白 / 引号）
#:
#: ⚠ 为什么要有形状过滤：记忆引擎登记的是「实体」，
#:   里面混着「地下值班同事」「灰引书裂缝中的存在」这类**描述**，
#:   直接进白名单会把提示词里的名单稀释成噪音。
_NAME_SHAPE = re.compile(r"^[一-鿿]{2,5}$")

#: 名字里出现这些字 ⇒ 多半是**描述**而不是名字（「林姐丈夫」「门外之物」）
_NOT_A_NAME_CHARS = frozenset("的们了着过")

#: Bible 角色的**内部 id** 形态（``novel-xxx-char-3``）⇒ 那不是名字
#:
#: ⚠ 记忆引擎给 Bible 角色用的就是内部 id ⇒ 不排除的话，
#:   提示词里会出现 ``novel-1790226677602-char-1`` 这种东西。
_INTERNAL_ID = re.compile(r"^novel-[0-9A-Za-z\-]+-char-\d+$")

#: 白名单最多放多少个名字 —— 长书别把「只可使用以下角色」稀释成噪音
#:
#: ⚠ 超了就**截断**（按「最近出场」优先 ⇒ 截掉的是冷门角色）。
MAX_REGISTERED_NAMES = 80


def looks_like_character_name(name: str) -> bool:
    """这个字符串像不像一个**角色名**（把记忆引擎的登记项筛成人名）。"""
    n = str(name or "").strip()
    if not n or _INTERNAL_ID.match(n):
        return False
    if not _NAME_SHAPE.match(n):
        return False
    return not any(ch in _NOT_A_NAME_CHARS for ch in n)


def _bible_character_names(bible: Any) -> List[str]:
    """Bible 角色表里的名字（**白名单的第一半**）。

    ⚠ 单一实现：在 :mod:`application.engine.services.memory_engine` 里
      （``bible_character_names``）⇒ 这里只做转发，不复制一份逻辑
      ⇒ 复制逻辑 = 将来两边会悄悄漂。
    """
    if bible is None:
        return []
    from application.engine.services.memory_engine import bible_character_names

    return list(bible_character_names(bible))


def _bible_location_names(bible: Any) -> List[str]:
    """Bible 地点名（用来把「周队办公室」这种**地点**从角色白名单里剔掉）。"""
    if bible is None:
        return []
    locations = getattr(bible, "locations", None) or []
    return [str(loc.name) for loc in locations if getattr(loc, "name", "")]


def _memory_registered_names(novel_id: str) -> List[str]:
    """记忆引擎（``character_states``）里**已登记**的角色名。

    ⚠ 数据源的选择是量出来的，不是拍的：
    · ``character_states`` —— 556 章的书 61 行，其中约 43 个像人名 ✔
    · ``narrative_entities`` —— **622 行全是 ``entity_type='character'``**
      ⇒ 类型字段没有筛选力，且混着「乌木笔杆」「判定规则」这类物件 ✘
    · ``memory_atoms`` —— 15639 行 / 622 个不同实体 ⇒ 同上，噪声更大 ✘

    ⇒ 用 ``character_states``：它是**引擎认定的角色**（章末心理状态快照）
    ⇒ 量级可控，且「谢迟」这种长期存在但没进 Bible 的角色在里面。

    ⚠ 取不到 ⇒ 返回空（调用方退回 Bible 那一半）⇒ **不崩、不臆测**。
    """
    if not novel_id:
        return []
    try:
        from infrastructure.persistence.database.connection import get_database

        rows = get_database().fetch_all(
            "SELECT character_id, MAX(last_updated_chapter) AS last_chapter "
            "FROM character_states WHERE novel_id = ? "
            "GROUP BY character_id ORDER BY last_chapter DESC",
            (novel_id,),
        )
    except Exception as exc:  # noqa: BLE001 —— 取不到就当没有，不许把写作搞崩
        logger.debug("取记忆引擎已登记角色失败（白名单退回 Bible 那一半）novel=%s: %s", novel_id, exc)
        return []

    names: List[str] = []
    for row in rows or []:
        cid = str((row or {}).get("character_id") or "").strip()
        if cid and looks_like_character_name(cid):
            names.append(cid)
    return names


def registered_character_names(novel_id: str, bible: Any = None) -> List[str]:
    """本书**已登记**的角色名（白名单）。

    = **Bible 角色表** ∪ **记忆引擎已登记的角色**

    ⚠ 口径（第 92 批，用户拍板）：白名单管的是「**不许凭空创造新名字**」，
      不是「必须登记在 Bible 角色表里」⇒ 白名单窄 ⇒ 误报；白名单宽 ⇒ 漏报。
      ⇒ 取**宽**的一边（已登记就算数），而「本书从没出现过」的名字仍会被报。

    Args:
        novel_id: 小说 ID（空 ⇒ 只返回 Bible 那一半，不查库）
        bible: 已加载的 Bible（给了就复用，不重复查库）

    Returns:
        去重后的角色名（Bible 的名字在前，其余按「最近出场」排序）
    """
    bible_names = _bible_character_names(bible)
    if not novel_id:
        return list(bible_names)

    locations = set(_bible_location_names(bible))
    seen = set(bible_names)
    out: List[str] = list(bible_names)
    for name in _memory_registered_names(novel_id):
        if name in seen or name in locations:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= MAX_REGISTERED_NAMES:
            logger.debug(
                "角色白名单超过上限 %s 个 ⇒ 截断（按最近出场优先）novel=%s",
                MAX_REGISTERED_NAMES, novel_id,
            )
            break
    return out


def registered_character_whitelist_variants(novel_id: str, bible: Any = None) -> List[str]:
    """白名单的**可匹配变体集合**（机检核对用）。

    ⚠ 薄薄一层包装：变体展开的规则只有一份（``expand_whitelist``），
      这里只负责「名单从哪来」⇒ 不会与生成侧分叉。
    """
    from application.audit.services.character_whitelist_check import expand_whitelist

    return sorted(expand_whitelist(registered_character_names(novel_id, bible)))
