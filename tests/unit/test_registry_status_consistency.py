"""登记表「状态列 ↔ 备注」的**一致性守卫**（治本：状态列不再靠人眼维护）。

为什么需要这条
--------------
登记表（``docs/superpowers/specs/2026-10-16-ai-participation-decision-registry.md``）
的**状态列是手维护的** ⇒ 改代码时忘了同步状态 ⇒ 选活时会选错。

第 45 批实测到的坏行（**都是手滑/漏改**，不是代码问题）：

* 状态标 ``○``、但备注写着「现已修」⇒ **状态列过期**（A-03/A-04/A-06/A-07/A-08）；
* 「状态 + 证据」格**整格被补记占用**、备注格为空 ⇒ 状态与证据双丢
  （C-08/D-07/J-02）—— 上一次清理时把状态标记和补记粘进了证据格；
* 同一格里出现**两个**标记（``○ ✓``）⇒ 不知道哪个是真的。

⇒ ⇒ 本守卫把「状态 ↔ 备注 ↔ 守卫文件」三者**互锁**：

1. 主表行**有且只有一个**状态标记；
2. 标 ``✓`` ⇒ 备注必须点名守卫文件，且该文件**存在**（守卫被删了 ⇒ 状态也得改）；
3. 标 ``◐``（部分完成）⇒ 备注必须还有残留（「未做」）；
4. 标 ``○`` ⇒ 备注**不得**点名守卫（点名了 ⇒ 这一笔做过 ⇒ 状态列过期）。

⚠ 登记表是 **gitignored** 的本地文件 ⇒ CI 上没有它 ⇒ 本守卫在 CI 里 **skip**；
  它保护的是**本地**这份工作台账。
"""
from __future__ import annotations

import pathlib
import re

import pytest

REG = pathlib.Path(
    pathlib.Path(__file__).resolve().parents[2]
    / "docs/superpowers/specs/2026-10-16-ai-participation-decision-registry.md"
)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not REG.exists(), reason="登记表是本地文件（gitignored）⇒ CI 上没有，跳过"
)

MARKERS = ("○", "✓", "◐")
#: 主表行的 id 形状（**必须整个格就是一个 id**）
ID_RE = re.compile(r"^[A-Z]-\d\d$")
#: 状态列里「备注」的开头锚点（补记块）
NOTES_ANCHOR = re.compile(r"\*\*20\d\d-\d\d-\d\d[^|]*?补记\*\*")
#: 备注里点名的守卫文件
GUARD_RE = re.compile(r"tests/unit/[\w/]+\.py")
#: 残留措辞（与清理脚本 ``_t45_clean_pass3.py`` 的同名常量**保持一致**）
RESIDUAL_MARKERS = (
    "未做",
    "仍未做",
    "仍没有",
    "仍缺",
    "还没",
    "未实现",
    "未改",
    "未清理",
)


def _is_main_row(line: str) -> bool:
    """是不是**主表**行。

    ⚠ 登记表下面还有一张「遗留清单」两列小表，它的首格是
    「J-03 的阶段增删 UI」这种**带说明**的文本 ⇒ **不能**用
    ``^\\| [A-Z]-\\d\\d `` 来判（那样会把说明文字里带 id 的行也吃进来）。
    ⇒ ⇒ 判据：首格**整个**就是一个 id。
    """
    cells = line.split("|")
    if len(cells) < 3:
        return False
    return bool(ID_RE.match(cells[1].strip()))


def _main_rows() -> list[tuple[str, str, str]]:
    """返回 (条目号, 状态, 备注)。

    ⚠ **不按固定列下标取** —— 备注里含 ``|`` 的行会被 split 切歪
    ⇒ ⇒ 只靠「行内标记」与「补记锚点」定位。

    ⚠ 状态**只认状态格里的那个标记**（补记锚点之前）——
    此前这里取的是「整行里第一个 ✓，没有才取第一个标记」，
    ⇒ 「状态格写着 ◐、补记里写着 ✓」这种**过期状态**会被读成 ✓
    ⇒ 「唯一标记 / ✓ 必须点名守卫 / ◐ 必须写明残留」三条互锁规则
    全部合法通过 ⇒ G-04 那样过期了很久的状态列就这么漏了过去
    ⇒ ⇒ 状态只看状态格；补记里出现标记不算状态。
    """
    rows = []
    for line in REG.read_text(encoding="utf-8").splitlines():
        if not (line.startswith("| ") and _is_main_row(line)):
            continue
        anchor = NOTES_ANCHOR.search(line)
        head = line[: anchor.start()] if anchor else line
        marks = [m.start() for m in re.finditer(r"[○✓◐]", head)]
        if not marks:
            continue
        cid = line.split("|")[1].strip()
        status = head[marks[0]]
        notes = line[anchor.start():] if anchor else ""
        rows.append((cid, status, notes))
    return rows


def test_every_main_row_has_exactly_one_status_marker():
    """⚠ 状态格里的标记必须**唯一**（``○ ✓`` ⇒ 不知道哪个是真的）。

    ⚠ 只数**补记锚点之前**的标记 ⇒ 备注里**引用**标记（写「本行的 ○ 该改成 ✓」）
    不算状态标记。
    """
    bad = []
    for line in REG.read_text(encoding="utf-8").splitlines():
        if not (line.startswith("| ") and _is_main_row(line)):
            continue
        anchor = NOTES_ANCHOR.search(line)
        head = line[: anchor.start()] if anchor else line
        count = sum(head.count(m) for m in MARKERS)
        if count > 1:
            bad.append(f"{line.split('|')[1].strip()}({count})")
    assert not bad, f"这些行的状态标记不唯一：{bad}"


def test_done_rows_with_notes_must_name_an_existing_guard():
    """⚠ 标 ``✓`` **且有补记** ⇒ 补记必须点名守卫文件，且该文件**存在**。

    ⚠ 老条目**没有补记** ⇒ 无法机械核对 ⇒ 放行（不拿「没有证据」当「有问题」）。
    """
    missing_guard = []
    missing_file = []
    for cid, status, notes in _main_rows():
        if status != "✓" or not notes:
            continue
        if "守卫：" not in notes:
            continue
        guards = GUARD_RE.findall(notes)
        if not guards:
            missing_guard.append(cid)
            continue
        for rel in guards:
            if not (REPO_ROOT / rel).exists():
                missing_file.append(f"{cid}:{rel}")
    assert not missing_guard, f"标 ✓、补记里写了「守卫：」但没给出守卫路径：{missing_guard}"
    assert not missing_file, f"标 ✓ 但守卫文件不存在：{missing_file}"


def test_partial_rows_must_still_have_residuals():
    """⚠ 标 ``◐`` ⇒ 备注必须说明**还剩什么没做**。

    ⚠ 残留措辞不只一种（「未做 / 仍未做 / 仍没有 / 还没 / 未实现…」）
    ⇒ 词表与清理脚本 ``_t45_clean_pass3.py`` 的 ``RESIDUAL_MARKERS`` **保持一致**。

    ⚠ 这条是**词表启发式**，会命中无关语境
    （G-04 的补记里就有「还没回收的线不能因为上限而消失」这种「还没」）
    ⇒ 它**不是**「过期状态」的主防线；
    主防线是 :func:`test_status_comes_from_the_status_cell_not_from_the_notes`。
    """
    bad = []
    for cid, status, notes in _main_rows():
        if status != "◐":
            continue
        if not any(k in notes for k in RESIDUAL_MARKERS):
            bad.append(cid)
    assert not bad, f"标 ◐ 但备注没说还剩什么：{bad}"


def test_todo_rows_must_not_claim_a_guard():
    """⚠ 标 ``○`` 却点名了守卫 ⇒ 这一笔**做过** ⇒ 状态列过期（要改）。"""
    bad = []
    for cid, status, notes in _main_rows():
        if status != "○":
            continue
        if GUARD_RE.findall(notes):
            bad.append(cid)
    assert not bad, f"这些行标着 ○，但备注点名了守卫（状态列过期）：{bad}"


def test_status_comes_from_the_status_cell_not_from_the_notes():
    """⚠ 状态格说「部分完成」、补记却自称「已做完」 ⇒ 状态列过期。

    为什么单开一条：G-04 那一行状态格写着 ◐、补记第一句就是「本行已做完」，
    而代码形状（`coerce_urgency` / `MAX_ITEMS_PER_CATEGORY` /
    `INJECT_ITEMS_PER_CATEGORY` / `trimmed_items`）与三个守卫文件都在
    ⇒ 这笔**早已做完**，状态格是过期残留。

    ⚠ 此前这条漏过去，是因为状态取「整行里第一个 ✓」
    ⇒ 补记里的 ✓ 把状态格的 ◐ 掩盖了（见 `_main_rows` 的注释）。
    """
    stale = []
    for cid, status, notes in _main_rows():
        if status != "◐":
            continue
        if "已做完" in notes:
            stale.append(cid)
    assert not stale, f"这些行状态格是 ◐、补记却说做完了（状态列过期）：{stale}"


# ── 第十节「圈选」表 ↔ 主表状态列（两张表互锁）────────────────────────


def _checked_ids_in_selection_table() -> set[str]:
    """第十节圈选表里标了 ``[x]`` 的条目编号。

    ⚠ 那一段是围栏代码块，形态是
    ``第 0 批（20 · 静默失效）I-03 [x] I-04 [x] …``（编号后可能带 ``*`` 脚注标记）。
    """
    text = REG.read_text(encoding="utf-8")
    start = text.find("## 十、圈选")
    assert start > 0, "找不到第十节「圈选」"
    fence_start = text.find("```", start)
    fence_end = text.find("```", fence_start + 3)
    assert fence_start > 0 and fence_end > fence_start, "圈选表的围栏代码块没找到"
    block = text[fence_start + 3 : fence_end]

    ids: set[str] = set()
    for m in re.finditer(r"\b([A-Z]-\d\d)\*?\s*\[x\]", block):
        ids.add(m.group(1))
    return ids


def test_checked_items_in_the_selection_table_match_the_main_table_status():
    """⚠ **两张表必须互锁**（第 55 批新增）。

    第十节圈选表标 ``[x]`` 的条目，主表状态**必须**是 ``✓``（或 ``◐`` 且写明还剩什么）。

    ⚠ 为什么需要：第 55 批查出 **23 行**在圈选表里早已标 ``[x]``、主表却仍是 ``○``
    （状态格从未更新过）⇒ 登记表自述的老毛病「**选活时会选错**」又长回来了
    ⇒ 光靠「状态 ↔ 备注」互锁抓不到它（那 23 行**没有备注**）
    ⇒ 必须把**两张表**也互锁起来。
    """
    statuses = {cid: status for cid, status, _ in _main_rows()}
    stale = []
    for cid in sorted(_checked_ids_in_selection_table()):
        status = statuses.get(cid)
        if status is None:
            continue  # 主表里没有这一行（例如只在圈选表里出现）⇒ 不在本守卫范围
        if status not in ("✓", "◐"):
            stale.append("%s=%s" % (cid, status))
    assert not stale, (
        f"圈选表里已标 [x]、主表却不是 ✓/◐ 的条目：{stale}"
        "（状态列过期 ⇒ 选活时会选错）"
    )
