r"""一次性审计脚本（不进 CI）：把登记表主表的状态分布**算出来**。

为什么自己算而不是抄台账自述：
    台账的「已完成条目索引（§九点五）」与「主表状态列」是**两张表**，
    历史上出过「状态列过期 / 两表打架」。
    ⇒ 本脚本**只从主表的状态格**取状态（位置判据换成形状判据），
      再和 §九点五 / §十 圈选表交叉比对 ⇒ 差异就是「台账内部不自洽」的证据。

⚠ 解析规则（踩过的坑）：
    * 主表行判据 = **首格整个就是一个 id**（`^[A-Z]-\d\d$`），
      不能用 `^\\| [A-Z]-\\d\\d ` （会把「遗留清单」两列小表也吃进来）；
    * 状态只认**补记锚点之前**的标记（补记里引用的 ✓ 不算状态）。
"""
from __future__ import annotations

import collections
import pathlib
import re
import sys

#: Windows 控制台默认 GBK ⇒ 状态标记（○ ✓ ◐）会炸 ⇒ 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 老 Python 没有 reconfigure
    pass

REG = pathlib.Path(__file__).resolve().parents[1] / (
    "docs/superpowers/specs/2026-10-16-ai-participation-decision-registry.md"
)
ID_RE = re.compile(r"^[A-Z]-\d\d$")
NOTES_ANCHOR = re.compile(r"\*\*20\d\d-\d\d-\d\d[^|]*?补记\*\*")
GUARD_RE = re.compile(r"tests/unit/[\w/]+\.py")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def is_main_row(line: str) -> bool:
    cells = line.split("|")
    if len(cells) < 3:
        return False
    return bool(ID_RE.match(cells[1].strip()))


def main_rows() -> list[tuple[str, str, str, str]]:
    out = []
    for line in REG.read_text(encoding="utf-8").splitlines():
        if not (line.startswith("| ") and is_main_row(line)):
            continue
        anchor = NOTES_ANCHOR.search(line)
        head = line[: anchor.start()] if anchor else line
        marks = [m.start() for m in re.finditer(r"[○✓◐]", head)]
        if not marks:
            continue
        cid = line.split("|")[1].strip()
        out.append((cid, head[marks[0]], line[anchor.start():] if anchor else "", line))
    return out


def main() -> int:
    rows = main_rows()
    print(f"主表行数 = {len(rows)}")

    dist = collections.Counter(status for _, status, _, _ in rows)
    print("状态分布 =", dict(dist))

    by_class = collections.defaultdict(collections.Counter)
    for cid, status, _, _ in rows:
        by_class[cid[0]][status] += 1
    print("\n分类分布：")
    for klass in sorted(by_class):
        total = sum(by_class[klass].values())
        done = by_class[klass].get("✓", 0)
        print(f"  {klass}: 共 {total:>2}  ✓{done:>2}  ◐{by_class[klass].get('◐',0):>2}  ○{by_class[klass].get('○',0):>2}")

    print("\n未标 ✓ 的行：")
    for cid, status, notes, _ in rows:
        if status != "✓":
            snippet = re.sub(r"\s+", " ", notes)[:150]
            print(f"  {cid} [{status}] {snippet}")

    # §九点五「已完成条目索引」的主表覆盖
    text = REG.read_text(encoding="utf-8")
    start = text.index("## 九点五、已完成条目索引")
    end = text.index("## 九点六")
    idx = text[start:end]
    claimed = set()
    for line in idx.splitlines():
        if not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3:
            continue
        for token in cells[2].split(","):
            token = token.strip().strip("*")
            if ID_RE.match(token):
                claimed.add(token)
    print(f"\n§九点五 声称已做的条目数 = {len(claimed)}")

    main_ids = {cid for cid, _, _, _ in rows}
    print("主表有、§九点五 没有 =", sorted(main_ids - claimed))
    print("§九点五 有、主表没有 =", sorted(claimed - main_ids))

    not_done = {cid for cid, status, _, _ in rows if status != "✓"}
    print("\n⚠ §九点五 说「全部已做」，但主表状态列不是 ✓ 的 =", sorted(not_done & claimed))
    print("   （这些是两表打架的候选）")

    # §十 圈选表
    print("\n§十 待办总表里的开放项关键词：")
    tail = text[text.index("## 十、待办总表"):]
    for line in tail.splitlines():
        if line.startswith("| ") and "|" in line:
            cells = [c.strip() for c in line.split("|")]
            if len(cells) >= 4 and cells[1] and not cells[1].startswith("---"):
                print("   ", " | ".join(cells[1:4])[:160])

    # 守卫文件存在性
    missing = []
    for cid, status, notes, _ in rows:
        if status != "✓" or not notes:
            continue
        for rel in GUARD_RE.findall(notes):
            if not (REPO_ROOT / rel).exists():
                missing.append(f"{cid}:{rel}")
    print("\n标 ✓ 但守卫文件不存在 =", missing or "无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
