# CHANGELOG

## Round 0 — 系统初始化
- 创建项目三目录结构和初始种子任务
- 写入 SWARM_RULES.md、TODO.md、核心 SKILL
- 初始化 Git 仓库

## Round 17 — 2026-05-18 项目一 Phase 1+2 优化
- Phase 1（高并发优化）：LLM异步化、并发控制、Redis/Milvus连接池
- Phase 2（多路召回）：RRF融合、超时控制降级、Redis缓存层
- 12个文件，+779/-326 行

## Round 19 — 2026-05-18 项目一 Phase 3 RAGAS 评估框架
- evaluation/ragas_evaluator.py（280行，4项核心指标）
- rate_limit/milvus_pool 单元测试
- +4文件，+445行

## Round 20 — 2026-05-18 回归测试自动化
- tests/regression/regression_runner.py（360行）
- +8文件，+739/-121行

## Round 21 — 2026-05-18 retrieval_cache 单元测试
- test_retrieval_cache.py（280行，24个测试用例）

## Round 22 — 2026-05-18 knowledge_store 单元测试
- test_knowledge_store.py（393行，20个测试用例）

## Round 23 — 2026-05-18 路由 async def 改造
- routes/session.py + routes/auth.py sync→async
- +141/-139 行

## Round 24 — 2026-05-18 token 级截断
- services/token_utils.py 无外部依赖的 token 估算工具
- +3文件，+155/-10 行

## Round 25 — 2026-05-18 chat_service 单元测试
- test_chat_service.py（314行，13个测试用例）

## Round 27 — 2026-05-19 RAGAS 完全集成
- run_ragas_eval.py CLI + evaluation_service.py 异步触发器
- +2文件，+349行，commit b68557e

## Round 29 — 2026-05-19 压力测试套件
- tests/test_stress.py（437行，7项测试，全部通过）
- 令牌桶耗尽/Semaphore槽位占满/缓存穿透/综合并发，4场景覆盖
- commit cc3ad23，+437行
- 所有 HIGH 优先级 TODO 任务全部完成

## Round 30 — 2026-05-19 knowledge_store async 改造
- services/knowledge_store.py sync I/O → async def + asyncio.to_thread 桥接
- +299/-204 行，commit a7a40eb
- 测试直接测 sync 层避免线程边界问题

## Round 31 — 2026-05-19 session_service async 改造
- SessionService.create_session + list_sessions 改为 async def
- 纯 CPU 操作（无需 to_thread），+198/-125 行，commit 3c2ff64
- 13 个单元测试全部通过（含 11 async + 2 sync）

## Round 32 — 2026-05-19 清理废弃文件 + 修复 sync Redis I/O bug
- 删除 services/chat_pipeline.py（已被 chat_service.py 完全覆盖）
- 删除 services/session.py（已被 session_service.py 完全覆盖）
- 修复 async def build_context 中 sync Redis I/O 未用 to_thread 包装的 event loop 阻塞 bug
- commit 6cd7c82，-57 行
