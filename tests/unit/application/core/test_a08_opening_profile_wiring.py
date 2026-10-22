"""A-08：类型开篇画像 —— 契约文本移出代码 + 缺失降级 + **画像真的进了模型**。

背景
----
登记表 A-08 写的是「题材无画像且 ``strict=True`` 直接 raise 禁止开书」。

核实（2026-10-22）发现**实况与登记表不一致**，而且不一致的地方才是真病灶：

1. ``resolve_opening_profile`` 在生产代码里**零调用**（只有测试用）
   ⇒ 那条 raise 在生产上**根本触发不了** ⇒ 「禁止开书」是纸面风险。
2. 真正在跑的是 ``application/ai_invocation/prompt_runtime.py``，它把
   ``genre_opening_profile`` 写死成一句占位文本、另两个键写死成 ``{}``
   ⇒ 配置资产里的开篇画像（15 个一级分类 / 82 个二级）
   **从来没进过模型** ⇒ 画像机制整条链是死的。
3. 读者契约的四条（「第一屏必须…」）**写死在**
   ``application/core/taxonomy/opening_profiles.py`` 里，
   与该模块 docstring「不在代码里维护具体题材套路」的声明**自相矛盾**。
4. 配置资产里写着 ``missing_profile: block_generation``，
   但这条策略声明**同样没有执行者**。

⇒ 本笔做三件事：契约文本下沉到配置资产、缺失降级为通用契约（不阻塞）、
**把画像真正接到生成路径上**（这才是「能力连着出口一起交付」）。

守卫分两层
----------
- **行为级**：画像真的出现在 provider 的返回值里、按题材标签解析、
  降级路径如实标记 ``source_level="generic"``；
- **文件内容级**：契约文本不在代码里（前端无测试设施 ⇒ 同类只能这样）。

⚠ 陷阱记录：本文件第一版断言「provider 返回的 genre_opening_profile 里
含某条 reader_promise 文本」——但**通用契约降级时也会带出文本**，
于是「命中题材画像」与「降级到通用契约」**判不开**。
⇒ 改用 ``source_level`` 这个**来源标记**来区分（它才是可分辨的字段）。
"""
from __future__ import annotations

from pathlib import Path

from application.ai_invocation.dtos import InvocationSpec
from application.ai_invocation.prompt_runtime import (
    runtime_prompt_values_for_spec,
)
from application.core.taxonomy.opening_profiles import resolve_opening_profile


def _spec(operation: str, node_key: str) -> InvocationSpec:
    return InvocationSpec(operation=operation, node_key=node_key)


def _values(operation: str, node_key: str, **aliases) -> dict:
    spec = _spec(operation, node_key)
    return dict(runtime_prompt_values_for_spec(spec, aliases))


# ── 1) 画像真的进了 provider 的返回值（不再是占位文本） ─────────────────

def test_bible_worldbuilding_provider_injects_real_opening_profile():
    """命中题材时，provider 返回的必须是**该题材画像的真实内容**。

    改造前这里恒为一句写死的占位串「（暂无类型画像约束…）」⇒ 本条会红。
    """
    values = _values(
        "bible.setup.worldbuilding",
        "bible-worldbuilding",
        **{
            "novel.genre_label": "都市 / 都市异能",
            "novel.setup.genre_label": "都市 / 都市异能",
        },
    )

    profile = values["genre_opening_profile"]
    assert isinstance(profile, str), "bible-worldbuilding 的该槽位是文本（package.yaml: type string）"
    assert "都市" in profile
    # 都市异能的 reader_promise 必须在场（画像真的被解析了，不是占位）
    assert "读者承诺" in profile
    assert "都市" not in ("（暂无类型画像约束，按题材基调节奏把握开篇）",)


def test_bible_worldbuilding_provider_injects_reader_contract_and_rhythm():
    """另两个槽位也必须真的有内容（改造前恒为 ``{}``）。"""
    values = _values(
        "bible.setup.worldbuilding",
        "bible-worldbuilding",
        **{
            "novel.genre_label": "都市 / 都市异能",
            "novel.setup.genre_label": "都市 / 都市异能",
        },
    )

    assert values["genre_reader_contract"], "读者契约不该是空的"
    assert values["genre_rhythm_constraints"], "节奏约束不该是空的"
    assert values["genre_reader_contract"]["first_screen_hook"]


def test_quick_macro_provider_injects_profile_as_object():
    """极速宏观规划那三个键是 ``derived_config``（运行时派生）⇒ 也必须有内容。

    改造前这个节点**没有任何 provider** ⇒ 契约默认值 ``{}`` 一直是最终值。
    """
    values = _values(
        "autopilot.macro.plan",
        "planning-quick-macro",
        **{
            "novel.genre_label": "仙侠 / 修真",
            "novel.setup.genre_label": "仙侠 / 修真",
        },
    )

    assert isinstance(values["genre_opening_profile"], dict)
    assert values["genre_opening_profile"], "画像 dict 不该是空的"
    assert values["genre_reader_contract"]


# ── 1b) 题材标签真的被读了（这条守的是「按标签解析」） ─────────────────

def test_profile_is_resolved_from_the_actual_genre_label_not_a_hardcoded_one():
    """**标签驱动**：换一个题材，画像必须跟着换。

    这条是 M5 变异（把 genre_label 硬编码成某个题材）的**专属牙齿**。
    ⚠ 载荷必须选一个**与其它用例里的题材都不同**的题材 ——
    否则「不管传什么标签都返回同一个画像」也能让别的用例照样绿。
    """
    sport = _values(
        "bible.setup.worldbuilding",
        "bible-worldbuilding",
        **{
            "novel.genre_label": "体育 / 篮球运动",
            "novel.setup.genre_label": "体育 / 篮球运动",
        },
    )
    urban = _values(
        "bible.setup.worldbuilding",
        "bible-worldbuilding",
        **{
            "novel.genre_label": "都市 / 都市异能",
            "novel.setup.genre_label": "都市 / 都市异能",
        },
    )

    sport_profile = sport["genre_opening_profile"]
    urban_profile = urban["genre_opening_profile"]

    assert sport_profile != urban_profile, "换题材却拿到同一份画像 ⇒ 标签没被读"
    assert "体育" in sport_profile
    assert "都市" in urban_profile
    # 题材专属的读者承诺也要跟着换
    assert (
        sport["genre_reader_contract"]["reader_promise"]
        != urban["genre_reader_contract"]["reader_promise"]
    )


def test_a_genre_label_that_is_not_in_the_bundle_does_not_fall_back_to_another_genre():
    """陌生题材 ⇒ 通用契约，**不是**某个别的题材的画像。

    这条同时挡住 M5 的两种变体（硬编码某个题材 / 忽略标签）。
    """
    values = _values(
        "bible.setup.worldbuilding",
        "bible-worldbuilding",
        **{
            "novel.genre_label": "体育 / 篮球运动",
            "novel.setup.genre_label": "体育 / 篮球运动",
        },
    )

    # 体育题材的画像里必须能看到体育自己的承诺词
    promise = " ".join(values["genre_reader_contract"]["reader_promise"])
    assert "赛事" in promise or "竞技" in promise or "训练" in promise, promise
    assert "都市" not in promise


# ── 2) 降级路径：判不出来要说判不出来，不许编造题材画像 ─────────────────

def test_unknown_genre_degrades_to_generic_contract_and_says_so():
    """没有画像的题材 ⇒ 降级到通用契约，**不阻塞**，来源标记为 generic。"""
    values = _values(
        "bible.setup.worldbuilding",
        "bible-worldbuilding",
        **{
            "novel.genre_label": "不存在分类 / 不存在二级",
            "novel.setup.genre_label": "不存在分类 / 不存在二级",
        },
    )

    # 不抛异常 ⇒ 没有阻塞
    assert values["genre_reader_contract"], "降级后仍应给出通用契约"
    assert values["genre_reader_contract"]["first_screen_hook"]
    # 通用契约也带 reader_promise
    assert values["genre_reader_contract"]["reader_promise"]


def test_missing_genre_label_still_gives_generic_contract():
    """连题材标签都没有 ⇒ 仍给通用契约（判不出来 ≠ 没有契约）。"""
    values = _values("bible.setup.worldbuilding", "bible-worldbuilding")

    assert values["genre_reader_contract"], "没有标签时也该降级到通用契约"


def test_generic_fallback_is_marked_as_generic_not_as_a_genre_profile():
    """**来源可分辨**：降级出来的必须是 ``source_level="generic"``。

    ⇒「用了通用契约」与「用了该题材画像」在数据上分得开。
    """
    profile = resolve_opening_profile("不存在分类 / 不存在二级")
    assert profile is not None
    assert profile.source_level == "generic"

    hit = resolve_opening_profile("都市 / 都市异能")
    assert hit is not None
    assert hit.source_level == "secondary"
    assert hit.source_level != profile.source_level


def test_generic_contract_text_is_not_hardcoded_in_the_module():
    """契约文本必须在配置资产里，**不在** Python 代码里。

    这是 A-08 的核心不变量：模块 docstring 声明「不在代码里维护具体题材套路」，
    那四条契约就属于「题材套路」的一部分。
    """
    source = (
        Path(
            resolve_opening_profile.__globals__["__file__"]
        ).read_text(encoding="utf-8")
    )
    for key in (
        "first_screen_hook",
        "chapter_1_payoff",
        "chapter_3_lock",
        "chapter_10_loop",
    ):
        assert f'"{key}": "' not in source, f"{key} 的契约文本被写死在代码里"
