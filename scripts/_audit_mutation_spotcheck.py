"""一次性审计脚本（不进 CI）：**亲手做变异验证**抽查守卫是不是真的。

为什么自己做
------------
台账声称「每个改造条目至少一条变异测试 —— 改坏实现 ⇒ 用例转红」。
规格也把这条列为硬原则 3（可验）。
⇒ 本脚本独立打三条变异，看对应守卫**会不会红**：
    * **不红** ⇒ 守卫是空洞的（台账的承诺落空）
    * **红**   ⇒ 守卫真的锚住了行为

⚠ 每条变异结束后都用 ``git checkout --`` 还原被改的文件
   ⇒ 工作区不会留下任何污染。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

#: (编号, 目标文件, 原代码片段, 变异后片段, 守卫测试文件, 说明)
MUTATIONS = [
    (
        "M1 / B-02",
        "application/blueprint/services/continuous_planning_service.py",
        '                node["estimated_chapters"] = max(int(estimated), 0)',
        '                pass  # MUTATION: 忽略 AI 给的章数（部级章数退回公式兜底）',
        "tests/unit/application/blueprint/test_b02_part_chapters_by_ai.py",
        "AI 给的部级章数不再覆盖公式兜底",
    ),
    (
        "M2 / A-02",
        "application/blueprint/services/setup_plot_outline_continuation.py",
        '        chapter_end = _coerce_chapter_number(raw.get("chapter_end")) or default_end',
        '        chapter_end = default_end  # MUTATION: 忽略 AI 给的断点',
        "tests/unit/application/blueprint/test_stage_plan_ai_ownership.py",
        "AI 给的阶段断点不再生效（退回代码比例表）",
    ),
    (
        "M3 / X-02 总闸",
        "application/engine/services/ai_participation_settings.py",
        "    if not settings.master_enabled:\n        return False",
        "    if False:  # MUTATION: 总闸失效\n        return False",
        "tests/unit/application/engine/test_ai_participation_master_switch.py",
        "总闸关闭时不再拦截（AI 参与能力无法回退）",
    ),
]


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    print("基线：先确认三条守卫在**未变异**时全绿")
    for mid, _f, _a, _b, guard, _desc in MUTATIONS:
        code, out = run([PY, "-m", "pytest", guard, "-q", "--tb=no", "-p", "no:cacheprovider"])
        last = [l for l in out.splitlines() if "passed" in l or "failed" in l]
        print(f"  {mid:<14} {guard:<70} rc={code}  {last[-1] if last else out.strip()[-90:]}")

    print("\n逐条施加变异 ⇒ 期望对应守卫**转红**")
    verdicts = []
    for mid, rel, old, new, guard, desc in MUTATIONS:
        target = ROOT / rel
        original = target.read_text(encoding="utf-8")
        if old not in original:
            print(f"  {mid:<14} ❌ 变异锚点没找到（代码已变？）—— 跳过")
            verdicts.append((mid, "锚点丢失"))
            continue
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, out = run([PY, "-m", "pytest", guard, "-q", "--tb=no", "-p", "no:cacheprovider"])
        finally:
            # 还原（无论测试成不成都还原）
            run(["git", "checkout", "--", rel])
        last = [l for l in out.splitlines() if "passed" in l or "failed" in l or "error" in l]
        red = code != 0
        verdicts.append((mid, "守卫转红 ✅" if red else "守卫仍绿 ❌（空洞守卫）"))
        print(
            f"  {mid:<14} 变异：{desc}\n"
            f"  {'':<14} → {last[-1] if last else out.strip()[-90:]}\n"
            f"  {'':<14} ⇒ {'转红（守卫有效）' if red else '仍绿 ⇒ 这条守卫是空洞的'}"
        )

    print("\n结论：")
    for mid, v in verdicts:
        print(f"  {mid:<14} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
