"""角色一致性守门人 — OOC检测

三大检测维度：
1. OOC检测：行为vs面具比对（角色做了面具不该做的事）
2. 语言指纹一致性：对话是否符合角色的VoiceStyle
3. 创伤反应验证：触发条件出现时是否描写了条件反射

示例：
- OOC ❌：林羽（核心信念：信任是软肋）轻易相信陌生人
- OOC ✅：林羽表面配合，暗中设防

- 语言指纹 ❌：惜字如金的角色说了大段独白
- 语言指纹 ✅：惜字如金的角色只用短句回应

- 创伤反应 ❌：有人站在林羽左后方，他毫无反应
- 创伤反应 ✅：有人站在林羽左后方，他下意识偏身
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any, Optional

from engine.core.value_objects.character_mask import CharacterMask

logger = logging.getLogger(__name__)


@dataclass
class ConsistencyViolation:
    """一致性违规"""
    violation_type: str    # ooc / voice_mismatch / wound_trigger_miss
    character_name: str
    severity: float        # 0.0-1.0
    description: str
    suggestion: str

    @property
    def type_name(self) -> str:
        names = {
            "ooc": "OOC(角色脱离设定)",
            "voice_mismatch": "语言指纹不一致",
            "wound_trigger_miss": "创伤反应缺失",
            "thin_profile": "人设信息不足（专业向）",
        }
        return names.get(self.violation_type, self.violation_type)


class CharacterConsistencyGuardrail:
    """角色一致性守门人"""

    @staticmethod
    def _mask_has_editorial_signals(mask: CharacterMask) -> bool:
        """是否具备可审稿的人设约束字段（区别于仅有姓名的占位面具）。"""
        if (mask.core_belief or "").strip():
            return True
        if mask.moral_taboos:
            return True
        if mask.voice_style and str(mask.voice_style).strip() not in ("", "default"):
            return True
        if mask.active_wounds:
            return True
        return False

    # Unicode引号字符常量
    _LEFT_DOUBLE = '\u201c'   # "
    _RIGHT_DOUBLE = '\u201d'  # "
    _LEFT_SINGLE = '\u2018'   # '
    _RIGHT_SINGLE = '\u2019'  # '
    _LEFT_CORNER = '\u300c'   # 「
    _RIGHT_CORNER = '\u300d'  # 」

    # 对话提取正则：匹配所有类型的引号对
    _DIALOGUE_PATTERN = re.compile(
        r'[\u201c\u201d"\u300c]([^\u201c\u201d"\u300c\u300d]+)[\u201d\u201c"\u300d]'
        r'|[\u2018\u2019\']([^\u2018\u2019\']{2,})[\u2019\u2018\']'
    )

    # 触发条件同义词映射
    _TRIGGER_SYNONYMS: Dict[str, List[str]] = {
        "靠近": ["靠近", "在", "站", "出现在", "走到", "来到", "悄悄站"],
        "有人": ["有人", "人", "一个人", "某人", "别人", "他人"],
        "左后方": ["左后方", "左后", "身后左侧", "背后左", "左后方"],
        "右后方": ["右后方", "右后", "身后右侧", "背后右"],
        "身后": ["身后", "背后", "后方", "后边", "后面"],
        "触碰": ["触碰", "摸", "碰", "按", "握", "抓住"],
        "出现": ["出现", "现身", "到场", "来了"],
        "说": ["说", "道", "喊", "叫", "低语", "喃喃"],
        "看到": ["看到", "看见", "目睹", "面前", "眼前", "在场"],
        "女性": ["女性", "女人", "女子", "妇人", "姑娘", "女孩"],
        "遇险": ["遇险", "被打", "被杀", "被攻击", "被伤害", "受伤", "受欺负", "被打倒"],
        "遇害": ["遇害", "被杀", "被害", "身亡", "死亡", "死"],
    }

    # 禁忌的禁令外壳：moral_taboos 天然写成「绝不X / 禁止X / 不X」，
    # 而违规的表现是「做了 X」。比对前必须剥掉，否则永远检不出。
    # 必须按长度降序排列，保证「绝不」先于「不」被剥掉（否则「绝不滥杀」
    # 会先被剥成「绝滥杀」，壳没剥干净）。单字前缀放在最后。
    _TABOO_NEGATION_PREFIXES = (
        "绝不", "永不", "从不", "不会", "不得", "不准", "不许",
        "不可", "不宜", "不能", "禁止", "切勿", "杜绝",
        "不", "勿", "莫", "忌",
    )

    # 词根下限。单字（杀/死/偷/变）在正文里几乎必然出现，
    # 拿它们当禁忌证据等于乱报 —— 这条是「宁可漏报不误报」的落地点。
    _TABOO_MIN_ROOT_LEN = 2

    # 创伤反应关键词
    _REACTION_KEYWORDS = [
        "紧", "僵", "颤", "避", "缩", "握紧", "偏", "闪",
        "寒", "冷", "警", "戒", "凝", "顿", "滞", "停", "沉",
        "绷", "挡", "握", "抖", "抽", "缩回",
        "下意识", "本能", "条件反射", "僵硬", "肌肉紧",
        "瞳孔收缩", "呼吸一滞", "浑身一僵", "手指蜷缩",
        "脊背发凉", "心口一紧", "手心冒汗",
    ]

    def check(
        self,
        text: str,
        character_masks: Dict[str, CharacterMask],
    ) -> Tuple[float, List[ConsistencyViolation]]:
        """检查文本的角色一致性

        Args:
            text: 待检查文本
            character_masks: 角色面具字典

        Returns:
            (score, violations)
        """
        violations: List[ConsistencyViolation] = []

        for char_id, mask in character_masks.items():
            # 1. OOC检测
            violations.extend(self._check_ooc(text, mask))

            # 2. 语言指纹一致性
            violations.extend(self._check_voice(text, mask, character_masks))

            # 3. 创伤反应验证
            violations.extend(self._check_wound_trigger(text, mask))

        if character_masks:
            has_signal = any(
                self._mask_has_editorial_signals(m) for m in character_masks.values()
            )
            if not has_signal:
                violations.append(
                    ConsistencyViolation(
                        violation_type="thin_profile",
                        character_name="(全体登场角色)",
                        severity=0.62,
                        description=(
                            "仅有姓名字段的占位人设，无法进行编辑级行为/台词核验；该项分数按『未审评』折价。"
                        ),
                        suggestion=(
                            "为关键角色补足核心信念、禁忌、语言指纹或创伤触发，或由面具管线写入后再跑护栏。"
                        ),
                    )
                )

        if not violations:
            return 1.0, []

        total_penalty = sum(v.severity for v in violations)
        score = max(0.0, 1.0 - total_penalty * 0.2)

        return score, violations

    def _check_ooc(self, text: str, mask: CharacterMask) -> List[ConsistencyViolation]:
        """OOC检测：行为vs面具比对"""
        violations = []

        # 检查角色名是否出现在文本中
        if mask.name not in text:
            return violations

        # 禁忌触碰检测
        for taboo in mask.moral_taboos:
            if self._detect_taboo_violation(text, mask.name, taboo):
                violations.append(ConsistencyViolation(
                    violation_type="ooc",
                    character_name=mask.name,
                    severity=0.9,
                    description=f"角色{mask.name}可能违反了绝对禁忌：{taboo}",
                    suggestion=f"如果确实违反，需要提供充分的动机和代价；否则修改描写",
                ))

        # 信念一致性检测 — 基于核心信念中的关键词推理
        if mask.core_belief and mask.name in text:
            belief = mask.core_belief

            # 不信任类信念 → 检测轻易信任行为
            distrust_keywords = ["不信任", "警惕", "谨慎", "轻信", "防备", "不信", "软肋",
                                 "致命", "危险", "不可信"]
            if any(kw in belief for kw in distrust_keywords):
                trust_patterns = [
                    rf'{mask.name}.{{0,8}}(毫不犹豫地相信|完全信任|放心地|毫无戒备|毫无防备|轻信)',
                    rf'{mask.name}.{{0,4}}(相信|信任|放心).{{0,6}}(陌生|别人|他人)',
                ]
                for pattern in trust_patterns:
                    if re.search(pattern, text):
                        violations.append(ConsistencyViolation(
                            violation_type="ooc",
                            character_name=mask.name,
                            severity=0.7,
                            description=f"角色{mask.name}核心信念为'{belief}'，但出现了轻易信任的描写",
                            suggestion="添加内心挣扎或表面配合暗中设防的描写",
                        ))
                        break  # 只报一次

            # 力量/暴力类信念 → 检测软弱行为
            power_keywords = ["力量", "强者为尊", "弱肉强食"]
            if any(kw in belief for kw in power_keywords):
                weak_patterns = [rf'{mask.name}.{{0,8}}(退缩|害怕|求饶|示弱|妥协)']
                for pattern in weak_patterns:
                    if re.search(pattern, text):
                        violations.append(ConsistencyViolation(
                            violation_type="ooc",
                            character_name=mask.name,
                            severity=0.6,
                            description=f"角色{mask.name}核心信念为'{belief}'，但出现了软弱行为",
                            suggestion="如确有软弱，需要提供充分的外部压力描写",
                        ))

        return violations

    def _check_voice(self, text: str, mask: CharacterMask, character_masks: Dict[str, CharacterMask] = None) -> List[ConsistencyViolation]:
        """语言指纹一致性检测"""
        violations = []

        if mask.name not in text:
            return violations

        # 提取文本中所有类型的引号对话
        all_dialogues = []
        for match in self._DIALOGUE_PATTERN.finditer(text):
            # group(1)是双引号对话，group(2)是单引号对话
            dialogue = match.group(1) or match.group(2)
            if dialogue:
                all_dialogues.append(dialogue)

        # 筛选属于该角色的对话 — 基于就近归属原则
        # 对话归属判断：对话引号前面的最近的角色名即为说话人
        character_dialogues = []
        for d in all_dialogues:
            dialogue_pos = text.find(d)
            if dialogue_pos < 0:
                continue
            # 查找对话前面最近的汉字名称（2-4个字），作为说话人
            pre_text = text[:dialogue_pos]
            # 向前查找最近的角色名
            assigned = False
            all_masks = character_masks or {}
            if all_masks:
                # 找到对话前面最近的角色名，将对话归属给该角色
                closest_name = None
                closest_pos = -1
                for other_id, other_mask in all_masks.items():
                    other_name_pos = pre_text.rfind(other_mask.name)
                    if other_name_pos > closest_pos:
                        closest_pos = other_name_pos
                        closest_name = other_mask.name
                if closest_name == mask.name and closest_pos >= 0:
                    character_dialogues.append(d)
                    assigned = True
            # 如果就近代归未找到归属，则该对话不属于任何已知角色
            # 不再使用宽松的回退逻辑，避免多角色场景下误归属

        if not character_dialogues:
            return violations

        for dialogue in character_dialogues:
            # 检查话多/惜字如金
            if mask.voice_style == "惜字如金" and len(dialogue) > 30:
                violations.append(ConsistencyViolation(
                    violation_type="voice_mismatch",
                    character_name=mask.name,
                    severity=0.6,
                    description=f"角色{mask.name}语言风格为惜字如金，但对话过长({len(dialogue)}字)",
                    suggestion="缩短对话到1-2个短句",
                ))
            elif mask.voice_style == "话多" and len(dialogue) < 5:
                violations.append(ConsistencyViolation(
                    violation_type="voice_mismatch",
                    character_name=mask.name,
                    severity=0.4,
                    description=f"角色{mask.name}语言风格为话多，但对话过短({len(dialogue)}字)",
                    suggestion="适当扩展对话，加入口头禅或重复强调",
                ))

            # 检查句式偏好
            if mask.sentence_pattern == "陈述" and dialogue.count('\uff1f') > dialogue.count('\u3002'):
                violations.append(ConsistencyViolation(
                    violation_type="voice_mismatch",
                    character_name=mask.name,
                    severity=0.3,
                    description=f"角色{mask.name}句式偏好陈述，但对话中反问句过多",
                    suggestion="减少反问句，改用陈述语气",
                ))

        return violations

    def _check_wound_trigger(self, text: str, mask: CharacterMask) -> List[ConsistencyViolation]:
        """创伤反应验证"""
        violations = []

        for wound in mask.active_wounds:
            trigger = wound.get("trigger", "")
            if not trigger:
                continue

            # 检查触发条件是否出现在文本中（先精确匹配，再语义匹配）
            trigger_matched = trigger in text
            if not trigger_matched:
                trigger_matched = self._semantic_match_trigger(trigger, text)

            if trigger_matched:
                # 检查是否有条件反射描写
                effect = wound.get("effect", "")
                description = wound.get("description", "")

                # 在全文范围内检查是否有创伤反应
                # 如果触发条件出现但没有反应描写
                has_reaction = any(keyword in text for keyword in self._REACTION_KEYWORDS)

                if not has_reaction:
                    violations.append(ConsistencyViolation(
                        violation_type="wound_trigger_miss",
                        character_name=mask.name,
                        severity=0.8,
                        description=f"触发条件'{trigger}'出现，但角色{mask.name}没有条件反射描写",
                        suggestion=f"应描写条件反射：{effect}",
                    ))

        return violations

    def _detect_taboo_violation(self, text: str, name: str, taboo: str) -> bool:
        """检测正文是否触碰了角色的绝对禁忌。

        【已知局限 —— 这条注释本身就是交付的一部分】
        本检查是**表层词面匹配**，不是语义判断。它只问「禁忌的词根有没有
        出现在正文里」，不追问「出现它的语境是不是真的违规」：

        * **未命中不等于没有违规**，只等于没有检测到词面证据。角色用反话、
          隐喻或完全不同的措辞犯下同一件事，本检查一律看不见。
        * 命中也不等于真的违规：「他发誓绝不滥杀无辜」这类立誓/转述同样
          含词根，会误报。我们宁可在这一侧承担噪音，也不让检查保持沉默 ——
          沉默会让「最重的底线从未被执法过」这件事完全不可见。

        【为什么是这套规则】
        沿用同文件 ``_semantic_match_trigger`` 的既有机制（同一套分隔符拆词
        → 同义词兜底 → 二字子串兜底），不另起炉灶。差异只有两处，且都朝
        「宁可漏报不误报」收紧 —— 因为道德底线一旦冤判角色，代价高于漏报：

        1. 剥掉禁令外壳（"绝不/禁止/不…"）。违规的表现是「做了那件事」，
           而不是「声明绝不那件事」；不剥的话 "禁止滥杀无辜" 会因为要求
           正文同时出现「禁止」而永远检不出。
        2. 要求**全部**词根同时命中，并丢弃单字词根。``_semantic_match_trigger``
           用的是「过半即算」，那对创伤触发合适，对 severity=0.9 的道德底线
           太松 —— "滥杀无辜" 只因正文出现「无辜」二字就被判违规，是冤判。

        不调用 LLM、不查向量：这是同步护栏，不能因此变成阻塞调用。
        """
        if not taboo or not text:
            return False

        phrase = self._strip_taboo_negation(taboo)
        # 短于词根下限的禁忌无从判别（单字在正文里几乎必然出现）。
        if len(phrase) < self._TABOO_MIN_ROOT_LEN:
            return False

        # 整条禁忌原样出现 —— 最高置信的直接证据
        if phrase in text:
            return True

        roots = [
            w for w in self._split_match_words(phrase)
            if len(w) >= self._TABOO_MIN_ROOT_LEN
        ]
        if not roots:
            return False

        # 全中才算命中。半中就报会把「无辜的百姓」这种共现当成「滥杀」。
        return all(self._root_present(root, text) for root in roots)

    def _strip_taboo_negation(self, taboo: str) -> str:
        """剥掉禁忌的禁令外壳，返回「被禁止的那件事」本身。

        moral_taboos 天然以禁令句式书写（提示词模板即 ``"绝不做的事1"``），
        而违规的表现恰恰是去做了那件事，所以比对前必须先剥掉这层外壳。
        """
        phrase = (taboo or "").strip()
        stripped = True
        while stripped and phrase:
            stripped = False
            for prefix in self._TABOO_NEGATION_PREFIXES:
                # len 判断兼作兜底：剥到空串就停，绝不返回空。
                if len(phrase) > len(prefix) and phrase.startswith(prefix):
                    phrase = phrase[len(prefix):].strip()
                    stripped = True
                    break
        return phrase

    def _root_present(self, root: str, text: str) -> bool:
        """词根是否出现在正文：直接命中 → 同义词 → 二字子串兜底。

        与 ``_semantic_match_trigger`` 的三级匹配同构，唯一差别是同义词
        同样受词根下限约束：``_TRIGGER_SYNONYMS`` 里「遇害」的同义词含单字
        「死」，放它进来等于把泛化字重新引回禁忌证据，而泛化字正是这条检查
        要防的东西。
        """
        if root in text:
            return True
        for syn in self._TRIGGER_SYNONYMS.get(root, []):
            if len(syn) >= self._TABOO_MIN_ROOT_LEN and syn in text:
                return True
        for i in range(len(root) - 1):
            if root[i:i + 2] in text:
                return True
        return False

    def _split_match_words(self, phrase: str) -> List[str]:
        """把短语拆成关键词组（``_semantic_match_trigger`` 与禁忌检测共用）。

        拆词口径必须只有一处：两处各拆各的，迟早会在边界用例上分叉，
        而分叉出来的差异会伪装成「护栏结果不同」。
        """
        words = [w for w in re.split(r'[的了吗着了过在]', phrase) if len(w) >= 1]

        # 拆出来仍是一个长词时：先用同义词表的键当语义锚点切，锚不上再按 2 字切
        if len(words) == 1 and len(words[0]) > 2:
            word = words[0]
            sub_words: List[str] = []
            all_trigger_keys = list(self._TRIGGER_SYNONYMS.keys())
            # 按长度降序排列，优先匹配长词
            all_trigger_keys.sort(key=len, reverse=True)

            remaining = word
            while remaining:
                matched = False
                for key in all_trigger_keys:
                    if remaining.startswith(key):
                        sub_words.append(key)
                        remaining = remaining[len(key):]
                        matched = True
                        break
                if not matched:
                    # 尝试按2字词切分
                    if len(remaining) >= 2:
                        sub_words.append(remaining[:2])
                        remaining = remaining[2:]
                    else:
                        sub_words.append(remaining)
                        remaining = ""

            if len(sub_words) > 1:
                words = sub_words

        return words

    def _semantic_match_trigger(self, trigger: str, text: str) -> bool:
        """语义匹配触发条件

        将触发条件拆解为关键词组，在文本中查找满足条件的片段。
        例如 "看到女性遇险" -> 需要 (看到/看见/面前) + (女性/女人) + (遇险/被打)
        例如 "有人靠近左后方" -> 需要 (有人/人/某人) + (靠近/在/站) + (左后方/左后)
        """
        trigger_words = self._split_match_words(trigger)

        if not trigger_words:
            return False

        matched_count = 0
        for word in trigger_words:
            if len(word) == 0:
                continue
            # 直接匹配
            if word in text:
                matched_count += 1
                continue
            # 同义词匹配
            synonyms = self._TRIGGER_SYNONYMS.get(word, [])
            if any(syn in text for syn in synonyms):
                matched_count += 1
                continue
            # 子串匹配：触发条件词的一部分出现在文本中
            if len(word) >= 2:
                found_sub = False
                for i in range(len(word) - 1):
                    sub = word[i:i+2]
                    if sub in text:
                        matched_count += 1
                        found_sub = True
                        break
                if found_sub:
                    continue

        # 至少匹配一半以上的关键词才算触发
        return matched_count >= max(1, (len(trigger_words) + 1) // 2)

    def _extract_belief_keywords(self, belief: str) -> List[str]:
        """从核心信念中提取关键词"""
        parts = re.split(r'[，。、；：]', belief)
        return [p.strip() for p in parts if p.strip()]

    def _extract_dialogues(self, text: str, name: str) -> List[str]:
        """从文本中提取角色的对话"""
        dialogues = []
        # 使用统一的对华提取模式
        for match in self._DIALOGUE_PATTERN.finditer(text):
            dialogue = match.group(1) or match.group(2)
            if dialogue:
                # 检查对话是否在角色名附近
                pos = text.find(dialogue)
                name_pos = text.find(name)
                if pos >= 0 and name_pos >= 0 and abs(pos - name_pos) < 150:
                    dialogues.append(dialogue)
        return dialogues
