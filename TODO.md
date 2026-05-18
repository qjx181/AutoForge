# TODO — 项目三护航任务：持续改进项目一

> 项目三现在的工作目标：用 swarm 自主驱动对项目一的持续改进和优化。
> 项目一目录：`C:\Users\qjx\Desktop\agent-自进化版\项目一cursor版本\在线部分\`

## Priority: HIGH

- [ ] **Phase 3 — RAGAS 测试体系**
  - [ ] 搭建 RAGAS 评估框架（context_precision / context_recall / faithfulness / answer_relevancy）
  - [ ] 回归测试自动化（修改前后指标对比，核心指标下降>5%标记回归）
  - [ ] 压力测试（50并发 P95<10s，成功率>95%）

- [ ] **新增模块补测试**
  - [ ] middleware/rate_limit.py — TokenBucket + Semaphore 单元测试
  - [ ] milvus_pool.py — 连接池初始化测试
  - [ ] services/retrieval_cache.py — Redis 缓存读写测试
  - [ ] services/knowledge_store.py — 聊天知识库沉淀/检索测试
  - [ ] services/chat_service.py — 流式聊天编排测试

## Priority: MEDIUM

- [ ] **代码清理**
  - [ ] `chat_service.py` 和 `chat_pipeline.py` 二选一，删除重复代码
  - [ ] 检查旧版 `milvus.py` 是否可删除（已被 `milvus_pool.py` 替代）
  - [ ] `services/session.py` 与 `services/session_service.py` 清理

- [ ] **BM25 分词优化**
  - [ ] 当前 `tokenize()` 只用 `.split()` 按空格分词，中文效果差
  - [ ] 考虑引入 jieba 分词提升 BM25 召回效果

- [ ] **路由层异步化补全**
  - [ ] routes/session.py 从 sync def 改为 async def
  - [ ] routes/auth.py 从 sync def 改为 async def

## Priority: LOW

- [ ] `lru_cache` 模块级缓存大小参数调优（`cached_encode maxsize=8192` 等）
- [ ] bm25_top_k=20 与 Reranker 输入截断 12 的匹配性确认

---

## 项目三自身维护

- [x] 恢复 cronjob（swarm-evolve-round）
- [x] 启动 tmux daemon（hermes-swarm）
- [ ] 配置 round 结束后自动 push 到 GitHub
