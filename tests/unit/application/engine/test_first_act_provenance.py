"""C-07：代码写的幕，不能在数据库里声称是 AI 规划的。

## 问题

两处幕节点的标题/描述是**代码里的字面量**，却落库为
`planning_status=CONFIRMED` + `planning_source=AI_MACRO`：

- `engine/runtime/act_planning_delegate.py` —— 守护进程动态建的首幕
  （「第一幕 · 开端」/「故事起始，建立世界观与主角目标」）
- `application/core/services/novel_service.py` —— 新书的默认第一幕
  （描述里明写「初始规划自动创建，可在结构视图中重命名」）

⇒ 数据库里声称「这是 AI 规划好的」。**落库后的假来源**，比没有来源更坏：
下游会把它当可信 provenance 消费。

## 改法

用领域模型自己的「尚未真正规划」基线：`DRAFT` + `MANUAL`。
两个字段都**无下游消费者**（前端不读；`StoryNode.is_planned()` 零调用方），
所以这是纯粹的诚实化，零行为影响。

## 尚未落地

登记表 C-07 的真正诉求是「**AI 定首幕**」—— 那需要给规划服务加一个
「没有 current_act 也能建首幕」的入口（现在只有 `create_next_act_auto`，
它靠 `current_act.number + 1` 推算幕号），属第 2 批 P1。
"""
import ast
import pathlib

import pytest

from application.core.services.novel_service import NovelService
from domain.structure.story_node import PlanningSource, PlanningStatus


def _repo_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() or (parent / "pytest.ini").exists():
            return parent
    raise RuntimeError("找不到仓库根")


ROOT = _repo_root()


def _ast_of(rel: str):
    return ast.parse((ROOT / rel).read_bytes().decode("utf-8-sig"))


def _storynode_calls(rel: str):
    """找出该文件里所有 `StoryNode(...)` 构造调用。"""
    out = []
    for node in ast.walk(_ast_of(rel)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "StoryNode":
                out.append(node)
    return out


def _kwarg(call, arg: str):
    for kw in call.keywords:
        if kw.arg == arg:
            return ast.unparse(kw.value)
    return None


# ======================================================================
#  1. 两处写入点不再声称是 AI 规划
# ======================================================================


def test_daemon_first_act_is_not_labelled_as_ai_planned():
    """守护进程建的首幕：代码写死的内容，不能标 AI_MACRO / CONFIRMED。"""
    calls = _storynode_calls("engine/runtime/act_planning_delegate.py")
    assert calls, "没找到 StoryNode 构造"
    for call in calls:
        title = _kwarg(call, "title") or ""
        if "第一幕" not in title:
            continue
        assert _kwarg(call, "planning_source") != "PlanningSource.AI_MACRO", (
            f"首幕是代码写死的，却标成 AI_MACRO（L{call.lineno}）"
        )
        assert _kwarg(call, "planning_status") != "PlanningStatus.CONFIRMED", (
            f"首幕没人确认过，却标成 CONFIRMED（L{call.lineno}）"
        )


def test_novel_default_act_is_not_labelled_as_ai_planned():
    """新书默认幕：结构占位，不能标 AI_MACRO / CONFIRMED。"""
    calls = _storynode_calls("application/core/services/novel_service.py")
    assert calls, "没找到 StoryNode 构造"
    for call in calls:
        title = _kwarg(call, "title") or ""
        if title != '"第一幕"':
            continue
        assert _kwarg(call, "planning_source") != "PlanningSource.AI_MACRO", (
            f"默认幕是系统占位，却标成 AI_MACRO（L{call.lineno}）"
        )
        assert _kwarg(call, "planning_status") != "PlanningStatus.CONFIRMED", (
            f"默认幕没人确认过，却标成 CONFIRMED（L{call.lineno}）"
        )


def test_the_two_write_points_use_the_domain_baseline():
    """DRAFT + MANUAL 就是 `StoryNode` 自己的默认基线（= 未真正规划）。"""
    assert PlanningStatus.DRAFT.value == "draft"
    assert PlanningSource.MANUAL.value == "manual"
    # 实体自身的默认值
    src = (ROOT / "domain/structure/story_node.py").read_bytes().decode("utf-8-sig")
    assert "planning_status: PlanningStatus = PlanningStatus.DRAFT" in src
    assert "planning_source: PlanningSource = PlanningSource.MANUAL" in src


# ======================================================================
#  2. 反向守卫：真正 AI 规划的幕不能被误改
# ======================================================================


def test_ai_planned_acts_keep_the_ai_source():
    """`create_next_act_auto` 造的那些幕**是** AI 规划的，别一起改掉。

    上一版修法（把整个文件里的 AI_MACRO 全换掉）会误伤这里。
    """
    src = (ROOT / "application/blueprint/services/continuous_planning_service.py").read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)
    ai_acts = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "StoryNode" and _kwarg(node, "planning_source") == "PlanningSource.AI_MACRO":
                ai_acts += 1
    assert ai_acts > 0, "AI 规划的幕的来源标注被误改了"


def test_db_constraint_still_accepts_the_value_we_write():
    """`story_nodes.planning_source` 有 CHECK 约束 —— 写的值必须在允许集里。"""
    schema = (ROOT / "infrastructure/persistence/database/schema.sql").read_bytes().decode("utf-8-sig")
    assert "CHECK(planning_source IN ('manual', 'ai_macro', 'ai_act'))" in schema
    assert PlanningSource.MANUAL.value in ("manual", "ai_macro", "ai_act")


# ======================================================================
#  3. 行为：新书默认幕真的落成 manual
# ======================================================================


def test_novel_default_act_is_persisted_as_manual(tmp_path):
    """端到端：新书自动建的第一幕，存下来是 manual / draft。"""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = (ROOT / "infrastructure/persistence/database/schema.sql").read_bytes().decode("utf-8-sig")
    wanted = []
    for stmt in schema.split(";"):
        code = "\n".join(
            ln for ln in stmt.splitlines() if not ln.lstrip().startswith("--")
        ).strip()
        head = code.upper()
        if not head.startswith("CREATE TABLE IF NOT EXISTS"):
            continue
        if " STORY_NODES (" in head or " NOVELS (" in head:
            wanted.append(code)
    conn.executescript(";\n".join(wanted) + ";")
    conn.commit()

    class _DB:
        def get_connection(self): return conn
        def fetch_one(self, sql, params=()):
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
        def fetch_all(self, sql, params=()):
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    class _Repo:
        def __init__(self, db): self.db = db; self.saved = []
        def get_tree_sync(self, novel_id): return SimpleTree()
        def save_sync(self, node): self.saved.append(node)

    class SimpleTree:
        nodes = []

    svc = NovelService.__new__(NovelService)
    svc.story_node_repository = _Repo(_DB())

    svc.ensure_default_act_for_chapters("n-1")

    assert len(svc.story_node_repository.saved) == 1, "默认幕没被创建"
    node = svc.story_node_repository.saved[0]
    assert node.planning_source == PlanningSource.MANUAL
    assert node.planning_status == PlanningStatus.DRAFT
    assert node.title == "第一幕"
