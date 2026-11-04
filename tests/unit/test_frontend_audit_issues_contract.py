# -*- coding: utf-8 -*-
"""第 93 批：章后审计结论在**面板上渲染成空白** —— 前端契约守卫。

⚠ 病灶（生产库实测，不是推断）
------------------------------
后端 `last_audit_issues` 的条目形状是::

    {type, severity, category, description, suggestion}

⇒ **没有 `message`**。

而前端此前：
* 类型声明 ``issues?: Array<{ severity: string; message: string }>`` ⇒ **替后端撒谎**；
* 面板渲染 ``{{ issue.message }}`` ⇒ **渲染成空白**，
  而下面那行「还有 N 条问题...」**照样显示** ⇒ 用户看到「有问题」却看不到问题是什么；
* 判色只认 ``error`` ⇒ 后端的 ``critical``（世界观规则崩坏那一档）落进兜底
  ⇒ **最严重的一类显示成蓝色 info 卡片**。

守卫清单（前端零测试设施 ⇒ 只能做**文件内容级**断言）
---------------------------------------------------
1. 面板不许再渲染 ``issue.message``；
2. 面板必须渲染 ``description``（问题本身）与 ``suggestion``（最小修正建议）；
3. 判色必须认 ``critical``；
4. 类型里不许再声明 ``message: string``，且必须声明 ``description``。

⚠ **断言前先剥注释**（本批自己踩的）：第一版守卫直接扫整个文件，
  而**修复说明的注释里**正好写着 ``issue.message`` ⇒ 守卫把自己骗红了。
  ⇒ 守卫查的是**代码**，不是散文。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
PANEL = ROOT / "frontend/src/components/workbench/ChapterStatusPanel.vue"
API_TYPES = ROOT / "frontend/src/api/autopilot.ts"

#: HTML 注释（.vue 模板里的说明）
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: 块注释（/** … */ 与 /* … */）
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
#: 行注释（// …）
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _code_only(text: str) -> str:
    """剥掉注释 ⇒ 只留**代码**。

    ⚠ 顺序：先块注释（行注释会把 ``//`` 里的 ``/* */`` 也吃掉，无妨），
    再 HTML 注释，最后行注释。
    """
    text = _HTML_COMMENT.sub(" ", text)
    text = _BLOCK_COMMENT.sub(" ", text)
    text = _LINE_COMMENT.sub(" ", text)
    return text


def _read_code(path: pathlib.Path) -> str:
    return _code_only(path.read_text(encoding="utf-8-sig"))


def test_the_panel_does_not_render_a_field_the_backend_never_sends():
    """⚠ 后端**从不发** ``message`` ⇒ 渲染它就是渲染空白。"""
    text = _read_code(PANEL)
    assert "issue.message" not in text, (
        "面板又在渲染 issue.message ⇒ 后端从不发这个字段 ⇒ 渲染成空白"
    )


def test_the_panel_renders_the_real_fields():
    """面板必须渲染 ``description``（问题本身）与 ``suggestion``（最小修正建议）。"""
    text = _read_code(PANEL)
    assert "issue.description" in text, "面板没有渲染 issue.description ⇒ 问题内容是空白的"
    assert "issue.suggestion" in text, "面板没有渲染 issue.suggestion ⇒ 修正建议看不到"


def test_the_severity_mapping_knows_critical():
    """⚠ 后端的严重级别是 ``critical``（不是 ``error``）⇒ 漏了就显示成 info 卡片。"""
    text = _read_code(PANEL)
    assert "issueSeverityType" in text, "面板没有用统一的判色函数"
    assert "'critical'" in text, "判色函数不认 'critical' ⇒ 严重漂移被显示成 info 卡片"


def test_the_type_does_not_claim_a_field_the_backend_never_sends():
    """⚠ 类型不许替后端撒谎（否则 TS 编译期也抓不到）。

    ⚠ 只查 ``AutopilotAuditIssue`` 那一块 —— 同一个文件里**别的**接口
    （日志记录之类）本来就该有 ``message`` 字段 ⇒ 全文件断言会误伤。
    """
    text = _read_code(API_TYPES)
    m = re.search(r"export interface AutopilotAuditIssue\s*\{(.*?)\}", text, re.DOTALL)
    assert m, "找不到 AutopilotAuditIssue 接口 ⇒ 审计条目的类型没被写下来"
    block = m.group(1)

    assert "message:" not in block, (
        "类型里还声明着 message ⇒ 后端不发它 ⇒ 面板会拿到 undefined"
    )
    assert "description: string" in block, "类型里没有 description ⇒ 真实形状没被写下来"
    assert "issues?: AutopilotAuditIssue[]" in text, (
        "AutopilotChapterAudit.issues 还在用内联字面量 ⇒ 形状会与真源分叉"
    )
