"""第 30 批：**地点基准**按「本章正文里真出现的」筛（不冤枉模型 + 省 token）。

背景（生产实测，第 30 批核实）
------------------------------
审计侧（A1）的地点基准是 ``bible.locations`` **全量**（生产书 **11 条**），
而生成侧只给**本场**地点的描述：

* 实测：11 条里模型在正文提示词里**只见过 5 条**的描述
  ⇒ 另外 **6 条**模型**压根没被告知** ⇒ 拿它们判违规 = **冤枉模型**；
* 逐章实测：正文只出现 **1~5** 个地点 ⇒ 每章有 **6~10 条**事实是
  **白送进审计 prompt**的（既占 token，又制造口径差）。

⇒ 修法：审计基准的地点**只取本章正文里真出现的**
（模型没写到的地方不可能被违反 ⇒ 不该进基准）。

⚠ 已知缺口（如实登记）：**模型自造的新地点**（不在 Bible 里）仍然审不到
—— 中文地名没有分词依赖（第 22 批实测：靠模式抽具名实体召回≈0）⇒ 机检不可行。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from application.audit.services.setting_drift_audit import SettingDriftAuditService

#: 载荷：与仓库里其它用例都不同的名字（避免「碰巧命中」）
CITY = "临渊城"
HARBOR = "沉舟渡"
TEXT_WITH_CITY = f"她在{CITY}的旧码头等到天亮。"
TEXT_WITH_BOTH = f"{TEXT_WITH_CITY}风把{HARBOR}的旗子吹得猎猎响。"


def _bible():
    return SimpleNamespace(
        world_settings=[],
        locations=[
            SimpleNamespace(name=CITY, description="北境边城，城墙终年有霜", location_type="city"),
            SimpleNamespace(name=HARBOR, description="渡口，摆渡人只收旧铜钱", location_type="building"),
        ],
    )


def _service():
    svc = SettingDriftAuditService(llm_service=MagicMock(), bible_repository=MagicMock())
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    svc._load_fact_locks = MagicMock(return_value=["[FactLock] 沈砚青不得确知送票人身份"])
    return svc


def _location_facts(facts):
    return [f for f in facts if f.startswith("[地点/")]


# ── ① 正文里没出现的地点**不进**基准 ───────────────────────────────────


def test_a_location_not_in_the_chapter_is_not_a_baseline_fact():
    """⚠ 模型没写到的地方不可能被违反 ⇒ 不该进基准（更不该判它违规）。"""
    svc = _service()

    facts = svc._collect_setting_facts("n-1", 3, chapter_content=TEXT_WITH_CITY)
    locations = _location_facts(facts)

    assert locations, "正文里出现的地点必须留在基准里"
    assert not any(HARBOR in f for f in locations), (
        "本章没写到的地点仍进了基准 ⇒ 模型压根没被告知过它 ⇒ 冤枉"
    )


def test_a_location_in_the_chapter_stays_in_the_baseline():
    """⚠ 反向边界：正文里**真出现**的地点**必须**留在基准里（别把覆盖削没了）。"""
    svc = _service()

    facts = svc._collect_setting_facts("n-1", 3, chapter_content=TEXT_WITH_BOTH)
    locations = _location_facts(facts)

    assert any(CITY in f for f in locations), locations
    assert any(HARBOR in f for f in locations), locations


# ── ② 其它类基准**不受影响**（别把削范围的刀乱挥）──────────────────────


def test_the_fact_lock_baseline_is_not_filtered():
    """⚠ 筛选**只**针对地点 ⇒ FactLock 基准（模型在事实锁里见过的）一条都不许少。"""
    svc = _service()

    facts = svc._collect_setting_facts("n-1", 3, chapter_content=TEXT_WITH_CITY)

    assert any("沈砚青不得确知送票人身份" in f for f in facts), facts


def test_the_world_rule_baseline_is_not_filtered():
    """⚠ 世界规则基准（生成侧也给了）同样不许被筛。"""
    bible = _bible()
    bible.world_settings = [SimpleNamespace(category="力量体系", content="修为分九境")]
    svc = _service()
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=bible)

    facts = svc._collect_setting_facts("n-1", 3, chapter_content=TEXT_WITH_CITY)

    assert any("修为分九境" in f for f in facts), facts


# ── ③ 端到端：审计主流程传的就是**本章正文** ────────────────────────────


async def test_the_audit_chapter_scopes_the_locations_to_its_own_text():
    """⚠ 端到端：真跑 ``audit_chapter`` ⇒ 基准里只有**本章**出现的地点。

    ⚠ 用一个会记录收到的基准的假 LLM ⇒ 断言**送进 prompt 的**事实里
    没有「本章没写到的地点」。
    """
    seen: dict = {}

    class _FakeLLM:
        async def generate(self, prompt, config=None):
            seen["prompt"] = prompt
            return SimpleNamespace(
                content='{"drift_score": 0, "issues": [], "checked_facts": 0}'
            )

    svc = SettingDriftAuditService(llm_service=_FakeLLM(), bible_repository=MagicMock())
    svc._bible_repository.get_by_novel_id = MagicMock(return_value=_bible())
    svc._load_fact_locks = MagicMock(return_value=[])

    await svc.audit_chapter("n-1", 3, TEXT_WITH_CITY)

    prompt = seen.get("prompt", "")
    # ⚠ 送进 LLM 的是 `Prompt` 领域对象 ⇒ 取它的正文
    text = str(getattr(prompt, "content", "") or getattr(prompt, "text", "") or prompt)
    assert CITY in text, "正文里出现的地点没进送审基准"
    assert HARBOR not in text, "本章没写到的地点被送进了送审 prompt ⇒ 冤枉 + 白花 token"


def test_the_baseline_gets_smaller_with_the_scoping():
    """⚠ 收益：基准**条数**变少 ⇒ 送审 prompt 变小 ⇒ A1 直接提速。

    ⚠ 实测（生产书）：每章能少 **6~10 条**事实。
    """
    svc = _service()

    full = svc._collect_setting_facts("n-1", 3)
    scoped = svc._collect_setting_facts("n-1", 3, chapter_content=TEXT_WITH_CITY)

    assert len(scoped) < len(full), (len(scoped), len(full))
