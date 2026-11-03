"""文风漂移监控服务

支持两种模式：
1. 统计模式（默认）：基于形容词密度、句长等统计特征，零成本
2. LLM 模式：使用 LLM 分析深度风格特征，约 500 token/章

连续 N 章低于阈值时发出告警。
"""
import re
import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from domain.novel.value_objects.novel_id import NovelId
from application.analyst.services.voice_analysis_settings import (
    VoiceAnalysisRuntimeSettings,
    VOICE_REWRITE_THRESHOLD_DEFAULT,
    get_voice_analysis_runtime_settings,
)
from application.analyst.services.voice_drift_assessment import (
    RECENT_MEDIAN_WINDOW,
    assess,
    recent_similarity_median,
)
from application.engine.services.ai_participation_settings import (
    ai_participation_enabled,
)

if TYPE_CHECKING:
    from application.analyst.services.llm_voice_analysis_service import LLMVoiceAnalysisService

logger = logging.getLogger(__name__)

# 连续低分章节数触发告警
DRIFT_ALERT_CONSECUTIVE = 5
# LLM 模式建基准所需的**最少同量纲样本**数（第 86 批 · N-18）。
#
# ⚠ 为什么单独一个常量：基准必须与「当前值」**同一把尺子**（都是 LLM 的 0~1 量纲）。
#   旧实现用统计指标当基准 ⇒ 量纲错配 ⇒ 该维永久饱和 ⇒ 相似度天花板被压到 0.75
#   ⇒ 修文阈值 0.68 几乎**每章都触发** ⇒ 白烧钱。
LLM_BASELINE_MIN_SAMPLES = 5
# 相似度告警阈值（**默认值**）。
# 实际取值优先读 ``VoiceAnalysisRuntimeSettings.drift_alert_threshold``；
# 保留本常量是为了不破坏 daemon_host._similarity_below_warning_threshold 的导入。
#
# ⚠ 第 86 批：0.75 → **0.50**（口径重标定）。
#   相似度是「偏差 ÷ 容差」，容差 = ``max(0.1, 标准差×1.5)``
#   ⇒ **健康章节的典型得分天然落在 0.5 附近**（差 1 个标准差是常态）。
#   实测（两本书 12 个健康章节，真模型跑出来的）：min 0.468 / 中位 0.554 / 均值 0.569 / max 0.671
#   ⇒ 0.75 对健康章节**命中率 100%** ⇒ 每章都误报偏离 ⇒ 白烧钱。
DRIFT_ALERT_THRESHOLD = 0.50

# 常见形容词集合（与 VoiceFingerprintService 保持一致）
_COMMON_ADJECTIVES = set(
    "美丽漂亮英俊帅气可爱温柔善良聪明勇敢坚强勤奋努力认真仔细小心谨慎"
    "大小高低长短粗细胖瘦快慢冷热新旧好坏多少轻重深浅明暗干湿软硬"
    "红橙黄绿青蓝紫黑白灰粉棕金银"
)


class VoiceDriftService:
    """文风漂移监控服务

    支持统计模式和 LLM 模式：
    - 统计模式：零成本，基于简单的文本统计
    - LLM 模式：约 500 token/章，捕捉深度风格特征
    """

    def __init__(
        self,
        score_repo,
        fingerprint_repo,
        llm_voice_service: Optional["LLMVoiceAnalysisService"] = None,
        use_llm_mode: bool = False,
        runtime_settings: VoiceAnalysisRuntimeSettings | None = None,
        llm_service_factory: Optional[Callable[[], Any]] = None,
        chapter_repo: Optional[Any] = None,
    ):
        """
        Args:
            score_repo: SqliteChapterStyleScoreRepository
            fingerprint_repo: VoiceFingerprintRepository
            llm_voice_service: LLM 文风分析服务（可选；不给就**懒创建**）
            use_llm_mode: LLM 模式的**默认级**（默认 False = 统计模式）
            llm_service_factory: 懒创建 LLM 文风服务时用的「取 LLM 服务」工厂
                （⚠ 给工厂而不是实例 ⇒ 统计模式下**启动时什么都不建**）
            chapter_repo: 章节仓储（可选）
                ⚠ 第 86 批（N-18）：只用于**基准回填**——
                  给「早于 LLM 模式开启就已写完」的书补齐同量纲基准的原料。
                  没有它 ⇒ 那类书的基准只能等新章节慢慢攒（相似度先记「未评估」）。
        """
        self.score_repo = score_repo
        self.fingerprint_repo = fingerprint_repo
        self.llm_voice_service = llm_voice_service
        #: LLM 模式的**默认级**（构造时的取值）⇒ 真值由属性 ``use_llm_mode`` 解析。
        self._use_llm_mode_default = bool(use_llm_mode)
        #: 懒创建 LLM 文风服务用的工厂（可选）。
        self._llm_service_factory = llm_service_factory
        self._runtime_settings = runtime_settings or get_voice_analysis_runtime_settings()
        #: 章节仓储（可选，N-18 的基准回填用）。
        self._chapter_repo = chapter_repo
        #: 已经试过回填的书（第 86 批 · N-18）。
        #: ⚠ 必须记：回填要**调模型**，失败时若不记，每章都会重试一次
        #:   ⇒ 一本书能把调用次数翻好几倍（正是本批要治的「白烧钱」）。
        self._backfill_attempted: set[str] = set()

        self._baseline_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._baseline_cache_loaded_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 开关与懒创建
    # ------------------------------------------------------------------

    @property
    def use_llm_mode(self) -> bool:
        """文风漂移是否走 LLM 模式（**每次读都解析**）。

        ⚠ 第 79 批：以前这是**构造时冻结**的普通属性 ⇒ 服务是进程级单例
          ⇒ 「这本书」的覆盖（novel 级）在它身上**没有落点** ⇒ 静默无效。
        ⇒ 现在按统一读取层解析：**novel 级 > env 级 > 默认级**
          ⇒ 本书把 ``voice_llm_mode`` 设成 true 时，这本书的文风审计真的走 LLM。

        ⚠ 4 个生产调用点读的都是这个属性 ⇒ 它们**自动**跟着上下文走，
          不必逐个改签名。
        """
        from application.engine.services.ai_switch_layer import resolve_switch

        return resolve_switch("voice_llm_mode", default=self._use_llm_mode_default)

    @use_llm_mode.setter
    def use_llm_mode(self, value: bool) -> None:
        """显式设置时改的是**默认级**（兼容既有写法）。"""
        self._use_llm_mode_default = bool(value)

    def _ensure_llm_voice_service(self):
        """按需创建 LLM 文风服务（**懒创建**）。

        ⚠ 以前 LLM 文风服务只在**启动时**按 env 决定要不要建
          ⇒ 就算开关解析对了（本书想开），服务不存在也走不到 LLM 模式。
        ⇒ 改成懒创建：**统计模式下启动时什么都不建**（不擅自开 LLM 调用链），
          真要跑 LLM 模式时再建。
        """
        if self.llm_voice_service is None and self._llm_service_factory is not None:
            from application.analyst.services.llm_voice_analysis_service import (
                LLMVoiceAnalysisService,
            )

            self.llm_voice_service = LLMVoiceAnalysisService(
                llm_service=self._llm_service_factory(),
                runtime_settings=self._runtime_settings,
            )
        return self.llm_voice_service

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def score_chapter_async(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        pov_character_id: Optional[str] = None,
    ) -> dict:
        """异步版本：计算章节文风评分并持久化（支持 LLM 模式）

        Returns:
            包含 chapter_number, similarity_score, drift_alert 的字典
        """
        # ⚠ **总闸在服务层收口**（第 3 批二次复审 N3）：
        #   `use_llm_mode` 有 **4 个生产调用点**（`engine/pipeline/base.py`、
        #   `engine/runtime/daemon_host.py`、`application/engine/services/
        #   chapter_aftermath_pipeline.py`、`application/engine/dag/nodes/
        #   validation_nodes.py`）——此前只有 base.py 那处接了总闸，
        #   另外 3 处只判 `use_llm_mode` ⇒ 总闸关掉它们照样打模型。
        #   ⇒ 现在**闸门收在这一处**（分派点）⇒ 4 个调用点一次全覆盖，
        #   且「同一逻辑写两份」也消掉了（base.py 那处可以撤）。
        if (
            self.use_llm_mode
            and ai_participation_enabled("voice_llm_mode")
            and self._ensure_llm_voice_service() is not None
        ):
            return await self._score_chapter_llm(novel_id, chapter_number, content)
        else:
            return self.score_chapter(novel_id, chapter_number, content, pov_character_id)

    def score_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        pov_character_id: Optional[str] = None,
    ) -> dict:
        """同步版本：统计模式计算章节文风评分并持久化。

        若作者指纹不存在，similarity_score 记为 None。

        Returns:
            包含 chapter_number, similarity_score, drift_alert 的字典
        """
        metrics = self._compute_metrics(content)
        fingerprint = self.fingerprint_repo.get_by_novel(novel_id, pov_character_id)

        if fingerprint and fingerprint.get("sample_count", 0) >= 10:
            similarity = self._cosine_similarity(metrics, fingerprint)
        else:
            similarity = None

        self.score_repo.upsert(
            novel_id=novel_id,
            chapter_number=chapter_number,
            adjective_density=metrics["adjective_density"],
            avg_sentence_length=metrics["avg_sentence_length"],
            sentence_count=metrics["sentence_count"],
            # ⚠ 第 82 批（N-05）：**没有基准 ⇒ 这一章没评过** ⇒ 存 None（可空列）
            #   （此前压成 0.0 ⇒ 与「真·相似度 0.0」同形
            #    ⇒ 下游那条 `IS NOT NULL` 过滤恒真 ⇒ 无基准的章节被当成
            #    「相似度 0.0（远低于阈值）」⇒ **每本书的漂移告警都常亮**）
            similarity_score=similarity,
        )

        drift_alert = self._check_drift_alert(novel_id)

        return {
            "chapter_number": chapter_number,
            "metrics": metrics,
            "similarity_score": similarity,
            "drift_alert": drift_alert,
            # ⚠ 第 89 批（N-24）：统计模式没有「同量纲基准」⇒ 判定回落绝对口径
            "drift_assessment": self.assess_chapter(novel_id, similarity, chapter_number),
            "mode": "statistics",
        }

    async def _score_chapter_llm(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
    ) -> dict:
        """LLM 模式：使用 LLM 分析章节风格

        自动建立基准：前 5 章用于建立基准，之后开始检测漂移。

        ⚠ 第 86 批（N-18）：基准改成**同量纲**（本书自己的 LLM 风格向量），
          详见 ``_build_baseline_from_store``。
        """
        # 1. 分析当前章节风格
        style_vector = await self.llm_voice_service.analyze_chapter_style(
            novel_id, chapter_number, content
        )

        # 2. 获取或建立基准（⚠ 第 86 批：可能触发**回填**，要调模型 ⇒ async）
        baseline = await self._get_or_init_baseline(novel_id)

        # 3. 计算相似度
        if baseline and chapter_number > 5:
            similarity = self.llm_voice_service.compute_drift_score(style_vector, baseline)
        else:
            # 前 5 章用于建立基准，不判漂移
            similarity = None

        # 4. 更新基准（每 5 章重新计算）
        if chapter_number % 5 == 0:
            await self._update_baseline(novel_id)

        # 5. 持久化（兼容现有表结构）
        # ⚠ 第 86 批（N-18）：统计列**保持统计口径**（**一列一义**）。
        #   此前这里写的是 LLM 派生值（description_depth ⇒ adjective_density、
        #   pacing*50 ⇒ avg_sentence_length）⇒ 同一列在不同章上是**两套量纲**
        #   （生产实测：ch1~7 是统计值 0.0245/15.13，ch8+ 是 LLM 值 0.6/35.0）
        #   ⇒ 谁读这一列都说不清它是什么。
        #   ⇒ 现在两列都写**统计口径**的值；LLM 的判断另存 ``style_vector_json``。
        metrics = self._compute_metrics(content)

        self.score_repo.upsert(
            novel_id=novel_id,
            chapter_number=chapter_number,
            adjective_density=metrics["adjective_density"],
            avg_sentence_length=metrics["avg_sentence_length"],
            sentence_count=metrics["sentence_count"],
            # ⚠ 第 82 批（N-05）：**没有基准 ⇒ 这一章没评过** ⇒ 存 None（可空列）
            similarity_score=similarity,
            # ⚠ 第 86 批（N-18）：风格向量落库 ⇒ 它是「同量纲基准」的原料
            style_vector_json=self.llm_voice_service.vector_to_json(style_vector),
        )

        drift_alert = self._check_drift_alert(novel_id)

        # ⚠ 第 89 批（N-24）：**书内相对**的判定（告警/修文/面板三处共用这一份）
        assessment = self.assess_chapter(novel_id, similarity, chapter_number)

        logger.debug(
            "LLM 文风评分完成 novel=%s ch=%d similarity=%s drift=%s basis=%s drop=%s",
            novel_id, chapter_number, similarity, drift_alert,
            assessment.get("basis"), assessment.get("relative_drop"),
        )
        # ⚠ 第 89 批（N-24）：相对判定的结果**提到 INFO** ——
        #   生产日志里要能看见「这一章比本书近期常态低多少」
        #   （否则这套新判定等于黑箱，出问题没法查）。
        logger.info(
            "[%s] 第 %d 章文风：similarity=%s 常态=%s 相对跌幅=%s 判据=%s 建议改写=%s",
            novel_id, chapter_number, similarity,
            assessment.get("book_median"), assessment.get("relative_drop"),
            assessment.get("basis"), assessment.get("rewrite"),
        )

        return {
            "chapter_number": chapter_number,
            "style_vector": style_vector,
            "similarity_score": similarity,
            "drift_alert": drift_alert,
            "drift_assessment": assessment,
            "mode": "llm",
        }

    async def _get_or_init_baseline(self, novel_id: str) -> Optional[dict]:
        """获取或初始化基准风格（第 86 批 · N-18：改成**同量纲**）。

        ⚠ 病根：旧实现从**统计口径**的指标推基准
          （``adjective_density`` 当作 ``description_depth``、
          ``avg_sentence_length/50`` 当作 ``pacing``），
          而「当前值」是 LLM 给的 0~1 ⇒ **两把尺子量同一个东西**。
          ⇒ 生产实测：基准 description_depth = **0.0239** vs 当前值 ≈ 0.5
            ⇒ rel_diff = **4.761** ⇒ 该维**永久饱和**
            ⇒ 硬截断口径下相似度天花板恰好 **0.75**，而修文阈值是 **0.68**
            ⇒ **几乎每章都判成「严重偏离」⇒ 每次都触发 3 轮定向修文 ⇒ 白烧钱**。

        ⇒ 现在基准用**本书自己的 LLM 风格向量**建（同一把尺子）。
        ⇒ 凑不够样本 ⇒ 返回 ``None`` ⇒ 上游记「**未评估**」
          （宁可没数字，也不用错量纲的数字凑一个「看起来评过了」的分数）。
        """
        cached = self._get_cached_baseline(novel_id)
        if cached is not None:
            return cached

        baseline = await self._build_baseline_from_store(novel_id)
        if baseline is None:
            return None

        self._remember_baseline(novel_id, baseline)
        return baseline

    async def _build_baseline_from_store(self, novel_id: str) -> Optional[dict]:
        """用**已落库的同量纲风格向量**建基准（第 86 批 · N-18）。"""
        llm = self.llm_voice_service
        if llm is None:
            return None

        vectors = self._load_baseline_vectors(novel_id)
        if len(vectors) < LLM_BASELINE_MIN_SAMPLES:
            vectors = await self._backfill_baseline_vectors(novel_id)
        if len(vectors) < LLM_BASELINE_MIN_SAMPLES:
            logger.info(
                "文风基准样本不足（需要 %d，现有 %d）novel=%s ⇒ 相似度记为未评估",
                LLM_BASELINE_MIN_SAMPLES,
                len(vectors),
                novel_id,
            )
            return None

        return llm.build_baseline_from_vectors(vectors)

    def _load_baseline_vectors(self, novel_id: str) -> List[Dict[str, float]]:
        """取本书**最早**的若干条 LLM 风格向量（同一把尺子）。

        ⚠ 按章号升序取 ⇒ 基准是「这本书**开头**的样子」
          ⇒ 后面章节相对它的偏移就是「漂移」（而不是「最近 5 章的样子」，
          后者会让每一章都拿身边邻居当镜子 ⇒ 漂移永远看不出来）。
        """
        llm = self.llm_voice_service
        if llm is None:
            return []
        try:
            rows = self.score_repo.list_by_novel(
                novel_id, limit=LLM_BASELINE_MIN_SAMPLES * 10
            )
        except Exception as exc:  # noqa: BLE001 —— 读不到就当没基准，不许崩
            logger.warning("文风基准：读风格向量失败 novel=%s: %s", novel_id, exc)
            return []

        vectors: List[Dict[str, float]] = []
        for row in rows:
            raw = row.get("style_vector_json")
            if not raw:
                continue
            vector = llm.vector_from_json(raw)
            if not vector:
                continue
            vectors.append(vector)
            if len(vectors) >= LLM_BASELINE_MIN_SAMPLES:
                break
        return vectors

    async def _backfill_baseline_vectors(self, novel_id: str) -> List[Dict[str, float]]:
        """补齐基准原料：分析本书**最早的几章**，把风格向量落库。

        ⚠ 为什么需要：LLM 模式的基准要「本书前 5 章」的自建风格，
          但**早于 LLM 模式开启就写完**的书，那些章**没有**风格向量
          ⇒ 不补的话，这本书的漂移检测永远是「未评估」（诚实，但没保护）。
        ⇒ 补一次即可（向量落库 ⇒ 跨进程/重启都在）。

        ⚠ **每本书只试一次**（``_backfill_attempted``）：回填要调模型，
          失败时若每章都重试 ⇒ 一本书能把调用次数翻好几倍
          ⇒ 那正是本批要治的「白烧钱」。
        """
        if novel_id in self._backfill_attempted:
            return []
        self._backfill_attempted.add(novel_id)

        llm = self.llm_voice_service
        if llm is None or self._chapter_repo is None:
            return []

        try:
            chapters = self._chapter_repo.list_by_novel(
                NovelId(novel_id), limit=LLM_BASELINE_MIN_SAMPLES
            )
        except Exception as exc:  # noqa: BLE001 —— 回填失败不影响主流程
            logger.warning("文风基准回填：取章节失败 novel=%s: %s", novel_id, exc)
            return []

        for chapter in chapters:
            content = (getattr(chapter, "content", "") or "").strip()
            number = getattr(chapter, "number", None)
            if not content or number is None:
                continue
            try:
                vector = await llm.analyze_chapter_style(novel_id, int(number), content)
                self.score_repo.upsert_style_vector(
                    novel_id, int(number), llm.vector_to_json(vector)
                )
            except Exception as exc:  # noqa: BLE001 —— 单章失败不拖垮整本
                logger.warning(
                    "文风基准回填：分析失败 novel=%s ch=%s: %s", novel_id, number, exc
                )

        return self._load_baseline_vectors(novel_id)

    async def _update_baseline(self, novel_id: str) -> None:
        """更新基准风格（每 5 章调用一次）。

        ⚠ 第 86 批（N-18）：口径换成**同量纲**（本书自己的 LLM 风格向量）。
          基准的样本是「**最早**被分析过的那几章」⇒ 稳定不变；
          这个刷新只是把「后来补齐的向量」并进来（回填）并续上缓存 TTL。
        """
        if not self.llm_voice_service:
            return

        baseline = await self._build_baseline_from_store(novel_id)
        if baseline is None:
            return
        self._remember_baseline(novel_id, baseline)
        logger.info("基准风格已更新 novel=%s", novel_id)

    def _baseline_cache_enabled(self) -> bool:
        return (
            self._runtime_settings.baseline_cache_ttl_seconds > 0
            and self._runtime_settings.baseline_cache_max_size > 0
        )

    def _get_cached_baseline(self, novel_id: str) -> Optional[dict]:
        if not self._baseline_cache_enabled():
            self._forget_cached_baseline(novel_id)
            return None

        baseline = self._baseline_cache.get(novel_id)
        if baseline is None:
            return None

        loaded_at = self._baseline_cache_loaded_at.get(novel_id, 0.0)
        if time.monotonic() - loaded_at >= self._runtime_settings.baseline_cache_ttl_seconds:
            self._forget_cached_baseline(novel_id)
            return None

        self._baseline_cache.move_to_end(novel_id)
        return baseline

    def _remember_baseline(self, novel_id: str, baseline: dict) -> None:
        if not self._baseline_cache_enabled():
            self._forget_cached_baseline(novel_id)
            return

        self._baseline_cache[novel_id] = baseline
        self._baseline_cache.move_to_end(novel_id)
        self._baseline_cache_loaded_at[novel_id] = time.monotonic()

        max_size = self._runtime_settings.baseline_cache_max_size
        while len(self._baseline_cache) > max_size:
            evicted_novel_id, _ = self._baseline_cache.popitem(last=False)
            self._baseline_cache_loaded_at.pop(evicted_novel_id, None)

    def _forget_cached_baseline(self, novel_id: str) -> None:
        self._baseline_cache.pop(novel_id, None)
        self._baseline_cache_loaded_at.pop(novel_id, None)

    def get_drift_report(self, novel_id: str) -> dict:
        """获取漂移报告（**全量**评分 + 告警状态）。

        ⚠ 第 90 批（N-22）：此前用默认 ``limit=50`` ⇒ 长书只返回**最早** 50 行
          ⇒ 端点承诺的「全量章节评分序列」没兑现 ⇒ 面板看到的是开篇数据。
          ⇒ 现在不限条数。
        """
        scores = self.score_repo.list_by_novel(novel_id, limit=None)
        drift_alert = self._check_drift_alert(novel_id)

        return {
            "novel_id": novel_id,
            "scores": scores,
            "drift_alert": drift_alert,
            "alert_threshold": float(
                getattr(self._runtime_settings, "drift_alert_threshold", DRIFT_ALERT_THRESHOLD)
            ),
            "alert_consecutive": DRIFT_ALERT_CONSECUTIVE,
            "mode": "llm" if self.use_llm_mode else "statistics",
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_metrics(text: str) -> dict:
        """从章节文本提取与指纹相同维度的统计特征。"""
        if not text:
            return {"adjective_density": 0.0, "avg_sentence_length": 0.0, "sentence_count": 0}

        adj_count = sum(1 for ch in text if ch in _COMMON_ADJECTIVES)
        total_chars = len(text)
        adj_density = adj_count / total_chars if total_chars else 0.0

        sentences = re.split(r"[。！？]", text)
        sentences = [s.strip() for s in sentences if s.strip()]
        sentence_count = len(sentences)
        avg_len = sum(len(s) for s in sentences) / sentence_count if sentence_count else 0.0

        return {
            "adjective_density": round(adj_density, 4),
            "avg_sentence_length": round(avg_len, 2),
            "sentence_count": sentence_count,
        }

    @staticmethod
    def _cosine_similarity(chapter_metrics: dict, fingerprint: dict) -> float:
        """用二维向量近似计算相似度（形容词密度 + 平均句长归一化）。

        归一化方式：以指纹值为参照，计算相对差异后映射到 [0,1]。
        """
        def _relative_closeness(a: float, b: float) -> float:
            """返回 a 与 b 的接近程度 (0~1)，b 为参照基准。"""
            if b == 0:
                return 1.0 if a == 0 else 0.0
            diff = abs(a - b) / b
            return max(0.0, 1.0 - diff)

        adj_sim = _relative_closeness(
            chapter_metrics["adjective_density"],
            fingerprint["adjective_density"],
        )
        len_sim = _relative_closeness(
            chapter_metrics["avg_sentence_length"],
            fingerprint["avg_sentence_length"],
        )
        # 加权平均：两个维度各 50%
        return round((adj_sim + len_sim) / 2, 4)

    def _check_drift_alert(self, novel_id: str) -> bool:
        """检查最近 N 章是否**连续**异常。

        跳过 similarity_score 为 None 的章节（没评过 ⇒ 不是「没偏离」）。

        ⚠ 第 82 批（N-04）：窗口必须是**最近** N 章。
          此前用的是 ``list_by_novel``，而它是 ``ORDER BY chapter_number ASC LIMIT ?``
          ⇒ 拿到的是**最早**的 N 章 ⇒ 超过 N 章的书，告警永远反映第 1~N 章。
          ⇒ 现在改用 ``list_recent_by_novel``（**倒序取最近、正序返回**）。

        ⚠ 第 89 批（N-24）：判定改**书内相对** ——
          「最近 N 章都低于阈值」在长书上**恒真**（常态本身就在 0.5 附近）
          ⇒ 改成「最近 N 章都**明显低于本书近期的常态**」。
        """
        rows = self.score_repo.list_recent_by_novel(
            novel_id, limit=RECENT_MEDIAN_WINDOW + DRIFT_ALERT_CONSECUTIVE
        )
        valid_scores = [s for s in rows if s.get("similarity_score") is not None]

        if len(valid_scores) < DRIFT_ALERT_CONSECUTIVE:
            return False

        recent = valid_scores[-DRIFT_ALERT_CONSECUTIVE:]
        # ⚠ 第 89 批：常态必须取**窗口之前**的历史 ——
        #   若把被怀疑的这 5 章也算进中位数 ⇒ 离群值会把常态一起拽低
        #   ⇒ 每一章相对常态的跌幅都被压缩 ⇒ 「连续 5 章都偏低」反而**测不出来**。
        history = valid_scores[:-DRIFT_ALERT_CONSECUTIVE]
        median = recent_similarity_median(
            [s["similarity_score"] for s in history]
        )
        alert_threshold = float(
            getattr(self._runtime_settings, "drift_alert_threshold", DRIFT_ALERT_THRESHOLD)
        )
        rewrite_threshold = float(
            getattr(
                self._runtime_settings,
                "voice_rewrite_threshold",
                VOICE_REWRITE_THRESHOLD_DEFAULT,
            )
        )
        relative_alert_drop = float(
            getattr(self._runtime_settings, "drift_relative_drop_alert", 0.15)
        )
        relative_rewrite_drop = float(
            getattr(self._runtime_settings, "voice_rewrite_relative_drop", 0.25)
        )
        return all(
            assess(
                s["similarity_score"],
                median,
                alert_threshold,
                rewrite_threshold,
                relative_alert_drop=relative_alert_drop,
                relative_rewrite_drop=relative_rewrite_drop,
            )["alert"]
            for s in recent
        )

    # ── 第 89 批（N-24）：书内相对判定 ──────────────────────────────────

    def _recent_similarity_median(
        self, novel_id: str, exclude_chapter_number: Optional[int] = None
    ) -> Optional[float]:
        """本书**近期常态** = 最近 N 章相似度的中位数。

        ⚠ 判「当前章」时必须**把自己排除掉** ⇒ 否则一个异常低值会把自己的
          中位数拉下来 ⇒ 把自己「洗白」。
        """
        rows = self.score_repo.list_recent_by_novel(
            novel_id, limit=RECENT_MEDIAN_WINDOW + 2
        )
        return recent_similarity_median(
            [r.get("similarity_score") for r in rows],
            exclude_chapter_number=exclude_chapter_number,
            chapters=[r.get("chapter_number") for r in rows],
        )

    def assess_chapter(
        self,
        novel_id: str,
        similarity: Optional[float],
        chapter_number: Optional[int] = None,
    ) -> dict:
        """判定一章的相似度：**相对优先，历史不足时回落绝对**（第 89 批 · N-24）。

        ⚠ 这是**唯一**的判定入口 ⇒ 告警、修文、面板三处都用它
          （同一逻辑不许写两份）。
        """
        median = self._recent_similarity_median(
            novel_id, exclude_chapter_number=chapter_number
        )
        return assess(
            similarity,
            median,
            absolute_alert_threshold=float(
                getattr(
                    self._runtime_settings,
                    "drift_alert_threshold",
                    DRIFT_ALERT_THRESHOLD,
                )
            ),
            absolute_rewrite_threshold=float(
                getattr(
                    self._runtime_settings,
                    "voice_rewrite_threshold",
                    VOICE_REWRITE_THRESHOLD_DEFAULT,
                )
            ),
            relative_alert_drop=float(
                getattr(self._runtime_settings, "drift_relative_drop_alert", 0.15)
            ),
            relative_rewrite_drop=float(
                getattr(self._runtime_settings, "voice_rewrite_relative_drop", 0.25)
            ),
        )
