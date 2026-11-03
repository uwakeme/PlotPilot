# -*- coding: utf-8 -*-
"""第 89 批（N-25）：**自造 JSON 解析器**的结构守卫。

病根
----
`application/ai/llm_json_extract.parse_llm_json_to_dict` 的 docstring 写着：

    🔥 **统一入口**：所有需要从 LLM 输出解析 JSON 的地方都应使用此函数，
    不要各自造 parse_json_from_response。

但有 **19 个**服务自己手写了 ``json.loads((raw_text or "").strip())``
⇒ **不剥 ```json 围栏** ⇒ 模型一包围栏就解析失败
⇒ 该通道静默降级成「未判定 / 未评估」（**每章白烧一次模型调用**）。

生产实测（第 89 批真跑大书）：
* D-01 章节节奏判定：`章节节奏判定返回非 JSON … char 0`
* E-06 节奏配额：`节奏配额判定返回非 JSON … char 0`
  ⇒ **第 81 批「三个灰度通道在生产里真的生效」这句话，对 E-06 是空的**。

⚠ 为什么它一直没人发现：**这些通道大多一直关着** ⇒ 没被调用 ⇒ 没机会失败
⇒ ⇒ 一旦打开就等于「没打开」（每章烧一次调用、每次都降级）。

本文件钉住
----------
1. ``application/`` 下**不许**再出现「直接 json.loads 模型的 raw 输出」；
2. 共享抽取器**真的**能剥围栏（行为级 ⇒ 守卫不是空转）；
3. 抽查若干已迁移的通道：围栏包裹的 JSON **必须**解析成功。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[4]
APP_DIR = ROOT / "application"

# 「拿模型原始输出直接 json.loads」的各种写法
FORBIDDEN_PATTERNS = [
    re.compile(r"json\.loads\(\(raw_text"),
    re.compile(r"json\.loads\(raw_text"),
    re.compile(r"json\.loads\(\(raw\b"),
    re.compile(r"json\.loads\(content\)"),   # 部分服务把模型输出叫 content
]

#: **正当例外**（文件 → 理由）。例外必须写清理由，否则它就变成了「漏网」的借口。
EXEMPTED: dict[str, str] = {
    "application/world/services/worldbuilding_stream_parser.py": (
        "流式解析器：自带**三段降级链**（注入式 repair 回调 + 大括号截取 + "
        "缓冲快照兜底），换共享抽取器会改它的语义 ⇒ 属另一类实现，**不改**。"
    ),
    "application/engine/dag/nodes/anti_ai_nodes.py": (
        "DAG 是**展示层**（生产写作走 StoryPipeline）⇒ 节点的降级形态是"
        "「原样透出 raw_text」⇒ 与本守卫的规则不同 ⇒ **已登记未修**（N-25 残余）。"
    ),
    "application/engine/dag/nodes/ext_supplement_nodes.py": (
        "同上：DAG 展示层节点 ⇒ **已登记未修**（N-25 残余）。"
    ),
    "application/engine/dag/nodes/planning_nodes.py": (
        "同上：DAG 展示层节点 ⇒ **已登记未修**（N-25 残余）。"
    ),
    "application/engine/dag/nodes/review_nodes.py": (
        "同上：DAG 展示层节点 ⇒ **已登记未修**（N-25 残余）。"
    ),
}


def _offending_files() -> list[str]:
    hits = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in EXEMPTED:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                hits.append(rel)
                break
    return hits


def test_no_service_parses_the_model_output_with_a_bare_json_loads():
    """⚠ 不许再自造解析器 ⇒ 统一走 ``parse_llm_json_to_dict``。"""
    offenders = _offending_files()
    assert not offenders, (
        "这些服务又自己 json.loads 模型的原始输出（不剥 ```json 围栏 ⇒ 通道会静默失效）：\n  "
        + "\n  ".join(offenders)
    )


def test_every_exemption_has_a_reason():
    """⚠ 例外必须带理由（否则「例外」会变成漏网的借口）。"""
    for path, reason in EXEMPTED.items():
        assert reason.strip(), "例外 %s 没写理由" % path
        assert (ROOT / path).exists(), "例外的文件不存在了：%s" % path


def test_the_shared_extractor_strips_the_json_fence():
    """⚠ 共享抽取器**真的**能剥围栏（守卫不许空转）。"""
    from application.ai.llm_json_extract import parse_llm_json_to_dict

    fenced = '```json\n{"needs_buffer": false, "reason": "x"}\n```'
    data, errors = parse_llm_json_to_dict(fenced)
    assert data == {"needs_buffer": False, "reason": "x"}, (
        "共享抽取器没能剥掉 ```json 围栏 ⇒ 失败原因 %s" % errors
    )


def test_the_shared_extractor_rejects_real_garbage():
    from application.ai.llm_json_extract import parse_llm_json_to_dict

    data, errors = parse_llm_json_to_dict("这不是 JSON")
    assert data is None
    assert errors, "解析失败必须给出原因（否则日志里是空的）"


def test_the_migrated_channels_accept_a_fenced_response():
    """⚠ 抽查已迁移的通道：围栏包裹的 JSON **必须**解析成功。

    ⚠ 这些通道是「AI 参与」判定 ⇒ 解析失败 ⇒ 静默降级 ⇒ 通道等于没开。
    """
    from application.engine.services.rhythm_quota_planner import RhythmQuotaPlanner
    from application.engine.services.chapter_mode_planner import ChapterModePlanner
    from application.world.services.plot_point_classifier import PlotPointClassifier

    fenced = (
        '```json\n{"needs_buffer": false, "reason": "本章本身是爆点",'
        ' "signals": ["张力 72"]}\n```'
    )
    assert ChapterModePlanner._parse(fenced) is not None, "D-01 解析不了围栏 JSON"
    assert RhythmQuotaPlanner._parse(fenced) is not None, "E-06 解析不了围栏 JSON"
    assert PlotPointClassifier._parse(fenced) is not None, "剧情点分类解析不了围栏 JSON"
