"""experience_retriever.py — 语义向量检索经验（Milvus 版）

功能：将经验库的修复经验编码为向量，支持语义检索 top-K。
     LLMFixer 修复前调用 retriever.search() 获取语义相似经验。

设计：
  写入路径：修复成功→record_experience()写入 experience_store.json→
           标记 index_dirty→retriever 检测后增量更新索引
  读取路径：LLMFixer.fix()→提取 buggy_code→编码查询→Milvus 检索 top-3→
           注入 _build_fix_prompt→LLM 参考经验修复

技术栈：
  - BGE-M3 (BAAI/bge-m3): 1024 维向量，语义理解
  - Milvus Lite: 本地文件持久化，零服务依赖
  - 可选：MILVUS_HOST 环境变量可换为全量 Standalone/Cluster

与 ExperienceFixer 的协作：
  - ExperienceFixer: 精确 code_before 字符串匹配（零成本）
  - Retriever: 语义匹配（有 LLM 成本，代码不同但语义相近也能命中）
  - 两者共享同一个 experience_store.json 数据源
  - Pipeline 调用链：ExperienceFixer → LLMFixer(含 Retriever)
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from pymilvus import MilvusClient, DataType
from pymilvus.milvus_client.index import IndexParams, IndexParam

logger = logging.getLogger(__name__)

# ── 项目路径 ──────────────────────────────────

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
DATA_DIR = SWARM_DIR / "data"
EXPERIENCE_FILE = DATA_DIR / "experience_store.json"

# Milvus 存储目录
MILVUS_DB_DIR = DATA_DIR / "milvus"

# 集合名
COLLECTION_NAME = "experience_vectors"

# BGE-M3 固定维度
VECTOR_DIM = 1024


def _get_milvus_uri() -> str:
    """获取 Milvus 连接 URI。

    - 如果设置了 MILVUS_HOST，连远程 Milvus 服务
    - 否则用 Milvus Lite 本地文件存储（零服务依赖）
    """
    host = os.environ.get("MILVUS_HOST")
    if host:
        port = os.environ.get("MILVUS_PORT", "19530")
        logger.info("[Retriever] 连接远程 Milvus: %s:%s", host, port)
        return f"http://{host}:{port}"
    # Milvus Lite 本地模式
    MILVUS_DB_DIR.mkdir(parents=True, exist_ok=True)
    db_file = str(MILVUS_DB_DIR / "milvus.db")
    logger.info("[Retriever] 使用 Milvus Lite 本地存储: %s", db_file)
    return db_file


class ExperienceRetriever:
    """语义检索器：管理经验向量索引，提供 top-K 检索。

    底层用 Milvus（Lite 或远程）替代原 FAISS 本地文件索引。
    支持灰度切换：设置 MILVUS_HOST 即连远程服务，否则自动用 Lite。

    Attributes:
        model_name: embedding 模型名称（默认 BAAI/bge-m3）
        model: SentenceTransformer 实例（延迟加载）
        client: MilvusClient 实例
        _ready: 索引是否就绪
    """

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        self.model_name = model_name
        self._model = None
        self._client: Optional[MilvusClient] = None
        self._dim = VECTOR_DIM
        self._ready = False
        self._init_collection()

    # ── 公共 API ──────────────────────────────

    def search(self, query_text: str, top_k: int = 3, min_score: float = 0.3) -> list[dict]:
        """语义检索 top-K 经验记录。

        1. 编码查询文本为向量
        2. Milvus 向量检索
        3. 过滤低分结果
        4. 从 experience_store.json 取出完整记录

        Args:
            query_text: 查询文本（描述问题类型 + 代码上下文）
            top_k: 返回最多几条
            min_score: 最低相似度阈值

        Returns:
            list[dict]，每条含:
              id, issue_type, action, success, confidence,
              code_before, code_after, description, similarity_score
        """
        if not self._ready or self._client is None:
            logger.debug("[Retriever] 索引不可用，返回空结果")
            return []

        try:
            count = self._client.query(COLLECTION_NAME, output_fields=["count(*)"])
            if not count or count[0]["count(*)"] == 0:
                return []
        except Exception:
            return []

        query_vec = self._encode([query_text])

        try:
            results = self._client.search(
                collection_name=COLLECTION_NAME,
                data=query_vec,
                limit=top_k,
                output_fields=["exp_id"],
            )
        except Exception as e:
            logger.warning("[Retriever] 搜索失败: %s", e)
            return []

        if not results or not results[0]:
            return []

        search_results = []
        for hit in results[0]:
            score = float(hit.get("distance", 0))
            if score < min_score:
                continue
            exp_id = hit.get("entity", {}).get("exp_id", "")
            if not exp_id:
                continue
            full_exp = self._get_experience_by_id(exp_id)
            if not full_exp:
                continue
            search_results.append({
                "id": exp_id,
                "issue_type": full_exp.get("issue_type", ""),
                "action": full_exp.get("action", ""),
                "success": full_exp.get("success", True),
                "confidence": full_exp.get("confidence", 0.5),
                "code_before": full_exp.get("code_before", ""),
                "code_after": full_exp.get("code_after", ""),
                "description": self._generate_description(full_exp),
                "similarity_score": score,
                "_semantic": True,
            })

        search_results.sort(key=lambda r: r["similarity_score"], reverse=True)
        return search_results[:top_k]

    def incremental_update(self) -> int:
        """增量更新索引：只编码新增的经验，插入 Milvus。

        Returns:
            新增的经验数量
        """
        if not self._check_dirty():
            return 0

        existing_ids = self._get_indexed_ids()
        exps = self._load_all_experiences()
        if not exps:
            return 0

        new_exps = [e for e in exps if e.get("id", "") not in existing_ids]
        if not new_exps:
            return 0

        logger.info("[Retriever] 增量更新: %d 条新经验", len(new_exps))
        self._insert_experiences(new_exps)
        return len(new_exps)

    def rebuild_index(self) -> int:
        """全量重建 Milvus 索引。

        Returns:
            索引的经验数量
        """
        exps = self._load_all_experiences()

        # 删旧集合重建
        try:
            if self._client is not None:
                cols = self._client.list_collections() or []
                if COLLECTION_NAME in cols:
                    self._client.drop_collection(COLLECTION_NAME)
        except Exception as e:
            logger.warning("[Retriever] 删除旧集合失败: %s", e)

        self._create_collection()

        if not exps:
            logger.info("[Retriever] 经验库为空，集合已建")
            self._ready = True
            return 0

        logger.info("[Retriever] 全量重建: %d 条经验", len(exps))
        self._insert_experiences(exps)
        return len(exps)

    def get_index_stats(self) -> dict:
        """获取索引状态。"""
        if self._client is None:
            return {"total": 0, "ready": False, "engine": "milvus"}
        try:
            count = self._client.query(COLLECTION_NAME, output_fields=["count(*)"])
            total = count[0]["count(*)"] if count else 0
        except Exception:
            total = 0
        return {
            "total": total,
            "dim": self._dim,
            "model": self.model_name,
            "engine": "milvus",
            "ready": self._ready,
        }

    def close(self) -> None:
        """释放连接。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ── 内部方法 ───────────────────────────────

    def _init_collection(self) -> None:
        """初始化 Milvus 连接和集合。"""
        try:
            uri = _get_milvus_uri()
            self._client = MilvusClient(uri)

            collections = self._client.list_collections() or []
            if COLLECTION_NAME not in collections:
                self._create_collection()
                logger.info("[Retriever] 集合 %s 已创建", COLLECTION_NAME)
            else:
                logger.info("[Retriever] 集合 %s 已存在", COLLECTION_NAME)

            # 增量更新
            try:
                added = self.incremental_update()
                if added > 0:
                    logger.info("[Retriever] 增量更新完成: +%d 条", added)
            except Exception as e:
                logger.warning("[Retriever] 增量更新失败: %s", e)

            self._ready = True
        except Exception as e:
            logger.warning("[Retriever] Milvus 初始化失败（降级为纯字符串匹配）: %s", e)
            self._ready = False

    def _create_collection(self) -> None:
        """创建 Milvus 集合和索引。"""
        if self._client is None:
            return

        schema = MilvusClient.create_schema(
            auto_id=False,
            enable_dynamic_field=True,
        )
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        schema.add_field("exp_id", DataType.VARCHAR, max_length=128)

        self._client.create_collection(COLLECTION_NAME, schema=schema)

        ip = IndexParams()
        ip.add_index(field_name="vector", index_type="FLAT", metric_type="IP")
        self._client.create_index(COLLECTION_NAME, ip)
        self._client.load_collection(COLLECTION_NAME)

    def _insert_experiences(self, exps: list[dict]) -> None:
        """编码并插入经验到 Milvus。"""
        if not exps or self._client is None:
            return

        descriptions = [self._generate_description(e) for e in exps]

        batch_size = 32
        for i in range(0, len(exps), batch_size):
            batch_exps = exps[i:i + batch_size]
            batch_descs = descriptions[i:i + batch_size]
            vecs = self._encode(batch_descs)

            entities = []
            for j, exp in enumerate(batch_exps):
                vec = vecs[j]
                exp_id = exp.get("id", f"exp_{i + j}")
                int_id = abs(hash(exp_id)) % (2**63 - 1)
                entities.append({
                    "id": int_id,
                    "vector": vec,
                    "exp_id": exp_id,
                })

            self._client.insert(COLLECTION_NAME, entities)
            logger.debug("[Retriever] 插入 %d 条经验 (batch %d)", len(entities), i // batch_size)

        self._client.flush(COLLECTION_NAME)
        logger.info("[Retriever] 总计插入 %d 条经验", len(exps))

    def _get_indexed_ids(self) -> set:
        """获取 Milvus 中已索引的所有 exp_id。"""
        if self._client is None:
            return set()
        try:
            results = self._client.query(
                COLLECTION_NAME,
                filter="",
                output_fields=["exp_id"],
                limit=10000,
            )
            return {r.get("exp_id", "") for r in results}
        except Exception:
            return set()

    def _check_dirty(self) -> bool:
        """检查经验库是否有新数据需要同步。"""
        if not EXPERIENCE_FILE.exists():
            return False

        # 集合为空 → 需要重建
        if self._client is None:
            return True
        try:
            count = self._client.query(COLLECTION_NAME, output_fields=["count(*)"])
            if not count or count[0]["count(*)"] == 0:
                return True
        except Exception:
            return True

        # 比较 mtime
        exp_mtime = EXPERIENCE_FILE.stat().st_mtime
        milvus_path = str(MILVUS_DB_DIR / "milvus.db")
        if not os.path.exists(milvus_path):
            return True
        return exp_mtime > os.path.getmtime(milvus_path)

    def _encode(self, texts: list[str]) -> list:
        """将文本编码为向量列表。"""
        if self._model is None:
            self._load_model()
        import numpy as np
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return np.array(vecs, dtype=np.float32).tolist()

    def _load_model(self) -> None:
        """加载 SentenceTransformer 模型。

        优先从本地 modelscope 缓存加载（绕过 huggingface 网络限制），
        回退到在线加载 BAAI/bge-m3。
        """
        try:
            from sentence_transformers import SentenceTransformer
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"

            local_paths = [
                "/mnt/c/Users/qjx/.cache/modelscope/hub/models/BAAI/bge-m3",
                os.path.expanduser("~/.cache/modelscope/hub/models/BAAI/bge-m3"),
            ]
            model_source = None
            for p in local_paths:
                if os.path.isdir(p) and os.path.isfile(os.path.join(p, "config.json")):
                    model_source = p
                    break

            if model_source is None:
                model_source = self.model_name
                logger.info("[Retriever] 在线加载模型 %s (device=%s)...", model_source, device)
            else:
                logger.info("[Retriever] 从本地 modelscope 缓存加载模型 (device=%s)...", device)

            self._model = SentenceTransformer(model_source, device=device)
            logger.info("[Retriever] 模型加载完成")
        except Exception as e:
            logger.warning("[Retriever] 加载模型失败: %s", e)
            raise

    def _generate_description(self, experience: dict) -> str:
        """为一条经验生成检索用的自然语言描述文本。"""
        issue_type = experience.get("issue_type", "unknown")
        action = experience.get("action", "")
        success = experience.get("success", True)
        code_before = experience.get("code_before", "")
        code_after = experience.get("code_after", "")
        error = experience.get("error", "")
        fixer = experience.get("fixer", "")

        code_feature = ""
        if code_before:
            lines = code_before.split("\n")
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith('"""'):
                    code_feature = stripped[:120]
                    break

        if issue_type == "sql_injection":
            desc = f"问题类型: sql_injection。"
            if "f-string" in code_feature or "f'" in code_feature or 'f"' in code_feature:
                desc += f"代码通过 f-string 拼接 SQL 查询。语句: {code_feature}。"
            elif "execute(" in code_feature and ("+" in code_feature or "%" in code_feature or ".format" in code_feature):
                desc += f"代码通过拼接方式构造 execute() 参数。语句: {code_feature}。"
            else:
                desc += f"代码上下文: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        elif issue_type == "command_injection":
            desc = f"问题类型: command_injection。"
            if "os.system" in code_feature or "os.popen" in code_feature:
                desc += f"使用 os.system/os.popen 直接执行命令。语句: {code_feature}。"
            elif "subprocess" in code_feature and "shell=True" in code_feature:
                desc += f"使用 subprocess 但启用 shell=True。语句: {code_feature}。"
            else:
                desc += f"代码上下文: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        elif issue_type == "secret_leak":
            desc = f"问题类型: secret_leak。"
            desc += f"代码中存在硬编码密钥或凭据。语句: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        elif issue_type == "ssrf_vulnerability":
            desc = f"问题类型: ssrf_vulnerability。"
            if "requests" in code_feature or "httpx" in code_feature or "urllib" in code_feature:
                desc += f"代码使用网络请求库直接访问用户输入的 URL。语句: {code_feature}。"
            else:
                desc += f"代码上下文: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        elif issue_type == "missing_null_check":
            desc = f"问题类型: missing_null_check。"
            desc += f"代码未对可能为 None 的值进行判空。语句: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        elif issue_type == "missing_error_handling":
            desc = f"问题类型: missing_error_handling。"
            desc += f"代码缺少异常处理或错误返回。语句: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        elif issue_type == "swallowed_exception":
            desc = f"问题类型: swallowed_exception。"
            desc += f"异常被静默吞没（如空的 except 块）。语句: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        elif issue_type == "hardcoded_secret":
            desc = f"问题类型: hardcoded_secret。"
            desc += f"代码中硬编码了密钥、密码或令牌。语句: {code_feature}。"
            desc += f"修复方法: {action}。修复结果: {'成功' if success else '失败'}"
        else:
            desc = f"问题类型: {issue_type}。"
            desc += f"代码上下文: {code_feature}。" if code_feature else ""
            desc += f"修复方法: {action}。"
            desc += f"修复工具: {fixer}。" if fixer else ""
            desc += f"修复结果: {'成功' if success else '失败'}"
            if error and not success:
                desc += f"失败原因: {error[:100]}"

        return desc

    def _load_all_experiences(self) -> list[dict]:
        """从 experience_store.json 加载所有经验。"""
        if not EXPERIENCE_FILE.exists():
            return []
        try:
            data = json.loads(EXPERIENCE_FILE.read_text(encoding="utf-8"))
            return data.get("experiences", [])
        except Exception as e:
            logger.warning("[Retriever] 加载经验文件失败: %s", e)
            return []

    def _get_experience_by_id(self, exp_id: str) -> Optional[dict]:
        """按 ID 从经验文件取完整记录。"""
        try:
            from src.core.experience_store import get_experience_by_id
            return get_experience_by_id(exp_id)
        except ImportError:
            logging.exception("Import failed")
        exps = self._load_all_experiences()
        for exp in exps:
            if exp.get("id") == exp_id:
                return exp
        return None


# ── 全局单例工厂 ──────────────────────────────

_RETRIEVER_INSTANCE: Optional[ExperienceRetriever] = None


def get_retriever(
    model_name: str = "BAAI/bge-m3",
    skip_deps_check: bool = False,
) -> Optional[ExperienceRetriever]:
    """获取全局唯一的 ExperienceRetriever 实例。

    首次调用时初始化（可能触发模型加载 + Milvus 索引构建）。
    后续调用直接返回缓存实例，避免重复加载。

    Args:
        model_name: embedding 模型名称
        skip_deps_check: 跳过依赖检查（用于调试）

    Returns:
        ExperienceRetriever 实例，依赖缺失时返回 None
    """
    global _RETRIEVER_INSTANCE

    if _RETRIEVER_INSTANCE is not None:
        return _RETRIEVER_INSTANCE

    if not skip_deps_check:
        try:
            from pymilvus import MilvusClient  # noqa: F401
            from sentence_transformers import SentenceTransformer  # noqa: F401
        except ImportError:
            logger.warning(
                "[Retriever] 依赖缺失（pymilvus/sentence-transformers），"
                "降级为纯字符串匹配模式"
            )
            return None

    try:
        _RETRIEVER_INSTANCE = ExperienceRetriever(model_name=model_name)
        return _RETRIEVER_INSTANCE
    except Exception as e:
        logger.warning("[Retriever] 初始化失败: %s", e)
        return None


def reset_retriever() -> None:
    """重置全局单例（主要用于测试）。"""
    global _RETRIEVER_INSTANCE
    _RETRIEVER_INSTANCE = None
