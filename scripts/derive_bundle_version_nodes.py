"""S15：给 bundle_meta.yaml 加一段机器可读的 `version_nodes`。

做法：从 git 历史里**机械推导** —— 每个改过 bundle_meta.yaml 的提交，
它在同一笔里动了哪些 CPMS 节点包 ⇒ 版本 → 节点 key 的映射。

⚠ 不靠人肉回忆（回忆会把版本号与节点配错），也不靠散文 changelog 解析。
"""
from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(r"D:\WorkSpace\projects\PlotPilot")
META = ROOT / "infrastructure/ai/prompt_packages/bundle_meta.yaml"
NODES_DIR = ROOT / "infrastructure/ai/prompt_packages/nodes"

VERSION_RE = re.compile(r"^version:\s*(\S+)", re.M)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout


def main() -> None:
    # 1) 找所有改过 bundle_meta.yaml 的提交（新→旧）
    log = git("log", "--format=%H", "--", "infrastructure/ai/prompt_packages/bundle_meta.yaml")
    commits = [c for c in log.split() if c]

    mapping: dict[str, list[str]] = {}
    for commit in commits:
        # 该提交**之前**的版本号
        prev = git("show", f"{commit}~1:infrastructure/ai/prompt_packages/bundle_meta.yaml")
        m_prev = VERSION_RE.search(prev)
        version = m_prev.group(1) if m_prev else None
        # 该提交之后的版本号
        cur = git("show", f"{commit}:infrastructure/ai/prompt_packages/bundle_meta.yaml")
        m_cur = VERSION_RE.search(cur)
        new_version = m_cur.group(1) if m_cur else version
        if not new_version:
            continue
        # 该提交动了哪些节点包
        files = git("show", "--name-only", "--format=", commit).split()
        touched = sorted(
            {
                f.split("/")[-2]
                for f in files
                if f.startswith("infrastructure/ai/prompt_packages/nodes/")
                and len(f.split("/")) >= 2
            }
        )
        if not touched:
            continue
        bucket = mapping.setdefault(new_version, [])
        for node in touched:
            if node not in bucket:
                bucket.append(node)

    # 只保留「版本号是纯数字开头」的条目（排除 dev/本地版本）
    def base_version(v: str) -> str:
        return v.split("-")[0].split("+")[0]

    merged: dict[str, list[str]] = {}
    for version, nodes in sorted(mapping.items(), key=lambda kv: _ver_key(kv[0])):
        merged.setdefault(base_version(version), [])
        for node in nodes:
            if node not in merged[base_version(version)]:
                merged[base_version(version)].append(node)

    lines = ["", "# 机器可读：每个版本动了哪些 CPMS 节点包。", "#",
             "# 散文 changelog 只给人看；这一节给**审计脚本**用 ⇒",
             "# 能机械核对「提示词包 ↔ 登记表条目」的覆盖，而不是靠人读散文。",
             "#",
             "# 由 scripts/derive_bundle_version_nodes.py 从 git 历史机械推导。",
             "version_nodes:"]
    for version, nodes in merged.items():
        lines.append(f"  {version}:")
        for node in nodes:
            lines.append(f"    - {node}")

    text = META.read_text(encoding="utf-8")
    if "version_nodes:" in text:
        print("version_nodes 已存在，跳过")
        return
    # 插到 changelog 之前（changelog 是最后一行大散文）
    marker = "\nchangelog:"
    idx = text.find(marker)
    if idx == -1:
        META.write_text(text.rstrip("\n") + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    else:
        text = text[:idx] + "\n".join(lines) + text[idx:]
        META.write_text(text, encoding="utf-8")
    print(f"version_nodes 已写入：{len(merged)} 个版本")
    for version, nodes in list(merged.items())[-4:]:
        print(f"  {version}: {nodes}")


def _ver_key(v: str) -> tuple:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:4]) or (0,)


if __name__ == "__main__":
    main()
