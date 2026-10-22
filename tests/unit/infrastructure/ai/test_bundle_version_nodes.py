"""S15：`bundle_meta.yaml` 的 `version_nodes` 必须与磁盘上的节点包对得上。

背景（第 3 批复审 S15）
----------------------
``bundle_meta.yaml`` 的 changelog 是**一整行散文**，里面不写节点 key
⇒ 「这一版改了哪些提示词包」只能靠人读散文 ⇒ **无法机械审计**
「提示词包 ↔ 登记表条目」的覆盖。

⇒ 现在 bundle_meta 里多了一段机器可读的 ``version_nodes``：
版本 → 该版本动过的 CPMS 节点包。它由
``scripts/derive_bundle_version_nodes.py`` **从 git 历史机械推导**（不靠人肉回忆）。

本文件钉住两件事：
1. ``version_nodes`` 里提到的每个节点 key **必须真实存在**（不许留陈旧引用）；
2. 每个节点 key 必须出现在 ``prompt_keys.ALL_KEYS`` 里（不许出现孤儿包）。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from infrastructure.ai.prompt_keys import ALL_KEYS

ROOT = Path(__file__).resolve().parents[4]
META = ROOT / "infrastructure/ai/prompt_packages/bundle_meta.yaml"
NODES_DIR = ROOT / "infrastructure/ai/prompt_packages/nodes"

#: **历史上被删掉**的节点包 —— 旧版本引用它们是**正常的历史事实**，
#: 不是陈旧引用。但这份名单**不许腐化**（下面有一条守卫钉住它）。
DELETED_NODES = frozenset(
    {
        "planning-main-plot-suggest",  # 旧名，5.3.x 时代
        "refactor-proposal",  # 已并入其它节点
        "autopilot-info-density-supplement",  # 已并入其它节点
    }
)


def _meta() -> dict:
    return yaml.safe_load(META.read_text(encoding="utf-8")) or {}


def _version_nodes() -> dict[str, list[str]]:
    section = _meta().get("version_nodes")
    assert isinstance(section, dict), "bundle_meta.yaml 缺 version_nodes 段"
    return {str(k): [str(x) for x in (v or [])] for k, v in section.items()}


def test_the_version_nodes_section_exists():
    """机器可读的版本 → 节点映射必须存在（散文 changelog 不能替代它）。"""
    section = _meta().get("version_nodes")
    assert isinstance(section, dict) and section, (
        "bundle_meta.yaml 缺 version_nodes 段 ⇒ 「这一版改了哪些提示词包」"
        "仍然只能靠人读散文 ⇒ 无法机械审计"
    )


def test_every_referenced_node_package_exists():
    """``version_nodes`` 里提到的每个节点包都必须真实存在。

    不许留陈旧引用（节点被删了但映射里还写着 ⇒ 审计脚本会指着一个不存在的东西）。

    ⚠ **例外**：历史上被删掉的节点包（旧版本引用它们是正常的历史事实）
    ⇒ 走 ``DELETED_NODES`` 白名单，且那份名单本身被另一条守卫钉住不许腐化。
    """
    missing = []
    for version, nodes in _version_nodes().items():
        for node in nodes:
            if node in DELETED_NODES:
                continue
            if not (NODES_DIR / node / "package.yaml").is_file():
                missing.append(f"{version} -> {node}")
    assert not missing, f"version_nodes 指向了不存在的节点包：{missing}"


def test_the_deleted_node_allowlist_has_not_rotted():
    """白名单里的每个节点**必须真的已经不在磁盘上**。

    否则有人把节点加回来（或名字写错），白名单就会变成「永久豁免」，
    把真问题盖掉。
    """
    still_there = [
        node
        for node in DELETED_NODES
        if (NODES_DIR / node / "package.yaml").is_file()
    ]
    assert not still_there, (
        f"这些节点其实还在磁盘上，不该留在 DELETED_NODES 白名单里：{still_there}"
    )


def test_every_referenced_node_is_registered_in_all_keys():
    """节点包必须在 ``prompt_keys.ALL_KEYS`` 里登记（不许孤儿包）。

    ⚠ 同上：已删除的历史节点走 ``DELETED_NODES`` 白名单。
    """
    orphans = []
    for version, nodes in _version_nodes().items():
        for node in nodes:
            if node in DELETED_NODES:
                continue
            if node not in ALL_KEYS:
                orphans.append(f"{version} -> {node}")
    assert not orphans, f"这些节点包没登记进 prompt_keys.ALL_KEYS：{orphans}"


def test_the_current_version_is_covered():
    """当前版本必须有一条目（否则「这一版改了哪些包」仍然是空的）。"""
    version = str(_meta().get("version") or "")
    base = version.split("-")[0]
    nodes = _version_nodes()
    assert base in nodes or version in nodes, (
        f"当前版本 {version} 在 version_nodes 里没有对应条目"
    )
