"""F-12 机械活：提示词包的 `source:` 不许是**假路径**，AI 味模式数不许是旧数。

核实（探针）：**8 个**内置提示词包的 `source:` 指向
`infrastructure/ai/prompts/prompts_defaults.json` —— 那个文件**早就不存在了**
（提示词已迁到 CPMS），但该字段会透出到提示词广场 ⇒ 读者看到的是**假来源**。
另外全仓的「35+ 模式」是早期 10 条时代的遗留说法（扫描器实测 **51** 条）。

本文件钉住三件事：
1. 每个 `package.yaml` 的 `source:` 若是**路径**，那个路径必须真的存在；
2. 全仓不再有「35+」这个过期数字；
3. 写出来的「50+」必须仍然为真（扫描器实际模式数 ≥ 50）
   ⇒ 以后模式被删到 50 以下时，这里会红，逼着人把文案一起改对。
"""
from __future__ import annotations

import re
from pathlib import Path

from application.audit.services.cliche_scanner import (
    AI_CLICHE_PATTERNS_ENHANCED,
    ClicheScanner,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
PKG_ROOT = REPO_ROOT / "infrastructure" / "ai" / "prompt_packages" / "nodes"

#: 源码扩展名 ⇒ 出现这些就算「路径声明」，必须真实存在
_PATH_SUFFIXES = (".py", ".json", ".yaml", ".yml", ".md", ".txt")

#: 扫描文本范围（前端也扫：它也会把「35+」显示给用户）
_SCAN_DIRS = (
    "application",
    "engine",
    "interfaces",
    "infrastructure",
    "domain",
    "frontend/src",
    "scripts",
)


def _package_sources() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for pkg in sorted(PKG_ROOT.iterdir()):
        yaml_path = pkg / "package.yaml"
        if not yaml_path.is_file():
            continue
        text = yaml_path.read_text(encoding="utf-8")
        match = re.search(r"^source:\s*(.+)$", text, re.M)
        if match:
            rows.append((pkg.name, match.group(1).strip()))
    return rows


def test_every_package_source_path_exists():
    """每个包的 `source:`（若是路径）必须真的存在。"""
    missing: list[str] = []
    for node, raw in _package_sources():
        token = re.split(r"\s*\+\s*|\s+", raw)[0].strip()
        path_part = token.split("::")[0]
        if not path_part.endswith(_PATH_SUFFIXES):
            # 不是路径声明（如 invocation operation id）⇒ 不校验
            continue
        candidates = [
            REPO_ROOT / path_part,
            PKG_ROOT / path_part,
            PKG_ROOT / node / path_part,
        ]
        if not any(candidate.exists() for candidate in candidates):
            missing.append(f"{node} -> {raw}")
    assert not missing, f"这些提示词包的 source: 指向**不存在的**文件（假来源）：{missing}"


def test_stale_35_plus_claim_is_gone():
    """全仓不再有「35+ 模式」这个过期数字。"""
    offenders: list[str] = []
    for dirname in _SCAN_DIRS:
        base = REPO_ROOT / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".md",
                ".yaml",
                ".yml",
                ".vue",
                ".ts",
                ".json",
            }:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            if path.suffix in {".yaml", ".yml"}:
                # changelog 是**历史记录** ⇒ 里面的旧数字不许被改写
                # （改了就等于篡改历史），只扫其余行。
                text = "\n".join(
                    line
                    for line in text.splitlines()
                    if not line.lstrip().startswith("changelog:")
                )
            if "35+" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"这些地方还写着过期的「35+」：{offenders}"


def test_the_50_plus_claim_is_still_true():
    """写出来的「50+」必须仍然为真（实际模式数 ≥ 50）。

    ⇒ 以后模式被删到 50 以下时，这里会红 ⇒ 逼着人把文案一起改对
    （而不是让一个假数字长期挂在界面上）。
    """
    assert len(AI_CLICHE_PATTERNS_ENHANCED) >= 50, (
        f"扫描器只剩 {len(AI_CLICHE_PATTERNS_ENHANCED)} 条模式 ⇒ "
        "「50+ 模式」的文案已经不成立，必须一起改"
    )
    assert len(ClicheScanner().compiled_patterns) >= 50
