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
    """
    rows = []
    for line in REG.read_text(encoding="utf-8").splitlines():
        if not (line.startswith("| ") and _is_main_row(line)):
            continue
        marks = [m.start() for m in re.finditer(r"[○✓◐]", line)]
        if not marks:
            continue
        cid = line.split("|")[1].strip()
        status = line[next((i for i in marks if line[i] == "✓"), marks[0])]
        anchor = NOTES_ANCHOR.search(line)
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
