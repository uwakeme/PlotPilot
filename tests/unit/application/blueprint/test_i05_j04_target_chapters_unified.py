"""I-05 + J-04：目标章数 —— **统一解析入口** + **创建/更新同一套约束**。

改造前
------
**I-05（口径不一）**：目标章数读不到时的兜底**散在 6 处、用了 4 个不同的默认值**
（30 / 50 / 100 / 100…）⇒ **同一本书按不同代码路径会算出不同的结构预算**。
而第 2 批 J-01 建的 ``clamp_target_chapters``（唯一的后端不变量）
**生产零调用** ⇒ 钳制层存在，却没人走。

**J-04（创建/更新两套约束）**：创建侧 ``target_chapters: Field(100, ge=0)``
**没有上限**；更新侧是 ``gt=0, le=TARGET_CHAPTERS_MAX``（J-01 加的）
⇒ **能建出一本之后任何带 target_chapters 的 PUT 都 422 的书**。

改造后
------
- ``target_budget.resolve_target_chapters`` = **唯一解析入口**：
  读不到 / 非法 ⇒ 统一默认 100；超界 ⇒ 钳到 ``TARGET_CHAPTERS_MAX``；
  6 处各写各的兜底全部改走它；
- 创建侧补上**同一个上限**（``le=TARGET_CHAPTERS_MAX``）
  ⇒ 「建得出来就一定改得动」这条不变量成立。
"""
from __future__ import annotations

import pathlib
import re

import pytest
from pydantic import ValidationError

from application.blueprint.services.target_budget import (
    DEFAULT_TARGET_CHAPTERS,
    TARGET_CHAPTERS_MAX,
    resolve_target_chapters,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: 曾经各写各的兜底的 6 个位置
SITES = (
    "engine/runtime/macro_planning_delegate.py",
    "engine/runtime/legacy_writing_delegate.py",
    "interfaces/api/v1/engine/generation.py",
    "application/blueprint/services/setup_plot_outline_continuation.py",
    "application/blueprint/services/setup_context_builder.py",
    "interfaces/api/v1/blueprint/continuous_planning_routes.py",
)

#: 旧的「各写各的」形态：**写死数字**的兜底。
#: ⚠ 只抓「or <数字>」——`novel.target_chapters or target_chapters`（回落到局部变量）
#: 是**合法**的，不该被误伤。
LEGACY_DEFAULT_RE = re.compile(r"target_chapters[^\n]*\bor\s+(30|50|100)\b")


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_bytes().decode("utf-8")


# =====================================================================
# 一、I-05：唯一解析入口
# =====================================================================


def test_the_resolution_entry_point_is_exported():
    import application.blueprint.services.target_budget as mod

    assert hasattr(mod, "resolve_target_chapters")
    assert "resolve_target_chapters" in mod.__all__
    assert "DEFAULT_TARGET_CHAPTERS" in mod.__all__


def test_the_entry_point_has_one_default():
    """读不到 / 非法 / 非正 ⇒ 同一个默认值（不再 30 / 50 / 100 各来一套）。"""
    assert DEFAULT_TARGET_CHAPTERS == 100
    assert resolve_target_chapters(None) == DEFAULT_TARGET_CHAPTERS
    assert resolve_target_chapters(0) == DEFAULT_TARGET_CHAPTERS
    assert resolve_target_chapters(-5) == DEFAULT_TARGET_CHAPTERS
    assert resolve_target_chapters("不是数字") == DEFAULT_TARGET_CHAPTERS


def test_the_entry_point_clamps_to_the_ceiling():
    assert resolve_target_chapters(10 ** 9) == TARGET_CHAPTERS_MAX
    assert resolve_target_chapters(300) == 300


@pytest.mark.parametrize("rel", SITES)
def test_no_site_keeps_its_own_fallback(rel):
    """6 处都不许再各写各的兜底 ⇒ 口径只有一处。"""
    text = _read(rel)

    hit = LEGACY_DEFAULT_RE.search(text)
    assert hit is None, f"{rel} 还留着各自的兜底：{hit.group(0)[:80] if hit else ''}"


@pytest.mark.parametrize("rel", SITES)
def test_every_site_goes_through_the_entry_point(rel):
    """每一处都改成走统一入口（否则「统一」只是嘴上说说）。

    ⚠ 查的是**调用点**（带左括号）—— 光查名字的话，
    那行 ``import ... resolve_target_chapters`` 自己就满足断言了。
    """
    assert "resolve_target_chapters(" in _read(rel), f"{rel} 没走统一解析入口"


# =====================================================================
# 二、J-04：创建 / 更新同一套约束
# =====================================================================


def test_the_create_side_has_the_same_ceiling():
    """创建侧必须和更新侧共用 ``TARGET_CHAPTERS_MAX``。"""
    from interfaces.api.v1.core.novels import CreateNovelRequest

    ok = CreateNovelRequest(
        novel_id="n1", title="书名", author="作者", target_chapters=TARGET_CHAPTERS_MAX
    )
    assert ok.target_chapters == TARGET_CHAPTERS_MAX

    with pytest.raises(ValidationError):
        CreateNovelRequest(
            novel_id="n1",
            title="书名",
            author="作者",
            target_chapters=TARGET_CHAPTERS_MAX + 1,
        )


def test_the_create_side_still_accepts_zero_for_derivation():
    """0 = 「由服务端推导」，创建侧要继续接受（体量档推导用）。"""
    from interfaces.api.v1.core.novels import CreateNovelRequest

    assert CreateNovelRequest(
        novel_id="n1", title="书名", author="作者", target_chapters=0
    ).target_chapters == 0


def test_the_update_side_has_the_same_ceiling():
    from interfaces.api.v1.core.novels import UpdateNovelRequest

    assert UpdateNovelRequest(
        target_chapters=TARGET_CHAPTERS_MAX
    ).target_chapters == TARGET_CHAPTERS_MAX

    with pytest.raises(ValidationError):
        UpdateNovelRequest(target_chapters=TARGET_CHAPTERS_MAX + 1)


def test_whatever_can_be_created_can_also_be_updated():
    """J-04 的核心不变量：**建得出来就一定改得动**。

    此前创建侧没有上限 ⇒ 能建出一本之后任何带 target_chapters 的 PUT 都 422 的书。
    """
    from interfaces.api.v1.core.novels import CreateNovelRequest, UpdateNovelRequest

    for value in (0, 1, 10, 100, 999, TARGET_CHAPTERS_MAX):
        created = CreateNovelRequest(
            novel_id="n1", title="书名", author="作者", target_chapters=value
        )
        # 0 在创建侧是「推导」，更新侧不接受 0（那是「不合法」而不是「不改」）
        if created.target_chapters == 0:
            continue
        updated = UpdateNovelRequest(target_chapters=created.target_chapters)
        assert updated.target_chapters == created.target_chapters
