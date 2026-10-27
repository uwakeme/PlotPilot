"""F：反 AI 审计要查得出「同一信息在相邻段落换个说法再说一遍」。

背景（第 16 批 · F）
------------------
反 AI 审计此前只有**词面套路** 8 类（微表情/声线/比喻/生理性/句式/叙事/环境/节奏），
**查不出「复述冗余」** —— 而那正是 AI 长文最常见的注水方式
（第 15 批生产库实测三章里，人设一致但叙述注水）。

⚠ 为什么这条判据落在**提示词**里、而不是代码里：
词面判据做不出它 —— 实测 4-gram Jaccard 对目标案例只有 **0.066**（无区分度）；
实词 2-gram 包含度 **0.375**，却**低于**「设定呼应」这一假阳性的 0.5~0.8
⇒ 词面重合**分不清**「复述冗余」与「伏笔回扣」
⇒ 它是**语义判断** ⇒ 交给 LLM 通道（``anti-ai-chapter-audit``）。

⇒ 判据必须带**负向边界**，否则模型会把「设定呼应 / 伏笔回扣」误判成 AI 味
⇒ 本文件钉住：判据、负向边界、类别编号三处一致、bundle 版本，
以及**最后一跳** —— 改完 YAML 之后，**存量安装**能不能真的拿到新提示词。

⚠ 纪律：会被喂给模型的模板里**只能有给模型看的内容** ——
变更理由、issue 编号一律放 ledger / commit message ⇒ 本文件也守这一条。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
NODES = ROOT / "infrastructure" / "ai" / "prompt_packages" / "nodes"
NODE_KEY = "anti-ai-chapter-audit"
SYSTEM_MD = NODES / NODE_KEY / "system.md"
PACKAGE_YAML = NODES / NODE_KEY / "package.yaml"
META_YAML = ROOT / "infrastructure" / "ai" / "prompt_packages" / "bundle_meta.yaml"
DAG_NODE_PY = ROOT / "application" / "engine" / "dag" / "nodes" / "anti_ai_nodes.py"

#: 判据里的**负向边界**关键词 —— 少了它，模型会把「设定呼应/伏笔回扣」判成 AI 味。
NEGATIVE_BOUNDARY_MARKERS = ("递进而非复述", "伏笔回扣")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _system() -> str:
    return _read(SYSTEM_MD)


def _meta() -> dict:
    return yaml.safe_load(_read(META_YAML)) or {}


def _version_base(version: str) -> str:
    return version.split("-")[0].split("+")[0]


def _version_tuple(version: str) -> tuple[int, int, int]:
    parts = re.findall(r"\d+", version)
    if len(parts) < 3:
        return (0, 0, 0)
    return (int(parts[0]), int(parts[1]), int(parts[2]))


# ── 1) 判据本身 ────────────────────────────────────────────────────────


def test_repetition_criterion_is_in_the_audit_prompt():
    """审计提示词必须真的含「复述冗余」这一类判据。"""
    text = _system()
    assert "【I类·复述冗余】" in text, "审计提示词里没有 I类·复述冗余"
    assert "相邻段落" in text, "判据没说清「相邻」这个范围"
    assert "删掉后一次不损失任何新信息" in text, "判据没给出「冗余」的可操作定义"


def test_repetition_criterion_has_a_negative_boundary():
    """判据必须带**负向边界**，否则「设定呼应/伏笔回扣」会被误判成 AI 味。

    ⚠ 这是这条判据**能不能上线**的关键：没有边界的判据比没有判据更糟 ——
    它会把合法的叙事手法（隔段呼应、伏笔回扣）也算成 AI 味。
    """
    text = _system()
    for marker in NEGATIVE_BOUNDARY_MARKERS:
        assert marker in text, f"I 类判据缺少负向边界标记：{marker!r}"
    assert "不报" in text, "负向边界没写明「不报」"


def _declared_category_letters(text: str) -> str:
    """从提示词里抽出**声明的** category 枚举字母（不写死内容）。"""
    import re

    match = re.search(r'"category":\s*"([A-Z/]+)"', text)
    assert match, "提示词里找不到 category 枚举声明"
    return match.group(1).replace("/", "")


def test_category_enum_includes_i():
    """输出 JSON 的 category 枚举必须把 I 加进去（否则模型报不出这一类）。

    ⚠ 不写死整条枚举（实测踩过：加了 J/K 之后，写死 `"A/.../I"` 的断言直接假红
    ⇒ 断言应当只钉**它关心的那一类**在不在）。
    """
    letters = _declared_category_letters(_system())
    assert "I" in letters, f"category 枚举里没有 I（现在是 {letters}）"


# ── 2) 同一个事实散在三处 ⇒ 三处都要改 ────────────────────────────────


def _declared_category_count(text: str) -> int:
    """从文本里抽出「N 大类」这个数字（不写死 N）。"""
    import re

    match = re.search(r"(\d+)\s*大类", text)
    assert match, f"文本里找不到「N 大类」：{text[:120]!r}"
    return int(match.group(1))


def test_category_count_is_consistent_in_all_three_places():
    """「N 大类」这个事实出现在三处 ⇒ 改一处不够。

    - ``anti-ai-chapter-audit/system.md``（模型看到的那份）
    - ``anti-ai-chapter-audit/package.yaml`` 的 description（提示词广场显示）
    - ``anti_ai_nodes.py`` 的 DAG 节点描述（DAG 编辑器显示）

    ⚠ **不写死 N**（实测踩过：加了 J/K 之后，写死「9大类」的断言直接假红
    ⇒ 那是断言自己过时了，不是被测代码坏了）⇒ 改成**三处互相对齐**：
    数字一致 + 与提示词里实际声明的类别数一致。
    """
    system = _system()
    package = _read(PACKAGE_YAML)
    dag = _read(DAG_NODE_PY)

    counts = {
        "system.md": _declared_category_count(system),
        "package.yaml": _declared_category_count(package),
        "anti_ai_nodes.py": _declared_category_count(dag),
    }
    assert len(set(counts.values())) == 1, f"三处的类别数不一致：{counts}"

    declared = len(_declared_category_letters(system))
    assert counts["system.md"] == declared, (
        f"文案说 {counts['system.md']} 大类，但枚举里声明了 {declared} 个 ⇒ 文案与枚举对不上"
    )


# ── 3) bundle 版本：改了提示词必须 bump（否则存量安装不生效）──────────


def _changelog_segment(base: str) -> str:
    """**这一版**的 changelog 片段（从 ``v<base>:`` 到下一条 ``|v`` 之前）。

    ⚠ 不能用「整份 changelog 里 contains 某短语」——那个短语可能在**别的版本**
    条目里也出现（实测踩过：改标题后，断言仍被正文里的同名词命中 ⇒ 假绿）。
    ⇒ 必须**按版本切片**再断言。
    """
    changelog = str(_meta().get("changelog") or "")
    marker = f"v{base}:"
    start = changelog.find(marker)
    if start == -1:
        return ""
    rest = changelog[start:]
    nxt = rest.find("|v", len(marker))
    return rest[:nxt] if nxt != -1 else rest


def _changelog_segments() -> list[str]:
    """changelog 的**全部**版本片段（一条一个元素）。

    ⚠ 用途：断言「某一版的改动**有被记账**」时，不能只查**当前版本**那一段
    （实测踩过：I 类是 5.47.0 引入的，后来又有了 5.48.0 ⇒ 只查当前版本 ⇒ 假红）。
    ⇒ 这里给的是**全部**片段，断言自己在里面找对应那条。
    """
    changelog = str(_meta().get("changelog") or "")
    parts = changelog.split("|v")
    return [part for part in parts if part.strip()]


def test_bundle_version_was_bumped_for_this_change():
    """提示词包改了 ⇒ bundle 版本必须 bump，且机器可读的 version_nodes 要登记它。"""
    meta = _meta()
    version = str(meta.get("version") or "")
    base = _version_base(version)

    version_nodes = meta.get("version_nodes") or {}
    assert base in version_nodes, f"当前版本 {base} 在 version_nodes 里没有条目"
    assert NODE_KEY in version_nodes[base], (
        f"version_nodes[{base}] 里没有 {NODE_KEY} ⇒ "
        "「这一版改了哪些提示词包」漏了它（审计脚本会指不出来）"
    )

    # I 类是**某一版**引入的（不一定是当前版本）⇒ 扫全部版本片段找那条
    assert any("I类" in part for part in _changelog_segments()), (
        "changelog 里找不到「I类」的记账条目 ⇒ "
        "读者对不上「提示词包 ↔ 登记表条目」"
    )


def test_prompt_has_no_maintainer_facing_notes():
    """模板会被喂给模型 ⇒ 只能有给模型看的内容。

    ⚠ 变更理由、issue 编号、踩坑记录一律放 ledger / commit message。
    """
    text = _system()
    for phrase in ("登记表", "第 16 批", "Jaccard", "实测", "守卫"):
        assert phrase not in text, f"system.md 里出现了面向维护者的说明: {phrase!r}"


# ── 4) 最后一跳：改完 YAML，存量安装能不能真的拿到新提示词 ─────────────


def _old_system_text() -> str:
    """**改造前**的 system.md（把 I 类那一行去掉、编号回退）。

    用来模拟「存量安装」：它库里的 system_prompt 就是这份。
    """
    lines = [
        line
        for line in _system().splitlines()
        if not line.startswith("【I类·复述冗余】")
    ]
    old = "\n".join(lines)
    old = old.replace("涵盖9大类别", "涵盖8大类别")
    old = old.replace('"A/B/C/D/E/F/G/H/I"', '"A/B/C/D/E/F/G/H"')
    return old


@pytest.fixture
def seeded_db(tmp_path):
    """一个装好 CPMS 的临时库（seed 到当前 bundle 版本）。"""
    from infrastructure.persistence.database.connection import DatabaseConnection

    from infrastructure.ai.prompt_manager import PromptManager

    db = DatabaseConnection(str(tmp_path / "cpms.db"))
    manager = PromptManager(db)
    assert manager.ensure_seeded() is True
    return db, manager


def _active_system(manager) -> str:
    node = manager.get_node(NODE_KEY)
    assert node is not None, f"库里没有 {NODE_KEY} 这个节点"
    return node.get_active_system()


def test_seeded_install_receives_the_new_criterion(seeded_db):
    """**全新安装**：seed 之后，模型看到的那份提示词必须含 I 类判据。"""
    _db, manager = seeded_db
    assert "【I类·复述冗余】" in _active_system(manager)


def test_existing_install_gets_the_new_criterion_after_a_version_bump(seeded_db):
    """**存量安装**（库里的版本号是旧的、内容也是旧的）⇒ bump 之后必须同步到新内容。

    这条守的是本项目反复踩的那条纪律：
    「提示词 YAML 是种子源，改 YAML 不 bump 版本 ⇒ 存量安装永远拿不到新提示词」。

    模拟方式：把库里的模板版本号改回旧版、把该节点的 system_prompt 换回旧内容，
    再跑一次 ``ensure_seeded()`` ⇒ 增量同步路径必须把新内容盖回去。
    """
    db, manager = seeded_db
    conn = db.get_connection()
    old_text = _old_system_text()
    assert "【I类·复述冗余】" not in old_text, "旧内容构造失败（它不该含 I 类）"

    template = conn.execute(
        "SELECT id FROM prompt_templates WHERE is_builtin=1 LIMIT 1"
    ).fetchone()
    assert template is not None, "库里没有内置提示词模板"
    old_meta = dict(_meta())
    old_meta["version"] = "5.46.0-j02-style-preset-match"
    conn.execute(
        "UPDATE prompt_templates SET version = ?, metadata = ? WHERE id = ?",
        (old_meta["version"], json.dumps(old_meta, ensure_ascii=False), template["id"]),
    )
    node_row = conn.execute(
        "SELECT active_version_id FROM prompt_nodes WHERE node_key = ?", (NODE_KEY,)
    ).fetchone()
    assert node_row is not None
    conn.execute(
        "UPDATE prompt_versions SET system_prompt = ? WHERE id = ?",
        (old_text, node_row["active_version_id"]),
    )
    conn.commit()

    # 存量安装启动时的同步
    manager._seeded = False
    manager.ensure_seeded()

    assert "【I类·复述冗余】" in _active_system(manager), (
        "版本号 bump 了，存量安装却没拿到新提示词 ⇒ "
        "「改 YAML 不 bump 版本 ⇒ 老用户永远用不到」这条纪律被破了"
    )


def test_prompt_sync_is_content_driven_even_without_a_version_bump(seeded_db):
    """**如实记录**：内置提示词的同步是**内容驱动**的，**不是**版本号驱动。

    ⚠ 本条与 CLAUDE.md 里「改了 YAML 不 bump 版本 ⇒ 存量安装不生效」那句**相反** ——
    实测（不是推断）：``PromptManager._repair_corrupt_builtin_versions`` 在
    ``existing_version == seed_version`` 这条路径上，会把「库里正文与 YAML 不一致」
    的**内置**版本直接覆盖掉（``created_by='user'`` 的除外）⇒ 不 bump 版本也会同步。

    ⇒ 版本 bump 的真实作用是**记账**（``prompt_templates.version`` + ``version_nodes``
    + changelog）与**触发增量路径**，**不是**内容送达的前提。

    留这条守卫的目的：把**真实行为**钉住 ⇒ 以后谁再写「不 bump 就生效不了」
    （文档、注释、提交信息），这条会红；反过来，若哪天同步真的改成版本驱动，
    这条也会红 ⇒ 逼着人重新说明口径。
    """
    db, manager = seeded_db
    conn = db.get_connection()
    old_text = _old_system_text()

    # 库里的版本号 = 当前版本（= 假装没 bump），内容却是旧的
    node_row = conn.execute(
        "SELECT active_version_id FROM prompt_nodes WHERE node_key = ?", (NODE_KEY,)
    ).fetchone()
    conn.execute(
        "UPDATE prompt_versions SET system_prompt = ? WHERE id = ?",
        (old_text, node_row["active_version_id"]),
    )
    conn.commit()

    manager._seeded = False
    manager.ensure_seeded()

    assert "【I类·复述冗余】" in _active_system(manager), (
        "同版本路径没有同步内容 ⇒ 「内容驱动同步」这个口径变了，"
        "请重新核对 CLAUDE.md 与登记表里关于「bump 才生效」的表述"
    )
