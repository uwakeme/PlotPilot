"""J-02：文风归属 —— **前端不再替作者决定**。

改造前
------
① **默认注入**：`BiblePanel.styleNotesWithCreationDefault('')` 把
   ``MARKET_STYLE_PRESETS[0]``（修仙·升级打脸）的正文写进**每一本新书**的 Bible
   ⇒ 前端替作者定了文风；
   而它会走「创作契约」具名槽进正文提示词 ⇒ **确实生效**（不是显示层的事）。

② **关键词打分**：``marketStylePresets.matchPresetValue`` 按关键词给预设打分，
   分数 ≥ 45 就显示那个预设的名字。
   ⚠ 登记表把它写成「前端替 AI 判定文风归属」—— **因果讲反了**：
   打分只影响**面板上的标签**（内置模板 / 自定义公约），从来不写进 Bible。
   ⇒ 真正替作者做决定的是 ①，不是 ②。

改造后
------
- ① 删掉：新书**不再**被注入默认文风 ⇒ 空就是空；
- 出口不丢：文风为空时面板给出**「选择文风」**（作者自选）或指去「AI 生成」
  （AI 写）⇒ 登记表要的「AI 语义匹配**或**用户自选」里，「用户自选」这条路是通的；
- ② 保留，但把「只管显示」写进 docstring（避免下一个读代码的人把因果再看反）。

⚠ **前端没有测试设施**（只有一个 ``npm run build`` 门禁）⇒ 本文件的守卫是
**文件内容级断言**，不是行为级断言。它能挡住「默认注入又被写回去」，
挡不住「换个写法再注入一次」。⇒ 这条局限写在这里，不假装它更强。
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
BIBLE_PANEL = REPO_ROOT / "frontend/src/components/panels/BiblePanel.vue"
PRESETS = REPO_ROOT / "frontend/src/constants/marketStylePresets.ts"


def _read(path: pathlib.Path) -> str:
    return path.read_bytes().decode("utf-8")


def _code_only(text: str) -> str:
    """去掉整行注释（只留代码）。

    ⚠ 不剥注释的话，**解释「此前怎么做的」的注释会把守卫自己命中**
    （守卫用 `contains` 命中自己的注释 ⇒ 恒绿或恒红）。
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*", "<!--", "*/")):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_the_frontend_no_longer_injects_a_default_style():
    """默认注入的那套函数必须**不存在**（不是「不用了」，是「没了」）。

    ⚠ 断言前先剥掉注释 —— 否则这个文件里解释「此前怎么做的」注释
    会把断言自己命中。
    """
    code = _code_only(_read(BIBLE_PANEL))

    assert "function styleNotesWithCreationDefault" not in code, (
        "新书又会被前端默认注入文风预设了"
    )
    assert "styleNotesWithCreationDefault(" not in code, "默认注入的调用点还在"


def test_the_empty_style_state_gives_the_author_an_exit():
    """空态必须有出口（否则去掉默认注入就等于把作者的路堵死）。"""
    text = _read(BIBLE_PANEL)
    # ⚠ 只看**模板段**：样式表里也有 `.bible-style-card--empty`，
    # 查全文会命中 CSS 而不是那个卡片本身。
    template = text.split("</template>")[0]

    assert "bible-style-card--empty" in template, "文风未定的空态卡片没了"
    assert "选择文风" in template, "空态没有给出「选择文风」的出口"
    assert "openStylePresetModal" in template


def test_the_scoring_result_is_never_written_into_the_bible():
    """关键词打分**只管显示**：它的返回值不许被写进 ``style_notes``。"""
    code = _code_only(_read(BIBLE_PANEL))

    for line in code.splitlines():
        stripped = line.strip()
        assert "style_notes = matchPresetValue(" not in stripped, (
            f"打分结果被写进了文风：{stripped[:80]}"
        )
        assert "style_notes: matchPresetValue(" not in stripped, (
            f"打分结果被写进了文风：{stripped[:80]}"
        )


def test_the_scoring_is_documented_as_display_only():
    """打分「只管显示」这件事要写进代码注释（否则下一个人会把因果再看反）。"""
    text = _read(PRESETS)

    idx = text.find("export function matchPresetValue")
    assert idx > 0, "找不到 matchPresetValue"
    doc = text[max(0, idx - 1200) : idx]
    assert "只管显示" in doc, "打分函数的注释没写明「只管显示、不决定文风归属」"


def test_the_preset_picker_is_still_reachable_for_an_existing_style():
    """已有文风时「更换文风」仍在（别把出口改没了）。"""
    text = _read(BIBLE_PANEL)

    assert "更换文风" in text
    assert "openStylePresetModal" in text
