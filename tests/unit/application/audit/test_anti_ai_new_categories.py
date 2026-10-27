"""第 19 批：审计提示词新增 J 类（生造词）与 K 类（越界角色）+ 白名单变量。

背景
----
① **生造词**：模型会把两个词生硬拼在一起凑一个「有质感」的词（生产实测里模型自己点出过这类词），
   但反 AI 审计**没有这一类判据** ⇒ 只能靠人眼发现。
   ⚠ 风险：民国市井词、戏曲行当、方言土话、时代称谓**天生有出处** ⇒ 误伤它们
   会把「有质感」判成「没质感」⇒ **负向边界必须写进提示词**。

② **越界角色**：「禁止创造任何其他有名字的角色」这条**只在生成侧的事实锁里**；
   而设定漂移审计（A1）**只在手动审稿 API 里跑** ⇒ **自动写作的章后流程里
   没有任何一处会查它** ⇒ 现在放进**每章都跑**的反 AI 审计，结论直接进面板的风险标记。
   ⚠ 白名单必须与生成侧**同源**（否则审计比的与生成时被锁的不是一回事）。

守卫清单
--------
- 判据存在 + **负向边界**存在（缺一不可）
- 类别枚举含 J/K，且**顺序**是 A…H → I → J → K
- 节点声明了白名单变量、user 模板渲染了它
- bundle 已 bump 且 ``version_nodes`` 登记了这个节点
- **送达 DB**：seed 进临时库 ⇒ 库里那份 system/user 就是新的
- 白名单**同源**：生成侧与审计侧用**同一个** helper（不各抄一份）
- 审计调用点**真的把白名单传进去了**
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[4]
NODE_DIR = ROOT / "infrastructure/ai/prompt_packages/nodes/anti-ai-chapter-audit"
SYSTEM_MD = NODE_DIR / "system.md"
USER_MD = NODE_DIR / "user.md"
PACKAGE_YAML = NODE_DIR / "package.yaml"
META_YAML = ROOT / "infrastructure/ai/prompt_packages/bundle_meta.yaml"


def _system() -> str:
    return SYSTEM_MD.read_text(encoding="utf-8")


def _package() -> dict:
    return yaml.safe_load(PACKAGE_YAML.read_text(encoding="utf-8")) or {}


def _meta() -> dict:
    return yaml.safe_load(META_YAML.read_text(encoding="utf-8")) or {}


# ── 1) J 类：生造词 ─────────────────────────────────────────────────────


def test_the_prompt_has_the_awkward_word_criterion():
    """J 类必须在，且写清「什么才算生造词」。"""
    text = _system()
    assert "【J类·生造词】" in text, "审计提示词里没有 J 类·生造词"


def test_the_awkward_word_criterion_has_a_negative_boundary():
    """⚠ 负向边界：专名/地名/方言/行当/称谓/外来词**都不是**生造词。

    没有这条，民国市井词与戏曲行当天生有出处 ⇒ 会被大面积误判。
    """
    text = _system()
    boundary = _system()
    idx = boundary.find("【J类·生造词】")
    assert idx >= 0
    segment = boundary[idx : idx + 400]
    for marker in ("方言", "行当", "专有名词", "拿不准就不报"):
        assert marker in segment, f"J 类的负向边界缺「{marker}」"


# ── 2) K 类：越界角色 ───────────────────────────────────────────────────


def test_the_prompt_has_the_rogue_character_criterion():
    """K 类必须在。"""
    text = _system()
    assert "【K类·越界角色】" in text, "审计提示词里没有 K 类·越界角色"
    assert "白名单之外" in text, "K 类没有说清是「白名单之外的角色」"


def test_the_rogue_character_criterion_skips_when_the_whitelist_is_empty():
    """⚠ 白名单为空 ⇒ **跳过这一类**，不许臆测（降级不许编）。

    没有这条 ⇒ 没有 Bible 的书会被判出一堆「越界角色」。
    """
    text = _system()
    idx = text.find("【K类·越界角色】")
    assert idx >= 0
    segment = text[idx : idx + 400]
    assert "为空" in segment and "跳过" in segment, (
        "K 类没有写「白名单为空就跳过」⇒ 没有白名单时会臆测"
    )


# ── 3) 枚举与顺序 ───────────────────────────────────────────────────────


def test_the_category_enum_includes_j_and_k():
    """输出 JSON 的 category 枚举要把 J/K 加进去。"""
    assert '"category": "A/B/C/D/E/F/G/H/I/J/K"' in _system()


def test_the_categories_are_in_order():
    """类别顺序必须是 A…H → I → J → K（乱序会让模型先看后面的）。"""
    text = _system()
    order = [
        "【A类", "【B类", "【C类", "【D类", "【E类", "【F类", "【G类", "【H类",
        "【I类", "【J类", "【K类",
    ]
    positions = []
    for marker in order:
        idx = text.find(marker)
        assert idx >= 0, f"提示词里找不到 {marker}"
        positions.append(idx)
    assert positions == sorted(positions), f"类别顺序乱了：{list(zip(order, positions))}"


def test_each_category_appears_exactly_once():
    """每一类**只能出现一次**。

    ⚠ 实测踩过：幂等补丁脚本把 J/K 插了**两份**，
    而「顺序」那条守卫只看**第一次**出现的位置 ⇒ **没抓住**。
    ⇒ 必须单独钉住「不重复」。
    """
    text = _system()
    for marker in (
        "【A类", "【B类", "【C类", "【D类", "【E类", "【F类",
        "【G类", "【H类", "【I类", "【J类", "【K类",
    ):
        count = text.count(marker)
        assert count == 1, f"{marker} 出现了 {count} 次（应当恰好 1 次）"


# ── 4) 变量声明与模板渲染 ───────────────────────────────────────────────


def test_the_node_declares_the_whitelist_variable():
    """节点包必须声明 ``allowed_character_names``（否则渲染拿不到值）。"""
    variables = {v.get("name"): v for v in (_package().get("variables") or [])}
    assert "allowed_character_names" in variables, (
        f"package.yaml 的 variables 里没有 allowed_character_names：{sorted(variables)}"
    )
    assert variables["allowed_character_names"].get("required") is False, (
        "白名单变量应当是**可选**的（没有 Bible 的书取不到 ⇒ 空串 ⇒ 跳过该项）"
    )


def test_the_user_template_renders_the_whitelist():
    """user 模板要把白名单渲染出来（模型看不到就判不了）。"""
    user = USER_MD.read_text(encoding="utf-8")
    assert "{allowed_character_names}" in user, "user.md 没有渲染 {allowed_character_names}"
    assert "{content}" in user, "user.md 里的 {content} 不见了"


# ── 5) bundle 版本 ───────────────────────────────────────────────────────


def test_the_bundle_version_was_bumped_and_registered():
    """改了提示词包 ⇒ bundle 版本必须 bump，且 version_nodes 登记这个节点。"""
    meta = _meta()
    version = str(meta.get("version") or "")
    base = version.split("-")[0].split("+")[0]
    nums = re.findall(r"\d+", base)
    assert tuple(int(n) for n in nums[:3]) >= (5, 48, 0), f"bundle 版本没 bump（当前 {version}）"
    version_nodes = meta.get("version_nodes") or {}
    assert base in version_nodes, f"version_nodes 里没有 {base}"
    assert "anti-ai-chapter-audit" in version_nodes[base], (
        f"version_nodes[{base}] 里没有登记 anti-ai-chapter-audit"
    )
    assert f"v{base}:" in str(meta.get("changelog") or ""), f"changelog 里缺 v{base} 条目"


# ── 6) 送达 DB（seed 之后模型真的能看到） ────────────────────────────────


@pytest.fixture
def seeded_node(tmp_path):
    """把 CPMS 种子灌进一个临时库，返回那个节点。"""
    from infrastructure.persistence.database.connection import DatabaseConnection
    from infrastructure.ai.prompt_manager import PromptManager

    db = DatabaseConnection(str(tmp_path / "cpms.db"))
    manager = PromptManager(db)
    assert manager.ensure_seeded() is True
    node = manager.get_node("anti-ai-chapter-audit")
    assert node is not None
    return node


def test_the_seeded_prompt_carries_the_new_criteria(seeded_node):
    """**送达**：seed 之后，库里那份 system 就是新的（不然改了 YAML 也没用）。"""
    system = seeded_node.get_active_system() or ""
    assert "【J类·生造词】" in system, "seed 之后库里那份 system 里没有 J 类"
    assert "【K类·越界角色】" in system, "seed 之后库里那份 system 里没有 K 类"


def test_the_seeded_user_template_carries_the_whitelist_slot(seeded_node):
    """**送达**：user 模板里也得有白名单那一段（渲染才拿得到值）。"""
    user = seeded_node.get_active_user_template() or ""
    assert "{allowed_character_names}" in user or "角色白名单" in user, (
        "seed 之后 user 模板里没有白名单那段"
    )


# ── 7) 白名单**同源**（生成侧与审计侧同一份） ────────────────────────────


def test_both_sides_use_the_same_whitelist_helper():
    """生成侧与审计侧必须用**同一个** helper（此前各抄了一份 ⇒ 会悄悄分叉）。"""
    from application.engine.services.memory_engine import FactLockBuilder

    gen = inspect.getsource(FactLockBuilder._build_from_bible)
    audit = inspect.getsource(FactLockBuilder._facts_from_bible)
    assert "whitelist_lines(" in gen, "生成侧没用共享的白名单 helper"
    assert "whitelist_lines(" in audit, "审计侧没用共享的白名单 helper"
    # 且两边都不许再内联拼白名单
    for name, src in (("生成侧", gen), ("审计侧", audit)):
        assert "只可使用以下有名字的角色" not in src, (
            f"{name}仍然内联拼白名单文本 ⇒ 两份会漂"
        )


def test_the_whitelist_helper_returns_the_same_text_for_the_same_names():
    """helper 的输出必须稳定（生成侧与审计侧喂同一份名单 ⇒ 得到同一行）。"""
    from application.engine.services.memory_engine import whitelist_lines

    lines = whitelist_lines(["甲", "乙"])
    assert lines and "角色白名单" in lines[0]
    assert "甲, 乙" in lines[1]
    assert whitelist_lines([]) == []


# ── 8) 审计调用点真的把白名单传进去了 ───────────────────────────────────


def test_the_audit_invocation_passes_the_whitelist():
    """⚠ 接线：审计的 invocation 必须带上 ``allowed_character_names``。

    ⚠ 用「按函数边界切片」查（``inspect.getsource``），不是全文 contains ——
    全文 contains 会命中别的函数里的同名字符串。
    """
    from engine.runtime.audit_delegate import run_chapter_audit

    src = inspect.getsource(run_chapter_audit)
    assert "allowed_character_names" in src, (
        "审计调用点没有传 allowed_character_names ⇒ 模型看不到白名单 ⇒ K 类判不了"
    )
    assert "_allowed_character_names" in src, (
        "审计调用点没有走宿主的同源取法（_allowed_character_names）"
    )
