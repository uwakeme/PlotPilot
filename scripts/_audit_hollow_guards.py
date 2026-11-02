"""一次性审计脚本（不进 CI）：复核探针报出的 **两处「自指型空洞守卫」**。

指控
----
1. **B-05**：`application/blueprint/services/structure_proposal.py` 的 ``FALLBACK_GRID``
   兜底网格被守卫**从被测模块自己 import 进来**再与结果比较
   ⇒ 把 ``(3, 3, 3)`` 改成 ``(9, 9, 9)``（= 729 幕！）⇒ 守卫照样绿。
2. **F-08**：`application/world/services/plot_point_classifier_schema.py` 的 ``SOURCE_NONE``
   是「未评估」这个三态语义标记 ⇒ 把它改成任意字符串 ⇒ 全仓没有一个测试转红。

⚠ 每条变异后都 ``git checkout --`` 还原；并跑**全量 tests/unit**，
   因为「局部守卫绿」不等于「全仓绿」—— 要证明的是**连全仓都不红**。
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

MUTATIONS = [
    (
        "B-05 / FALLBACK_GRID",
        "application/blueprint/services/structure_proposal.py",
        "FALLBACK_GRID = (3, 3, 3)",
        "FALLBACK_GRID = (9, 9, 9)  # MUTATION",
        "tests/unit/application/blueprint/test_b05_structure_proposal.py",
        "AI 判不出结构时的兜底网格：3部×3卷×3幕 → 9部×9卷×9幕（729 幕）",
    ),
    (
        "F-08 / SOURCE_NONE",
        "application/world/services/plot_point_classifier_schema.py",
        'SOURCE_NONE = ""',
        'SOURCE_NONE = "none"  # MUTATION',
        "tests/unit/application/world/test_plot_point_classifier.py",
        "「未评估」这个三态语义标记被改成别的字符串",
    ),
]


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def tail(out: str) -> str:
    lines = [l for l in out.splitlines() if "passed" in l or "failed" in l or "error" in l]
    return lines[-1] if lines else out.strip()[-120:]


def main() -> int:
    for mid, rel, old, new, guard, desc in MUTATIONS:
        target = ROOT / rel
        original = target.read_text(encoding="utf-8")
        if old not in original:
            print(f"{mid}: ❌ 锚点没找到 —— {rel} 里没有 {old!r}")
            continue

        print(f"\n{'='*70}\n{mid} · {desc}\n  目标文件：{rel}")
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code1, out1 = run([PY, "-m", "pytest", guard, "-q", "--tb=no", "-p", "no:cacheprovider"])
            print(f"  点名守卫  {guard}\n    → {tail(out1)}  (rc={code1})")
            code2, out2 = run([PY, "-m", "pytest", "tests/unit", "-q", "--tb=no", "-p", "no:cacheprovider"])
            print(f"  全量 unit  tests/unit\n    → {tail(out2)}  (rc={code2})")
            if code2 != 0:
                print("    ⇒ 全量有测试转红 ⇒ 守卫有效")
            else:
                print("    ⇒ ⚠ 全量也全绿 ⇒ 这条守卫是**空洞的**（改坏实现没人拦）")
        finally:
            run(["git", "checkout", "--", rel])

    print(f"\n{'='*70}\n工作区还原确认：")
    code, out = run(["git", "status", "--porcelain"])
    print("  " + (out.strip() or "（干净）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
