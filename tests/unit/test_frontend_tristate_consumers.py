"""第 66 批守卫：B-6 —— 前端消费方必须能分辨 ``null``（三态不许被折叠）。

背景（独立审查报告 ``review-briefs/2026-10-30-review-report-85c0f726-closeout.md`` B-6）
------------------------------------------------------------------------------
契约早就是三态（``boolean | null``），但**不在 diff 里**的消费方把 null 折叠了：

- ``WorkArea.vue``  ``sync: !!r.narrative_sync_ok``
  ⇒ null 变 false ⇒ 渲染出黄标「叙事未同步」
  ⇒ 「没测出来」被说成「测出失败」
  （**同一个 computed 里紧挨着的上一行**对 ``tension`` 用了完全正确的写法 ⇒ 疏漏而非设计）。
- ``autopilotStatus.ts``  ``audit.narrative_sync_ok === true ? '1' : '0'``
  ⇒ 响应式指纹把 null 折叠成 ``'0'`` ⇒ ``null↔false`` 的切换**不改变指纹**
  ⇒ ``WorkArea.vue`` 只在指纹变化时刷新 ⇒ **徽标长期陈旧**。

根因：**改的是「值的形状」，受影响的是「所有读它的地方」** ⇒
审查范围 = grep 出来的全部消费方，**不是** diff。

本文件钉住
----------
1. 两个已知消费方的写法（静态断言，锚到确切语句）；
2. **行为级**：真跑前端的指纹函数，断言 true/false/null 产生**三个不同**的指纹；
3. **通则**：前端任何地方都不许再用「折叠器」读这两个三态字段。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
WORKAREA = FRONTEND / "src" / "components" / "workbench" / "WorkArea.vue"
AUTOPILOT_STATUS = FRONTEND / "src" / "workbench" / "autopilotStatus.ts"

#: 三态字段（bool | null）。⚠ 只收这两个 —— ``tension`` 是数值三态，
#:   且在很多名字里出现（``last_chapter_tension`` 等）⇒ 全仓扫它会误报。
TRISTATE_FIELDS = ("narrative_sync_ok", "drift_alert")

#: 折叠器：这些写法会把 ``null`` 变成 ``false``/``true`` ⇒ 三态塌成两态。
#:
#: ⚠ 第 66 批变异验证发现的洞：第一版用「``!!`` 紧贴字段名」匹配，
#:   而真实写法是 ``!!r.narrative_sync_ok``（中间还有 ``r.``）⇒ 匹配不到
#:   ⇒ 变异打在那儿时守卫**不会红** ⇒ 现在改成**正则**（容忍中间任意表达式）。
FOLD_PATTERNS = (
    re.compile(r"!![^,\n]*\b(narrative_sync_ok|drift_alert)\b"),
    re.compile(r"Boolean\([^)\n]*\b(narrative_sync_ok|drift_alert)\b"),
    re.compile(r"\b(narrative_sync_ok|drift_alert)\s*\?\?\s*(false|true)\b"),
)


# ─────────────────────── 1) 已知消费方（静态，锚到确切语句） ───────────────────────


def test_the_signal_strip_keeps_the_three_states():
    """⚠ 信号条不许把 null 折叠成 false。"""
    src = WORKAREA.read_text(encoding="utf-8")

    assert "!!r.narrative_sync_ok" not in src, (
        "WorkArea.vue 又把 null 折叠成 false ⇒ 「未评估」被渲染成「叙事未同步」"
    )
    assert "sync: r.narrative_sync_ok ?? null" in src, (
        "信号条没有保留三态（null = 未评估）"
    )
    # 模板必须**分别**处理 false（没同步）与 null（未评估）。
    assert 'v-if="signalStrip.sync === false"' in src, "模板没有区分「没同步」"
    assert 'v-else-if="signalStrip.sync === null"' in src, (
        "模板没有区分「未评估」⇒ null 会静默什么都不显示"
    )
    assert "叙事未评估" in src, "「未评估」这个状态在界面上没有出口"


def test_the_reactive_fingerprint_keeps_the_three_states():
    """⚠ 指纹不许把 null 折叠成 '0'。"""
    src = AUTOPILOT_STATUS.read_text(encoding="utf-8")

    assert "audit.narrative_sync_ok === true ? '1' : '0'" not in src, (
        "指纹把 null 折叠成 '0' ⇒ null↔false 不触发重渲染 ⇒ 徽标长期陈旧"
    )
    assert "audit.drift_alert === true ? '1' : '0'" not in src, (
        "漂移告警的指纹同样把 null 折叠了"
    )
    assert "triState(audit.narrative_sync_ok)" in src, "指纹没用三态标记函数"
    assert "triState(audit.drift_alert)" in src, "漂移告警的指纹没用三态标记函数"
    # 三态标记函数本身必须把 null/undefined 归到**第三个**标记。
    assert "return '2'" in src, "三态标记函数没有为「未评估」保留独立标记"


# ─────────────────────── 2) 行为级：真跑前端指纹函数 ───────────────────────


def test_the_fingerprint_distinguishes_true_false_and_null(tmp_path):
    """⚠ **行为级**：真跑前端的指纹函数，断言三态产生**三个不同**的指纹。

    ⚠ 为什么非得用 node：这条守卫要钉的是「null 与 false 的指纹**不同**」，
      而那正是 bug 的**表现**（折叠后两者相同）⇒ 只看源码形状不够。

    ⚠ node 不在 PATH 时**跳过**（不是失败）：CI 只跑 pytest；
      这时上面前两条静态守卫仍然生效。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不在 PATH ⇒ 行为级前端守卫无法运行（静态守卫仍然有效）")

    module_url = AUTOPILOT_STATUS.resolve().as_uri()
    probe = tmp_path / "tristate_probe.mjs"
    probe.write_text(
        """
const mod = await import(MODULE_URL)
const base = {
  autopilot_status: 'running',
  current_stage: 'writing',
  current_chapter_number: 3,
  completed_chapters: 2,
  manuscript_chapters: 2,
  current_beat_index: 0,
  total_beats: 0,
  planned_micro_beats: [],
  outline_plan_mode: '',
  writing_substep: '',
  writing_substep_label: '',
  accumulated_words: 100,
  beat_phase: '',
  beat_focus: '',
  beat_target_words: 0,
  chapter_target_words: 0,
  beat_remaining_budget: 0,
  beat_max_words_hint: 0,
}
const fp = (value) => mod.buildAutopilotReactiveFingerprint({
  ...base,
  last_chapter_audit: {
    chapter_number: 3,
    tension: 70,
    narrative_sync_ok: value,
    similarity_score: 0.9,
    at: '2026-10-30T00:00:00',
    drift_alert: false,
  },
})
process.stdout.write(JSON.stringify({ t: fp(true), f: fp(false), n: fp(null) }))
""".replace("MODULE_URL", json.dumps(module_url)),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [node, "--experimental-strip-types", str(probe)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(FRONTEND),
    )
    assert proc.returncode == 0, (
        f"node 跑前端指纹函数失败（守卫前提失效）：\n{proc.stdout}\n{proc.stderr}"
    )

    fingerprints = json.loads(proc.stdout)
    assert len(set(fingerprints.values())) == 3, (
        f"三态被折叠成两态了（指纹相同）：{fingerprints}"
    )
    assert fingerprints["n"] != fingerprints["f"], (
        "null 与 false 的指纹相同 ⇒ 切到「未评估」不会触发重渲染"
    )


# ─────────────────────── 3) 通则：前端任何地方都不许用折叠器 ───────────────────────


def _frontend_sources() -> list[Path]:
    files: list[Path] = []
    for pattern in ("**/*.ts", "**/*.vue"):
        files.extend((FRONTEND / "src").glob(pattern))
    return sorted(files)


def test_no_frontend_consumer_folds_the_tristate_fields():
    """⚠ 通则守卫：前端**任何**文件都不许用折叠器读这两个三态字段。

    ⚠ 为什么是通则：契约升级（bool → bool | null）影响的是**所有读它的地方**，
      而代码审查通常只看 diff ⇒ 新加一个消费方很容易再折叠一次。
    """
    offenders: list[str] = []
    for path in _frontend_sources():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if not any(field in line for field in TRISTATE_FIELDS):
                continue
            # 类型声明（``field?: boolean | null``）不是「读值」⇒ 放行。
            if "boolean | null" in line or "boolean|null" in line:
                continue
            for pattern in FOLD_PATTERNS:
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
                    break

    assert not offenders, (
        "前端又出现了折叠三态的写法（null ⇒ false/true）：\n  " + "\n  ".join(offenders)
    )
