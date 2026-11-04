"""给 bundle_meta.yaml 维护一段机器可读的 `version_nodes`。

做法：从 git 历史里**机械推导** —— 「某个时刻**生效的版本**」改了哪些 CPMS 节点包
⇒ 版本 → 节点 key 的映射。

⚠ 不靠人肉回忆（回忆会把版本号与节点配错），也不靠散文 changelog 解析。

⚠⚠ 第 83 批（N-13）修掉的三处病：
① **一次性快照**：此前 ``if "version_nodes:" in text: 跳过`` ⇒ 脚本**永远**不会更新
   ⇒ 建好之后它就成了冻结的快照 ⇒ 新的提示词改动**永远进不了**版本桶。
   ⇒ 现在：**替换**已有块 ⇒ 可重复运行（幂等）。
② **归因口径错**：此前只认「**同一笔提交**里既改了 bundle_meta 又改了节点包」
   ⇒ **没 bump 版本**的提示词改动（历史上存在 2 笔）**永远进不了任何桶**
   ⇒ 连「线上跑的是哪版提示词」都不可知。
   ⇒ 现在：按「**该提交时刻生效的版本**」归因 ⇒ 没 bump 的改动会落到它当时所在的版本，
   ⇒ 台账变回**如实**。
③ **路径写死**：``ROOT = pathlib.Path(r"D:\\WorkSpace\\projects\\PlotPilot")``
   ⇒ 换台机器就废 ⇒ 现在从脚本自身位置推导。

用法::

    python scripts/derive_bundle_version_nodes.py            # 写回 bundle_meta.yaml
    python scripts/derive_bundle_version_nodes.py --check    # 只检查，不写（守卫用）
    python scripts/derive_bundle_version_nodes.py --meta <路径>
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_META = ROOT / "infrastructure/ai/prompt_packages/bundle_meta.yaml"
META_REL = "infrastructure/ai/prompt_packages/bundle_meta.yaml"
NODES_PREFIX = "infrastructure/ai/prompt_packages/nodes/"

VERSION_RE = re.compile(r"^version:\s*(\S+)", re.M)

BLOCK_HEADER = [
    "# 机器可读：每个版本动了哪些 CPMS 节点包。",
    "#",
    "# 散文 changelog 只给人看；这一节给**审计脚本**用 ⇒",
    "# 能机械核对「提示词包 ↔ 登记表条目」的覆盖，而不是靠人读散文。",
    "#",
    "# 由 scripts/derive_bundle_version_nodes.py 从 git 历史机械推导。",
]


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def base_version(version: str) -> str:
    """只保留「纯数字」部分（排除 dev/本地版本后缀）。"""
    return version.split("-")[0].split("+")[0]


def _tip_version() -> str | None:
    """**HEAD 生效的版本** —— 给「比最后一次 bump 更新的那些提交」当归属版本。

    ⚠ 为什么需要（第 93 批）：推导是「新 → 旧」扫描
    ⇒ 在遇到**第一个**改过 bundle_meta 的提交之前，``current_version`` 是 None
    ⇒ 那几笔提交改过的节点包会被**整段静默丢掉**
    ⇒ 而「没 bump 的改动会落到它当时所在的版本」正是这个脚本的契约
    ⇒ ⇒ 用 HEAD 的版本兜底 ⇒ 不再丢。

    ⚠ 为什么取 **HEAD** 而不是工作区文件：脚本要能从**历史**重算
    （守卫 ``--check`` 跑的就是它）⇒ 口径必须只依赖 git 历史。
    """
    meta = git("show", "HEAD:%s" % META_REL)
    match = VERSION_RE.search(meta or "")
    return base_version(match.group(1)) if match else None


def derive() -> dict[str, list[str]]:
    """从 git 历史推导「版本 → 该版本期间改动的节点包」。

    ⚠ 归因口径：**该提交时刻生效的版本**（而不是「这笔提交把版本改成了什么」）
    ⇒ 忘了 bump 的改动会落到它当时所在的版本，而不是凭空消失。

    ⚠ 性能：**单趟** ``git log --name-only`` 扫全历史
    （早先每笔提交两次 ``git show`` ⇒ 上千次进程调用 ⇒ 要跑几分钟）。
    """
    meta_commits = set(git("log", "--format=%H", "--", META_REL).split())
    history = git("log", "--format=@@@%H", "--name-only")

    buckets: dict[str, list[str]] = {}
    # ⚠ 第 93 批：**先种下 HEAD 的版本** ⇒ 最新那几笔（还没 bump 的）提交
    #   改过的节点包**不会**因为「还没遇到 meta 提交」而被整段丢掉。
    current_version: str | None = _tip_version()
    commit: str | None = None
    touched: set[str] = set()

    def _flush() -> None:
        nonlocal touched, current_version
        if commit and current_version and touched:
            bucket = buckets.setdefault(current_version, [])
            for node in sorted(touched):
                if node not in bucket:
                    bucket.append(node)
        touched = set()

    for line in history.splitlines():
        if line.startswith("@@@"):
            _flush()
            commit = line[3:].strip()
            if commit in meta_commits:
                meta = git("show", f"{commit}:{META_REL}")
                match = VERSION_RE.search(meta or "")
                current_version = base_version(match.group(1)) if match else current_version
            continue
        path = line.strip()
        if not path or not path.startswith(NODES_PREFIX):
            continue
        parts = path.split("/")
        if len(parts) >= 2:
            touched.add(parts[-2])
    _flush()
    return buckets


def _ver_key(v: str) -> tuple:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:4]) or (0,)


def render(buckets: dict[str, list[str]]) -> str:
    lines = list(BLOCK_HEADER) + ["version_nodes:"]
    for version in sorted(buckets, key=_ver_key):
        lines.append(f"  {version}:")
        for node in sorted(buckets[version]):
            lines.append(f"    - {node}")
    return "\n".join(lines)


def replace_block(text: str, block: str) -> str:
    """把 ``version_nodes:`` 块**替换**成新内容（可重复运行 ⇒ 幂等）。

    ⚠ 替换区间要**连注释头一起**覆盖：
    注释头在 ``version_nodes:`` **之前** ⇒ 只替换后者 ⇒ 每次运行都会多插一份注释
    ⇒ 跑第二遍文件就不一样 ⇒ 「可重复运行」是假的。
    """
    anchor = text.find("version_nodes:")
    if anchor == -1:
        # 还没有块 ⇒ 插到 changelog 之前（没有 changelog 就追加到末尾）
        marker = "\nchangelog:"
        idx = text.find(marker)
        if idx == -1:
            return text.rstrip("\n") + "\n" + block + "\n"
        return text[:idx] + "\n" + block + text[idx:]

    # 往回吃掉紧挨着的注释行（块头）
    start = anchor
    lines_before = text[:anchor].splitlines()
    consumed = 0
    for line in reversed(lines_before):
        if line.startswith("#"):
            consumed += 1
            continue
        break
    if consumed:
        start = anchor - sum(len(line) + 1 for line in lines_before[-consumed:])

    # 块的结束 = 下一个顶层键（changelog:）或文件尾
    end = text.find("\nchangelog:", anchor)
    if end == -1:
        end = len(text)
    return text[:start] + block + text[end:]


def main(argv: list[str] | None = None) -> int:
    # ⚠ 控制台可能是 cp936（GBK）⇒ 打不出「⇒」⇒ 强制 UTF-8 + 替换
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="推导 bundle_meta.yaml 的 version_nodes")
    parser.add_argument("--meta", default=str(DEFAULT_META), help="bundle_meta.yaml 路径")
    parser.add_argument("--check", action="store_true", help="只检查，不写（退出码非 0 = 过期）")
    args = parser.parse_args(argv)

    meta_path = pathlib.Path(args.meta)
    text = meta_path.read_text(encoding="utf-8")
    block = render(derive())
    updated = replace_block(text, block)

    if args.check:
        if updated.strip() == text.strip():
            print("version_nodes 是最新的")
            return 0
        print("version_nodes 已过期 ⇒ 请跑：python scripts/derive_bundle_version_nodes.py")
        return 1

    if updated == text:
        print("version_nodes 已是最新（无改动）")
        return 0
    meta_path.write_text(updated, encoding="utf-8")
    print(f"version_nodes 已更新：{meta_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
