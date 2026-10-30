"""第 35 批：生成画像里**不许**再出现 ``max_tokens``（死配置清理）。

⚠ 为什么（第 34 批实测 + 第 35 批核实）
----------------------------------------
``GenerationConfig.__post_init__`` 里有::

    self.max_tokens = max(int(self.max_tokens), DEFAULT_MAX_OUTPUT_TOKENS)

⇒ 任何 **< 120000** 的入参都会被**静默顶到 120000**
⇒ ⇒ 画像里原先写的 ``max_tokens``（500 / 800 / 1200 / 1500 / 2000 / 3000 / 4000 …）
**一条都没生效** ⇒ **38 个画像里 22 个是死配置**（实际全是 120000）。

⚠ 仓库**既有先例**：``timeout_seconds`` / ``retries`` 这两个「全仓无人读的键」
已经被删掉了，理由写在 ``config/generation_profiles.yaml`` 的文件头：
「**留着只会让人以为『调小它就能限』—— 上一份审查简报就信了**」
⇒ ⇒ ``max_tokens`` 是**同族**（读了又被下限顶掉 ⇒ 等价于没人读）⇒ 按同一规矩删。

⚠ 零行为变化：不设时取默认值 ``DEFAULT_MAX_OUTPUT_TOKENS``（= 120000）
⇒ 与删前**逐字相同** ⇒ 由下面第 ② 条守卫钉住。

⚠ 上限的**真实**边界是 **provider**（实测智谱 endpoint：合法范围 ``[1, 131072]``）
⇒ ⇒ 抬这个数会**让每次调用 400**，所以「想输出更多」不是这条路。

⚠ 2026-10-30 用户拍板：**地板按设计保留**（不改小、也不给「小输出」开口子）。
理由：地板是**下限**不是上限 ⇒ 传 1000000 就是 1000000（**大值永远自由**）；
120000 在当前端点合法；max_tokens 是上限不是实耗 ⇒ **成本不受影响**。
⇒ 下面第 ③ 条把「向下也生效」钉成契约，别让下一个人当成 bug 顺手修
（那会一次性改 38 处从未在真实小上限下跑过的行为）。
"""
from __future__ import annotations

import pathlib

import yaml

from domain.ai.services.llm_service import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    GenerationConfig,
)
from infrastructure.ai.generation_profiles import (
    generation_config_from_profile,
    load_generation_profiles,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PROFILES = REPO_ROOT / "config" / "generation_profiles.yaml"

#: **口径值**：domain 层给「输出上限」设的地板。
#: ⚠ 它同时是**死配置的地板** ⇒ 画像里的任何更小的值都会被顶上来。
EXPECTED_OUTPUT_BUDGET = 120000


def test_no_profile_declares_max_tokens():
    """⚠ 画像里**不许**再出现 ``max_tokens``（它是被下限顶掉的死键）。"""
    profiles = yaml.safe_load(PROFILES.read_text(encoding="utf-8")) or {}

    offenders = [
        name
        for name, profile in profiles.items()
        if isinstance(profile, dict) and "max_tokens" in profile
    ]
    assert not offenders, f"这些画像还留着读不动的 max_tokens（会被下限顶掉）：{offenders}"


def test_the_output_budget_floor_is_pinned():
    """⚠ 把「输出上限的地板」钉住 —— 改它必须是有意识的决定。

    ⚠ 真实上限由 **provider** 决定（实测智谱 endpoint：合法范围 ``[1, 131072]``）
    ⇒ ⇒ 地板**必须** ≤ 131072，否则每次调用都被 400 拒。
    """
    assert DEFAULT_MAX_OUTPUT_TOKENS == EXPECTED_OUTPUT_BUDGET


def test_the_floor_is_a_lower_bound_by_design():
    """⚠ 地板是**下限**（不是上限）—— 这是**按设计保留**的契约（2026-10-30 用户拍板）。

    ⚠ **向上永远自由**：填多少就是多少（``max(x, 120000)``）
    ⇒ 「地板会卡住上限 / 填了没生效」是误解。

    ⚠ **向下没有口子**：任何 ``max_tokens < 120000`` 的入参都被抬到 120000
    ⇒ 生产代码里 38 处调用点（张力打分 512 / 情绪账本 1500 / 节拍表 2048 /
    章末抽取 4096 / DAG 节点 1500~4000 / 沙盒 240 …）**实际发出去的都是 120000**
    ⇒ ⇒ **「限输出提速」这条杠杆在 domain 层用不了**
    （第 33 批审计提速之所以必须「关思考 + 一起封顶」就是这个原因：
    thinking 的 token 与正文**共用**同一个输出预算）。

    ⚠ 钉住它的意义：让「向下没口子」成为**有意识的契约** ——
    否则下一个人看到 38 处传小值却发 120000，会当成 bug 去「顺手修」，
    而那会一次性改 38 处**从未在真实小上限下跑过**的行为（JSON 可能被截断、
    正文可能砍半截）。
    """
    assert GenerationConfig(max_tokens=512).max_tokens == EXPECTED_OUTPUT_BUDGET
    assert GenerationConfig(max_tokens=2048).max_tokens == EXPECTED_OUTPUT_BUDGET
    assert GenerationConfig(max_tokens=4096).max_tokens == EXPECTED_OUTPUT_BUDGET

    # 向上自由：填多少就是多少
    assert GenerationConfig(max_tokens=1_000_000).max_tokens == 1_000_000


def test_every_profile_still_gets_the_same_output_budget():
    """⚠ 删掉画像里的 ``max_tokens`` 之后，**每个画像的实际输出上限逐字不变**。"""
    profiles = load_generation_profiles()

    for name in profiles:
        config = generation_config_from_profile(name)
        assert config.max_tokens == EXPECTED_OUTPUT_BUDGET, (
            f"画像 {name} 的实际输出上限变了（删配置前是 120000）"
        )
