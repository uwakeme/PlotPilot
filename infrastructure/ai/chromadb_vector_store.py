# infrastructure/ai/chromadb_vector_store.py
"""
基于 FAISS 的向量存储实现（纯本地，兼容 Windows）

⚠️ 重要：本模块采用懒加载（Lazy Import）策略。
  faiss / numpy 均不在模块顶层导入，而是在 __init__ 和各方法中
  按需导入。这样即使未安装 requirements-local.txt 的用户，
  import 本模块也不会崩溃。

使用 FAISS 进行向量检索，使用 JSON 文件管理元数据。
命名保持 ChromaDB 以兼容现有代码。
"""
from typing import List, Optional
import asyncio
import json
import os
import logging
from pathlib import Path

from domain.ai.services.vector_store import VectorStore
from domain.novel.value_objects.chapter_renumber_spec import ChapterRenumberSpec



class _SimpleFlatL2Index:
    """Small local L2 index used when optional faiss/numpy packages are absent."""

    def __init__(self, dimension: int):
        self.d = int(dimension)
        self._vectors: list[list[float]] = []

    @property
    def ntotal(self) -> int:
        return len(self._vectors)

    def add(self, vectors) -> None:
        for raw in vectors:
            row = [float(x) for x in raw]
            if len(row) != self.d:
                raise ValueError(f"vector dimension mismatch: expected {self.d}, got {len(row)}")
            self._vectors.append(row)

    def search(self, query_vectors, limit: int):
        distance_rows = []
        index_rows = []
        for raw_query in query_vectors:
            query = [float(x) for x in raw_query]
            scored = []
            for idx, vector in enumerate(self._vectors):
                distance = sum((a - b) ** 2 for a, b in zip(query, vector))
                scored.append((distance, idx))
            scored.sort(key=lambda item: item[0])
            top = scored[: max(0, int(limit))]
            distance_rows.append([item[0] for item in top])
            index_rows.append([item[1] for item in top])
        return distance_rows, index_rows

    def reconstruct(self, idx: int) -> list[float]:
        return list(self._vectors[int(idx)])


class _SimpleVectorIndexBackend:
    IndexFlatL2 = _SimpleFlatL2Index

    @staticmethod
    def write_index(index: _SimpleFlatL2Index, path: str) -> None:
        data = {"dimension": index.d, "vectors": index._vectors}
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def read_index(path: str) -> _SimpleFlatL2Index:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        index = _SimpleFlatL2Index(int(data.get("dimension") or 0))
        index.add(data.get("vectors") or [])
        return index


def _get_vector_index_backend():
    """Return the fastest available vector index backend."""
    try:
        import faiss  # type: ignore
        return faiss
    except ImportError:
        return _SimpleVectorIndexBackend


def _as_vector_matrix(vector: List[float]):
    """Convert one vector to a backend-compatible 2D matrix."""
    try:
        import numpy as np  # type: ignore
        return np.array([vector], dtype=np.float32)
    except ImportError:
        return [[float(x) for x in vector]]


def _as_vector_batch(vectors: list) -> list:
    try:
        import numpy as np  # type: ignore
        return np.array(vectors, dtype=np.float32)
    except ImportError:
        return [[float(x) for x in row] for row in vectors]

_vector_renumber_log = logging.getLogger(__name__)


def _vector_payload_targets_novel(collection: str, novel_id: str, payload: dict) -> bool:
    """约定：novel_{id}_chunks / novel_{id}_triples 整库属于该书；其它 collection 用 payload.novel_id。"""
    if collection == f"novel_{novel_id}_chunks" or collection == f"novel_{novel_id}_triples":
        return True
    return payload.get("novel_id") == novel_id


def _vector_id_after_chapter_shift(
    collection: str,
    old_vector_id: str,
    payload: dict,
    novel_id: str,
    new_chapter_number: int,
) -> str:
    """与 ChapterIndexingService / IndexingService / TripleIndexingService 的 id 规则对齐。"""
    if collection == f"novel_{novel_id}_triples" or payload.get("triple_id"):
        return old_vector_id
    kind = payload.get("kind")
    if kind == "chapter_summary":
        return f"{novel_id}_ch{new_chapter_number}_summary"
    if kind == "bible_snippet":
        return f"{novel_id}_ch{new_chapter_number}_bible"
    if collection == "chapters" or old_vector_id.startswith(f"{novel_id}_"):
        return f"{novel_id}_{new_chapter_number}"
    return old_vector_id


class ChromaDBVectorStore(VectorStore):
    """基于 FAISS 的向量存储实现（纯本地，兼容 Windows）

    使用 FAISS 进行向量检索，使用 JSON 文件管理元数据。
    命名保持 ChromaDB 以兼容现有代码。

    所有重依赖（faiss, numpy）均采用懒加载策略。
    """

    def __init__(self, persist_directory: str = "./data/chromadb"):
        """
        初始化向量存储

        Args:
            persist_directory: 本地持久化目录
        """
        self._index_backend = _get_vector_index_backend()
        self.persist_directory = Path(persist_directory)
        self.persist_directory.mkdir(parents=True, exist_ok=True)
        self.collections = {}  # {collection_name: {"index": faiss.Index, "metadata": dict}}
        # 延迟落盘：每条 insert 全量 write_index + json.dump 是 O(n²) 磁盘写
        #（千章积累下索引 MB 级），改为脏标记 + 阈值触发 + 退出兜底
        self._dirty: dict = {}          # {collection: pending_writes}
        self._last_flush_at: dict = {}  # {collection: monotonic}
        # ⚠ 第 88 批（N-23）：**落盘目录是跨进程共享的**（API 进程与守护进程
        #   各持一份实例，但写同一个目录）⇒ 必须记住「我们上次见到磁盘时」
        #   的状态，才能发现「另一个进程在我们之后写过」并做合并。
        self._disk_state: dict = {}     # {collection: (mtime, size)}
        self._op_lock = asyncio.Lock()
        self._load_collections()
        import atexit

        atexit.register(self._atexit_flush)

    def _load_collections(self):
        """加载所有已存在的集合"""
        index_backend = _get_vector_index_backend()

        if not self.persist_directory.exists():
            return

        for collection_dir in self.persist_directory.iterdir():
            if collection_dir.is_dir():
                collection_name = collection_dir.name
                index_path = collection_dir / "index.faiss"
                metadata_path = collection_dir / "metadata.json"

                if index_path.exists() and metadata_path.exists():
                    index = index_backend.read_index(str(index_path))
                    with open(metadata_path, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                    self.collections[collection_name] = {
                        "index": index,
                        "metadata": metadata
                    }
                    self._disk_state[collection_name] = self._current_disk_state(
                        collection_name
                    )

    # ── 磁盘真相（跨进程共享 ⇒ 内存只是缓存）──────────────────────────

    def _current_disk_state(self, collection: str) -> Optional[tuple]:
        """磁盘上该集合索引文件的 ``(mtime, size)``；不在磁盘上则 ``None``。

        ⚠ 第 88 批（N-23）：落盘目录被**多个进程共用**
        ⇒ 「我们见到磁盘的样子」必须能被记录与比较，
          否则无法判断「另一个进程在我们之后改过它」。
        """
        index_path = self.persist_directory / collection / "index.faiss"
        try:
            stat = index_path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    def _read_disk_collection(self, collection: str) -> Optional[dict]:
        """从磁盘读一个集合（``index`` + ``metadata``）；读不到/坏了 ⇒ ``None``。"""
        index_backend = _get_vector_index_backend()
        collection_dir = self.persist_directory / collection
        index_path = collection_dir / "index.faiss"
        metadata_path = collection_dir / "metadata.json"
        if not (index_path.exists() and metadata_path.exists()):
            return None
        try:
            index = index_backend.read_index(str(index_path))
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
        except Exception as exc:  # noqa: BLE001 —— 坏文件当「没有」，别拖垮流程
            logging.getLogger(__name__).warning(
                "向量集合磁盘文件读取失败（当作不存在）collection=%s err=%s", collection, exc
            )
            return None
        if not isinstance(metadata, dict):
            return None
        return {"index": index, "metadata": metadata}

    def _load_collection_from_disk(self, collection: str) -> bool:
        """把磁盘上的集合载入**本进程内存**（成功 ⇒ True）。

        ⚠ 第 88 批（N-23）：这是「**别把别人的数据清空**」的关键 ——
          集合可能不在本进程内存里（另一个进程创建的），
          但**在磁盘上是有数据的** ⇒ 此时若新建空索引并覆盖磁盘，
          另一个进程写进去的向量就**全丢了**。
          ⇒ 生产实测：600 章那本书的向量索引被清成了「本次跑的 5 章」的量。
        """
        disk = self._read_disk_collection(collection)
        if disk is None:
            return False
        self.collections[collection] = disk
        self._disk_state[collection] = self._current_disk_state(collection)
        logging.getLogger(__name__).info(
            "从磁盘载入已有向量集合 collection=%s vectors=%d",
            collection, disk["index"].ntotal,
        )
        return True

    def _merge_disk_vectors(self, collection: str) -> int:
        """把磁盘上「**我们没有**」的向量并进来（多进程 ⇒ 防后写覆盖先写）。

        ⇒ 返回并入的条数。
        ⚠ 只并入**维度相同**的向量：维度不同意味着「换了嵌入模型」
          ⇒ 那是另一个语义（该重建，不该合并）。
        """
        disk = self._read_disk_collection(collection)
        if disk is None:
            return 0
        coll = self.collections.get(collection)
        if coll is None:
            return 0

        disk_meta = disk["metadata"]
        disk_index = disk["index"]
        dimension = coll["index"].d
        pending = []
        for vid, entry in disk_meta.items():
            if vid in coll["metadata"]:
                continue
            try:
                vector = disk_index.reconstruct(int(entry["idx"]))
            except Exception:  # noqa: BLE001 —— 单条坏数据跳过即可
                continue
            if len(vector) != dimension:
                continue
            pending.append((vid, vector, entry.get("payload") or {}))

        if not pending:
            return 0

        base = coll["index"].ntotal
        coll["index"].add(_as_vector_batch([v for _, v, _ in pending]))
        for offset, (vid, _vector, payload) in enumerate(pending):
            coll["metadata"][vid] = {"idx": base + offset, "payload": payload}
        return len(pending)

    def _save_collection(self, collection: str):
        """保存集合到磁盘"""
        index_backend = _get_vector_index_backend()

        coll = self.collections[collection]

        # ⚠ 第 88 批（N-23）：落盘目录**跨进程共享** ⇒ 我们写之前，
        #   先看「磁盘是不是被另一个进程改过」⇒ 是的话**先把它的向量并进来**
        #   ⇒ 否则「后写覆盖先写」⇒ 另一个进程刚写的向量全丢。
        known = self._disk_state.get(collection)
        current = self._current_disk_state(collection)
        if known is not None and current is not None and current != known:
            merged = self._merge_disk_vectors(collection)
            if merged:
                logging.getLogger(__name__).warning(
                    "检测到其他进程写过同一集合，已并入其向量 collection=%s merged=%d",
                    collection, merged,
                )
                coll = self.collections[collection]

        collection_dir = self.persist_directory / collection
        collection_dir.mkdir(parents=True, exist_ok=True)

        index_path = collection_dir / "index.faiss"
        metadata_path = collection_dir / "metadata.json"

        index_backend.write_index(coll["index"], str(index_path))
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(coll["metadata"], f, ensure_ascii=False, indent=2)

        # 记住我们刚写下的磁盘状态 ⇒ 下次只有「别人又改了」才会再合并
        self._disk_state[collection] = self._current_disk_state(collection)

    async def insert(
        self,
        collection: str,
        id: str,
        vector: List[float],
        payload: dict
    ) -> None:
        """插入向量到集合中（内存立即生效，落盘延迟批量执行）"""
        await self.insert_batch(collection, [(id, vector, payload)])

    async def insert_batch(
        self,
        collection: str,
        items: List[tuple],
    ) -> None:
        """批量插入向量：一次矩阵 add + 一次脏标记，供章级/三元组批量索引用。"""
        try:
            if collection not in self.collections:
                # ⚠ 第 88 批（N-23）：集合可能只是**不在本进程内存**（另一个进程建的）
                #   ⇒ 先试着从磁盘载入，而不是直接报错。
                if not self._load_collection_from_disk(collection):
                    raise Exception(f"Collection {collection} does not exist")

            async with self._op_lock:
                coll = self.collections[collection]

                dimension = None
                filtered = []
                for point_id, vector, payload in items:
                    if dimension is None:
                        dimension = len(vector)
                    elif len(vector) != dimension:
                        raise Exception("insert_batch vectors must share one dimension")
                    # 已存在的 ID 先移除旧记录（FAISS 端由 compact 回收）
                    coll["metadata"].pop(point_id, None)
                    filtered.append((point_id, vector, payload))

                if filtered and coll["index"].d != dimension:
                    import logging
                    logging.getLogger(__name__).warning(
                        "FAISS索引维度不匹配，自动重建 collection=%s old_dim=%d actual_dim=%d",
                        collection, coll["index"].d, dimension
                    )
                    await self._delete_collection_locked(collection)
                    await self._create_collection_locked(collection, dimension)
                    coll = self.collections[collection]

                index_backend = _get_vector_index_backend()
                matrix = _as_vector_batch([v for _, v, _ in filtered])
                coll["index"].add(matrix)
                base_idx = coll["index"].ntotal - len(filtered)
                for offset, (point_id, _vec, payload) in enumerate(filtered):
                    coll["metadata"][point_id] = {
                        "idx": base_idx + offset,
                        "payload": payload
                    }

                self._mark_dirty(collection, writes=len(filtered))
        except Exception as e:
            raise Exception(f"Failed to insert vector: {str(e)}")

    async def search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int
    ) -> List[dict]:
        """搜索相似向量"""
        try:
            if collection not in self.collections:
                raise Exception(f"Collection {collection} does not exist")

            coll = self.collections[collection]
            if coll["index"].ntotal == 0:
                return []

            query_array = _as_vector_matrix(query_vector)
            distances, indices = coll["index"].search(query_array, min(limit, coll["index"].ntotal))

            # 构建 ID 到索引的反向映射
            idx_to_id = {v["idx"]: k for k, v in coll["metadata"].items()}

            # 转换为统一格式
            output = []
            for i, (dist, idx) in enumerate(zip(distances[0], indices[0])):
                if idx == -1:  # FAISS 返回 -1 表示无效结果
                    continue

                vec_id = idx_to_id.get(int(idx))
                if vec_id:
                    # 将 L2 距离转换为相似度分数 (0-1)
                    score = 1.0 / (1.0 + float(dist))
                    output.append({
                        "id": vec_id,
                        "score": score,
                        "payload": coll["metadata"][vec_id]["payload"]
                    })

            return output
        except Exception as e:
            raise Exception(f"Failed to search vectors: {str(e)}")

    def _mark_dirty(self, collection: str, writes: int = 1) -> None:
        """累计脏写；达到阈值（32 条或距上次落盘 ≥5s）时批量落盘。"""
        import time as _time

        pending = self._dirty.get(collection, 0) + writes
        last = self._last_flush_at.get(collection, 0.0)
        if pending >= 32 or (pending > 0 and _time.monotonic() - last >= 5.0):
            self._flush_collection(collection)
            return
        self._dirty[collection] = pending

    def _flush_collection(self, collection: str) -> None:
        import time as _time

        if collection in self.collections:
            self._save_collection(collection)
        self._dirty.pop(collection, None)
        self._last_flush_at[collection] = _time.monotonic()

    def flush(self) -> None:
        """立即落盘所有脏集合（进程退出兜底 / 关键路径显式调用）。"""
        for collection in list(self._dirty.keys()):
            self._flush_collection(collection)

    def _atexit_flush(self) -> None:
        try:
            self.flush()
        except Exception:
            pass

    async def delete(
        self,
        collection: str,
        id: str
    ) -> None:
        """删除向量（标记删除 + 碎片整理；落盘走脏标记延迟批量）

        当累计删除数量超过阈值时自动触发 compact()，
        重建索引并回收已删除向量的空间，防止内存持续增长。
        """
        try:
            if collection not in self.collections:
                raise Exception(f"Collection {collection} does not exist")

            async with self._op_lock:
                coll = self.collections[collection]
                if id in coll["metadata"]:
                    del coll["metadata"][id]
                    self._mark_dirty(collection)

                    # 碎片整理：当 metadata 条目数 < 索引向量数的 50% 时触发重建
                    self._maybe_compact(collection)
        except Exception as e:
            raise Exception(f"Failed to delete vector: {str(e)}")

    async def create_collection(
        self,
        collection: str,
        dimension: int
    ) -> None:
        """创建集合（若已存在且维度匹配则跳过；维度不匹配时删除后重建）"""
        async with self._op_lock:
            await self._create_collection_locked(collection, dimension)

    async def _create_collection_locked(
        self,
        collection: str,
        dimension: int
    ) -> None:
        """create_collection 的锁内实现（调用方须已持有 _op_lock）。"""
        index_backend = _get_vector_index_backend()

        try:
            if collection in self.collections:
                existing_dim = self.collections[collection]["index"].d
                if dimension == 0 or existing_dim == dimension:
                    return  # 未知维度(0)或维度匹配，跳过重建
                # 嵌入模型已更换，旧索引不兼容，重建
                import logging
                logging.getLogger(__name__).warning(
                    "向量集合维度不匹配，重建索引 collection=%s old_dim=%d new_dim=%d",
                    collection, existing_dim, dimension
                )
                await self._delete_collection_locked(collection)
            elif not self._load_collection_from_disk(collection):
                # ⚠ 第 88 批（N-23）：集合不在本进程内存里、磁盘上也没有
                #   ⇒ 才是真正的「新建」。
                #   （不在内存但**磁盘上有**的情况已由上面的
                #     ``_load_collection_from_disk`` 处理 ⇒ **不许覆盖磁盘**）
                pass
            else:
                # 磁盘上有 ⇒ 载入完成 ⇒ 维度一致就直接用；不一致才重建
                existing_dim = self.collections[collection]["index"].d
                if dimension == 0 or existing_dim == dimension:
                    return
                import logging
                logging.getLogger(__name__).warning(
                    "向量集合维度不匹配，重建索引 collection=%s old_dim=%d new_dim=%d",
                    collection, existing_dim, dimension
                )
                await self._delete_collection_locked(collection)

            # 创建 FAISS 索引（使用 L2 距离）
            index = index_backend.IndexFlatL2(dimension)
            self.collections[collection] = {
                "index": index,
                "metadata": {}
            }
            self._save_collection(collection)
            import time as _time

            self._last_flush_at[collection] = _time.monotonic()  # 建集合即落盘基线，避免首条 insert 误判超时
        except Exception as e:
            raise Exception(f"Failed to create collection: {repr(e)}")

    async def delete_collection(
        self,
        collection: str
    ) -> None:
        """删除集合"""
        async with self._op_lock:
            await self._delete_collection_locked(collection)

    async def _delete_collection_locked(self, collection: str) -> None:
        """delete_collection 的锁内实现（调用方须已持有 _op_lock）。"""
        import shutil

        try:
            if collection in self.collections:
                del self.collections[collection]
            self._dirty.pop(collection, None)
            self._disk_state.pop(collection, None)

            # 删除磁盘文件
            collection_dir = self.persist_directory / collection
            if collection_dir.exists():
                shutil.rmtree(collection_dir)
        except Exception as e:
            raise Exception(f"Failed to delete collection: {str(e)}")

    def _maybe_compact(self, collection: str) -> None:
        """碎片整理：当 metadata 条目数远小于 FAISS 索引向量数时，重建索引。

        delete() 只从 metadata 移除记录，FAISS 索引体积只增不减。
        当碎片率超过 50%（索引中有一半以上的"僵尸"向量）时触发重建，
        回收内存并提高检索精度。
        """
        index_backend = _get_vector_index_backend()

        if collection not in self.collections:
            return

        coll = self.collections[collection]
        metadata_count = len(coll["metadata"])
        index_count = coll["index"].ntotal

        # 碎片率阈值：索引向量数 > metadata 条目数的 2 倍
        if index_count == 0 or metadata_count >= index_count * 0.5:
            return

        _vector_renumber_log.info(
            "FAISS 碎片整理开始: collection=%s index_vectors=%d metadata_entries=%d",
            collection, index_count, metadata_count,
        )

        try:
            # 收集所有存活向量的 ID 和索引
            alive_entries = [(vid, entry) for vid, entry in coll["metadata"].items()]
            if not alive_entries:
                return

            # 提取存活向量
            dimension = coll["index"].d
            vectors = []
            new_metadata = {}
            for new_idx, (vid, entry) in enumerate(alive_entries):
                old_idx = entry["idx"]
                vec = coll["index"].reconstruct(int(old_idx))
                vectors.append(vec)
                new_metadata[vid] = {"idx": new_idx, "payload": entry["payload"]}

            # 重建索引
            new_index = index_backend.IndexFlatL2(dimension)
            if vectors:
                new_index.add(_as_vector_batch(vectors))

            coll["index"] = new_index
            coll["metadata"] = new_metadata
            self._save_collection(collection)

            _vector_renumber_log.info(
                "FAISS 碎片整理完成: collection=%s old=%d new=%d",
                collection, index_count, new_index.ntotal,
            )
        except Exception as e:
            _vector_renumber_log.warning("FAISS 碎片整理失败（不影响功能）: collection=%s err=%s", collection, e)

    async def compact_all(self) -> int:
        """对所有集合执行碎片整理。返回整理的集合数。"""
        count = 0
        for collection in list(self.collections.keys()):
            try:
                self._maybe_compact(collection)
                count += 1
            except Exception:
                pass
        return count

    async def list_collections(self) -> List[str]:
        """列出所有集合。

        ⚠ 第 88 批（N-23）：返回**内存 ∪ 磁盘**。
          落盘目录是**跨进程共享**的 ⇒ 另一个进程建的集合不在本进程内存里
          ⇒ 只列内存 ⇒ 调用方会误判「集合不存在」⇒ 触发 ``create_collection``
          ⇒ 而旧的 ``create_collection`` 会**新建空索引覆盖磁盘** ⇒ 数据全丢。
          ⇒ 这里以**磁盘真相**为准。
        """
        try:
            names = set(self.collections.keys())
            try:
                for entry in self.persist_directory.iterdir():
                    if entry.is_dir():
                        names.add(entry.name)
            except OSError:
                pass
            return sorted(names)
        except Exception as e:
            raise Exception(f"Failed to list collections: {str(e)}")

    def renumber_chapter_metadata_for_novel(
        self,
        spec: ChapterRenumberSpec,
        collection_names: List[str],
    ) -> int:
        """删章并重排章节号后，修正向量元数据中的 chapter_number（及需重建的 point id）。

        仅改写 metadata，不重算向量；与各 IndexingService 写入约定一致。
        """
        changed = 0
        for collection in collection_names:
            if collection not in self.collections:
                continue
            coll = self.collections[collection]
            meta = coll["metadata"]
            for old_id in list(meta.keys()):
                entry = meta.get(old_id)
                if not entry:
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                if not _vector_payload_targets_novel(collection, spec.novel_id, payload):
                    continue
                raw_cn = payload.get("chapter_number")
                if raw_cn is None or isinstance(raw_cn, bool):
                    continue
                try:
                    cn_int = int(raw_cn)
                except (TypeError, ValueError):
                    continue
                new_cn = spec.shift_chapter_ref(cn_int)
                if new_cn == cn_int:
                    continue
                new_payload = dict(payload)
                new_payload["chapter_number"] = new_cn
                new_id = _vector_id_after_chapter_shift(
                    collection, old_id, new_payload, spec.novel_id, new_cn
                )
                entry["payload"] = new_payload
                if new_id == old_id:
                    meta[old_id] = entry
                    changed += 1
                    continue
                if new_id in meta:
                    _vector_renumber_log.warning(
                        "向量重编号 id 已存在，仅更新 payload，建议对该书重扫向量: "
                        "collection=%s old_id=%s new_id=%s",
                        collection,
                        old_id,
                        new_id,
                    )
                    meta[old_id] = entry
                    changed += 1
                    continue
                del meta[old_id]
                meta[new_id] = entry
                changed += 1
            self._save_collection(collection)
        return changed
