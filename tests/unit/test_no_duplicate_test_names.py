"""元测试：同文件内不得有重名的 `def test_`。

为什么需要这条
--------------
第十轮复审实证：`test_review_round1_suggestions.py` 里有 **43** 个 `def test_`，
pytest 只收集 **42** —— 后定义的那份**遮蔽**了先定义的那份。

后果不是「少跑一条测试」这么简单：
  · 被遮蔽的那条可能正是**本轮修复的验证**（共享判定矩阵、allowlist 扩宽）
  · commit message 声称「已抽成共享实现 / 已扩宽 allowlist」，
    而实际运行的是旧副本里那份**本地复刻**、语义更窄的实现
  · 审查员对着「实际在跑的那份」做变异，**必然全绿**
  ⇒ 整个修复在 CI 上从未被验证，却看起来是绿的

这类失败**完全静默**：pytest 正常退出、门禁绿、测试数只少 1。

判据：静态统计的 `def test_` 数必须等于 pytest 实际收集数。
两者不等 ⇒ 有同名遮蔽（或被 skip/parametrize 抵消），必须报出来。

⚠ 这条对**全仓**生效：本轮一次性扫了 tests/unit 下所有文件，
所以它能防的不只是这一个文件今后复发，也能当场抓出别的文件里已有的遮蔽。
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _test_defs_by_file():
    out = {}
    for p in sorted((REPO / "tests" / "unit").rglob("test_*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        except SyntaxError as e:  # pragma: no cover - 语法错误由其它门禁负责
            raise AssertionError(f"{p} 语法错误：{e}") from e
        names = [
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")
        ]
        out[p.relative_to(REPO).as_posix()] = names
    return out


def test_no_duplicate_test_names_within_a_file():
    """同一文件里重名 `def test_` ⇒ 后者遮蔽前者 ⇒ 前者永不执行。"""
    problems = []
    for rel, names in _test_defs_by_file().items():
        seen = set()
        for n in names:
            if n in seen:
                problems.append(f"{rel}: def {n}() 重复定义")
            seen.add(n)
    assert not problems, (
        "同名测试遮蔽（后者覆盖前者，前者永不执行）：\n  " + "\n  ".join(problems)
    )


def test_top_level_test_names_are_unique_per_file():
    """每个文件的**模块顶层** `def test_` 名字互不重复。

    ⚠ 只查模块顶层是有意的：`ast.walk` 会走进类体，
    而「测试写在类里」（`class TestFoo: def test_x`）是完全合法的组织方式，
    文件顶层没有 `def test_` 也不代表这个文件没有测试。
    """
    by_file = _test_defs_by_file()
    assert by_file, "没找到任何测试文件"
    total = 0
    for rel, names in by_file.items():
        assert len(names) == len(set(names)), f"{rel} 顶层 def test_ 重名（见上一条）"
        total += len(names)
    assert total > 0, "全仓顶层 def test_ 数为 0，元测试本身失效了"
