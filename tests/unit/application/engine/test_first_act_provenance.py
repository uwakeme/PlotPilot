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

## 已落地（第 3 批 · C-07）

登记表 C-07 的真正诉求是「**AI 定首幕**」—— 2026-10-21 已落地：

- 守护进程的首幕写入点**搬到了规划服务** `create_first_act_auto`
  （`application/blueprint/services/continuous_planning_service.py`），
  标题 / 描述 / 建议章数改由 **AI** 决定；
- AI 不可用 ⇒ 落**代码兜底**文案，落库来源**如实**保持 `DRAFT` / `MANUAL`
  —— **不**把兜底文案标成「AI 规划好的」（那还是落库后的假来源）；
- 守护进程自己**不再**构造幕节点。

⇒ 下面「daemon 首幕不许标 AI 规划」那条守卫随之**迁到新的写入点**：
守住的是**同一个不变式**（代码代写的内容不许声称是 AI 规划的），
不是被删掉。三个新增守卫分别盯：守护进程不再自己建幕 /
首幕分支真的调了服务 / 兜底分支的来源标注。
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


def _storynode_calls_in_function(rel: str, func_name: str):
    """找出某个函数体（含 async）内所有 `StoryNode(...)` 构造调用。"""
    out = []
    for node in ast.walk(_ast_of(rel)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name == "StoryNode":
                        out.append(sub)
    return out


def _called_names(rel: str):
    """该文件里所有**被调用**的名字（属性调用 + 名字调用）。"""
    names = set()
    for node in ast.walk(_ast_of(rel)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name:
                names.add(name)
    return names


def test_daemon_does_not_construct_act_nodes_itself():
    """守护进程不再自己建幕节点 —— 首幕交给服务（AI 决定）。

    C-07 之前这里直接 `StoryNode(title="第一幕 · 开端", ...)`，
    那是「代码代写创作内容」的源头。写入点已搬到规划服务 ⇒ 这里必须**空**。
    """
    calls = _storynode_calls("engine/runtime/act_planning_delegate.py")
    for call in calls:
        node_type = _kwarg(call, "node_type") or ""
        assert node_type != "NodeType.ACT", (
            f"守护进程又开始自己建幕节点了（L{call.lineno}）"
        )


def test_daemon_first_act_branch_really_calls_the_service():
    """首幕分支必须**真的调用**服务（AST 判调用，不判文本）。

    ⚠ 不用源码 `contains` —— 文件里的说明注释会提到这个函数名，
    文本匹配会**命中自己的注释** ⇒ 恒绿。
    """
    assert "create_first_act_auto" in _called_names(
        "engine/runtime/act_planning_delegate.py"
    ), "守护进程的首幕分支没有调用 create_first_act_auto"


def test_code_written_first_act_fallback_is_not_labelled_as_ai_planned():
    """代码写死的首幕兜底文案，**不能**标成 AI 规划。

    C-07 之后写入点搬到了 `continuous_planning_service.create_first_act_auto`：
    AI 分支走 `_create_node_from_data`（真 AI 内容 ⇒ 标 AI 来源）；
    兜底分支直接 `StoryNode(...)`（代码代写 ⇒ 必须 DRAFT / MANUAL）。
    这里盯的是**兜底分支**那个直接构造。
    """
    calls = _storynode_calls_in_function(
        "application/blueprint/services/continuous_planning_service.py",
        "create_first_act_auto",
    )
    assert calls, "首幕写入点里找不到兜底的 StoryNode 构造（守卫失效？）"
    for call in calls:
        assert _kwarg(call, "planning_source") == "PlanningSource.MANUAL", (
            f"代码代写的首幕，来源必须是 MANUAL（L{call.lineno}）"
        )
        assert _kwarg(call, "planning_status") == "PlanningStatus.DRAFT", (
            f"代码代写的首幕没人确认过，状态必须是 DRAFT（L{call.lineno}）"
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
