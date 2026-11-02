# -*- coding: utf-8 -*-
"""第 83 批（N-13）：提示词包的**部署纪律**守卫。

病根（第 80 批独立验收 N-13）
----------------------------
① **2 笔提示词改动没 bump 版本**（``adadc6e`` / ``5c1da09``）
   ⇒ 「线上跑的是哪版提示词」不可知（交付说明会漏这两笔）；
② ``scripts/derive_bundle_version_nodes.py`` 里
   ``if "version_nodes:" in text: 跳过`` ⇒ 它是**一次性冻结快照**
   ⇒ 建好之后**永远**不会更新 ⇒ 新改动永远进不了版本桶；
③ **10 个节点目录从未出现在任何版本桶**。

修法
----
* 推导脚本：可**重复运行**（替换而非跳过）+ 按「**该提交时刻生效的版本**」归因
  （⇒ 忘了 bump 的改动会落到它当时所在的版本，而不是凭空消失）+ 路径可移植；
* 本守卫把纪律**钉住**：
  ① 每个节点目录都必须出现在某个版本桶（**文件级** ⇒ CI 可跑）；
  ② ``version_nodes`` 必须是最新的（跑脚本 ``--check``；浅克隆无历史 ⇒ 跳过）；
  ③ 脚本必须可移植且**幂等**（跑两遍结果一致）；
  ④ 改了节点包的提交必须同时 bump 版本（**历史级** ⇒ 浅克隆 ⇒ 跳过）。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
META = ROOT / "infrastructure" / "ai" / "prompt_packages" / "bundle_meta.yaml"
NODES_DIR = ROOT / "infrastructure" / "ai" / "prompt_packages" / "nodes"
SCRIPT = ROOT / "scripts" / "derive_bundle_version_nodes.py"

sys.path.insert(0, str(ROOT / "scripts"))

#: 纪律生效日（从这天起，改提示词包必须同时 bump 版本）。
#:
#: ⚠ 历史**不追改**（追改 = 伪造历史）⇒ 用「重新推导的映射表」如实记录。
DISCIPLINE_START = "2026-11-02"


def _node_dirs() -> set[str]:
    return {p.name for p in NODES_DIR.iterdir() if p.is_dir()}


def _buckets() -> dict[str, list[str]]:
    """从 bundle_meta.yaml 里读出版本 → 节点列表。"""
    import yaml

    data = yaml.safe_load(META.read_text(encoding="utf-8-sig")) or {}
    raw = data.get("version_nodes") or {}
    return {str(k): list(v or []) for k, v in raw.items()}


def _git_history_available() -> bool:
    """CI 是**浅克隆**（``actions/checkout`` 默认 depth=1）⇒ 历史级守卫要跳过。"""
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        count = int(proc.stdout.strip() or "0")
    except Exception:
        return False
    return count >= 50


# ── ① 每个节点目录都得在某个版本桶里（文件级 ⇒ CI 可跑）──────────────────


def test_every_node_package_is_registered_in_some_version_bucket():
    """⚠ 节点目录必须出现在 ``version_nodes`` 的某个桶里。

    ⚠ N-13 的第 ③ 条病：**10 个节点目录从未出现在任何版本桶**
    ⇒ 「这个提示词包属于哪一版」不可知 ⇒ 台账失真。
    """
    registered: set[str] = set()
    for nodes in _buckets().values():
        registered.update(nodes)

    missing = sorted(_node_dirs() - registered)
    assert not missing, (
        "这些提示词包没出现在任何 version_nodes 桶里"
        "（⇒ 改了它也不知道属于哪一版）：\n"
        + "\n".join("  - %s" % name for name in missing)
        + "\n修法：跑 python scripts/derive_bundle_version_nodes.py（它会重新推导整张表）"
    )


def test_the_version_nodes_block_is_not_empty():
    """⚠ 映射表不许是空的（空表 ⇒ 「都登记了」的假绿）。"""
    buckets = _buckets()
    assert buckets, "version_nodes 是空的 ⇒ 推导脚本没跑过"
    assert sum(len(v) for v in buckets.values()) > 0


# ── ② version_nodes 必须是最新的 ───────────────────────────────────────


@pytest.mark.skipif(not _git_history_available(), reason="浅克隆 ⇒ 没有历史可比")
def test_the_version_nodes_mapping_is_up_to_date():
    """⚠ 跑一次推导（``--check``）⇒ 映射表必须已经是最新的。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, (
        "version_nodes 已过期（脚本自己说的）：\n%s\n%s"
        % (proc.stdout.strip(), proc.stderr.strip())
        + "\n修法：跑 python scripts/derive_bundle_version_nodes.py"
    )


# ── ③ 脚本必须可移植且幂等 ─────────────────────────────────────────────


def test_the_derivation_script_is_portable():
    """⚠ 脚本不能写死绝对路径（换台机器就废 ⇒ 纪律没人执行）。"""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "D:\\WorkSpace" not in text and "D:/WorkSpace" not in text, (
        "推导脚本里写死了本机绝对路径 ⇒ 换机器就废"
    )
    assert "parents[1]" in text or "parents[2]" in text, (
        "脚本的仓库根应当从**脚本自身位置**推导"
    )


def test_the_derivation_script_is_idempotent():
    """⚠ 跑两遍 ⇒ 结果必须一致（否则「可重复运行」是假的 ⇒ 映射表会漂）。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import derive_bundle_version_nodes as d

    text = META.read_text(encoding="utf-8-sig")
    block = d.render(d.derive())
    once = d.replace_block(text, block)
    twice = d.replace_block(once, block)
    assert once == twice, "跑第二遍结果就变了 ⇒ 脚本不幂等"
    assert once.strip() == text.strip(), (
        "推导结果与文件里的内容不一致 ⇒ version_nodes 已过期"
        "（跑 python scripts/derive_bundle_version_nodes.py）"
    )


def test_the_script_actually_repairs_a_stale_block(tmp_path):
    """⚠ 脚本必须**真的改写**过期的块（不是「已有块就跳过」）。

    ⚠ 第 83 批变异验证（M01 打不红）：只测 ``replace_block`` 这个**函数**
      ⇒ 脚本的 ``main()`` 退回「一次性快照」也没人发现
      ⇒ 这里**真跑一遍脚本**（对临时文件），并断言过期块被**改回来**。
    """
    source = META.read_text(encoding="utf-8-sig")
    broken = source.replace("    - chapter-state-extraction\n", "", 1)
    assert broken != source, "没造出「过期的块」⇒ 本用例前提失效"

    target = tmp_path / "bundle_meta.yaml"
    target.write_text(broken, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--meta", str(target)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, "脚本跑失败：%s" % proc.stderr.strip()

    repaired = target.read_text(encoding="utf-8")
    assert "    - chapter-state-extraction\n" in repaired, (
        "脚本没把过期的 version_nodes 块改回来 ⇒ 它仍然是**一次性冻结快照**"
    )
    assert repaired.strip() == source.strip(), (
        "脚本改写后的内容与仓库里的不一致 ⇒ 推导口径漂了"
    )


# ── ④ 改了节点包就必须 bump 版本（历史级）──────────────────────────────


@pytest.mark.skipif(not _git_history_available(), reason="浅克隆 ⇒ 没有历史可比")
def test_a_prompt_change_always_bumps_the_bundle_version():
    """⚠ 改了 ``prompt_packages/nodes/`` 的提交**必须**同时改 ``bundle_meta.yaml``。

    ⚠ N-13 的第 ① 条病：历史上存在 **20+ 笔**只改提示词、没 bump 版本的提交
    ⇒ 「线上跑的是哪版提示词」不可知。
    ⇒ **历史不追改**（追改等于伪造历史），而是靠「重新推导的映射表」如实记录
    （那个表现在按「该提交时刻生效的版本」归因 ⇒ 漏 bump 的改动会落到当时的版本）。
    ⇒ 这条守卫只管**从纪律生效日起**的新提交。
    """
    proc = subprocess.run(
        [
            "git", "log", "--since=" + DISCIPLINE_START, "--format=%H", "--name-only",
            "--", "infrastructure/ai/prompt_packages/nodes/",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    commits = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line and not line.startswith(" ") and len(line.strip()) == 40
    ]

    offenders = []
    for commit in commits:
        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", commit],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.split()
        touched_nodes = any(
            f.startswith("infrastructure/ai/prompt_packages/nodes/") for f in files
        )
        touched_meta = "infrastructure/ai/prompt_packages/bundle_meta.yaml" in files
        if touched_nodes and not touched_meta:
            offenders.append(commit[:12])

    assert not offenders, (
        "这些提交改了提示词包却没 bump bundle 版本"
        "（⇒ 线上跑的是哪版提示词不可知）：\n"
        + "\n".join("  - %s" % c for c in offenders[:20])
        + "\n修法：同一笔提交里改 bundle_meta.yaml 的 version（并写 changelog）"
    )
