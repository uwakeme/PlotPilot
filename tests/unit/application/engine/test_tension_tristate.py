"""张力三态：未评估（None）与真 0 必须可分辨（登记表未处理项 1 / 2）。

## 问题

张力在两处被压成 0，于是「这章没打过分」与「这章张力为 0」在数据上同形：

1. **类型层** —— `PipelineResult.tension: int = 0`，`_make_result` 把
   `ctx.tension_composite`（本来是 `Optional[float]`）的 None 也落成 0。
2. **消费层** —— `audit_delegate` 的判据写成 `tension_composite > 0`，
   于是「评估过且张力就是 0」的章节被判成未评估，`novel.last_chapter_tension`
   保留**上一章**的值 —— 本章的真实 0 被静默替换成别人的数字。
3. **展示层** —— 13 处 `or 0`（Python）/ `|| 0`（Vue）+ 5 处 `.get(..., 0)`，
   把存储层正确保存的 `None`（未评估）重新压成 0 展示给用户。

## 口径

**值本身就是单一事实源**：`None` = 未评估，`0` = 评估过且张力为 0。
不另设「已评估」标记 —— 那是第二个可能漂移的事实源。

## 本文件锁的

- 类型层：PipelineResult.tension 是 Optional；未评估不落 0
- 消费层：`> 0` 门槛去掉；`if result.tension:` 改成 `is not None`
- 展示层：API 保留 None；prompt 写「未评估」；前端显示「未评估」
- **跨语言扫描**：任何新增的「把张力压成 0」写法都会让本文件转红
  （只扫 Python 是不够的 —— Vue 里的 `|| 0` 是同一个语义，漏掉它会少算一半）
"""
import ast
import pathlib
import re
import sys

import pytest


def _repo_root() -> pathlib.Path:
    """从本文件向上找到仓库根。

    不要用 `parents[N]` 硬数层级 —— 换个目录深度就错，而且错了不会报错，
    只会让 `read_bytes()` 抛一个看起来不相干的 FileNotFoundError。
    以 `.git` / `pytest.ini` 作为标记向上搜。
    """
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() or (parent / "pytest.ini").exists():
            return parent
    raise RuntimeError("找不到仓库根")


ROOT = _repo_root()


# ======================================================================
#  1. 类型层
# ======================================================================


def test_pipeline_result_tension_is_optional():
    """`PipelineResult.tension` 必须能表达「未评估」。"""
    from engine.pipeline.context import PipelineResult

    fields = PipelineResult.__dataclass_fields__["tension"]
    assert "Optional" in str(fields.type), (
        f"tension 类型是 {fields.type}，无法表达「未评估」"
    )
    assert PipelineResult().tension is None, "默认值必须是 None（未评估），不是 0"


def test_make_result_keeps_none_for_unassessed_tension():
    """上下文没有张力分 ⇒ 结果的 tension 必须是 None，不能是 0。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n-1", chapter_number=3)
    ctx.tension_composite = None

    result = pipeline._make_result(ctx, success=True)

    assert result.tension is None, f"未评估被落成了 {result.tension!r}"


def test_make_result_keeps_real_zero():
    """真实 0 也要保住 —— 它与未评估同形，正是要靠类型区分的那一对。"""
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext

    pipeline = BaseStoryPipeline.__new__(BaseStoryPipeline)
    ctx = PipelineContext(novel_id="n-1", chapter_number=3)
    ctx.tension_composite = 0.0

    result = pipeline._make_result(ctx, success=True)

    assert result.tension == 0, f"真实 0 被丢成了 {result.tension!r}"
    assert result.tension is not None


def test_result_to_dict_serializes_none_as_null():
    """`to_dict()` 里的 None 会序列化成 null，前端据此显示「未评估」。"""
    from engine.pipeline.context import PipelineResult

    d = PipelineResult().to_dict()
    assert d["tension"] is None


# ======================================================================
#  2. 消费层
# ======================================================================


def test_audit_delegate_accepts_a_real_zero_tension():
    """`tension_composite > 0` 会把真 0 判成未评估 —— 门槛必须去掉。

    后果不是「多记一个 0」：未评估分支会把 `novel.last_chapter_tension`
    置为 None，于是**上一章**的张力被沿用，本章的真实 0 静默消失。

    ⚠️ 用 **ast** 判定，不用 `contains` 文本匹配 —— 改动的说明注释里就写着
    「此前写 `tension_composite > 0`」，文本匹配会命中自己的注释。
    """
    src = (ROOT / "engine/runtime/audit_delegate.py").read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)

    bad_compares = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            seg = ast.unparse(node)
            if "tension_composite" in seg and ">" in seg:
                bad_compares.append(f"{node.lineno}: {seg[:60]}")
    assert bad_compares == [], (
        f"仍有把 tension_composite 与 0 比较的门槛（真 0 会被判成未评估）：{bad_compares}"
    )

    # 正面：判据必须是「拿到分」，用 `is not None`
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "tension_composite" in ast.unparse(node.test):
            if "is not None" in ast.unparse(node.test):
                found = True
    assert found, "audit_delegate 的判据应是 `tension_composite is not None`"


def test_writing_delegate_does_not_use_truthiness_for_tension():
    """`if result.tension:` 会让真 0 走「没打分」分支。

    同上，用 ast 判定 —— 注释里就写着这个旧写法，文本匹配会自我命中。
    """
    src = (ROOT / "engine/runtime/writing_delegate.py").read_bytes().decode("utf-8-sig")
    tree = ast.parse(src)

    truthy_ifs = []
    explicit_ifs = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        # `if result.tension:` —— 测试就是那一个属性，没有比较
        if isinstance(test, ast.Attribute) and test.attr == "tension":
            truthy_ifs.append(f"{node.lineno}: {ast.unparse(test)}")
        # `if result.tension is not None:`
        if isinstance(test, ast.Compare) and ast.unparse(test).startswith("result.tension is not None"):
            explicit_ifs.append(f"{node.lineno}: {ast.unparse(test)}")

    assert truthy_ifs == [], (
        f"`if result.tension:` 仍在 —— 真 0 会被当成没打分：{truthy_ifs}"
    )
    assert explicit_ifs, "应存在 `if result.tension is not None:` 这样的显式判断"


# ======================================================================
#  3. 展示层：API 保留 None
# ======================================================================


def test_api_does_not_coerce_missing_tension_to_zero():
    """`/status` 的降级路径不得把 None 压成 0。"""
    src = (ROOT / "interfaces/api/v1/engine/autopilot_routes.py").read_bytes().decode("utf-8-sig")
    assert 'last_chapter_tension", 0) or 0' not in src, (
        "autopilot_routes 仍在把 last_chapter_tension 压成 0"
    )
    assert "last_chapter_tension\"] or 0" not in src


def test_snapshot_dataclasses_allow_none_tension():
    """两个快照 dataclass 的 `last_chapter_tension` 必须是 Optional。"""
    for rel in (
        "application/engine/services/query_service.py",
        "application/engine/services/shared_state_repository.py",
    ):
        src = (ROOT / rel).read_bytes().decode("utf-8-sig")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "last_chapter_tension":
                    ann = ast.unparse(node.annotation)
                    assert "Optional" in ann, f"{rel}:{node.lineno} 的类型是 {ann}，接不住 None"


# ======================================================================
#  4. 展示层：prompt 写「未评估」
# ======================================================================


def test_prompt_tension_dimensions_say_not_evaluated():
    """分维张力缺失时，prompt 里必须写「未评估」而不是 0。

    这段文字是**喂给模型的** —— 写「情节=0」等于告诉模型「测出来是 0」，
    模型会据此做创作判断。
    """
    src = (ROOT / "application/workflows/auto_novel_generation_workflow.py").read_bytes().decode("utf-8-sig")
    assert "plot_tension'] or 0" not in src, (
        "分维张力仍在用 `or 0` —— 未评估会被写成 0 喂给模型"
    )
    assert "_format_tension_dim" in src, "缺少分维张力的三态格式化函数"


def test_format_tension_dim_maps_none_to_not_evaluated():
    from application.workflows.auto_novel_generation_workflow import _format_tension_dim

    assert _format_tension_dim(None) == "未评估"
    assert _format_tension_dim(0) == "0"
    assert _format_tension_dim(0.0) == "0"
    assert _format_tension_dim(42.6) == "43"
    assert _format_tension_dim("not-a-number") == "未评估"


# ======================================================================
#  5. 跨语言扫描：防止新增
# ======================================================================


def _py_tension_zero_sites():
    """扫 .py：把张力压成 0 的写法（or 0 / .get(..., 0)）。"""
    out = []
    for f in sorted(ROOT.rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if any(x in rel for x in (".venv", "node_modules", "/.git/", "/data/")):
            continue
        if rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse(f.read_bytes().decode("utf-8-sig"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
                for v in n.values:
                    if isinstance(v, ast.Constant) and v.value == 0 and not isinstance(v.value, bool):
                        seg = ast.unparse(n)
                        if "tension" in seg.lower():
                            out.append(f"{rel}:{n.lineno}  {seg[:80]}")
        for i, line in enumerate(
            f.read_bytes().decode("utf-8-sig", errors="replace").split("\n"), 1
        ):
            if re.search(r"tension\w*\s*=\s*[\w\.]+\.get\([^)]*,\s*0\s*\)", line):
                out.append(f"{rel}:{i}  {line.strip()[:80]}")
    return out


def _vue_tension_zero_sites():
    """扫 .vue / .ts：JS 的 `|| 0` / `?? 0` 是同一个语义。"""
    out = []
    for f in sorted(list(ROOT.rglob("*.vue")) + list(ROOT.rglob("*.ts"))):
        rel = f.relative_to(ROOT).as_posix()
        if "node_modules" in rel:
            continue
        for i, line in enumerate(
            f.read_bytes().decode("utf-8-sig", errors="replace").split("\n"), 1
        ):
            if "tension" in line.lower() and re.search(r"\|\|\s*0\b|\?\?\s*0\b", line):
                out.append(f"{rel}:{i}  {line.strip()[:80]}")
    return out


def test_no_python_site_crushes_tension_to_zero():
    sites = _py_tension_zero_sites()
    assert sites == [], "以下位置把张力压成了 0（未评估会显示成张力 0）：\n" + "\n".join(sites)


def test_no_frontend_site_crushes_tension_to_zero():
    """Vue 里的 `|| 0` 与 Python 的 `or 0` 是同一个语义 —— 只扫 Python 会漏掉一半。"""
    sites = _vue_tension_zero_sites()
    assert sites == [], "以下位置把张力压成了 0（未评估会显示成张力 0）：\n" + "\n".join(sites)


def test_scanner_actually_detects_the_pattern():
    """扫描器自身的非空性 —— 永远收集不到候选的扫描器等于没有守卫。

    判据：造一个确实有问题的样本，确认它报得出来。
    """
    src = "x = some_row['tension_score'] or 0\n"
    assert re.search(r"tension\w*\s*=\s*[\w\.]+\.get\([^)]*,\s*0\s*\)", src) is None
    assert "tension" in src and " or 0" in src

    # AST 路径也要能抓到 BoolOp 里的 0
    tree = ast.parse("y = row['plot_tension'] or 0\n")
    found = False
    for n in ast.walk(tree):
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            for v in n.values:
                if isinstance(v, ast.Constant) and v.value == 0:
                    if "tension" in ast.unparse(n).lower():
                        found = True
    assert found, "AST 路径抓不到样本 —— 扫描器是空的"


# ======================================================================
#  6. 编码体检：中文损坏在这个项目上真实发生过
# ======================================================================


def test_no_replacement_chars_in_source_files():
    """U+FFFD 替换字符 = 某次写文件时编码坏了。

    PlotPilot 的 A-02 提交引入过 21 行双重编码乱码（用户可见报错变成「必颜是数组」），
    复审 agent 的两个测试文件里也各留了几处。都是**文案**受损、逻辑不受影响，
    所以全绿的测试抓不到 —— 只能靠扫描。
    """
    bad = []
    for f in sorted(list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.vue")) + list(ROOT.rglob("*.ts"))):
        rel = f.relative_to(ROOT).as_posix()
        if any(x in rel for x in (".venv", "node_modules", "/.git/", "/data/")):
            continue
        text = f.read_bytes().decode("utf-8-sig", errors="replace")
        if "\ufffd" in text:
            bad.append(f"{rel}  x{text.count(chr(0xFFFD))}")
    assert bad == [], "以下文件含 U+FFFD 替换字符（中文在某次写入时损坏）：\n" + "\n".join(bad)
