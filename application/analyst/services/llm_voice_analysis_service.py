"""LLM 文风分析服务

使用 LLM 分析章节的写作风格特征，生成风格向量用于漂移检测。
相比统计特征方法，能捕捉更深层的风格特点：
- 叙事视角和人称使用
- 对话与描写的比例
- 情感表达方式
- 节奏和韵律
- 用词偏好和修辞手法

成本：约 500 token/章（300 输入 + 200 输出）
"""
import hashlib
import json
import logging
import math
import time
from collections import OrderedDict
from typing import Any, Dict, List

from domain.ai.services.llm_service import LLMService
from application.analyst.services.voice_analysis_settings import (
    VoiceAnalysisRuntimeSettings,
    get_voice_analysis_runtime_settings,
)
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_contracts.style_analysis import STYLE_ANALYSIS_CONTRACT
from infrastructure.ai.prompt_contracts.voice_baseline_analysis import (
    VOICE_BASELINE_ANALYSIS_CONTRACT,
)
from infrastructure.ai.prompt_gateway import get_prompt_gateway
from application.ai.trace_context import ensure_trace

logger = logging.getLogger(__name__)

#: 送给模型的正文上限（超出的部分本来就不进模型 ⇒ 不该让缓存失效）
STYLE_SNIPPET_LIMIT = 2000


def content_fingerprint(text: str) -> str:
    """内容指纹（缓存键用）。

    ⚠ 算在**实际送进模型的那段文本**上（正文前 2000 字），不是整篇正文
      ⇒ 正文后半段的改动**不会**让缓存失效（那部分本来就没进模型，
      重算也是同一个结果 ⇒ 重算纯属白花 token）。
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# 风格维度定义
STYLE_DIMENSIONS = [
    "narrative_voice",      # 叙事声音：第一人称/第三人称/全知视角
    "dialogue_ratio",       # 对话占比：0-1
    "description_depth",    # 描写深度：0-1（浮光掠影 vs 细腻入微）
    "emotional_intensity",  # 情感强度：0-1
    "pacing",               # 节奏：0-1（舒缓 vs 紧凑）
    "sensory_richness",     # 感官丰富度：0-1
    "metaphor_usage",       # 修辞使用：0-1
    "sentence_variety",     # 句式变化：0-1
]


class LLMVoiceAnalysisService:
    """LLM 文风分析服务"""

    def __init__(
        self,
        llm_service: LLMService,
        runtime_settings: VoiceAnalysisRuntimeSettings | None = None,
    ):
        self._llm = llm_service
        self._runtime_settings = runtime_settings or get_voice_analysis_runtime_settings()
        self._cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._cache_loaded_at: Dict[str, float] = {}

    async def analyze_chapter_style(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
    ) -> Dict[str, Any]:
        """分析单个章节的写作风格

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            content: 章节内容

        Returns:
            风格向量字典，包含各维度分数和元数据
        """
        # ⚠ 第 86 批（N-16）：缓存键必须含**内容身份**。
        #
        #   此前是 ``f"{novel_id}:{chapter_number}"`` ⇒ **同一章改写之后重评，
        #   仍然命中改写前那次的缓存** ⇒ 拿回来的风格向量一字未变
        #   ⇒ 相似度**纹丝不动**。
        #
        #   ⇒ 生产实测（第 85 批真跑一本书）：第 8 章连续 3 次定向修文，
        #     相似度**始终 0.4167**；日志里每次「修文后重评」**都没有新的
        #     模型请求**（只有修文本身那一次）⇒ 证实重评吃的是缓存。
        #     ⇒ 循环因此**不可能收敛** ⇒ 后两次调用纯烧钱。
        #
        #   ⇒ 现在把「实际送进模型的那段文本」的指纹并进键里：
        #     * 内容改了 ⇒ 键变了 ⇒ 重新分析（相似度才可能动）；
        #     * 内容没改 ⇒ 键不变 ⇒ 仍然命中（不白花 token）。
        snippet = content[:STYLE_SNIPPET_LIMIT] if len(content) > STYLE_SNIPPET_LIMIT else content
        cache_key = f"{novel_id}:{chapter_number}:{content_fingerprint(snippet)}"
        cached = self._get_cached_style(cache_key)
        if cached is not None:
            return cached

        try:
            prompt = get_prompt_gateway().render(
                STYLE_ANALYSIS_CONTRACT,
                {"content": snippet},
            ).prompt
            config = generation_config_from_profile("voice_style_analysis")

            ensure_trace(novel_id=novel_id, stage="analyst.voice.analyze", stage_label="声线分析")
            result = await self._llm.generate(prompt, config)
            raw = result.content.strip()

            # 解析 JSON
            style_vector = self._parse_style_json(raw)

            # 添加元数据
            style_vector["_meta"] = {
                "chapter_number": chapter_number,
                "content_length": len(content),
                "snippet_length": len(snippet),
            }

            self._remember_style(cache_key, style_vector)

            logger.debug(
                "LLM 文风分析完成 novel=%s ch=%d dimensions=%s",
                novel_id, chapter_number, list(style_vector.keys())[:8]
            )

            return style_vector

        except Exception as e:
            logger.warning(
                "LLM 文风分析失败 novel=%s ch=%d: %s",
                novel_id, chapter_number, e
            )
            # 返回中性值，避免阻断流程
            return self._get_neutral_style(chapter_number)

    def _style_cache_enabled(self) -> bool:
        return (
            self._runtime_settings.style_cache_ttl_seconds > 0
            and self._runtime_settings.style_cache_max_size > 0
        )

    def _get_cached_style(self, cache_key: str) -> Dict[str, Any] | None:
        if not self._style_cache_enabled():
            self._forget_cached_style(cache_key)
            return None

        cached = self._cache.get(cache_key)
        if cached is None:
            return None

        loaded_at = self._cache_loaded_at.get(cache_key, 0.0)
        if time.monotonic() - loaded_at >= self._runtime_settings.style_cache_ttl_seconds:
            self._forget_cached_style(cache_key)
            return None

        self._cache.move_to_end(cache_key)
        return cached

    def _remember_style(self, cache_key: str, style_vector: Dict[str, Any]) -> None:
        if not self._style_cache_enabled():
            self._forget_cached_style(cache_key)
            return

        self._cache[cache_key] = style_vector
        self._cache.move_to_end(cache_key)
        self._cache_loaded_at[cache_key] = time.monotonic()

        max_size = self._runtime_settings.style_cache_max_size
        while len(self._cache) > max_size:
            evicted_key, _ = self._cache.popitem(last=False)
            self._cache_loaded_at.pop(evicted_key, None)

    def _forget_cached_style(self, cache_key: str) -> None:
        self._cache.pop(cache_key, None)
        self._cache_loaded_at.pop(cache_key, None)

    async def compute_baseline(
        self,
        novel_id: str,
        style_vectors: List[Dict[str, float]],
    ) -> Dict[str, Any]:
        """从多个风格向量中计算基准风格

        Args:
            novel_id: 小说 ID
            style_vectors: 已分析的章节风格向量列表

        Returns:
            包含 baseline（基准值）和 tolerance（容差）的字典
        """
        if not style_vectors:
            return self._get_default_baseline()

        # 如果样本少，直接计算平均值
        if len(style_vectors) < 5:
            return self._compute_simple_baseline(style_vectors)

        # 样本足够时，用 LLM 提取更智能的基准
        try:
            # 准备数据
            style_data = []
            for i, vec in enumerate(style_vectors[-10:]):  # 最多取最近 10 章
                clean_vec = {k: v for k, v in vec.items() if not k.startswith("_")}
                style_data.append(f"章节{i+1}: {json.dumps(clean_vec, ensure_ascii=False)}")

            prompt = get_prompt_gateway().render(
                VOICE_BASELINE_ANALYSIS_CONTRACT,
                {"style_data": "\n".join(style_data)},
            ).prompt
            config = generation_config_from_profile("voice_baseline_analysis")

            result = await self._llm.generate(prompt, config)
            raw = result.content.strip()

            baseline_data = self._parse_baseline_json(raw)
            logger.info("LLM 基准风格提取完成 novel=%s", novel_id)
            return baseline_data

        except Exception as e:
            logger.warning("LLM 基准风格提取失败 novel=%s: %s", novel_id, e)
            return self._compute_simple_baseline(style_vectors)

    def compute_drift_score(
        self,
        style_vector: Dict[str, float],
        baseline: Dict[str, Any],
    ) -> float:
        """计算风格漂移分数（0-1，越高越接近基准）

        Args:
            style_vector: 当前章节的风格向量
            baseline: 基准风格数据（含 baseline 和 tolerance）

        Returns:
            相似度分数（0-1）
        """
        if not baseline or "baseline" not in baseline:
            return 1.0  # 无基准时不判漂移

        base = baseline.get("baseline", {})
        tol = baseline.get("tolerance", {})

        total_diff = 0.0
        dimensions = 0

        for dim in STYLE_DIMENSIONS:
            if dim not in base:
                continue

            current_val = style_vector.get(dim, 0.5)
            base_val = base.get(dim, 0.5)
            tolerance = tol.get(dim, 0.2)  # 默认容差

            # 计算相对偏差
            if tolerance > 0:
                rel_diff = abs(current_val - base_val) / tolerance
            else:
                rel_diff = abs(current_val - base_val)

            # ⚠ 第 86 批（N-17）：单维偏差改用**软饱和**，不再硬截断在 1.0。
            #
            #   此前是 ``min(1.0, rel_diff)`` ⇒ **每个维度**的偏差被截断在 1.0
            #   ⇒ 「差 1 个容差」与「差 5 个容差」在该维**完全同分**
            #   ⇒ 已饱和的那一维**怎么改都没反应**（分数不动 ⇒ 循环无从收敛）。
            #
            #   ⚠ 别把它说成「总分被量化成 1/N 的整数倍」——**那是不对的**：
            #     截断发生在**单维**上；只要各维偏差都还不到 1 个容差，
            #     总分仍然是连续的（0.4167 这类值就是这么来的）。
            #     真正的病灶是「**有几维已经饱和** ⇒ 那一维的改进被吃掉」。
            #
            #   ⇒ 现在用指数软饱和 ``1 - e^(-x)``：
            #     * **严格单调** ⇒ 任何一维动了一点，分数就动（可收敛）；
            #     * 仍落在 0..1；
            #     * 阈值口径**略微放宽**（越靠近 0 越接近原值）：
            #       相似度 0.75 ⇔ 平均偏差 0.288 个容差（线性口径是 0.25，约 +15%）
            #       相似度 0.68 ⇔ 平均偏差 0.386 个容差（线性口径是 0.32，约 +21%）
            #       ⇒ 方向是「**少误报**」，与「几乎每章都被误报严重偏离」这个
            #         要解决的问题方向一致；两个阈值**不用重标定**
            #         （含义是「相似度低于 X = 偏离」，这个含义没变）。
            normalized_diff = 1.0 - math.exp(-rel_diff)
            total_diff += normalized_diff
            dimensions += 1

        if dimensions == 0:
            return 1.0

        # 相似度 = 1 - 平均偏差
        avg_diff = total_diff / dimensions
        similarity = max(0.0, 1.0 - avg_diff)

        return round(similarity, 4)

    def build_baseline_from_vectors(
        self, style_vectors: List[Dict[str, float]]
    ) -> Dict[str, Any]:
        """用**同量纲**的风格向量建基准（第 86 批 · N-18）。

        ⚠ 基准与「当前值」必须**同一个量纲**，否则那一维会**永久饱和**
          （偏差恒 ≫ 1 个容差 ⇒ 那一维的改进永远看不见）。
          ⇒ 生产实测（N-18）：旧基准的 ``description_depth`` 是 **0.0239**
            （形容词密度口径），而当前值是 LLM 的 0~1 ⇒ rel_diff = **4.761**
            ⇒ 硬截断口径下相似度天花板恰好 **0.75**，而修文阈值是 **0.68**
            ⇒ **几乎每章都触发 3 轮定向修文**。
        """
        return self._compute_simple_baseline(style_vectors)

    @staticmethod
    def vector_to_json(style_vector: Dict[str, Any]) -> str:
        """风格向量 → JSON（落库用）。

        ⚠ 去掉 ``_meta``（那是本次分析的元信息，不是风格维度）。
        """
        clean = {k: v for k, v in style_vector.items() if not k.startswith("_")}
        return json.dumps(clean, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def vector_from_json(raw: Any) -> Dict[str, float]:
        """JSON → 风格向量（读库用）。

        ⚠ 坏数据一律返回**空字典**（当「没有这一维」处理）
          ⇒ 宁可少比一维，也不拿半个向量去算分。
        """
        if not raw or not isinstance(raw, str):
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, float] = {}
        for key, value in data.items():
            if key.startswith("_"):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            out[key] = float(value)
        return out

    def _parse_style_json(self, raw: str) -> Dict[str, float]:
        """解析风格 JSON"""
        try:
            # 尝试直接解析
            data = json.loads(raw)
            result = {}
            for dim in STYLE_DIMENSIONS:
                val = data.get(dim, 0.5)
                result[dim] = max(0.0, min(1.0, float(val)))
            return result
        except json.JSONDecodeError:
            # 尝试提取 JSON 块
            import re
            match = re.search(r'\{[^}]+\}', raw)
            if match:
                try:
                    data = json.loads(match.group())
                    result = {}
                    for dim in STYLE_DIMENSIONS:
                        val = data.get(dim, 0.5)
                        result[dim] = max(0.0, min(1.0, float(val)))
                    return result
                except (json.JSONDecodeError, ValueError):
                    pass
            return self._get_neutral_style(0)

    def _parse_baseline_json(self, raw: str) -> Dict[str, Any]:
        """解析基准风格 JSON"""
        try:
            data = json.loads(raw)
            return {
                "baseline": data.get("baseline", {}),
                "tolerance": data.get("tolerance", {}),
            }
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                    return {
                        "baseline": data.get("baseline", {}),
                        "tolerance": data.get("tolerance", {}),
                    }
                except (json.JSONDecodeError, ValueError):
                    pass
            return self._get_default_baseline()

    def _get_neutral_style(self, chapter_number: int) -> Dict[str, Any]:
        """返回中性风格向量"""
        return {
            dim: 0.5 for dim in STYLE_DIMENSIONS
        }

    def _get_default_baseline(self) -> Dict[str, Any]:
        """返回默认基准"""
        baseline = {dim: 0.5 for dim in STYLE_DIMENSIONS}
        tolerance = {dim: 0.3 for dim in STYLE_DIMENSIONS}
        return {"baseline": baseline, "tolerance": tolerance}

    def _compute_simple_baseline(self, style_vectors: List[Dict[str, float]]) -> Dict[str, Any]:
        """简单平均计算基准"""
        if not style_vectors:
            return self._get_default_baseline()

        # 过滤掉元数据
        clean_vectors = []
        for vec in style_vectors:
            clean = {k: v for k, v in vec.items() if not k.startswith("_")}
            clean_vectors.append(clean)

        # 计算平均值和标准差
        baseline = {}
        tolerance = {}

        for dim in STYLE_DIMENSIONS:
            values = [v.get(dim, 0.5) for v in clean_vectors if dim in v]
            if values:
                avg = sum(values) / len(values)
                # 标准差
                if len(values) > 1:
                    variance = sum((x - avg) ** 2 for x in values) / len(values)
                    std = variance ** 0.5
                else:
                    std = 0.2
                baseline[dim] = round(avg, 4)
                tolerance[dim] = round(max(0.1, std * 1.5), 4)
            else:
                baseline[dim] = 0.5
                tolerance[dim] = 0.3

        return {"baseline": baseline, "tolerance": tolerance}
