# application/audit/services/cliche_scanner.py
"""增强版俗套句式扫描器 — 覆盖用户规则集中的全部禁用模式。

从原有10个模式扩展至50+模式，覆盖：
- 微表情/微动作（嘴角上扬、眼里闪过、指尖泛白、一丝系列等）
- 声线/语气描述（带着XX口吻、不容置疑等）
- 比喻句式（仿佛、宛如、犹如、心湖涟漪等）
- 生理性系列（生理性泪水、生理性前缀等）
- 情绪标签（直接情绪标签、心中波澜等）
- 句式（不是而是、破折号等）
- 小动物比喻
- 面部大忌、身体大忌
- 其他AI高频俗套
"""
import re
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional

# ⚠ F-12：分类学的类 id（A–K）来自**单一事实源**（config/anti_ai_taxonomy.yaml）
#   ⇒ 机检与提示词（模型输出）用**同一套口径**，两侧输出才可比。
from application.audit.services.anti_ai_taxonomy import class_id_for_scanner_category


# ── N-1：中文正文夹带英文词（可机检） ──────────────────────────────
#
#: 拉丁单词（≥2 字母；``a`` / ``I`` 这类不算）。
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
#: 中文字符（用于判定「这个英文词处在中文语境里」）。
_HAS_CJK = re.compile(r"[一-鿿]")
#: 判定「中文语境」时，英文词左右各看多少字。
_ENGLISH_CONTEXT_CHARS = 8
#: 引号/书名号/括号 —— 里面的外文是**引文**，不是「夹带」。
_ENGLISH_EXEMPT_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("“", "”"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("（", "）"),
    ("(", ")"),
    ("【", "】"),
    ("[", "]"),
)
#: 常见专名/缩写白名单（**全小写**比较）。
#:
#: ⚠ 白名单是**封闭清单** ⇒ 不在表里的专名会被标记（info 级，代价低）。
#:   遇到真被误伤的专名，往这里加即可。
ENGLISH_PROPER_NOUN_ALLOWLIST: frozenset = frozenset({
    "dna", "fbi", "cnn", "bbc", "tv", "cd", "dvd", "usb", "pdf", "id",
    "ceo", "cfo", "vip", "sos", "mri", "ct", "ai", "iq", "eq", "usa", "uk",
    "un", "ok", "no", "vs", "inc", "ltd", "co", "mr", "mrs", "ms", "dr",
})


@dataclass
class ClicheHit:
    """俗套句式命中结果（增强版）"""
    pattern: str       # 匹配的模式名称
    text: str          # 匹配的文本
    start: int         # 起始位置
    end: int           # 结束位置
    severity: str = "warning"     # "critical" | "warning" | "info"
    category: str = ""            # 分类标签（扫描器侧的中文分类，**保持不变**）
    replacement_hint: str = ""    # 替换建议
    #: ⚠ F-12：分类学的**类 id**（A–K），与提示词（模型输出）的口径一致。
    #:
    #: ⚠ 之前机检报中文分类（微表情）、模型报类 id（A）⇒ **两侧输出不可比**
    #:   ⇒ 审阅时没法把机检结果映射到提示词的评分口径。
    #: ⚠ 只在代码侧出现的分类（俗套 / 情绪 / 严禁词 / 语言混杂）**没有**类 id
    #:   ⇒ 这里留空串（**不猜**），它们在词表里显式登记为 code_only。
    taxonomy_class: str = ""


# ─── 增强版 AI 生成文本俗套检测模式 ───
# 格式：(正则, 模式名, 严重性, 分类, 替换建议)
# ─── 增强版 AI 生成文本俗套检测模式 ───
# 格式：(正则, 模式名, 严重性, 分类, 替换建议)
#
# ⚠ 第 77 批（A）：这 51 条**搬进数据文件**了 ⇒ 唯一来源是
#   ``config/anti_ai_patterns.yaml``（规则可调：改 YAML 即可，不用改代码）。
#   ⇒ 这里只做**转发导入**（保持原名 ⇒ 既有调用方零改动）；
#   ⇒ 守卫钉住清单的校验和（``test_anti_ai_patterns_datafile.py``）
#     ⇒ 清单内容一改就红，不会被「顺手改坏」。
from application.audit.services.anti_ai_patterns import AI_CLICHE_PATTERNS_ENHANCED


class ClicheScanner:
    """增强版俗套句式扫描器

    使用正则表达式检测 AI 生成文本中的常见俗套句式。
    支持三种严重级别：critical / warning / info
    支持分类统计和替换建议。
    """

    def __init__(self, use_enhanced: bool = True, extra_patterns=None):
        """初始化扫描器

        Args:
            use_enhanced: 是否使用增强版模式（默认 True）
            extra_patterns: 额外规则（F-06：本书特有的**已学规则**）。
                格式同 AI_CLICHE_PATTERNS_ENHANCED：
                (正则, 模式名, 严重性, 分类, 替换建议)。
                追加在既有规则**之后**，不改动既有规则。
        """
        if use_enhanced:
            raw_patterns = AI_CLICHE_PATTERNS_ENHANCED
            self.compiled_patterns = [
                (re.compile(pattern), name, severity, category, hint)
                for pattern, name, severity, category, hint in raw_patterns
            ]
        else:
            # 兼容旧版：仅使用原有 10 个模式
            legacy_patterns = [
                (r"熊熊(烈火|怒火|火焰|燃烧)", "熊熊系列"),
                (r"(眼中|眸中|目光中)闪过一丝", "眼神闪过系列"),
                (r"嘴角(勾起|扬起|浮现|上扬).*?[笑弧]", "嘴角笑意系列"),
                (r"(心中|内心)五味杂陈", "五味杂陈系列"),
                (r"如同.{1,10}一般", "如同一般系列"),
                (r"(眼神|目光|气势).{0,3}(变得|显得|愈发)?凌厉", "凌厉系列"),
                (r"(眼神|目光|眼中).{0,5}复杂", "复杂眼神系列"),
                (r"(内心|心中).{0,5}(泛起|涌起|掀起).{0,5}波澜", "心中波澜系列"),
                (r"(眼神|目光|眼眸).{0,3}(深邃|幽深)", "深邃眼神系列"),
                (r"(嘴角|脸上|眼中).{0,3}(浮现|闪过|掠过)一抹", "一抹系列"),
            ]
            self.compiled_patterns = [
                (re.compile(pattern), name, "warning", "", "")
                for pattern, name in legacy_patterns
            ]

        # F-06：追加本书特有的已学规则（AI 归纳 → 落库 → 回灌）。
        if extra_patterns:
            for item in extra_patterns:
                try:
                    if len(item) == 5:
                        pattern, name, severity, category, hint = item
                    else:
                        pattern, name = item
                        severity, category, hint = "warning", "", ""
                    # F-06：已学规则是**字面关键词**（不是正则）⇒ 这里必须
                    # ``re.escape``。否则人工添加的规则能塞进灾难性回溯的正则
                    # （如 ``(a+)+$``），扫描时会卡死线程。
                    # 注意：只 escape 追加进来的已学规则，
                    # 既有内置规则本来就是正则，不能动。
                    self.compiled_patterns.append(
                        (re.compile(re.escape(str(pattern))), name, severity, category, hint)
                    )
                except Exception:
                    continue

    def scan_cliches(self, text: str) -> List[ClicheHit]:
        """扫描文本中的俗套句式

        Args:
            text: 要扫描的文本

        Returns:
            检测到的俗套句式列表，按出现位置排序
        """
        hits = []

        for item in self.compiled_patterns:
            if len(item) == 5:
                compiled_pattern, pattern_name, severity, category, hint = item
            else:
                compiled_pattern, pattern_name = item
                severity, category, hint = "warning", "", ""

            for match in compiled_pattern.finditer(text):
                hit = ClicheHit(
                    pattern=pattern_name,
                    text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    severity=severity,
                    category=category,
                    replacement_hint=hint,
                    # ⚠ F-12：带上类 id（A–K），与提示词同一套口径。
                    taxonomy_class=class_id_for_scanner_category(category),
                )
                hits.append(hit)

        # N-1：中文正文里夹带的英文词（**专用检查**，不是正则表的一行 ——
        # 「孤立/在中文语境里」是跨全文的语义，单条正则表达不了）。
        hits.extend(self._scan_english_in_chinese(text))

        # 按位置排序
        hits.sort(key=lambda h: h.start)

        return hits

    # ── N-1：中文正文夹带英文词 ────────────────────────────────
    #
    # ⚠ 为什么要单独一个检查函数（而不是往 ``AI_CLICHE_PATTERNS_ENHANCED``
    #   里加一行正则）：
    #   「这个词是不是不该出现在中文正文里」要看**上下文**（是不是引文、
    #   是不是专名、是不是纯外文段落），而规则表那一行只能表达「模式」，
    #   表达不了「语境」。
    #   ⇒ 这里产出**标准** ``ClicheHit``，于是 score / hits / assessment /
    #     落库 / API 这条链路**自动生效**，三个消费方（章后审计 / 流式 AC
    #     扫描 / 分块缓冲扫描）也自动覆盖（它们都调 ``scan_cliches``）。
    #
    # ⚠ 严重性取 **info**：这是**低噪声**的机检规则（正文里不会成片出现），
    #   用 warning 会让每章凭空跳到「中等」。

    def _scan_english_in_chinese(self, text: str) -> List[ClicheHit]:
        """中文正文里夹带的英文单词。

        判据（逐条都可机检，不依赖 LLM）：

        1. 词长 ≥ 2 的拉丁单词（``a`` / ``I`` 不算）；
        2. **在中文语境里** —— 词的左右各 8 字内出现中文字符
           ⇒ 纯外文段落（英文信函、外文引文）不算「夹带」；
        3. 不在**引号/书名号/括号**内 ⇒ 引文里的外文是引文，不是夹带；
        4. 不在**专名白名单**里（见 ``ENGLISH_PROPER_NOUN_ALLOWLIST``）。

        「孤立」体现在**提示语**上：全文只出现一次的，更可能是机械翻译残留。
        """
        if not text:
            return []
        exempt = self._exempt_spans(text)
        counts: Dict[str, int] = {}
        for match in _LATIN_WORD_RE.finditer(text):
            counts[match.group(0).lower()] = counts.get(match.group(0).lower(), 0) + 1

        hits: List[ClicheHit] = []
        for match in _LATIN_WORD_RE.finditer(text):
            word = match.group(0)
            if word.lower() in ENGLISH_PROPER_NOUN_ALLOWLIST:
                continue
            if any(start <= match.start() < end for start, end in exempt):
                continue
            left = text[max(0, match.start() - _ENGLISH_CONTEXT_CHARS):match.start()]
            right = text[match.end():match.end() + _ENGLISH_CONTEXT_CHARS]
            if not _HAS_CJK.search(left) and not _HAS_CJK.search(right):
                continue
            isolated = counts[word.lower()] == 1
            hits.append(
                ClicheHit(
                    pattern="中文正文夹带英文词",
                    text=word,
                    start=match.start(),
                    end=match.end(),
                    severity="info",
                    category="语言混杂",
                    replacement_hint=(
                        "疑似机械翻译残留，改用中文"
                        if isolated
                        else "若为专名请加进白名单；否则改用中文"
                    ),
                )
            )
        return hits

    @staticmethod
    def _exempt_spans(text: str) -> List[Tuple[int, int]]:
        """引号/书名号/括号内部的区间（里面的外文是引文，不算「夹带」）。"""
        spans: List[Tuple[int, int]] = []
        for open_ch, close_ch in _ENGLISH_EXEMPT_PAIRS:
            start = 0
            while True:
                left = text.find(open_ch, start)
                if left < 0:
                    break
                right = text.find(close_ch, left + len(open_ch))
                if right < 0:
                    break
                spans.append((left, right + len(close_ch)))
                start = right + len(close_ch)
        return spans

    def scan_by_category(self, text: str) -> Dict[str, List[ClicheHit]]:
        """按分类统计扫描结果"""
        hits = self.scan_cliches(text)
        by_category: Dict[str, List[ClicheHit]] = {}
        for h in hits:
            by_category.setdefault(h.category, []).append(h)
        return by_category

    def get_critical_count(self, text: str) -> int:
        """获取 critical 级别违规数"""
        return sum(1 for h in self.scan_cliches(text) if h.severity == "critical")

    def get_category_stats(self, text: str) -> Dict[str, int]:
        """获取分类统计"""
        hits = self.scan_cliches(text)
        stats: Dict[str, int] = {}
        for h in hits:
            stats[h.category] = stats.get(h.category, 0) + 1
        return stats
