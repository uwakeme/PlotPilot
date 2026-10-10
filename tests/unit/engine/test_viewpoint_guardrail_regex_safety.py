"""角色名里的正则元字符不得破坏视角泄露检测。

`_check_viewpoint_leak` 曾把未转义的角色名拼进正则：
`pattern = rf'{other}.{{0,4}}{keyword}'`。角色名来自 LLM 抽取的 cast，
带 `(`、`+`、`[` 这类字符完全可能。未转义时两类后果都很坏：

1. 不平衡的括号 → re.error 抛出 → 被 base.py 的 except 吞掉 →
   该章 validation_score 静默落到 0.0；
2. 名字本身即使正则合法，也会被当成模式的一部分，匹配到错误的位置或漏匹配。

两条测试分别钉住这两类后果：去掉 re.escape，1 抛异常、2 漏报。
"""
from engine.runtime.quality_guardrails.viewpoint_guardrail import ViewpointGuardrail

# 名字里带正则元字符：未转义时 `苏(雪.{0,4}心想` 是 unbalanced group
METACHAR_NAME = "苏(雪"

LEAK_TEXT = "林渊握紧了剑。苏(雪心里想着别的事，剑锋在雨里闪了一下。"

SCENE = {
    "viewpoint_character": "林渊",
    "characters_present": ["林渊", METACHAR_NAME],
    "key_event": "",
    "information_asymmetry": {},
}


def _leaks(text: str, scene=None):
    return ViewpointGuardrail()._check_viewpoint_leak(text, scene or SCENE)


def test_metacharacter_name_does_not_raise():
    """未转义 → re.error → 整章校验静默失败。这里必须不抛。"""
    violations = _leaks(LEAK_TEXT)
    assert isinstance(violations, list)


def test_metacharacter_name_still_detects_leak():
    """转义之后名字要按字面匹配，泄露仍然查得出来。"""
    violations = _leaks(LEAK_TEXT)

    assert [v.violation_type for v in violations] == ["viewpoint_leak"]
    assert METACHAR_NAME in violations[0].description


def test_character_class_name_is_matched_literally():
    """`司马[光]` 未转义时会被当成字符类，匹配到"司马光"这段并不存在的角色。"""
    scene = {
        "viewpoint_character": "林渊",
        "characters_present": ["林渊", "司马[光]"],
        "key_event": "",
        "information_asymmetry": {},
    }
    # 文中只有"司马光"这个名字，与"司马[光]"不是同一个角色
    text = "林渊握紧了剑。司马光心里想着别的事，剑锋在雨里闪了一下。"

    assert _leaks(text, scene) == []