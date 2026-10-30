# -*- coding: utf-8 -*-
"""全仓 .py 不许带 UTF-8 BOM。

为什么（2026-10-30 实测）
------------------------
**import 机制容忍 BOM，但工具链不容忍** ——
``ast.parse`` / ``compile()`` 对带 BOM 的源码直接抛
``SyntaxError: invalid non-printable character U+FEFF``。

实测踩坑：本仓库有 **19 个** ``.py`` 带 BOM（其中 **13 个是生产文件**：
``infrastructure/ai/prompt_contracts/*.py``、``generation_profiles.py``、
``prompt_contract.py``、``application/ai_invocation/*.py``、
``interfaces/api/v1/engine/character_scheduler_routes.py`` 等）⇒
一条 AST 扫描守卫（扫全仓测试目录）**扫到它们就整个崩掉**
⇒ 工具（守卫 / 审计脚本 / 任何静态扫描）会「因为一个 BOM 而瞎掉」
⇒ 而且崩得莫名其妙（报错指向那个文件，与扫描逻辑毫无关系）。

⇒ BOM 一律去掉；**读**别人的源码时用 ``utf-8-sig``（它会吃掉 BOM）保命。
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 扫描范围：仓库的源代码目录（不含数据 / 日志 / 依赖 / 本地台账）。
SOURCE_DIRS = (
    "application",
    "config",
    "domain",
    "engine",
    "infrastructure",
    "interfaces",
    "scripts",
    "tests",
)

BOM = b"\xef\xbb\xbf"


def _python_sources() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for name in SOURCE_DIRS:
        base = REPO_ROOT / name
        if not base.exists():
            continue
        files.extend(sorted(base.rglob("*.py")))
    return files


def _bom_offenders(files: list[pathlib.Path]) -> list[str]:
    """挑出带 BOM 的文件（抽成函数 ⇒ 下面的自检能真的验它有牙齿）。"""
    return [str(path) for path in files if path.read_bytes().startswith(BOM)]


def test_no_python_source_carries_a_utf8_bom():
    """任何 ``.py`` 都不得以 UTF-8 BOM 开头（工具链会因此瞎掉）。"""
    offenders = _bom_offenders(_python_sources())
    assert not offenders, (
        "这些 .py 带 UTF-8 BOM（ast.parse / compile() 会直接抛 SyntaxError）："
        f"{offenders}"
    )


def test_the_guard_itself_finds_a_bom(tmp_path):
    """反向自检：这条守卫**真的**能看出 BOM（否则它就是个装饰品）。"""
    bad = tmp_path / "bad.py"
    bad.write_bytes(BOM + b"x = 1\n")
    good = tmp_path / "good.py"
    good.write_bytes(b"x = 1\n")

    assert _bom_offenders([bad, good]) == [str(bad)]
