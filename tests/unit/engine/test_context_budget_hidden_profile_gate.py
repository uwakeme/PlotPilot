"""I-02：隐藏面的揭示门控在渲染方执行，口径与既有 POV 防火墙一致。

红线（本文件负责钉死）：
1. reveal_chapter 为 None = 总是可见；到达 reveal_chapter 当章即可见。
2. 未到揭示章必须看不见 —— 这一条防的是「提前泄密」。
3. **同一份缓存 projection 在不同章号下必须渲染出不同结果** —— 这一条防的是缓存中毒。
4. 渲染不得就地改写缓存 projection。
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from application.engine.services.context_budget_allocator import ContextBudgetAllocator

SECRET = "其实是潜伏的东厂暗桩"


class _CachedProjectionService:
    """模拟缓存命中：每次返回同一个 dict 对象，永不重建。"""

    def __init__(self, projections):
        self.projections = projections
        self.calls = 0

    def get_projection(self, novel_id, character_id):
        self.calls += 1
        return self.projections.get(character_id, {})


def _locks(reveal, hidden=SECRET):
    return {
        "context_locks": {
            "t0": "- 沈砚 | 公开面:刑部主事",
            "t1": "- 沈砚 | 状态:冷硬紧绷",
            "t2": "- 沈砚：允许过场/提及，禁止抢走主线焦点",
            "t0_hidden": f"隐藏面:{hidden}" if hidden else "",
            "t0_hidden_reveal_chapter": reveal,
        }
    }


def _plan(*slots):
    return SimpleNamespace(
        slots=[SimpleNamespace(character_id=cid, importance=imp) for cid, imp in slots]
    )


def _allocator(projections):
    return ContextBudgetAllocator(character_projection_service=_CachedProjectionService(projections))


# ── T0 主角色层 ────────────────────────────────────────────────────────────


def test_hidden_face_is_always_visible_when_reveal_chapter_is_absent():
    allocator = _allocator({"c1": _locks(reveal=None)})

    early = allocator._projection_locks_for_plan("n1", _plan(("c1", "major")), tier="t0", chapter_number=1)
    late = allocator._projection_locks_for_plan("n1", _plan(("c1", "major")), tier="t0", chapter_number=99)

    assert SECRET in early
    assert SECRET in late


def test_hidden_face_is_withheld_before_the_reveal_chapter():
    """红线 2：未到揭示章就是不能出现。"""
    allocator = _allocator({"c1": _locks(reveal=20)})

    locks = allocator._projection_locks_for_plan("n1", _plan(("c1", "major")), tier="t0", chapter_number=19)

    assert SECRET not in locks
    assert locks == "- 沈砚 | 公开面:刑部主事"


def test_hidden_face_appears_exactly_on_the_reveal_chapter():
    allocator = _allocator({"c1": _locks(reveal=20)})

    locks = allocator._projection_locks_for_plan("n1", _plan(("c1", "major")), tier="t0", chapter_number=20)

    assert SECRET in locks
    assert locks == f"- 沈砚 | 公开面:刑部主事 | 隐藏面:{SECRET}"


def test_same_cached_projection_renders_differently_per_chapter():
    """红线 3（缓存中毒）：一份缓存 projection，两个章号，两个结果。

    如果章号被烤进 compile()，这里两个输出会完全相同 —— 要么提前泄密，
    要么第 20 章之后秘密再也出不来。
    """
    service = _CachedProjectionService({"c1": _locks(reveal=20)})
    allocator = ContextBudgetAllocator(character_projection_service=service)
    plan = _plan(("c1", "major"))

    at_19 = allocator._projection_locks_for_plan("n1", plan, tier="t0", chapter_number=19)
    at_20 = allocator._projection_locks_for_plan("n1", plan, tier="t0", chapter_number=20)

    assert at_19 != at_20
    assert SECRET not in at_19
    assert SECRET in at_20
    # 两次读的是同一份缓存，不是重建出来的
    assert service.calls == 2
    assert service.projections["c1"] is service.projections["c1"]


def test_rendering_order_does_not_change_the_verdict():
    """红线 3 续：先渲染第 20 章再回第 19 章，第 19 章仍必须看不见。

    防止实现里出现「渲染过就放行」这类有状态污染。
    """
    allocator = _allocator({"c1": _locks(reveal=20)})
    plan = _plan(("c1", "major"))

    at_20 = allocator._projection_locks_for_plan("n1", plan, tier="t0", chapter_number=20)
    at_19 = allocator._projection_locks_for_plan("n1", plan, tier="t0", chapter_number=19)

    assert SECRET in at_20
    assert SECRET not in at_19


def test_rendering_does_not_mutate_the_cached_projection():
    """红线 4：渲染只读缓存，不得就地拼接。"""
    projection = _locks(reveal=20)
    snapshot = deepcopy(projection)
    allocator = _allocator({"c1": projection})
    plan = _plan(("c1", "major"))

    allocator._projection_locks_for_plan("n1", plan, tier="t0", chapter_number=20)

    assert projection == snapshot
    assert SECRET not in projection["context_locks"]["t0"]


def test_unparseable_reveal_chapter_fails_closed():
    """解析不出章号就不能当成「总是可见」—— 那是拿兜底值伪装成正常。

    注意这里断言的是**整条锁仍在、只是秘密被扣住**：如果实现让 ValueError 冒到
    `_projection_locks_for_plan` 的兜底 except，T0 会整块变成空串，光断言
    「秘密不出现」是分辨不出来的 —— 那是更严重的静默降级。
    """
    allocator = _allocator({"c1": _locks(reveal="第二十章")})

    locks = allocator._projection_locks_for_plan("n1", _plan(("c1", "major")), tier="t0", chapter_number=99)

    assert locks == "- 沈砚 | 公开面:刑部主事"


def test_gate_failure_does_not_silently_drop_the_whole_lock_block():
    """锁块本身不许因为一个坏字段整块消失（except 兜底吞异常的老毛病）。"""
    allocator = _allocator({"c1": _locks(reveal=20)})
    plan = _plan(("c1", "major"), ("c2", "major"))
    allocator.character_projection_service.projections["c2"] = {"context_locks": {"t0": "- 江沅 | 公开面:仵作"}}

    locks = allocator._projection_locks_for_plan("n1", plan, tier="t0", chapter_number=20)

    assert "- 江沅 | 公开面:仵作" in locks
    assert f"- 沈砚 | 公开面:刑部主事 | 隐藏面:{SECRET}" in locks


def test_projection_without_hidden_fields_renders_unchanged():
    """向后兼容：没有隐藏面字段的老 projection 行为不变。"""
    allocator = _allocator({"c1": {"context_locks": {"t0": "- 沈砚 | 公开面:刑部主事"}}})

    locks = allocator._projection_locks_for_plan("n1", _plan(("c1", "major")), tier="t0", chapter_number=20)

    assert locks == "- 沈砚 | 公开面:刑部主事"


# ── support 层（t1/t2）────────────────────────────────────────────────────


def test_support_tier_applies_the_same_reveal_gate():
    allocator = _allocator({"c1": _locks(reveal=20)})
    plan = _plan(("c1", "normal"))

    at_19 = allocator._projection_locks_for_plan("n1", plan, tier="support", chapter_number=19)
    at_20 = allocator._projection_locks_for_plan("n1", plan, tier="support", chapter_number=20)

    assert SECRET not in at_19
    assert at_19 == "- 沈砚 | 状态:冷硬紧绷"
    assert at_20 == f"- 沈砚 | 状态:冷硬紧绷 | 隐藏面:{SECRET}"


def test_support_tier_keeps_hidden_face_always_visible_without_reveal_chapter():
    allocator = _allocator({"c1": _locks(reveal=None)})

    locks = allocator._projection_locks_for_plan(
        "n1", _plan(("c1", "normal")), tier="support", chapter_number=3
    )

    assert locks == f"- 沈砚 | 状态:冷硬紧绷 | 隐藏面:{SECRET}"


def test_t0_tier_ignores_non_major_slots_even_when_reveal_due():
    allocator = _allocator({"c1": _locks(reveal=None)})

    locks = allocator._projection_locks_for_plan("n1", _plan(("c1", "normal")), tier="t0", chapter_number=50)

    assert locks == ""


# ── 两个调用点确实把 chapter_number 传下去了 ───────────────────────────────


class _Kernel:
    def __init__(self):
        self.seen_chapters = []

    def plan_cast(self, novel_id, chapter_number, outline="", scene_director=None):
        self.seen_chapters.append(chapter_number)
        return _plan(("c1", "major"))

    def apply_cast_plan(self, plan):
        return None

    def build_context_locks(self, novel_id, chapter_number, plan=None):
        return SimpleNamespace(t0="KERNEL-FALLBACK", t1="KERNEL-T1", t2="KERNEL-T2")


class _BibleRepo:
    def get_by_novel_id(self, novel_id):
        return SimpleNamespace(characters=[], locations=[])


def _anchored_allocator(proj_service):
    return ContextBudgetAllocator(
        bible_repository=_BibleRepo(),
        character_narrative_kernel=_Kernel(),
        character_projection_service=proj_service,
    )


def test_anchor_call_site_forwards_chapter_number_into_the_gate():
    """_get_character_anchors 把章号一路传到门控，而不是用固定值。"""
    allocator = _anchored_allocator(_CachedProjectionService({"c1": _locks(reveal=20)}))

    at_19 = allocator._get_character_anchors("n1", 19, outline="沈砚当值")
    at_20 = allocator._get_character_anchors("n1", 20, outline="沈砚当值")

    assert SECRET not in at_19
    assert SECRET in at_20
    assert at_19 != at_20


def test_state_lock_call_site_forwards_chapter_number_into_the_gate():
    """_build_character_state_lock_block 同样透传章号。"""
    allocator = _anchored_allocator(_CachedProjectionService({"c1": _locks(reveal=20)}))

    at_19 = allocator._build_character_state_lock_block("n1", 19)
    at_20 = allocator._build_character_state_lock_block("n1", 20)

    assert SECRET not in at_19
    assert SECRET in at_20
    # 投影路径优先，kernel fallback 不该出现
    assert "KERNEL-FALLBACK" not in at_20


def test_caller_without_chapter_context_fails_closed():
    """没有章号上下文的调用方按未揭示处理（缺省 0），不靠兜底值放行秘密。"""
    allocator = _allocator({"c1": _locks(reveal=20)})

    locks = allocator._projection_locks_for_plan("n1", _plan(("c1", "major")), tier="t0")

    assert SECRET not in locks
